import asyncio
import json
import os
import subprocess
import sys
import unittest

from super_super_harness.adapters import procs
from tests.support import MCP_SERVER, ServiceCase


class RouterDocuments(ServiceCase):
    async def test_missing_soul_keeps_original_prompt_and_startup(self):
        from super_super_harness.router.router import prompt_text
        self.assertFalse((self.cfg.router_dir / 'soul.md').exists())
        self.assertEqual(prompt_text(self.cfg), prompt_text())
        self.say('ECHO no soul needed')
        await self.until(lambda: self.statuses() == ['done'])
        self.assertIn('no soul needed', self.outbox()[0])
        self.assertEqual((self.cfg.router_dir / 'memory.md').stat().st_mode & 0o777, 0o600)

    async def test_soul_is_loaded_on_spawn_not_message_and_on_attach(self):
        from unittest.mock import patch
        from super_super_harness.cli import cmd_attach
        from super_super_harness.router.router import prompt_text
        file = self.cfg.router_dir / 'soul.md'
        file.write_text('# Soul\nNeutral test style')
        self.assertIn('Neutral test style', prompt_text(self.cfg))
        self.say('ECHO first')
        await self.until(lambda: self.statuses() == ['done'])
        self.assertNotIn('Neutral test style', str(self.argv_log()[-1]['argv']))
        await self.service.router.spawn()
        self.say('ECHO second')
        await self.until(lambda: self.statuses() == ['done', 'done'])
        self.assertIn('Neutral test style', str(self.argv_log()[-1]['argv']))
        file.write_text('# Soul\nNew test style')
        self.say('ECHO third')
        await self.until(lambda: self.statuses() == ['done', 'done', 'done'])
        self.assertNotIn('New test style', str(self.argv_log()[-1]['argv']))
        from unittest.mock import Mock
        client = Mock()
        client.call.side_effect = [{'started': True, 'session': 'fake-session', 'profile': 'main'}, {'path': 'fake-mcp'}, {}]
        with patch('super_super_harness.cli.Client', return_value=client), patch('super_super_harness.cli.exec_interactive', return_value=0) as launch:
            self.assertEqual(cmd_attach(self.cfg, None), 0)
        self.assertIn('New test style', launch.call_args.args[5])
        client.reset_mock()
        client.call.side_effect = [{'started': True, 'session': 'fake-session', 'profile': 'main'}, {}]
        file.write_bytes(b'\xff')
        from contextlib import redirect_stderr
        import io
        with patch('super_super_harness.cli.Client', return_value=client), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cmd_attach(self.cfg, None), 1)
        self.assertEqual(client.call.call_args_list[-1].args, ('router_attach',))
        self.assertIn('接管启动失败', errors.getvalue())

    async def test_memory_tools_are_router_only_validate_and_serialize(self):
        specs = {s['name']: s for s in self.service.tool_specs(('router', None))}
        self.assertIn('memory_read', specs)
        self.assertEqual(specs['memory_write']['inputSchema']['required'], ['content', 'version'])
        with self.assertRaises(ValueError):
            await self.service.call_tool(('worker', 't1'), 'memory_read', {})
        first = await self.tool('memory_read')
        for args in ({'content': 'x'}, {'content': 'x', 'version': first['version'], 'path': '/tmp/elsewhere'},
                     {'content': 'x' * (16 * 1024 + 1), 'version': first['version']}):
            with self.assertRaises(ValueError):
                await self.service.call_tool(('router', None), 'memory_write', args)
        results = await asyncio.gather(*(self.tool('memory_write', content=str(i), version=first['version']) for i in range(2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ValueError) and 'conflict' in str(r) for r in results), 1)
        from super_super_harness.router.router import prompt_text
        prompt = prompt_text()
        self.assertIn('memory_read', prompt)
        self.assertIn('memory_write', prompt)
        self.assertIn('Do not edit memory.md', prompt)


class RouterFileSafety(unittest.TestCase):
    def test_atomic_failure_keeps_old_file_and_cleans_temporary(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from super_super_harness.core.router_files import RouterFiles
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            files = RouterFiles(directory)
            first = files.write('memory', 'original', 'missing')
            with patch('super_super_harness.core.private_files.os.replace', side_effect=OSError('fake disk failure')):
                with self.assertRaises(OSError):
                    files.write('memory', 'new', first['version'])
            self.assertEqual(files.read('memory'), first)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ['.documents.lock', 'memory.md'])

    def test_separate_processes_cannot_both_save_same_version(self):
        from pathlib import Path
        import tempfile
        from super_super_harness.core.router_files import RouterFiles
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            version = RouterFiles(directory).write('memory', 'initial', 'missing')['version']
            code = '''import sys
from pathlib import Path
from super_super_harness.core.router_files import RouterFiles, DocumentError
sys.stdin.readline()
try:
    RouterFiles(Path(sys.argv[1])).write('memory', sys.argv[3], sys.argv[2])
    print('saved')
except DocumentError as error:
    print(error.status)
'''
            processes = [subprocess.Popen([sys.executable, '-c', code, folder, version, str(i)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, env=dict(os.environ, SSHARNESS_HOME=folder)) for i in range(2)]
            try:
                for process in processes:
                    process.stdin.write('go\n')
                    process.stdin.flush()
                results = [process.communicate(timeout=10) for process in processes]
                self.assertEqual(sorted(out.strip() for out, _ in results), ['409', 'saved'])
                self.assertTrue(all(process.returncode == 0 for process in processes), results)
                self.assertTrue(all(not err for _, err in results), results)
                self.assertIn(RouterFiles(directory).read('memory')['content'], ('0', '1'))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

    def test_symlink_directory_fifo_and_invalid_names_are_rejected(self):
        from pathlib import Path
        import tempfile
        from super_super_harness.core.router_files import RouterFiles, DocumentError
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            real = directory / 'real'
            real.mkdir()
            alias = directory / 'alias'
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(DocumentError):
                RouterFiles(alias).read('memory')
            files = RouterFiles(real)
            for name in ('../memory', '/tmp/elsewhere', 'memory.md', 'unknown'):
                with self.assertRaises(DocumentError):
                    files.read(name)
                with self.assertRaises(DocumentError):
                    files.write(name, 'x', 'missing')
            os.mkfifo(real / 'soul.md')
            with self.assertRaises(DocumentError):
                files.read('soul')


class Inbox(ServiceCase):
    async def test_message_goes_written_confirmed_done(self):
        self.say("ECHO hello")
        await self.until(lambda: self.statuses() == ["done"], label="done")
        self.assertIsNone(self.store.current_turn())

    async def test_clock_failure_cannot_block_message_delivery(self):
        from unittest.mock import Mock, patch
        router = self.service.router
        router.mode = "paused"
        self.say("ECHO safe delivery")
        await asyncio.sleep(0)
        import struct
        for failure in (OSError("missing clock"), ValueError("bad clock"), struct.error("truncated TZif"),
                        AssertionError("incomplete TZif footer")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(router, "process", Mock()), patch("super_super_harness.router.router.time_context", side_effect=failure):
                    router.ready = True
                    router.mode = "running"
                    await router.pump()
                    self.assertTrue(router.process.send.called)
                router.mode = "paused"
                self.store.run("UPDATE messages SET status='queued'")

    async def test_no_reply_falls_back_to_the_final_text(self):
        self.say("ECHO plain text")
        await self.until(lambda: self.outbox(), label="fallback")
        self.assertIn("plain text", self.outbox()[0])

    async def test_reply_counts_and_suppresses_the_fallback(self):
        self.say("SLEEP 0.5 ECHO unused")
        await self.until(lambda: self.store.current_turn(), label="turn")
        await self.tool("reply", text="这是回复")
        await self.until(lambda: self.statuses() == ["done"], label="done")
        await asyncio.sleep(0.1)
        self.assertEqual(self.outbox(), ["这是回复"])

    async def test_task_list_is_sent_once_until_it_changes(self):
        self.say("ECHO one")
        await self.until(lambda: self.statuses() == ["done"], label="first")
        first = self.service.router.last_list
        self.say("ECHO two")
        await self.until(lambda: self.statuses() == ["done", "done"], label="second")
        self.assertEqual(self.service.router.last_list, first)
        session = self.home / "sessions" / "proj" / (self.service.router.session_id + ".jsonl")
        users = [json.loads(line) for line in session.read_text().splitlines() if '"type": "user"' in line]
        self.assertEqual(sum("<workers>" in user["message"]["content"] for user in users), 1)

    async def test_compaction_forces_the_list_again(self):
        await self.service.router._on_event({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}})
        self.assertTrue(self.service.router.force_list)


class Effects(ServiceCase):
    async def test_side_effects_run_once_per_turn(self):
        self.say("SLEEP 1 ECHO x")
        await self.until(lambda: self.store.current_turn(), label="turn")
        first = await self.tool("worker_hire", name="修 bug", brief="ECHO fixed")
        again = await self.tool("worker_hire", name="修 bug", brief="ECHO fixed")
        other = await self.tool("worker_hire", name="另一个", brief="ECHO other")
        self.assertEqual(first["worker"], again["worker"])
        self.assertNotEqual(first["worker"], other["worker"])
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM tasks")["c"], 2)

    async def test_router_cannot_use_worker_tools(self):
        with self.assertRaises(ValueError):
            await self.tool("report_done", text="x")
        with self.assertRaises(ValueError):
            await self.service.call_tool(("worker", "t1"), "worker_hire", {"name": "x", "brief": "y"})


class QuestionEffects(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pathlib import Path
        import tempfile
        from super_super_harness.ops import config
        from super_super_harness.daemon import Service
        from tests.support import write_config
        self.folder = tempfile.TemporaryDirectory(prefix='ssharness-question-')
        home = Path(self.folder.name)
        write_config(home, 1, 2)
        self.cfg = config.load(home)
        self.service = Service(self.cfg)
        self.hold_transport()

    def hold_transport(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        # No model or listeners: pump writes, and the test controls replay timing.
        self.router.process = SimpleNamespace(send=Mock(return_value=True), close=AsyncMock())
        self.router.ready = True

    @property
    def router(self):
        return self.service.router

    @property
    def store(self):
        return self.service.store

    async def close_service(self):
        await self.router.shutdown()
        await self.service.tasks.shutdown()
        await self.service.reminders.close()
        await self.service.outbox.flush()
        self.service.outbox.closed = True
        await asyncio.sleep(0)
        self.store.close()

    async def asyncTearDown(self):
        await self.close_service()
        self.folder.cleanup()

    async def message(self, text, read=True, role='user', kind=''):
        mid = self.router.submit(role, text, 'terminal', kind)
        await self.router.pump()
        self.assertEqual(self.store.one('SELECT status FROM messages WHERE id=?', mid)['status'], 'written')
        if read:
            await self.confirm(mid)
        return mid

    async def confirm(self, mid):
        row = self.store.one('SELECT uuid FROM messages WHERE id=?', mid)
        await self.router._on_event({'type': 'user', 'isReplay': True, 'uuid': row['uuid']})

    async def ask(self):
        return await self.service.call_tool(('router', None), 'ask_user', {'question': 'Same question?'})

    def questions(self):
        return self.store.one("SELECT COUNT(*) n FROM conversation WHERE role='router' AND kind='question'")['n']

    async def test_same_question_reissued_after_new_read_user_in_same_turn(self):
        await self.message('initial request')
        turn = self.store.current_turn()['id']
        await self.ask()
        self.assertEqual(self.questions(), 1)
        self.assertEqual(self.store.get('router_waiting'), 'Same question?')
        answer = await self.message('a new answer', read=False)
        self.assertFalse(self.store.get('router_waiting'))
        await self.ask()
        self.assertEqual(self.questions(), 1)  # Unread input does not change the effect identity.
        await self.confirm(answer)
        await self.ask()
        self.assertEqual(self.store.current_turn()['id'], turn)
        self.assertEqual(self.questions(), 2)
        self.assertEqual(self.store.get('router_waiting'), 'Same question?')
        await self.router._on_result({'subtype': 'success', 'result': 'unused fallback'})
        self.assertEqual(self.questions(), 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM conversation WHERE kind='fallback'")['n'], 0)

    async def test_unread_first_question_reissued_after_user_confirmation(self):
        first = await self.message('not read yet', read=False)
        turn = self.store.current_turn()['id']
        await self.ask()
        self.assertEqual(self.questions(), 1)
        self.assertFalse(self.store.get('router_waiting'))
        await self.confirm(first)
        await self.ask()
        self.assertEqual(self.store.current_turn()['id'], turn)
        self.assertEqual(self.questions(), 2)
        self.assertEqual(self.store.get('router_waiting'), 'Same question?')

    async def test_same_read_user_deduplicates_after_done_and_store_reopen(self):
        from super_super_harness.daemon import Service
        mid = await self.message('already read')
        result = await self.ask()
        self.store.run("UPDATE messages SET status='done' WHERE id=?", mid)
        await self.close_service()
        self.service = Service(self.cfg)
        self.hold_transport()
        self.assertEqual(await self.ask(), result)
        self.assertEqual(self.questions(), 1)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM effects')['n'], 1)
        self.assertEqual(self.store.get('router_waiting'), 'Same question?')

    async def test_unread_user_reminder_and_event_do_not_change_question_identity(self):
        await self.message('initial request')
        await self.ask()
        answer = await self.message('answer still unread', read=False)
        await self.message('scheduled input', kind='reminder.run')
        await self.message('worker data', role='event', kind='worker.done')
        await self.ask()
        self.assertEqual(self.questions(), 1)
        self.assertFalse(self.store.get('router_waiting'))
        await self.confirm(answer)
        self.store.run("UPDATE messages SET status='done' WHERE id=?", answer)
        await self.ask()
        self.assertEqual(self.questions(), 2)
        self.assertEqual(self.store.get('router_waiting'), 'Same question?')


class Recovery(ServiceCase):
    async def test_message_only_queued_is_sent_again(self):
        self.say("SLEEP 30")
        await self.until(lambda: self.statuses() == ["confirmed"], label="first confirmed")
        self.say("ECHO queued one")
        await self.until(lambda: self.statuses() == ["confirmed", "written"], label="second written")
        await asyncio.sleep(0.2)
        procs.kill_tree(self.service.router.process.pid)
        await self.until(lambda: self.statuses()[1] == "done", timeout=10, label="second handled")
        session = self.home / "sessions" / "proj" / (self.service.router.session_id + ".jsonl")
        lines = [line for line in session.read_text().splitlines() if "queued one" in line and '"type": "user"' in line]
        self.assertEqual(len(lines), 1)

    async def test_message_in_session_without_replay_is_not_sent_again(self):
        message = self.say("SILENTHANG")
        await self.until(lambda: self.statuses() == ["written"], label="written")
        session = self.home / "sessions" / "proj" / (self.service.router.session_id + ".jsonl")
        await self.until(lambda: session.exists() and '"type": "user"' in session.read_text(), label="in session")
        procs.kill_tree(self.service.router.process.pid)
        await self.until(lambda: self.store.one("SELECT 1 FROM messages WHERE role='system'"), timeout=10, label="recovery note")
        self.assertEqual(self.store.one("SELECT status FROM messages WHERE id=?", message)["status"] in ("confirmed", "done"), True)
        session = self.home / "sessions" / "proj" / (self.service.router.session_id + ".jsonl")
        lines = [line for line in session.read_text().splitlines() if "SILENTHANG" in line and '"type": "user"' in line]
        self.assertEqual(len(lines), 1)

    async def test_recovery_note_lists_what_already_happened(self):
        self.say("SILENTHANG")
        await self.until(lambda: self.store.current_turn(), label="turn")
        session = self.home / "sessions" / "proj" / (self.service.router.session_id + ".jsonl")
        await self.until(lambda: session.exists() and '"type": "user"' in session.read_text(), label="in session")
        await self.tool("worker_hire", name="修 bug", brief="ECHO fixed")
        procs.kill_tree(self.service.router.process.pid)
        await self.until(lambda: self.store.one("SELECT 1 FROM messages WHERE role='system'"), timeout=10, label="note")
        note = self.store.one("SELECT text FROM messages WHERE role='system'")["text"]
        self.assertIn("worker_hire", note)
        self.assertIn("Nova", note)

    async def test_busy_router_is_interrupted_for_the_user(self):
        self.say("TOOLSLEEP 30")
        await self.until(lambda: self.service.router.inflight, label="tool running")
        self.say("ECHO quick answer")
        await self.until(lambda: any("quick answer" in text for text in self.outbox()), timeout=5, label="answer")
        self.assertTrue(self.store.one("SELECT 1 FROM events WHERE type='router.interrupted'"))
        self.assertFalse(any("没有回复" in text for text in self.outbox()))

    async def test_idle_router_is_not_interrupted(self):
        self.say("SLEEP 2 ECHO slow thinking")
        await self.until(lambda: self.statuses() == ["confirmed"], label="confirmed")
        self.say("ECHO next")
        await self.until(lambda: self.statuses() == ["done", "done"], timeout=8, label="both done")
        self.assertFalse(self.store.one("SELECT 1 FROM events WHERE type='router.interrupted'"))

    async def test_crash_loop_pauses(self):
        self.service.router.crashes = 3
        self.say("ALWAYSCRASH")
        await self.until(lambda: self.service.router.mode == "paused", timeout=10, label="paused")
        self.assertTrue(any("暂停" in text for text in self.outbox()))


class Failover(ServiceCase):
    async def test_dead_primary_switches_to_backup_and_keeps_the_session(self):
        await self.until(lambda: self.store.get("router_pid"), label="pid")
        session = self.service.router.session_id
        self.primary.down()
        self.say("HANG")
        await self.until(lambda: self.service.router.profile_name == "spare", timeout=10, label="switched")
        await self.until(lambda: self.argv_log()[-1]["model"] == "spare-model", label="spare launched")
        last = self.argv_log()[-1]["argv"]
        self.assertEqual(last[last.index("--resume") + 1], session)
        await self.until(lambda: any("顶着" in text for text in self.outbox()), label="notice")

    async def test_primary_back_asks_before_switching(self):
        self.primary.down()
        self.say("HANG")
        await self.until(lambda: self.service.router.profile_name == "spare", timeout=10, label="switched")
        self.primary.up()
        await self.until(lambda: any("恢复了" in text for text in self.outbox()), timeout=10, label="asks")
        self.assertEqual(self.service.router.profile_name, "spare")

    async def test_slow_model_with_healthy_gateway_is_left_alone(self):
        self.say("HANG")
        await asyncio.sleep(3.5)
        self.assertEqual(self.service.router.profile_name, "main")
        self.assertTrue(self.service.router.process)

    async def test_both_down_pauses_then_recovers_on_its_own(self):
        self.say("ECHO warm")
        await self.until(lambda: self.statuses() == ["done"], label="warm")
        self.primary.down()
        self.backup.down()
        self.say("HANG")
        await self.until(lambda: self.service.router.mode == "paused", timeout=10, label="paused")
        self.assertTrue(any("两个模型都不可用" in text for text in self.outbox()))
        self.service.router.last_probe = 0
        self.primary.up()
        await self.until(lambda: self.service.router.mode == "running" and self.service.router.process, timeout=25, label="running")

    async def test_workers_survive_a_router_switch(self):
        task_id = self.start_task("SLEEP 4 ECHO worker done")
        await self.until(lambda: self.service.tasks.live.get(task_id) and self.service.tasks.live[task_id].last_event, label="started")
        worker = self.service.tasks.live[task_id].process.pid
        await self.service.router.switch("spare")
        self.assertTrue(procs.alive(worker))
        await self.wait_status(task_id, "completed")


class EarlyFailure(ServiceCase):
    async def test_switch_before_the_first_replay_resumes(self):
        self.say("SILENTHANG")
        session = self.session_path(self.service.router.session_id)
        await self.until(lambda: session.exists() and '"type": "user"' in session.read_text(), label="in session")
        await self.service.router.switch("spare")
        await self.until(lambda: self.argv_log()[-1]["model"] == "spare-model", label="spare")
        self.assertIn("--resume", self.argv_log()[-1]["argv"])
        await asyncio.sleep(0.5)
        self.assertEqual(self.service.router.crashes, 0)

    async def test_session_with_no_real_turn_starts_over(self):
        await self.service.router._kill()
        old = self.service.router.session_id
        self.session_path(old).parent.mkdir(parents=True, exist_ok=True)
        self.session_path(old).write_text('{"type": "queue-operation"}\n')
        await self.service.router.spawn()
        self.assertNotEqual(self.service.router.session_id, old)
        self.say("ECHO fresh start")
        await self.until(lambda: self.statuses() == ["done"], label="done")

    async def test_recovery_notes_do_not_pile_up(self):
        self.say("SILENTHANG")
        session = self.session_path(self.service.router.session_id)
        await self.until(lambda: session.exists() and '"type": "user"' in session.read_text(), label="in session")
        self.service.router.mode = "paused"
        for _ in range(3):
            await self.service.router._kill()
            await self.service.router._recover_inbox()
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM messages WHERE role='system'")["c"], 1)
        self.service.router.mode = "running"


class Shutdown(ServiceCase):
    async def test_stop_during_a_pending_restart_does_not_hang(self):
        self.say("ECHO warm")
        await self.until(lambda: self.statuses() == ["done"], label="warm")
        procs.kill_tree(self.service.router.process.pid)
        await self.until(lambda: self.service.router.process is None, label="process gone")
        await asyncio.wait_for(self.halt(), 10)
        self.assertEqual(procs.in_dir(self.cfg.router_dir), [])


class Startup(ServiceCase):
    async def test_message_during_startup_goes_out_once(self):
        spawn = asyncio.create_task(self.service.router.spawn())
        await asyncio.sleep(0)
        self.say("ECHO only once")
        await spawn
        await self.until(lambda: self.statuses() == ["done"], label="done")
        session = self.home / "sessions" / "proj" / (self.service.router.session_id + ".jsonl")
        lines = [line for line in session.read_text().splitlines() if "only once" in line and '"type": "user"' in line]
        self.assertEqual(len(lines), 1)


class Attach(ServiceCase):
    async def test_detach_queues_messages_and_reattach_delivers(self):
        self.say("ECHO warm")
        await self.until(lambda: self.statuses() == ["done"], label="warm")
        info = await self.service.router.detach()
        self.assertTrue(info["started"])
        self.assertIsNone(self.service.router.process)
        self.say("ECHO while attached")
        await asyncio.sleep(0.2)
        self.assertEqual(self.statuses()[-1], "queued")
        await self.service.router.reattach()
        await self.until(lambda: self.statuses()[-1] == "done", label="delivered")

    async def test_inbox_can_be_read_while_attached(self):
        await self.service.router.detach()
        self.say("hello from feishu")
        items = await self.tool("inbox_read")
        self.assertEqual(len(items), 1)
        self.assertIn("hello from feishu", items[0])
        self.assertEqual(self.statuses(), ["done"])


class AttachWakeup(ServiceCase):
    async def test_wait_reminds_without_taking_the_message(self):
        await self.service.router.detach()
        waiting = asyncio.create_task(self.service.router.wait_inbox())
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        self.say("ECHO later")
        self.assertEqual(await asyncio.wait_for(waiting, 2), 1)
        self.assertEqual(self.statuses(), ["queued"])
        self.assertIn("inbox_read", self.service.router.attach_check())
        await self.tool("inbox_read")
        self.assertEqual(self.service.router.attach_check(), "")

    async def test_turn_cannot_end_with_workers_out_and_nobody_listening(self):
        await self.service.router.detach()
        task_id = self.start_task("SLEEP 30")
        self.assertIn("supersuperharness wait", self.service.router.attach_check())
        waiting = asyncio.create_task(self.service.router.wait_inbox())
        await asyncio.sleep(0.05)
        self.assertEqual(self.service.router.attach_check(), "")
        waiting.cancel()
        await self.service.tasks.stop(task_id)

    async def test_stop_hook_gives_up_rather_than_trapping_the_session(self):
        await self.service.router.detach()
        self.say("ECHO ignored")
        answers = [self.service.router.attach_check() for _ in range(5)]
        self.assertTrue(all(answers[:3]))
        self.assertEqual(answers[3:], ["", ""])

    async def test_reattach_releases_a_waiting_wait(self):
        self.say("ECHO warm")
        await self.until(lambda: self.statuses() == ["done"], label="warm")
        await self.service.router.detach()
        waiting = asyncio.create_task(self.service.router.wait_inbox())
        await asyncio.sleep(0.05)
        await self.service.router.reattach()
        await asyncio.wait_for(waiting, 2)


class ReplySelection(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from super_super_harness.core.store import Store
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.box = SimpleNamespace(rows=self.store.all, wait=lambda predicate, *args: predicate())
        self.old = self.store.open_turn()
        self.store.run("UPDATE turns SET status='done' WHERE id=?", self.old)
        self.turn = self.store.open_turn()
        self.message = self.store.add_message("new", "user", "Which explanation task?", "terminal")
        self.store.run("UPDATE messages SET turn_id=? WHERE id=?", self.turn, self.message)
        from tests.reply_helpers import MessageStamp
        self.stamp = MessageStamp(10, self.message)

    def reply(self, kind, text, created, turn=None):
        self.store.run("INSERT INTO outbox(channel,kind,text,created,turn_id) VALUES('terminal',?,?,?,?)",
                       kind, text, created, self.turn if turn is None else turn)

    def test_reply_after_ignores_late_previous_turn_and_notices(self):
        from tests.reply_helpers import reply_after
        self.reply("answer", "Merged the old task", 20, self.old)
        self.reply("notice", "Unrelated completion", 11)
        self.assertEqual(reply_after(self.box, self.stamp), (None, ""))
        self.reply("answer", "Fibonacci or prime?", 21)
        self.assertEqual(reply_after(self.box, self.stamp), (11, "Fibonacci or prime?"))

    def test_reply_after_counts_ask_user_question(self):
        from tests.reply_helpers import reply_after
        self.reply("question", "Fibonacci or prime?", 10.25)
        self.assertEqual(reply_after(self.box, self.stamp), (.25, "Fibonacci or prime?"))

    def test_final_reply_waits_for_turn_end_and_uses_last_answer(self):
        from tests.reply_helpers import reply_after
        self.reply("answer", "Starting merge", 11)
        self.reply("fallback", "Merged", 12)
        self.assertEqual(reply_after(self.box, self.stamp, final=True), (None, ""))
        self.store.run("UPDATE turns SET status='done' WHERE id=?", self.turn)
        self.assertEqual(reply_after(self.box, self.stamp, final=True), (2, "Merged"))


class Mcp(ServiceCase):
    async def test_stdio_server_round_trip(self):
        token = self.service.issue_token(("worker", "t1"))
        self.store.add_task("x", "y", "work", None, "s", "", "")
        env = dict(os.environ, SSHARNESS_SOCK=str(self.cfg.sock), SSHARNESS_TOKEN=token)
        script = str(MCP_SERVER)
        proc = await asyncio.create_subprocess_exec(sys.executable, "-I", script, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "report_progress", "arguments": {"text": "halfway"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "worker_hire", "arguments": {"name": "x", "brief": "y"}}},
        ]
        for line in lines:
            proc.stdin.write((json.dumps(line) + "\n").encode())
        await proc.stdin.drain()
        answers = [json.loads(await asyncio.wait_for(proc.stdout.readline(), 5)) for _ in lines]
        proc.stdin.close()
        await proc.wait()
        self.assertEqual({tool["name"] for tool in answers[1]["result"]["tools"]},
                         {"report_progress", "report_done", "ask_user", "subscribe", "unsubscribe", "hand_back", "item_read", "item_update", "notes_read", "notes_write", "request_help"})
        self.assertFalse(answers[2]["result"]["isError"])
        self.assertEqual(self.store.task("t1")["progress"], "halfway")
        self.assertTrue(answers[3]["result"]["isError"])
