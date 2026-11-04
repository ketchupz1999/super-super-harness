"""Worker compaction accounting and bounded, durable fresh-session handoff."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from super_super_harness.adapters import claude_code as harness, procs
from super_super_harness.core.store import Store
from super_super_harness.ops import config
from super_super_harness.workers.tasks import Run, Tasks
from tests.support import ServiceCase, write_config


class CompactionConfig(unittest.TestCase):
    def test_default_and_positive_integer_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_config(home, 1, 2)
            self.assertEqual(config.load(home).limits["compact_thrash"], 3)
            for value in ('0', '-1', 'true', '1.5', '"3"', '2'):
                with self.subTest(value=value):
                    write_config(home, 1, 2, limits="compact_thrash = " + value)
                    if value == '2':
                        self.assertEqual(config.load(home).limits["compact_thrash"], 2)
                    else:
                        with self.assertRaisesRegex(ValueError, "compact_thrash"):
                            config.load(home)


class CompactionEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.tasks = Tasks(SimpleNamespace(limits={"max_workers": 1, "compact_thrash": 3}), self.store, AsyncMock(), None)
        self.task_id = self.store.add_task("task", "brief", "work", None, "session", "", "")
        self.store.update_task(self.task_id, status="running")
        self.run = Run(ready=True, background=True, pending_uuids=set(), assignments={}, callers={})
        self.tasks.live[self.task_id] = self.run

    async def compact(self, **extra):
        await self.tasks._on_event(self.task_id, self.run, {"type": "system", "subtype": "compact_boundary", **extra})

    async def test_missing_metadata_and_subagent_boundaries(self):
        await self.compact(parent_tool_use_id="subagent", compact_metadata={"pre_tokens": 42})
        self.assertIsNone(self.store.get("session_compactions:session"))
        await self.compact()
        event = self.store.one("SELECT data FROM events WHERE type='worker.compacted'")
        self.assertEqual(json.loads(event["data"]), {"session_id": "session", "count": 1, "trigger": None, "pre_tokens": None})
        self.tasks.on_change.assert_not_awaited()

    async def test_thrash_is_per_turn_not_session_and_fires_before_result(self):
        self.tasks._start_handoff = Mock()
        for _ in range(3):
            await self.compact()
        await self.tasks._on_event(self.task_id, self.run, {"type": "result", "result": "still working"})
        for _ in range(3):
            await self.compact()
        self.tasks._start_handoff.assert_not_called()
        await self.compact()
        self.tasks._start_handoff.assert_called_once()
        self.assertEqual(self.store.get("session_compactions:session"), "7")

    async def test_error_shapes_take_precedence_over_interruption_but_not_success(self):
        self.tasks._start_handoff = Mock()
        errors = ["Prompt is too long", "automatic compaction failed: unavailable", "context_length_exceeded",
                  "maximum context length is 200000", "context window exceeded", "context window overflow",
                  "context overflow", "input is too long", "Auto-compaction thrashing detected"]
        for error in errors:
            with self.subTest(error=error):
                await self.tasks._on_event(self.task_id, self.run, {"type": "result", "subtype": "error_during_execution",
                                                                  "is_error": True, "errors": [error]})
        self.assertEqual(self.tasks._start_handoff.call_count, len(errors))
        self.tasks._start_handoff.reset_mock()
        await self.tasks._on_event(self.task_id, self.run, {"type": "result", "is_error": False, "result": "Prompt is too long"})
        self.tasks._start_handoff.assert_not_called()
        for error in ("API Error: 503", "rate limit exceeded", "invalid credentials", "disk thrashing"):
            self.assertFalse(Tasks._context_error(error))


class CompactionLifecycle(ServiceCase):
    async def idle(self, task_id, status="completed"):
        await self.wait_status(task_id, status)
        await self.until(lambda: task_id not in self.service.tasks.live and task_id not in self.service.tasks.retrying,
                         label="Worker fully closed")
        return self.store.task(task_id)

    async def acknowledged(self, task_id):
        await self.worker_ready(task_id)
        run = self.service.tasks.live[task_id]
        await self.until(lambda: run.ready and not run.pending_uuids, label="brief acknowledged")
        return run

    async def overflow(self, task_id, run):
        await self.service.tasks._on_event(task_id, run, {"type": "result", "is_error": True, "result": "Prompt is too long"})

    def runs(self, task_id):
        return self.store.all("SELECT * FROM worker_runs WHERE task_id=? ORDER BY id", task_id)

    def inputs(self, session):
        return [row["message"]["content"] for row in map(json.loads, self.session_path(session).read_text().splitlines())
                if row["type"] == "user"]

    def history(self, task_id):
        return [json.loads(row["value"]) for row in self.store.all("SELECT value FROM kv WHERE key LIKE ? ORDER BY key", "worker_handoff:%s:%%" % task_id)]

    async def test_compactions_record_metadata_and_accumulate_across_resume_without_wake(self):
        task_id = self.start_task("COMPACT 100 COMPACT 200 ECHO first")
        before = await self.idle(task_id)
        session = before["session_id"]
        self.assertEqual(self.store.get("session_compactions:" + session), "2")
        await self.halt()
        await self.boot()
        self.service.tasks.send(task_id, "COMPACT 300 COMPACT 400 ECHO next")
        after = await self.idle(task_id)
        self.assertEqual(after["session_id"], session)
        self.assertEqual(self.store.get("session_compactions:" + session), "4")
        events = [json.loads(row["data"]) for row in self.store.all("SELECT data FROM events WHERE task_id=? AND type='worker.compacted' ORDER BY id", task_id)]
        self.assertEqual([event["pre_tokens"] for event in events], [100, 200, 300, 400])
        self.assertEqual([event["count"] for event in events], [1, 2, 3, 4])
        self.assertTrue(all(event["trigger"] == "auto" and event["session_id"] == session for event in events))
        self.assertIsNone(self.store.one("SELECT 1 FROM triggers WHERE type IN ('worker.compacted','worker.handoff')"))
        self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE kind='worker.compacted'"))

    async def test_handoff_preserves_identity_notes_branch_and_original_assignment(self):
        repo = self.home / "source"
        repo.mkdir()
        for args in (("init", "-q"), ("-c", "user.email=a@b", "-c", "user.name=x", "commit", "-q", "--allow-empty", "-m", "base")):
            subprocess.run(["git", *args], cwd=repo, check=True)
        task_id = self.start_task("COMPACT 900 SLEEP 0.6 ECHO recovered", code_name="Keeper", role="Keep decisions", repo=str(repo))
        run = await self.acknowledged(task_id)
        await self.until(lambda: run.turn_compactions == 1)
        before = self.store.task(task_id)
        tasks = self.service.tasks
        notes = tasks.notes_read(task_id)
        tasks.notes_write(task_id, "durable worker decision", notes["version"])
        item = self.service.items.read(self.service.items.of(task_id))
        self.service.items.write_notes(item["code"], "durable item decision", item["notes_version"])
        progress = "verified the important step " + "x" * 250
        tasks.report_progress(task_id, progress)
        self.store.update_task(task_id, progress="Bash later tool activity")
        await self.overflow(task_id, run)
        after = await self.idle(task_id)
        self.assertNotEqual(after["session_id"], before["session_id"])
        for field in ("id", "code_name", "workspace", "cwd", "branch", "profile"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(len(self.runs(task_id)), 2)
        self.assertEqual(self.store.get("session_compactions:" + after["session_id"]), "1")
        self.assertEqual(self.store.get("session_compactions:" + before["session_id"]), "1")
        first = self.inputs(after["session_id"])[0]
        for text in (before["brief"], progress, "Read your notes", "`git status`", "`git log -5`"):
            self.assertIn(text, first)
        self.assertEqual(first.count(before["brief"]), 1)
        self.assertNotIn("Bash later tool activity", first)
        self.assertIsNone(self.store.get("handoff_pending:" + task_id))
        record, = self.history(task_id)
        self.assertEqual((record["old_session_id"], record["session_id"], record["reason"]),
                         (before["session_id"], after["session_id"], "Prompt is too long"))
        launches = [row["argv"] for row in self.argv_log() if row["model"] == "work-model"]
        self.assertTrue(all("--session-id" in argv and "--resume" not in argv for argv in launches))
        prompt = launches[-1][launches[-1].index("--append-system-prompt") + 1]
        for text in ("You are Keeper", "Keep decisions", "durable worker decision", "durable item decision"):
            self.assertIn(text, prompt)
        trigger, = self.store.all("SELECT * FROM triggers WHERE type='worker.handoff'")
        self.assertEqual((trigger["severity"], trigger["route"]), ("warn", "wake"))
        message, = self.store.all("SELECT text FROM messages WHERE task_id=? AND kind='worker.handoff'", task_id)
        self.assertIn("Keeper", message["text"])
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='worker.retry'", task_id))

    async def test_handoff_uses_latest_report_instead_of_older_progress(self):
        task_id = self.start_task("SLEEP 0.4 ECHO continued")
        run = await self.acknowledged(task_id)
        self.service.tasks.report_progress(task_id, "older progress")
        self.service.tasks.report_done(task_id, "latest complete report")
        await self.overflow(task_id, run)
        after = await self.idle(task_id)
        first = self.inputs(after["session_id"])[0]
        self.assertIn("latest complete report", first)
        self.assertNotIn("older progress", first)

    async def test_toolong_exhausts_two_handoffs_then_fails_without_ordinary_retry(self):
        self.cfg.limits["worker_retries"] = 99
        task_id = self.start_task("TOOLONG")
        await self.idle(task_id, "failed")
        runs = self.runs(task_id)
        self.assertEqual(len(runs), 3)
        self.assertEqual(len({row["session_id"] for row in runs}), 3)
        self.assertEqual(len(self.history(task_id)), 2)
        assignment = self.store.one("SELECT * FROM assignments WHERE task_id=?", task_id)
        self.assertEqual(assignment["status"], "failed")
        self.assertIn("handoff limit", assignment["result"])
        self.assertEqual(self.store.get("assignment_handoffs:%s" % assignment["id"]), "2")
        self.assertIsNone(self.store.get("assignment_retries:%s" % assignment["id"]))
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.handoff'", task_id)["n"], 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.failed'", task_id)["n"], 1)
        self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.done'", task_id))

    async def test_thrash_threshold_closes_a_turn_even_without_result(self):
        self.cfg.limits["compact_thrash"] = 2
        task_id = self.start_task("COMPACT 100 COMPACT 200 COMPACT 300 HANG")
        await self.idle(task_id, "failed")
        self.assertEqual(len(self.runs(task_id)), 3)
        for row in self.runs(task_id):
            self.assertEqual(self.store.get("session_compactions:" + row["session_id"]), "3")
        self.assertTrue(all("thrashing" in row["reason"] for row in self.history(task_id)))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='result'", task_id))

    async def test_ordinary_error_still_retries_same_session(self):
        task_id = self.start_task("FAIL")
        await self.idle(task_id)
        runs = self.runs(task_id)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["session_id"], runs[1]["session_id"])
        self.assertEqual(self.history(task_id), [])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='worker.retry'", task_id)["n"], 1)

    async def test_budget_is_per_assignment_and_current_text_is_not_initial_brief(self):
        task_id = self.start_task("ECHO initial work")
        await self.idle(task_id)
        tasks = self.service.tasks
        for index in range(2):
            text = "SLEEP 0.4 ECHO assignment-%d" % index
            tasks.send(task_id, text)
            run = await self.acknowledged(task_id)
            # worker_ready can see the preceding session's transcript; wait for this assignment's ACK.
            await self.until(lambda: self.store.one("SELECT status FROM assignments WHERE task_id=? ORDER BY id DESC LIMIT 1", task_id)["status"] == "doing")
            await self.overflow(task_id, run)
            after = await self.idle(task_id)
            first = self.inputs(after["session_id"])[0]
            self.assertIn(text, first)
            self.assertNotIn("ECHO initial work", first)
        assignments = self.store.all("SELECT id, status FROM assignments WHERE task_id=? ORDER BY id", task_id)
        self.assertTrue(all(row["status"] == "done" for row in assignments))
        self.assertEqual([self.store.get("assignment_handoffs:%s" % row["id"], "0") for row in assignments], ["0", "1", "1"])

    async def test_handoff_survives_host_restart_before_launch(self):
        task_id = self.start_task("SLEEP 0.4 ECHO recovered")
        run = await self.acknowledged(task_id)
        before = self.store.task(task_id)["session_id"]
        tasks = self.service.tasks
        with patch.object(tasks, "schedule"):
            await self.overflow(task_id, run)
            await self.until(lambda: self.store.get("handoff_pending:" + task_id) and task_id not in tasks.retrying)
            new_session = self.store.task(task_id)["session_id"]
            self.assertNotEqual(new_session, before)
            await self.halt()
        await self.boot()
        after = await self.idle(task_id)
        self.assertEqual(after["session_id"], new_session)
        self.assertTrue(self.inputs(new_session)[0].startswith("[Session handoff]"))
        self.assertIsNone(self.store.get("handoff_pending:" + task_id))
        self.assertEqual(len(self.history(task_id)), 1)

    async def test_pending_assignments_and_says_follow_handoff_not_stale_ack(self):
        task_id = self.start_task("SLEEP 0.7 ECHO old-work")
        run = await self.acknowledged(task_id)
        tasks = self.service.tasks
        tasks.send(task_id, "ECHO queued-assignment")
        tasks.say(task_id, "ECHO queued-conversation")
        assignment_uuid = next(iter(run.assignments))
        await self.overflow(task_id, run)
        await tasks._on_event(task_id, run, {"type": "user", "uuid": assignment_uuid, "isReplay": True})
        self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=? ORDER BY id DESC LIMIT 1", task_id)["status"], "queued")
        after = await self.idle(task_id)
        inputs = self.inputs(after["session_id"])
        self.assertTrue(inputs[0].startswith("[Session handoff]"))
        self.assertEqual(inputs[1:], ["ECHO queued-assignment", "ECHO queued-conversation"])
        self.assertEqual(tasks._pending_says(task_id), [])
        self.assertEqual(after["report"], "queued-conversation")
        self.assertTrue(all(row["status"] == "done" for row in self.store.all("SELECT status FROM assignments WHERE task_id=?", task_id)))

    async def test_unacknowledged_handoff_survives_restart_and_promotes_original_assignment(self):
        task_id = self.start_task("SILENTHANG")
        await self.worker_ready(task_id)   # saved, but deliberately not acknowledged
        tasks = self.service.tasks
        old = tasks.live[task_id]
        before = self.store.task(task_id)["session_id"]
        await self.overflow(task_id, old)
        await self.until(lambda: self.store.task(task_id)["session_id"] != before)
        await self.worker_ready(task_id)
        session = self.store.task(task_id)["session_id"]
        self.assertTrue(self.inputs(session)[0].startswith("[Session handoff]"))
        self.assertIsNotNone(self.store.get("handoff_pending:" + task_id))
        self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=?", task_id)["status"], "queued")
        await self.halt()
        await self.boot()
        tasks = self.service.tasks
        await self.until(lambda: task_id in tasks.live and tasks.live[task_id].handoff_uuid)
        await self.until(lambda: len(self.inputs(session)) == 2)
        self.assertEqual(self.inputs(session)[0], self.inputs(session)[1])
        run = tasks.live[task_id]
        await tasks._on_event(task_id, run, {"type": "user", "isReplay": True, "uuid": run.handoff_uuid})
        self.assertIsNone(self.store.get("handoff_pending:" + task_id))
        self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=?", task_id)["status"], "doing")
        self.assertEqual(len(self.history(task_id)), 1)
        self.assertEqual(self.store.task(task_id)["session_id"], session)
        await tasks.stop(task_id)

    async def test_acknowledged_conversation_is_handed_off_without_repeating_completed_work(self):
        task_id = self.start_task("ECHO original-completed-work")
        await self.idle(task_id)
        tasks = self.service.tasks
        text = "SLEEP 0.4 ECHO current-conversation"
        tasks.say(task_id, text)
        run = await self.acknowledged(task_id)
        self.assertEqual(tasks._pending_says(task_id), [])
        self.assertEqual(json.loads(self.store.get("turn_inputs:" + task_id)), [text])
        await self.overflow(task_id, run)
        after = await self.idle(task_id)
        first = self.inputs(after["session_id"])[0]
        self.assertIn(text, first)
        self.assertNotIn("original-completed-work", first)
        self.assertIn("current-conversation", after["report"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"], 1)
        self.assertIsNone(self.store.get("turn_inputs:" + task_id))

    async def test_acknowledged_clarification_is_carried_with_active_assignment(self):
        task_id = self.start_task("SLEEP 0.4 ECHO original")
        run = await self.acknowledged(task_id)
        tasks = self.service.tasks
        tasks.say(task_id, "clarification-not-yet-reported")
        uuid = next(iter(run.says))
        await tasks._on_event(task_id, run, {"type": "user", "isReplay": True, "uuid": uuid})
        await self.overflow(task_id, run)
        after = await self.idle(task_id)
        first = self.inputs(after["session_id"])[0]
        self.assertIn("SLEEP 0.4 ECHO original", first)
        self.assertIn("clarification-not-yet-reported", first)

    async def test_exhausted_unacknowledged_handoff_does_not_block_new_assignment(self):
        task_id = self.start_task("SILENTHANG")
        tasks = self.service.tasks
        for attempt in range(3):
            await self.worker_ready(task_id)
            run = tasks.live[task_id]
            before = self.store.task(task_id)["session_id"]
            await self.overflow(task_id, run)
            if attempt < 2:
                await self.until(lambda: self.store.task(task_id)["session_id"] != before and task_id in tasks.live)
        await self.idle(task_id, "failed")
        for key in ("handoff_pending:", "handoff_intent:", "turn_inputs:", "turn_assignment:"):
            self.assertIsNone(self.store.get(key + task_id))
        tasks.send(task_id, "ECHO genuinely-new-work")
        after = await self.idle(task_id)
        self.assertEqual(after["report"], "genuinely-new-work")
        self.assertEqual(self.inputs(after["session_id"])[-1], "ECHO genuinely-new-work")
        self.assertEqual([row["status"] for row in self.store.all("SELECT status FROM assignments WHERE task_id=? ORDER BY id", task_id)],
                         ["failed", "done"])

    async def test_failed_handoff_launch_retires_old_input_before_new_assignment(self):
        task_id = self.start_task("SILENTHANG")
        await self.worker_ready(task_id)
        tasks = self.service.tasks
        run = tasks.live[task_id]
        with patch.object(harness.Process, "start", new=AsyncMock(side_effect=OSError("cannot start replacement"))):
            await self.overflow(task_id, run)
            await self.idle(task_id, "failed")
        for key in ("handoff_pending:", "handoff_intent:", "turn_inputs:", "turn_assignment:"):
            self.assertIsNone(self.store.get(key + task_id))
        tasks.send(task_id, "ECHO new-work-after-launch-failure")
        after = await self.idle(task_id)
        self.assertEqual(after["report"], "new-work-after-launch-failure")
        self.assertEqual(self.inputs(after["session_id"]), ["ECHO new-work-after-launch-failure"])
        self.assertEqual([row["status"] for row in self.store.all("SELECT status FROM assignments WHERE task_id=? ORDER BY id", task_id)],
                         ["failed", "done"])

    async def test_restart_during_cleanup_keeps_handoff_intent_and_never_resumes_old_session(self):
        await self.restart_during_cleanup()

    async def test_waiting_worker_handoff_intent_is_recovered_after_restart(self):
        await self.restart_during_cleanup(waiting=True)

    async def restart_during_cleanup(self, waiting=False):
        task_id = self.start_task("SLEEP 0.4 ECHO recovered")
        run = await self.acknowledged(task_id)
        before = self.store.task(task_id)["session_id"]
        tasks = self.service.tasks
        if waiting:
            tasks.ask_user(task_id, "Should I proceed?")
        entered, release = threading.Event(), threading.Event()
        sweep = procs.sweep
        def delayed_sweep(root):
            if not entered.is_set():
                entered.set()
                release.wait(10)
            return sweep(root)
        with patch.object(procs, "sweep", side_effect=delayed_sweep):
            try:
                await self.overflow(task_id, run)
                await self.until(entered.is_set)
                self.assertEqual(json.loads(self.store.get("handoff_intent:" + task_id))["reason"], "Prompt is too long")
                await self.halt()
            finally:
                release.set()
            await self.until(lambda: task_id not in tasks.retrying)
        await self.boot()
        after = await self.idle(task_id)
        self.assertNotEqual(after["session_id"], before)
        self.assertEqual(len(self.history(task_id)), 1)
        self.assertTrue(self.inputs(after["session_id"])[0].startswith("[Session handoff]"))
        self.assertIsNone(self.store.get("handoff_intent:" + task_id))
        if waiting:
            self.assertIn("Should I proceed?", self.inputs(after["session_id"])[0])
        argv = [row["argv"] for row in self.argv_log() if row["model"] == "work-model"]
        self.assertEqual(len(argv), 2)
        self.assertTrue(all("--resume" not in args for args in argv))

    async def test_report_done_does_not_create_an_extra_handoff_budget(self):
        task_id = self.start_task("DONE finished TOOLONG")
        await self.idle(task_id, "failed")
        self.assertEqual(len(self.runs(task_id)), 3)
        self.assertEqual(len(self.history(task_id)), 2)
        assignment = self.store.one("SELECT id FROM assignments WHERE task_id=?", task_id)
        self.assertEqual(self.store.get("assignment_handoffs:%s" % assignment["id"]), "2")
        self.assertIsNone(self.store.get("event_handoffs:" + task_id))
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.handoff'", task_id)["n"], 2)

    async def test_handoff_waits_for_cleanup_and_stop_during_cleanup_wins(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                task_id = self.start_task("SLEEP 0.4 ECHO recovered")
                run = await self.acknowledged(task_id)
                before = self.store.task(task_id)["session_id"]
                tasks = self.service.tasks
                entered, release = threading.Event(), threading.Event()
                sweep = procs.sweep
                def delayed_sweep(root):
                    if not entered.is_set():
                        entered.set()
                        release.wait(10)
                    return sweep(root)
                with patch.object(procs, "sweep", side_effect=delayed_sweep):
                    try:
                        await self.overflow(task_id, run)
                        await self.until(entered.is_set, label="old workspace sweep")
                        if stop:
                            await tasks.stop(task_id)
                        else:
                            tasks.say(task_id, "ECHO arrived-during-cleanup")
                            tasks.schedule()
                        self.assertNotIn(task_id, tasks.live)
                        self.assertEqual(len(self.runs(task_id)), 1)
                        self.assertEqual(self.store.task(task_id)["session_id"], before)
                    finally:
                        release.set()
                    after = await self.idle(task_id, "stopped" if stop else "completed")
                if stop:
                    self.assertEqual(self.history(task_id), [])
                    self.assertEqual(after["session_id"], before)
                else:
                    self.assertNotEqual(after["session_id"], before)
                    self.assertEqual(self.inputs(after["session_id"])[1:], ["ECHO arrived-during-cleanup"])
