import asyncio
import json
import os
import subprocess
import unittest

import time

from super_super_harness.adapters import procs
from super_super_harness.ops import releases
from super_super_harness.workers import workspace
from super_super_harness.adapters import claude_code as harness
from super_super_harness.core.store import Store
from tests.support import ServiceCase


class Lifecycle(ServiceCase):
    async def test_task_completes_and_tells_the_router(self):
        task_id = self.start_task("ECHO all good")
        await self.wait_status(task_id, "completed")
        self.assertEqual(self.store.task(task_id)["report"], "all good")
        await self.until(lambda: self.store.one("SELECT 1 FROM messages WHERE kind='worker.done'"), label="done event")
        await self.until(lambda: task_id not in self.service.tasks.live, label="process gone")

    async def test_inline_background_notification_completes_with_only_one_result(self):
        self.cfg.limits["background_grace_seconds"] = 0.1
        task_id = self.start_task("BACKGROUND_INLINE DONE inline-report ECHO foreground")
        await self.wait_status(task_id, "completed", timeout=4)
        await self.until(lambda: task_id not in self.service.tasks.live, label="inline process closed")
        self.assertEqual(self.store.task(task_id)["report"], "inline-report")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.done'", task_id)["n"], 1)
        transcript = self.session_path(self.store.task(task_id)["session_id"]).read_text()
        self.assertIn('"type": "queued_command"', transcript)
        self.service.tasks.say(task_id, "ECHO after-completion")
        await self.wait_status(task_id, "completed", timeout=4)
        self.assertEqual(self.store.task(task_id)["report"], "after-completion")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"], 2)

    async def test_host_followup_after_inline_notification_can_complete(self):
        self.cfg.limits["background_grace_seconds"] = 0.1
        task_id = self.start_task("BACKGROUND_INLINE SLEEP 0.3 ECHO foreground")
        await self.until(lambda: task_id in self.service.tasks.live and self.service.tasks.live[task_id].background_pending,
                         label="inline notification debt")
        self.service.tasks.say(task_id, "ECHO host-followup")
        await self.wait_status(task_id, "completed", timeout=4)
        self.assertEqual(self.store.task(task_id)["report"], "host-followup")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"], 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.done'", task_id)["n"], 1)

    async def test_completion_stuck_wakes_router_with_warning(self):
        self.cfg.limits["background_grace_seconds"] = 4000
        task_id = self.start_task("BACKGROUND_INLINE ECHO foreground")
        await self.until(lambda: self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='result'", task_id))
        run = self.service.tasks.live[task_id]
        run.idle_since = run.background_empty_since = time.time() - 1801
        await self.service.tasks.tick()
        event = self.store.one("SELECT severity FROM events WHERE task_id=? AND source='system' AND type='worker.completion_stuck'", task_id)
        self.assertEqual(event["severity"], "error")
        self.assertIsNotNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.completion_stuck'", task_id))
        trigger = self.store.one("SELECT severity,route FROM triggers WHERE thread=? AND type='worker.completion_stuck'", "task:" + task_id)
        self.assertEqual((trigger["severity"], trigger["route"]), ("warn", "wake"))
        self.assertEqual(self.status(task_id), "running")

    async def test_background_finishes_before_foreground(self):
        task_id = self.start_task("BACKGROUND 0.1 SLEEP 0.6 ECHO foreground")
        await self.wait_status(task_id, "completed")
        await self.until(lambda: task_id not in self.service.tasks.live, label="process closed")
        count = self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"]
        self.assertEqual(count, 2, "completion dropped the internally queued background notification result")
        self.assertEqual(self.store.task(task_id)["report"], "background finished")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.done'", task_id)["n"], 1)

    async def test_multiple_background_notifications_are_all_consumed(self):
        for command, results in (("BACKGROUND 0.1 BACKGROUND 0.1", 3), ("BACKGROUND_BATCH 0.1 3", 2)):
            with self.subTest(command=command):
                task_id = self.start_task(command + " SLEEP 0.6 ECHO foreground")
                await self.wait_status(task_id, "completed")
                await self.until(lambda: task_id not in self.service.tasks.live, label="notifications drained")
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"], results)
                self.assertEqual(self.store.task(task_id)["report"], "background finished")
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.done'", task_id)["n"], 1)

    async def test_followup_queued_before_background_notification_is_not_completion(self):
        task_id = self.start_task("BACKGROUND 0.4 SLEEP 0.8 ECHO foreground")
        await self.until(lambda: task_id in self.service.tasks.live and self.service.tasks.live[task_id].background,
                         label="background started")
        self.service.tasks.send(task_id, "ECHO ordinary-followup")
        await self.wait_status(task_id, "completed")
        await self.until(lambda: task_id not in self.service.tasks.live, label="notification processed")
        self.assertEqual(self.store.task(task_id)["report"], "background finished")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"], 3)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.done'", task_id)["n"], 1)

    async def test_process_exit_with_background_work_does_not_leave_running(self):
        self.cfg.limits["worker_retries"] = 0
        for command in ("BACKGROUND 60 ECHO foreground", "BACKGROUND 0.1 SLEEP 0.6 CRASH"):
            with self.subTest(command=command):
                task_id = self.start_task(command)
                if "CRASH" not in command:
                    await self.until(lambda: self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='result'", task_id))
                    self.assertEqual(self.status(task_id), "running")
                    self.service.tasks.live[task_id].process.proc.kill()
                await self.wait_status(task_id, "failed")
                await self.until(lambda: task_id not in self.service.tasks.live, label="exited process removed")
                self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.done'", task_id))
                self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=?", task_id)["status"], "failed")

    async def test_background_wait_uses_stall_reminder_without_finishing(self):
        task_id = self.start_task("BACKGROUND 60 ECHO foreground")
        await self.until(lambda: self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='result'", task_id))
        run = self.service.tasks.live[task_id]
        run.last_event = time.time() - self.cfg.limits["stall_minutes"] * 60 - 1
        await self.service.tasks.tick()
        self.assertTrue(run.stalled)
        self.assertEqual(self.status(task_id), "running")
        self.assertFalse(run.ending)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='worker.stalled'", task_id))
        self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.done'", task_id))

    async def test_limit_queues_the_rest_in_order(self):
        first, second, third = [self.start_task("SLEEP 1 ECHO x") for _ in range(3)]
        await asyncio.sleep(0.3)
        self.assertEqual((self.status(first), self.status(second), self.status(third)), ("running", "running", "queued"))
        await self.wait_status(third, "completed")

    async def test_each_task_gets_its_own_workspace(self):
        one, two = self.start_task("ECHO a"), self.start_task("ECHO b")
        await self.wait_status(one, "completed")
        await self.wait_status(two, "completed")
        rows = [self.store.task(one), self.store.task(two)]
        self.assertNotEqual(rows[0]["cwd"], rows[1]["cwd"])
        self.assertTrue(os.path.isfile(rows[0]["workspace"] + "/brief.md"))
        tmpdirs = {entry["tmpdir"] for entry in self.argv_log() if entry["model"] == "work-model"}
        self.assertEqual(tmpdirs, {rows[0]["workspace"] + "/tmp", rows[1]["workspace"] + "/tmp"})

    async def test_question_then_answer_resumes_the_same_session(self):
        task_id = self.start_task("SLEEP 0.3 ECHO asked")
        await self.until(lambda: task_id in self.service.tasks.live and self.service.tasks.live[task_id].process, label="live")
        self.service.tasks.ask_user(task_id, "要继续吗")
        await self.until(lambda: self.store.one("SELECT 1 FROM messages WHERE kind='worker.question'"), label="question event")
        self.assertEqual(self.status(task_id), "waiting")
        await self.until(lambda: task_id not in self.service.tasks.live, label="process gone")
        self.service.tasks.send(task_id, "ECHO continued")
        await self.wait_status(task_id, "completed")
        runs = [entry for entry in self.argv_log() if entry["model"] == "work-model"]
        self.assertIn("--session-id", runs[0]["argv"])
        self.assertIn("--resume", runs[1]["argv"])
        self.assertEqual(self.store.task(task_id)["question"], "")

    async def test_answer_reaches_a_waiting_worker_with_background_tasks(self):
        for method in ("send", "say"):
            with self.subTest(method=method):
                tasks = self.service.tasks
                task_id = self.start_task("BACKGROUND 2 SLEEP 0.3 ECHO asked")
                await self.worker_ready(task_id)
                run = tasks.live[task_id]
                await self.until(lambda: run.background and not run.pending_uuids, label="background started")
                tasks.ask_user(task_id, "要继续吗", ["继续", "停止"])
                await self.until(lambda: self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='result'", task_id), label="question turn ended")
                self.assertEqual(self.status(task_id), "waiting")
                self.assertIs(tasks.live[task_id], run)
                tasks.tell(task_id, "ECHO held-note")
                tasks.deliver(task_id, "ECHO held-event")
                self.assertEqual(self.status(task_id), "waiting")
                answer = getattr(tasks, method)(task_id, "SLEEP 0.2 DONE reply-report ECHO answer")
                self.assertEqual(answer.get("delivered"), "after the current command")
                self.assertEqual((self.status(task_id), self.store.task(task_id)["question"], json.loads(self.store.task(task_id)["pending"])),
                                 ("running", "", []))
                await self.until(lambda: not run.pending_uuids, label="reply acknowledged")
                lines = self.session_path(self.store.task(task_id)["session_id"]).read_text().splitlines()
                inputs = [row["message"]["content"] for row in map(json.loads, lines) if row["type"] == "user"]
                self.assertEqual(inputs[-3:], ["ECHO held-note", "ECHO held-event", "SLEEP 0.2 DONE reply-report ECHO answer"])
                if method == "send":
                    self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=? ORDER BY id DESC LIMIT 1", task_id)["status"], "doing")
                else:
                    self.assertEqual(tasks._pending_says(task_id), [])
                    self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"], 1)
                questions = self.store.all("SELECT text FROM messages WHERE task_id=? AND kind='worker.question'", task_id)
                self.assertEqual(len(questions), 1)
                self.assertIn("继续 / 停止", questions[0]["text"])
                await self.until(lambda: self.store.task(task_id)["report"] == "reply-report", label="reply report")
                self.assertEqual(self.status(task_id), "running")
                self.assertTrue(run.background)
                self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.done'", task_id))
                await self.until(lambda: task_id not in tasks.live, label="background and process ended")
                task = self.store.task(task_id)
                self.assertEqual((task["status"], task["report"], json.loads(task["pending"])), ("completed", "reply-report", []))
                self.assertIsNone(self.store.one("SELECT 1 FROM assignments WHERE task_id=? AND status='queued'", task_id))
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=?", task_id)["n"], 1)
                done = self.store.all("SELECT text FROM messages WHERE task_id=? AND kind='worker.done'", task_id)
                self.assertEqual(len(done), 1)
                self.assertIn("reply-report", done[0]["text"])

    async def test_undelivered_input_restarts_instead_of_completing(self):
        for source in ("pending", "assignment", "say"):
            with self.subTest(source=source):
                tasks = self.service.tasks
                task_id = self.start_task("SLEEP 0.4 ECHO obsolete")
                await self.worker_ready(task_id)
                run = tasks.live[task_id]
                await self.until(lambda: not run.pending_uuids, label="brief acknowledged")
                text = "ECHO recovered-%s" % source
                # Simulate persisted input which did not reach the live process (the B19 end state).
                if source == "pending":
                    tasks._hold_input(task_id, text)
                elif source == "assignment":
                    tasks._assign(task_id, text)
                else:
                    self.store.set("say_pending:" + task_id, json.dumps([{"id": "unread", "text": text, "created": time.time()}]))
                tasks.held.add(task_id)   # inspect the guard before allowing the scheduler to restart
                try:
                    with self.assertLogs("super_super_harness.workers.tasks", level="ERROR") as logs:
                        await self.until(lambda: task_id not in tasks.live, label="guard ended process")
                    self.assertIn("cannot complete", logs.output[0])
                    self.assertEqual(self.status(task_id), "queued")
                    self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=? ORDER BY id LIMIT 1", task_id)["status"], "doing")
                    self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.done'", task_id))
                    self.assertEqual(self.store.one("SELECT severity FROM events WHERE task_id=? AND type='worker.completion_deferred'", task_id)["severity"], "error")
                finally:
                    tasks.release(task_id)
                await self.wait_status(task_id, "completed")
                await self.until(lambda: task_id not in tasks.live, label="recovered process ended")
                task = self.store.task(task_id)
                self.assertEqual((task["report"], json.loads(task["pending"]), tasks._pending_says(task_id)), ("recovered-%s" % source, [], []))
                self.assertIsNone(self.store.one("SELECT 1 FROM assignments WHERE task_id=? AND status='queued'", task_id))
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=?", task_id)["n"], 2)
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.done'", task_id)["n"], 1)

    async def test_pending_restart_waits_for_old_workspace_cleanup(self):
        import threading
        from unittest.mock import patch
        tasks = self.service.tasks
        self.cfg.limits["worker_retries"] = 0
        task_id = self.start_task("SLEEP 0.4 ECHO obsolete")
        await self.worker_ready(task_id)
        run = tasks.live[task_id]
        await self.until(lambda: not run.pending_uuids, label="brief acknowledged")
        tasks._hold_input(task_id, "SLEEP 0.3 ECHO recovered")
        entered, release = threading.Event(), threading.Event()
        sweep = procs.sweep
        def delayed_sweep(root):
            if not entered.is_set():
                entered.set()
                release.wait(10)
            return sweep(root)
        with patch.object(procs, "sweep", side_effect=delayed_sweep):
            try:
                with self.assertLogs("super_super_harness.workers.tasks", level="ERROR"):
                    await self.until(entered.is_set, label="old workspace sweep")
                self.assertEqual(self.status(task_id), "queued")
                self.assertNotIn(task_id, tasks.live)
                tasks.schedule()
                self.assertNotIn(task_id, tasks.live)
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=?", task_id)["n"], 1)
            finally:
                release.set()
            await self.wait_status(task_id, "completed")
            await self.until(lambda: task_id not in tasks.live and task_id not in tasks.retrying, label="recovered process ended")
        self.assertEqual(self.store.task(task_id)["report"], "recovered")
        runs = self.store.all("SELECT exit_code FROM worker_runs WHERE task_id=?", task_id)
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(row["exit_code"] == 0 for row in runs), runs)
        self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.failed'", task_id))

    async def test_background_turns_notify_each_question_only_once(self):
        task_id = self.start_task("BACKGROUND 0.8 BACKGROUND 1.5 SLEEP 0.3 ECHO asked")
        tasks = self.service.tasks
        await self.worker_ready(task_id)
        run = tasks.live[task_id]
        await self.until(lambda: run.background and not run.pending_uuids, label="background started")
        tasks.ask_user(task_id, "同一个问题？")
        await self.until(lambda: self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.question'", task_id), label="question notified before background ends")
        self.assertTrue(run.background)
        await self.until(lambda: task_id not in tasks.live, label="background and process ended")
        self.assertEqual(self.status(task_id), "waiting")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE task_id=? AND kind='worker.question'", task_id)["n"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='result'", task_id)["n"], 3)
        self.assertIsNone(self.store.one("SELECT 1 FROM messages WHERE task_id=? AND kind='worker.done'", task_id))

    async def test_send_refuses_a_stale_view(self):
        task_id = self.start_task("ECHO done")
        await self.wait_status(task_id, "completed")
        answer = self.service.tasks.send(task_id, "more", expected="running")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["status"], "completed")

    async def test_interrupt_then_resume(self):
        task_id = self.start_task("SLEEP 30 ECHO never")
        await self.until(lambda: self.service.tasks.live.get(task_id) and self.service.tasks.live[task_id].last_event, label="started")
        self.service.tasks.interrupt(task_id)
        await self.wait_status(task_id, "interrupted", 5)
        self.service.tasks.resume(task_id, "ECHO back")
        await self.wait_status(task_id, "completed")

    async def test_failure_is_retried_and_can_recover(self):
        task_id = self.start_task("FAIL")
        await self.wait_status(task_id, "completed", 10)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM worker_runs WHERE task_id=?", task_id)["c"], 2)

    async def test_repeated_failure_ends_in_failed(self):
        task_id = self.start_task("ALWAYSFAIL")
        await self.wait_status(task_id, "failed", 10)
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM worker_runs WHERE task_id=?", task_id)["c"], 2)
        await self.until(lambda: self.store.one("SELECT 1 FROM messages WHERE kind='worker.failed'"), label="failed event")

    async def test_exit_without_result_counts_as_failure(self):
        task_id = self.start_task("ALWAYSCRASH")
        await self.wait_status(task_id, "failed", 10)

    async def test_switch_model_keeps_the_session(self):
        task_id = self.start_task("SLEEP 30")
        await self.worker_ready(task_id)
        session = self.store.task(task_id)["session_id"]
        answer = await self.service.tasks.switch_model(task_id, "other")
        self.assertTrue(answer["ok"])
        await self.until(lambda: any(entry["model"] == "other-model" for entry in self.argv_log()), label="relaunch")
        last = [entry for entry in self.argv_log() if entry["model"] == "other-model"][0]
        self.assertEqual(last["argv"][last["argv"].index("--resume") + 1], session)

    async def test_stop_kills_commands_that_left_the_process_group(self):
        task_id = self.start_task("SLEEP 30")
        await self.until(lambda: self.service.tasks.live.get(task_id) and self.service.tasks.live[task_id].last_event, label="started")
        cwd = self.store.task(task_id)["cwd"]
        orphan = subprocess.Popen(["sleep", "40"], cwd=cwd, start_new_session=True)
        self.assertTrue(procs.in_dir(cwd))
        await self.service.tasks.stop(task_id)
        self.assertEqual(self.status(task_id), "stopped")
        self.assertEqual(procs.in_dir(cwd), [])
        orphan.wait(timeout=2)

    async def test_git_task_gets_a_worktree_and_branch(self):
        repo = self.home / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=x", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        task_id = self.start_task("ECHO ok", repo=str(repo))
        await self.wait_status(task_id, "completed")
        task = self.store.task(task_id)
        self.assertEqual(task["branch"], "w/nova")
        self.assertIn("w/nova", subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout)
        self.assertTrue(os.path.isfile(task["cwd"] + "/.git"))

    async def test_restart_kills_the_old_worker_and_resumes(self):
        task_id = self.start_task("SLEEP 60")
        await self.until(lambda: self.store.one("SELECT pid FROM worker_runs WHERE task_id=?", task_id), label="run row")
        await self.worker_ready(task_id)
        old = self.store.one("SELECT pid FROM worker_runs WHERE task_id=?", task_id)["pid"]
        cwd = self.store.task(task_id)["cwd"]
        orphan = subprocess.Popen(["sleep", "40"], cwd=cwd, start_new_session=True)
        self.running.cancel()
        try:
            await self.running
        except asyncio.CancelledError:
            pass
        self.service.tasks.closed = self.service.router.closed = True
        self.service.store.close()
        argv_count = len(self.argv_log())
        await self.boot()
        await self.until(lambda: self.store.one("SELECT COUNT(*) c FROM worker_runs WHERE task_id=?", task_id)["c"] == 2, label="relaunch")
        orphan.wait(timeout=3)
        self.assertFalse(procs.alive(old))
        await self.until(lambda: any(entry["model"] == "work-model" and "--resume" in entry["argv"]
                                     for entry in self.argv_log()[argv_count:]), label="resume argv")
        await self.until(lambda: any("接着跑" in text for text in self.outbox()), label="restart notice")

    async def test_worker_lost_before_reading_the_brief_gets_it_again(self):
        task_id = self.start_task("ECHO brief arrived")
        session = self.store.task(task_id)["session_id"]
        self.session_path(session).parent.mkdir(parents=True, exist_ok=True)
        self.session_path(session).write_text('{"type": "queue-operation"}\n')
        await self.wait_status(task_id, "completed")
        self.assertNotEqual(self.store.task(task_id)["session_id"], session)
        self.assertEqual(self.store.task(task_id)["report"], "brief arrived")

    async def test_recent_tasks_stay_in_the_list(self):
        task_id = self.start_task("ECHO ok", name="修对账")
        await self.wait_status(task_id, "completed")
        listing = self.service.tasks.render()
        self.assertIn("杂事", listing)
        self.assertIn("空闲", listing)
        self.assertIn("修对账", listing)


class FinalReport(ServiceCase):
    """Claude Code runs a sub-agent's MCP calls through the Worker's own router server; only the stream tells them apart."""

    def tool_results(self, task_id):
        path = self.session_path(self.store.task(task_id)["session_id"])
        return [row for row in map(json.loads, path.read_text().splitlines()) if row["type"] == "tool_result"]

    async def test_a_sub_agent_cannot_hand_in_the_workers_final_report(self):
        task_id = self.start_task("SUBDONE 子代理的审查结论 ECHO 本人修完后的结论")
        await self.wait_status(task_id, "completed")
        self.assertEqual(self.store.task(task_id)["report"], "本人修完后的结论")
        refused = self.tool_results(task_id)[0]
        self.assertTrue(refused["is_error"])
        self.assertIn("sub-agent", refused["text"])
        self.assertTrue(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='report.refused'", task_id))
        done = self.store.one("SELECT text FROM messages WHERE kind='worker.done' AND task_id=?", task_id)
        self.assertIn("本人修完后的结论", done["text"])
        self.assertNotIn("子代理的审查结论", done["text"])
        from super_super_harness.ops.config import REPO_ROOT
        self.assertIn("Only you hand in the final report", (REPO_ROOT / "prompts" / "worker.md").read_text())

    async def test_the_worker_itself_hands_in_the_final_report(self):
        task_id = self.start_task("DONE 本人的最终报告 ECHO 收尾")
        await self.wait_status(task_id, "completed")
        self.assertEqual(self.store.task(task_id)["report"], "本人的最终报告")
        self.assertFalse(self.tool_results(task_id)[0]["is_error"])

    async def test_without_the_tool_use_id_the_exact_call_on_the_stream_decides(self):
        sub = self.start_task("OLDSUBDONE 子代理的结论 ECHO 本人收尾")
        own = self.start_task("OLDDONE 本人的报告 ECHO 收尾")
        await self.wait_status(sub, "completed")
        await self.wait_status(own, "completed")
        self.assertEqual(self.store.task(sub)["report"], "本人收尾")
        self.assertIn("sub-agent", self.tool_results(sub)[0]["text"])
        self.assertEqual(self.store.task(own)["report"], "本人的报告")

    async def test_a_call_the_workers_stream_never_showed_does_not_end_the_task(self):
        self.service.tasks.caller_wait = 0.3
        task_id = self.start_task("GHOSTDONE 来源不明的报告 ECHO 本人收尾")
        await self.wait_status(task_id, "completed")
        task = self.store.task(task_id)
        self.assertEqual(task["report"], "本人收尾")
        self.assertIn("来源不明的报告", task["progress"])
        self.assertTrue(self.tool_results(task_id)[0]["is_error"])


class StopDuringLaunch(ServiceCase):
    """task_stop can land on any await of _launch. After it the Worker must not run, hold a slot, complete, report or tell the Router."""

    def gate(self, target, name, fail=None):
        """Patch target.name so the launch waits inside it until release is set; it raises fail when given."""
        import threading
        from unittest.mock import patch
        entered, release = threading.Event(), threading.Event()
        original = getattr(target, name)
        if asyncio.iscoroutinefunction(original):
            async def gated(*args, **kwargs):
                entered.set()
                await asyncio.to_thread(release.wait, 10)
                if fail:
                    raise fail
                return await original(*args, **kwargs)
        else:
            def gated(*args, **kwargs):
                if name != "_git" or args[1:] == ("rev-parse", "HEAD"):   # workspace.create runs other git commands first
                    entered.set()
                    release.wait(10)
                    if fail:
                        raise fail
                return original(*args, **kwargs)
        return patch.object(target, name, gated), entered, release

    def workers(self, *models):
        """Worker launches the fake Harness logged; the Router's own process may not have written the log yet."""
        path = self.home / "sessions" / "proj" / "argv.log"
        return [entry for entry in self.argv_log() if entry["model"] in models] if path.exists() else []

    async def launch_over(self):
        await self.until(lambda: not any(t.get_coro().__qualname__ == "Tasks._launch" for t in asyncio.all_tasks()), label="launch over")

    async def stop_during(self, target, name, repo=None, fail=None, while_closing=None):
        """while_closing(task_id) runs when the stop starts closing the process, before the process is gone."""
        from unittest.mock import patch
        patched, entered, release = self.gate(target, name, fail)
        close, hooks = harness.Process.close, [while_closing] if while_closing else []
        async def closing(process, *args, **kwargs):
            if hooks:
                hooks.pop()(task_id)
            return await close(process, *args, **kwargs)
        with patched, patch.object(harness.Process, "close", closing):
            task_id = self.start_task("ECHO 不该出现的报告", repo=repo)
            await self.until(entered.is_set, label=name)
            self.assertTrue((await self.service.tasks.stop(task_id))["ok"])
            right_after = (self.status(task_id), task_id in self.service.tasks.live)
            release.set()
            await self.launch_over()
        await asyncio.sleep(1.5)   # a Worker that did start answers the brief well within this
        task = self.store.task(task_id)
        seen = {"right after stop (status, holds a slot)": right_after,
                "status": task["status"], "report": task["report"], "holds a slot": task_id in self.service.tasks.live,
                "told the Router": [row["kind"] for row in self.store.all(
                    "SELECT kind FROM messages WHERE task_id=? AND kind IN ('worker.done', 'worker.failed')", task_id)],
                "open worker_runs": self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=? AND ended IS NULL", task_id)["n"],
                "live Worker processes": [entry["pid"] for entry in self.workers("work-model") if procs.alive(entry["pid"])]}
        self.assertEqual(seen, {"right after stop (status, holds a slot)": ("stopped", False),
                                "status": "stopped", "report": "", "holds a slot": False,
                                "told the Router": [], "open worker_runs": 0, "live Worker processes": []})
        return task_id

    async def test_stop_while_the_workspace_is_created(self):
        await self.stop_during(workspace, "create")

    async def test_stop_while_the_branch_head_is_read(self):
        repo = self.home / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=x", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        await self.stop_during(workspace, "_git", repo=str(repo))

    async def test_stop_while_the_provider_is_prepared(self):
        await self.stop_during(harness, "ensure_provider")

    async def test_stop_while_the_session_is_prepared(self):
        await self.stop_during(harness, "prepare_session")

    async def test_stop_while_the_process_starts(self):
        await self.stop_during(harness.Process, "start")

    async def test_stop_after_the_process_started(self):
        await self.stop_during(procs, "start_time")

    async def test_a_launch_that_fails_after_the_stop_stays_stopped(self):
        task_id = await self.stop_during(workspace, "create", fail=OSError("disk full"))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='task.failed'", task_id))

    async def test_a_process_that_fails_to_start_after_the_stop_stays_stopped(self):
        task_id = await self.stop_during(harness.Process, "start", fail=OSError("no such file"))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='task.failed'", task_id))

    async def test_input_while_the_stop_closes_the_process_does_not_start_it_again(self):
        answers = []
        def arrive(task_id):
            answers.append(self.service.tasks.send(task_id, "ECHO 又来一条")["ok"])
            answers.append(self.service.tasks.deliver(task_id, "ECHO 事件"))
            with self.assertRaises(ValueError):
                self.service.tasks.ask_user(task_id, "还要继续吗")
            answers.append(self.status(task_id))
        await self.stop_during(procs, "start_time", while_closing=arrive)
        self.assertEqual(answers, [False, False, "stopped"])

    async def test_a_report_while_the_stop_closes_the_process_is_refused(self):
        from unittest.mock import patch
        task_id = self.start_task("SLEEP 30")
        await self.worker_ready(task_id)
        text = "停止时交的报告"
        # The Worker's own call, as its stream would show it: only the stop can refuse it.
        self.service.tasks.live[task_id].callers["toolu_stop"] = (None, "mcp__router__report_done", {"text": text})
        close, refused = harness.Process.close, []
        async def report_then_close(process, *args, **kwargs):
            if not refused:
                with self.assertRaisesRegex(ValueError, "stopped"):
                    await self.service.tasks.hand_in(task_id, text, "toolu_stop")
                refused.append(True)
            return await close(process, *args, **kwargs)
        with patch.object(harness.Process, "close", report_then_close):
            await self.service.tasks.stop(task_id)
        self.assertEqual((refused, self.store.task(task_id)["report"]), ([True], ""))

    async def test_a_report_the_stop_overtakes_while_it_waits_for_the_stream_is_refused(self):
        from unittest.mock import patch
        task_id = self.start_task("SLEEP 30")
        await self.worker_ready(task_id)
        run, text = self.service.tasks.live[task_id], "停止时交的报告"
        handing_in = asyncio.create_task(self.service.tasks.hand_in(task_id, text, "toolu_late"))
        await asyncio.sleep(0.1)   # hand_in is waiting for the tool_use to show on the stream
        close = harness.Process.close
        async def stream_catches_up(process, *args, **kwargs):
            run.callers["toolu_late"] = (None, "mcp__router__report_done", {"text": text})
            await asyncio.sleep(0.2)
            return await close(process, *args, **kwargs)
        with patch.object(harness.Process, "close", stream_catches_up):
            await self.service.tasks.stop(task_id)
        with self.assertRaisesRegex(ValueError, "stopped"):
            await handing_in
        self.assertEqual(self.store.task(task_id)["report"], "")

    async def test_a_stop_before_the_launch_runs_creates_no_workspace(self):
        task_id = self.start_task("ECHO 不该出现的报告")
        await self.service.tasks.stop(task_id)   # before the event loop ran the launch scheduled by start
        await self.launch_over()
        self.assertEqual((self.status(task_id), self.store.task(task_id)["workspace"], self.service.tasks.unwinding), ("stopped", None, set()))

    async def test_a_failure_the_stop_overtook_does_not_turn_stopped_into_failed(self):
        task_id = self.start_task("SLEEP 30")
        await self.worker_ready(task_id)
        self.store.set("retries:" + task_id, 99)   # this failure would be the last retry
        await self.service.tasks.stop(task_id)
        # _retry_or_fail resumes after its own _end_process, which the stop overtook.
        await self.service.tasks._retry_or_fail(task_id, "process exited with 3 and no result")
        self.assertEqual(self.status(task_id), "stopped")
        self.assertEqual(self.store.all("SELECT kind FROM messages WHERE task_id=? AND kind='worker.failed'", task_id), [])

    async def test_a_hold_before_the_launch_runs_leaves_the_task_startable(self):
        tasks = self.service.tasks
        task_id = self.start_task("ECHO 不该出现的报告")
        await tasks.hold(task_id)   # before the event loop ran the launch scheduled by start
        await self.launch_over()
        self.assertEqual((self.status(task_id), task_id in tasks.live, task_id in tasks.unwinding), ("interrupted", False, False))
        tasks.release(task_id)
        tasks.send(task_id, "ECHO 交还后继续")
        await self.wait_status(task_id, "completed")
        self.assertEqual(self.store.task(task_id)["report"], "交还后继续")

    async def test_a_hold_during_the_launch_starts_no_worker(self):
        tasks = self.service.tasks
        patched, entered, release = self.gate(harness, "ensure_provider")
        with patched:
            task_id = self.start_task("ECHO 不该出现的报告")
            await self.until(entered.is_set, label="ensure_provider")
            await tasks.hold(task_id)
            release.set()
            await self.launch_over()
        await asyncio.sleep(1.5)
        self.assertEqual((self.status(task_id), self.store.task(task_id)["report"], task_id in tasks.live), ("interrupted", "", False))
        self.assertEqual(self.workers("work-model"), [])

    async def test_a_model_switch_during_the_launch_starts_one_worker_on_the_new_model(self):
        patched, entered, release = self.gate(harness, "ensure_provider")
        with patched:
            task_id = self.start_task("SLEEP 30")
            await self.until(entered.is_set, label="ensure_provider")
            self.assertTrue((await self.service.tasks.switch_model(task_id, "other"))["ok"])
            await asyncio.sleep(0.3)
            self.assertNotIn(task_id, self.service.tasks.live)   # the ended launch has not returned: no second one yet
            release.set()
            await self.until(lambda: self.workers("other-model"), label="relaunch")
        await self.worker_ready(task_id)
        self.assertEqual([entry["model"] for entry in self.workers("work-model", "other-model")], ["other-model"])
        self.assertEqual(self.status(task_id), "running")

    async def test_a_result_while_the_process_closes_does_not_complete_a_stopped_task(self):
        from unittest.mock import patch
        task_id = self.start_task("SLEEP 30")
        await self.worker_ready(task_id)
        run = self.service.tasks.live[task_id]
        await self.until(lambda: run.ready and not run.pending_uuids, label="brief taken")
        close, answered = harness.Process.close, []
        async def answer_then_close(process, *args, **kwargs):
            # A Harness can still finish the turn it is in while stop closes its stdin.
            if not answered:
                answered.append(1)
                await process.on_event({"type": "result", "subtype": "success", "is_error": False, "result": "收尾时的回答"})
            return await close(process, *args, **kwargs)
        with patch.object(harness.Process, "close", answer_then_close):
            await self.service.tasks.stop(task_id)
        await asyncio.sleep(0.5)
        task = self.store.task(task_id)
        told = [row["kind"] for row in self.store.all("SELECT kind FROM messages WHERE task_id=? AND kind='worker.done'", task_id)]
        self.assertEqual((task["status"], task["report"], told), ("stopped", "", []))


class RecoveryOrdering(ServiceCase):
    async def test_provider_preparation_failure_is_unhealthy_and_failover_still_works(self):
        from dataclasses import replace
        from unittest.mock import patch
        self.cfg.providers["one"] = replace(self.cfg.providers["one"], ensure="fake-preparation")
        original = harness.ensure_provider
        def prepare(provider):
            if provider.name == "one":
                raise ValueError("fake preparation failed")
            return original(provider)
        self.primary.down()
        with patch.object(harness, "ensure_provider", side_effect=prepare):
            self.assertFalse(await self.service.router._probe("main", deep=True))
            await self.service.router.failover()
        self.assertEqual(self.service.router.profile_name, "spare")
        self.assertIsNotNone(self.service.router.process)
        self.assertFalse(self.running.done())

    async def test_web_waits_for_worker_recovery_and_retry_preserves_messages(self):
        from unittest.mock import patch
        from super_super_harness.daemon import Service
        from tests.support import free_port
        task_id = self.start_task("SLEEP 60")
        await self.worker_ready(task_id)
        await self.until(lambda: self.store.one("SELECT id FROM worker_runs WHERE task_id=?", task_id))
        await self.halt()
        self.cfg.web["port"] = free_port()
        self.service = Service(self.cfg)
        self.store.update_task(task_id, pending=json.dumps(["SLEEP 0.3 ECHO persisted-before-restart"]))
        entered, release = asyncio.Event(), asyncio.Event()
        recover = self.service.tasks.recover
        async def blocked_recover():
            entered.set()
            await release.wait()
            return await recover()
        with patch.object(self.service.tasks, "recover", side_effect=blocked_recover):
            self.running = asyncio.create_task(self.service.run())
            await entered.wait()
            try:
                self.assertIsNone(self.service.web.server)
                self.assertEqual(self.service.tasks.live, {})
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=?", task_id)["n"], 1)
                with self.assertRaises(OSError):
                    await asyncio.open_connection("127.0.0.1", self.cfg.web["port"])
                # The owner socket must not admit task_start/start_many during recovery either.
                with self.assertRaises(OSError):
                    await asyncio.open_unix_connection(str(self.cfg.sock))
                self.assertEqual(json.loads(self.store.task(task_id)["pending"]), ["SLEEP 0.3 ECHO persisted-before-restart"])
            finally:
                release.set()
            await self.until(lambda: self.service.web.server and self.service.router.process)
            # A browser retries its unacknowledged send only after the listener is ready.
            reader, writer = await asyncio.open_connection("127.0.0.1", self.cfg.web["port"])
            body = json.dumps({"text": "ECHO retry-after-recovery"}).encode()
            cookie = self.service.web.cookie_name + "=" + self.service.web.session_cookie()
            writer.write(("POST /api/workers/%s/send HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\nCookie: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                          % (task_id, self.cfg.web["port"], self.cfg.web["port"], cookie, len(body))).encode() + body)
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            self.assertIn(b"HTTP/1.1 200", response)
            session = self.store.task(task_id)["session_id"]
            await self.until(lambda: harness.session_has(self.cfg, session, "retry-after-recovery"))
            self.assertTrue(harness.session_has(self.cfg, session, "persisted-before-restart"))
            self.assertEqual(self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=?", task_id)["n"], 2)


class LiveInput(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from super_super_harness.workers.tasks import Run, Tasks
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.tasks = Tasks(SimpleNamespace(limits={"max_workers": 1}), self.store, AsyncMock(), None)
        self.task_id = self.store.add_task("task", "brief", "work", None, "session", "", "")
        self.process = SimpleNamespace(send=Mock(side_effect=lambda text: "input-%s" % text), close=AsyncMock())
        self.run = Run(process=self.process, ready=True, pending_uuids=set(), assignments={}, callers={})
        self.tasks.live[self.task_id] = self.run

    async def test_queue_input_sends_to_ready_process_regardless_of_status(self):
        for status in ("running", "waiting", "queued"):
            with self.subTest(status=status):
                self.store.update_task(self.task_id, status=status, question="q?")
                self.assertEqual(self.tasks._queue_input(self.task_id, status), "after the current command")
                self.process.send.assert_called_with(status)
                task = self.store.task(self.task_id)
                self.assertEqual((task["status"], task["question"], json.loads(task["pending"])), ("running", "", []))
                self.assertIn("input-%s" % status, self.run.pending_uuids)

    async def test_launching_input_is_held_and_ending_input_is_queued(self):
        self.store.update_task(self.task_id, status="running")
        self.run.ready = False
        self.assertEqual(self.tasks._queue_input(self.task_id, "launching"), "when the Worker has started")
        self.assertEqual(self.store.task(self.task_id)["status"], "running")
        self.run.ready, self.run.ending = True, True
        self.assertIsNone(self.tasks._queue_input(self.task_id, "ending", first=True))
        self.process.send.assert_not_called()
        task = self.store.task(self.task_id)
        self.assertEqual((task["status"], json.loads(task["pending"])), ("queued", ["ending", "launching"]))

    async def test_failed_send_is_queued_instead_of_reported_delivered(self):
        from unittest.mock import Mock
        self.store.update_task(self.task_id, status="waiting", question="q?")
        self.process.send = Mock(return_value=None)
        answer = self.tasks.send(self.task_id, "answer")
        self.assertNotIn("delivered", answer)
        self.assertEqual(answer["status"], "queued")
        self.assertEqual(json.loads(self.store.task(self.task_id)["pending"]), ["answer"])
        self.assertEqual(self.store.one("SELECT status FROM assignments WHERE task_id=?", self.task_id)["status"], "queued")

    async def test_failed_flush_preserves_only_unsent_context_and_answer(self):
        from unittest.mock import Mock, call
        self.tasks.ask_user(self.task_id, "q?")
        self.tasks.tell(self.task_id, "first-context")
        self.tasks.deliver(self.task_id, "second-context")
        self.process.send = Mock(side_effect=["first-uuid", None])
        answer = self.tasks.send(self.task_id, "answer")
        self.assertNotIn("delivered", answer)
        self.assertEqual(self.process.send.call_args_list, [call("first-context"), call("second-context")])
        self.assertEqual(json.loads(self.store.task(self.task_id)["pending"]), ["second-context", "answer"])
        self.assertEqual(self.run.pending_uuids, {"first-uuid"})

    async def test_events_and_notes_use_live_queued_process_but_do_not_answer_questions(self):
        for method in ("deliver", "tell"):
            with self.subTest(method=method):
                self.store.update_task(self.task_id, status="queued", pending="[]")
                getattr(self.tasks, method)(self.task_id, method)
                self.process.send.assert_called_with(method)
                self.assertEqual(self.store.task(self.task_id)["status"], "running")
                self.process.send.reset_mock()
                self.tasks.ask_user(self.task_id, "q?")
                getattr(self.tasks, method)(self.task_id, "held")
                self.process.send.assert_not_called()
                task = self.store.task(self.task_id)
                self.assertEqual((task["status"], task["question"], json.loads(task["pending"])), ("waiting", "q?", ["held"]))

    async def test_queued_status_alone_blocks_completion(self):
        self.store.update_task(self.task_id, status="queued")
        with self.assertLogs("super_super_harness.workers.tasks", level="ERROR"):
            await self.tasks._on_result(self.task_id, self.run, {"result": "not done"})
        await asyncio.sleep(0)   # scheduled process close
        self.assertEqual(self.store.task(self.task_id)["status"], "queued")
        self.assertTrue(self.run.ending)
        self.process.close.assert_awaited_once()
        self.tasks.on_change.assert_not_awaited()

    async def test_distinct_questions_with_identical_text_are_each_notified_once(self):
        self.run.background = True
        for _ in range(2):
            self.tasks.ask_user(self.task_id, "same question", ["yes", "no"])
            for _ in range(2):
                await self.tasks._on_result(self.task_id, self.run, {"result": "waiting"})
        self.assertEqual(self.tasks.on_change.await_count, 2)
        self.tasks.on_change.assert_awaited_with("worker.question", self.task_id, "same question\n选项：yes / no")
        self.process.close.assert_not_awaited()


class BackgroundCompletion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from super_super_harness.workers.tasks import Run, Tasks
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.tasks = Tasks(SimpleNamespace(limits={"max_workers": 1, "dormant_days": 7, "stall_minutes": 30,
                                                  "background_grace_seconds": 20}), self.store, AsyncMock(), None)
        self.task_id = self.store.add_task("task", "brief", "work", None, "session", "", "")
        self.store.update_task(self.task_id, status="running")
        self.process = SimpleNamespace(close=AsyncMock())
        self.run = Run(process=self.process, ready=True, pending_uuids=set(), assignments={}, callers={})
        self.tasks.live[self.task_id] = self.run
        self.tasks.schedule = Mock()

    async def event(self, **msg):
        await self.tasks._on_event(self.task_id, self.run, msg)

    async def snapshot(self, *ids):
        await self.event(type="system", subtype="background_tasks_changed", tasks=[{"id": value} for value in ids])

    def notification(self, *ids):
        return "\n".join("<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n</task-notification>" % value for value in ids)

    async def notify(self, *ids, blocks=False, **fields):
        text = self.notification(*ids)
        content = [{"type": "text", "text": text}] if blocks else text
        await self.event(type="user", message={"role": "user", "content": content}, **fields)

    async def result(self):
        await self.event(type="result", subtype="success", result="finished")
        await asyncio.sleep(0)   # run any scheduled process close

    def assert_running(self):
        self.assertEqual(self.store.task(self.task_id)["status"], "running")
        self.assertFalse(self.run.ending)
        self.tasks.on_change.assert_not_awaited()
        self.process.close.assert_not_awaited()

    def assert_completed(self):
        self.assertEqual(self.store.task(self.task_id)["status"], "completed")
        self.tasks.on_change.assert_awaited_once_with("worker.done", self.task_id, "finished")
        self.process.close.assert_awaited_once()

    async def tick_after(self, seconds):
        from unittest.mock import patch
        with patch("super_super_harness.workers.tasks.time.time", return_value=self.run.idle_since + seconds):
            await self.tasks.tick()
        await asyncio.sleep(0)

    async def test_visible_inline_attachment_pays_same_turn_notification(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.event(type="attachment", attachment={"type": "queued_command", "commandMode": "task-notification",
                                                       "prompt": self.notification("a")})
        await self.result()
        self.assert_completed()

    async def test_child_and_ordinary_attachments_do_not_pay_notification_debt(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.event(type="attachment", parent_tool_use_id="child", attachment={"type": "queued_command",
                         "commandMode": "task-notification", "prompt": self.notification("a")})
        await self.event(type="attachment", attachment={"type": "queued_command", "commandMode": "prompt",
                                                       "prompt": self.notification("a")})
        self.assertFalse(self.run.background_notified)
        await self.result()
        self.assert_running()

    async def test_inline_notification_debt_expires_without_another_result(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        await self.tick_after(19)
        self.assert_running()
        await self.tick_after(21)
        self.assert_completed()
        self.assertFalse(self.run.background_pending)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE type='result'")["n"], 1)
        await self.tick_after(22)
        self.assert_completed()   # no duplicate done or close

    async def test_empty_snapshot_after_result_starts_its_own_grace(self):
        from unittest.mock import patch
        await self.snapshot("a")
        await self.result()
        with patch("super_super_harness.workers.tasks.time.time", return_value=self.run.idle_since + 25):
            await self.snapshot()
        await self.tick_after(30)
        self.assert_running()
        await self.tick_after(46)
        self.assert_completed()

    async def test_repeated_empty_snapshots_do_not_extend_grace(self):
        from unittest.mock import patch
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        with patch("super_super_harness.workers.tasks.time.time", return_value=self.run.idle_since + 19):
            await self.snapshot()
        await self.tick_after(21)
        self.assert_completed()

    async def test_new_root_activity_cancels_idle_completion(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        await self.notify("a")
        await self.tick_after(21)
        self.assert_running()
        await self.result()
        self.assert_completed()

    async def test_host_reply_resets_grace_then_can_finish(self):
        from unittest.mock import patch
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        old_idle = self.run.idle_since
        self.run.pending_uuids.add("host")
        await self.event(type="user", isReplay=True, uuid="host", message={"content": "followup"})
        await self.tick_after(100)
        self.assert_running()
        with patch("super_super_harness.workers.tasks.time.time", return_value=old_idle + 100):
            await self.result()
        await self.tick_after(19)
        self.assert_running()
        await self.tick_after(21)
        self.assert_completed()

    async def test_grace_never_finishes_nonempty_background_list(self):
        await self.snapshot("a")
        await self.result()
        await self.tick_after(21)
        self.assert_running()
        await self.snapshot()
        await self.snapshot("b")
        await self.tick_after(30)
        self.assert_running()

    async def test_grace_respects_all_delivery_barriers(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        self.run.pending_uuids.add("unacknowledged")
        await self.tick_after(21)
        self.assert_running()
        self.run.pending_uuids.clear()
        for field, value in (("pending", '["held input"]'), ("status", "queued")):
            self.store.update_task(self.task_id, **{field: value})
            await self.tick_after(21)
            self.assertFalse(self.run.ending)
            self.store.update_task(self.task_id, pending="[]", status="running")
        for key, value in (("say_pending:", '[{"id":1,"text":"hello"}]'), ("handoff_pending:", "{}")):
            self.store.set(key + self.task_id, value)
            await self.tick_after(21)
            self.assert_running()
            self.store.unset(key + self.task_id)
        assignment = self.tasks._assign(self.task_id, "queued assignment")
        await self.tick_after(21)
        self.assert_running()
        self.store.run("UPDATE assignments SET status='doing' WHERE id=?", assignment)
        await self.tick_after(21)
        self.assert_completed()

    async def test_child_result_cannot_make_active_root_idle(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.event(type="result", parent_tool_use_id="child", subtype="success", result="child")
        await self.tick_after(21)
        self.assert_running()

    async def test_error_result_cannot_leave_old_idle_success_eligible(self):
        from unittest.mock import AsyncMock
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        self.tasks._retry_or_fail = AsyncMock()
        await self.event(type="result", is_error=True, result="failed")
        await self.tick_after(21)
        self.assertIsNone(self.run.idle_result)
        self.assertEqual(self.store.task(self.task_id)["status"], "running")
        self.tasks.on_change.assert_not_awaited()
        self.tasks._retry_or_fail.assert_awaited_once()

    async def test_idle_stuck_alert_is_error_and_only_once(self):
        self.tasks.cfg.limits["background_grace_seconds"] = 4000
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        await self.tick_after(1801)
        await self.tick_after(1802)
        self.assertEqual(self.store.task(self.task_id)["status"], "running")
        self.assertEqual(self.store.one("SELECT severity FROM events WHERE type='worker.completion_stuck'")["severity"], "error")
        self.assertEqual(self.tasks.on_change.await_count, 1)
        self.assertEqual(self.tasks.on_change.call_args.args[0], "worker.completion_stuck")
        self.process.close.assert_not_awaited()

    async def test_stuck_alert_rechecks_input_after_waking_router(self):
        async def send_followup(*args):
            self.run.pending_uuids.add("new-input")
        self.tasks.on_change.side_effect = send_followup
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        await self.tick_after(1801)
        self.assertEqual(self.store.task(self.task_id)["status"], "running")
        self.assertFalse(self.run.ending)
        self.assertTrue(self.run.background_pending)

    async def test_empty_snapshot_does_not_finish_spawning_turn_or_retire_inputs(self):
        assignment = self.tasks._assign(self.task_id, "original")
        self.store.run("UPDATE assignments SET status='doing' WHERE id=?", assignment)
        self.run.current_assignment = assignment
        self.store.set("turn_inputs:" + self.task_id, '["original"]')
        self.store.set("turn_assignment:" + self.task_id, assignment)
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        self.assert_running()
        self.assertEqual(self.run.current_assignment, assignment)
        self.assertEqual(self.store.get("turn_inputs:" + self.task_id), '["original"]')
        self.assertEqual(self.store.one("SELECT status FROM assignments WHERE id=?", assignment)["status"], "doing")
        await self.notify("a")   # internal input need not carry isReplay or a host UUID
        self.assert_running()
        await self.result()
        self.assert_completed()
        self.assertIsNone(self.store.get("turn_inputs:" + self.task_id))

    async def test_separate_notifications_need_both_results(self):
        await self.snapshot("a", "b")
        await self.snapshot()
        await self.result()
        self.assert_running()
        await self.notify("a", isReplay=True)
        await self.result()
        self.assert_running()
        self.assertEqual(self.run.background_pending, {"b": 1})
        await self.notify("b", blocks=True)
        self.assert_running()
        await self.result()
        self.assert_completed()

    async def test_batched_notifications_need_one_result_not_one_per_task(self):
        await self.snapshot("a", "b")
        await self.result()
        await self.snapshot()
        await self.notify("a", "b", blocks=True)
        self.assert_running()
        await self.result()
        self.assert_completed()

    async def test_notification_turn_can_start_more_background_work(self):
        await self.snapshot("a")
        await self.result()
        await self.snapshot()
        await self.notify("a")
        await self.snapshot("b")
        await self.snapshot()
        await self.result()
        self.assert_running()
        await self.notify("b")
        await self.result()
        self.assert_completed()

    async def test_reused_task_id_requires_its_new_notification(self):
        await self.snapshot("a")
        await self.result()
        await self.snapshot()
        await self.notify("a")
        await self.snapshot("a")   # SendMessage resumed the same background agent
        await self.snapshot("a")   # duplicate snapshot must not count as another execution
        await self.snapshot()
        await self.result()
        self.assert_running()
        await self.notify("a")
        await self.result()
        self.assert_completed()

    async def test_host_followup_result_before_first_notification_keeps_debt(self):
        await self.snapshot("a")
        await self.snapshot()
        self.run.pending_uuids.add("followup")
        await self.result()
        self.assert_running()
        await self.event(type="user", uuid="followup", isReplay=True, message={"content": "ordinary followup"})
        await self.result()
        self.assert_running()
        self.assertEqual(self.run.background_pending, {"a": 1})
        self.assertFalse(self.run.host_input)
        await self.notify("a")
        await self.result()
        self.assert_completed()

    async def test_old_notification_after_agent_resume_cannot_pay_new_execution(self):
        await self.snapshot("a")
        await self.result()
        await self.snapshot()
        await self.snapshot("a")
        await self.notify("a")   # old execution's notification arrived after resuming
        await self.snapshot()
        await self.result()
        self.assert_running()
        self.assertEqual(self.run.background_pending, {"a": 1})
        await self.notify("a")
        await self.result()
        self.assert_completed()

    async def test_batched_notifications_of_reused_id_pay_both_executions(self):
        await self.snapshot("a")
        await self.result()
        await self.snapshot()
        await self.snapshot("a")
        await self.snapshot()
        await self.notify("a", "a")
        await self.result()
        self.assert_completed()

    async def test_visible_notification_does_not_disable_later_fallback(self):
        await self.snapshot("a", "b")
        await self.result()
        await self.snapshot()
        await self.notify("a")
        await self.result()
        self.assert_running()
        await self.result()   # B's notification input is not exposed by this stream
        self.assert_completed()

    async def test_no_notification_input_falls_back_to_later_result(self):
        await self.snapshot("a")
        await self.snapshot()
        await self.result()
        self.assert_running()
        await self.result()
        self.assert_completed()

    async def test_fallback_when_background_ends_after_foreground_result(self):
        await self.snapshot("a")
        await self.result()
        self.assert_running()
        await self.snapshot()
        self.assert_running()
        await self.result()
        self.assert_completed()

    async def test_snapshot_without_ids_uses_the_same_fallback(self):
        await self.event(type="system", subtype="background_tasks_changed", tasks=[{}])
        await self.snapshot()
        await self.result()
        self.assert_running()
        await self.result()
        self.assert_completed()

    async def test_snapshot_without_ids_can_drain_on_later_notification_result(self):
        await self.event(type="system", subtype="background_tasks_changed", tasks=[{}])
        await self.result()
        await self.snapshot()
        await self.notify("a")
        await self.result()
        self.assert_completed()

    async def test_nested_events_do_not_drain_parent_background_work(self):
        await self.snapshot("a")
        await self.result()
        await self.event(type="system", subtype="background_tasks_changed", tasks=[], parent_tool_use_id="child")
        await self.notify("a", parent_tool_use_id="child")
        await self.event(type="result", result="child result", parent_tool_use_id="child")
        self.assert_running()
        self.assertTrue(self.run.background)
        await self.snapshot()
        await self.notify("a")
        await self.result()
        self.assert_completed()

    async def test_host_input_and_tool_output_are_not_internal_notifications(self):
        await self.snapshot("a")
        self.run.pending_uuids.add("host-input")
        await self.notify("a", uuid="host-input", isReplay=True)
        await self.event(type="user", message={"content": [{"type": "tool_result", "content": self.notification("a")}]})
        self.assertFalse(self.run.background_notified)
        self.assertFalse(self.run.pending_uuids)


class ProcessCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from super_super_harness.workers.tasks import Tasks
        self.store = Store(":memory:")
        self.tasks = Tasks(SimpleNamespace(limits={"max_workers": 1}), self.store, None, None)
        self.task_id = self.store.add_task("task", "brief", "work", None, "session", "", "")
        self.addCleanup(self.store.close)

    async def test_cleanup_queued_after_shutdown_does_not_read_closed_store(self):
        self.tasks.closed = True
        self.store.close()
        await self.tasks._end_process(self.task_id)

    async def test_queued_cleanup_keeps_workspace_after_shutdown(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        process = SimpleNamespace(close=AsyncMock())
        self.tasks.live[self.task_id] = SimpleNamespace(process=process, ready=True)
        self.store.update_task(self.task_id, workspace="test-workspace")
        cleanup = self.tasks._end_process(self.task_id)
        self.tasks.closed = True
        self.store.close()
        with patch("super_super_harness.workers.tasks.procs.sweep") as sweep:
            await asyncio.wait_for(cleanup, 2)
            process.close.assert_awaited_once()
            sweep.assert_called_once_with("test-workspace")

    async def test_shutdown_during_process_close_keeps_workspace_cleanup(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        process = SimpleNamespace(close=AsyncMock(side_effect=close))
        self.tasks.live[self.task_id] = SimpleNamespace(process=process, ready=True)
        self.store.update_task(self.task_id, workspace="test-workspace")
        with patch("super_super_harness.workers.tasks.procs.sweep") as sweep:
            cleanup = asyncio.create_task(self.tasks._end_process(self.task_id))
            await entered.wait()
            self.tasks.closed = True
            self.store.close()
            release.set()
            await asyncio.wait_for(cleanup, 2)
            process.close.assert_awaited_once()
            sweep.assert_called_once_with("test-workspace")


class Releases(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        self.base = Path(tempfile.mkdtemp(dir="/tmp"))
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.cfg = SimpleNamespace(home=self.base / "home")
        self.cfg.home.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        self.commit("v1")

    def commit(self, text):
        (self.repo / "version.txt").write_text(text)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x", "commit", "-q", "-m", text], cwd=self.repo, check=True)

    def version(self):
        return (self.cfg.home / "current" / "version.txt").read_text()

    def upgrade(self, healthy=True, tests=True):
        return releases.upgrade(self.cfg, self.repo, "main", lambda: healthy, check=lambda target: tests)

    def test_release_gate_covers_all_unit_modules_and_short_temp_paths(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        def check_home(*args, **options):
            home = options["env"]["SSHARNESS_HOME"]
            self.assertNotEqual(home, "must-not-inherit")
            self.assertTrue(home.startswith("/tmp/ssharness-release-test-"))
            self.assertTrue(os.path.isdir(home))
            self.assertEqual(os.listdir(home), [])
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, SSHARNESS_HOME="must-not-inherit", TMPDIR="/long/system/temp/path"), \
             patch("super_super_harness.ops.releases.subprocess.run", side_effect=check_home) as run:
            self.assertTrue(releases.run_tests(self.repo))
        args, options = run.call_args
        self.assertEqual(args[0][3:], ["tests.test_workers", "tests.test_router", "tests.test_web",
                                      "tests.test_harness", "tests.test_chat", "tests.test_reminders", "tests.test_avatar",
                                      "tests.test_bus", "tests.test_items", "tests.test_suber", "tests.test_c1", "tests.test_c2",
                                   "tests.test_c5", "tests.test_ui_v2",
                                   "tests.test_ui_v2_1", "tests.test_routing_graph", "tests.test_worker_identity"])
        self.assertFalse(os.path.exists(options["env"]["SSHARNESS_HOME"]))
        self.assertEqual(options["env"]["TMPDIR"], "/tmp")
        self.assertEqual(options["env"]["SSHARNESS_NO_CAFFEINATE"], "1")

    def test_upgrade_switches_to_the_commit(self):
        self.assertTrue(self.upgrade()["ok"])
        self.commit("v2")
        answer = self.upgrade()
        self.assertTrue(answer["ok"])
        self.assertEqual(self.version(), "v2")
        self.assertIsNotNone(answer["previous"])

    def test_failed_tests_change_nothing(self):
        self.upgrade()
        self.commit("v2")
        self.assertFalse(self.upgrade(tests=False)["ok"])
        self.assertEqual(self.version(), "v1")

    def test_unhealthy_version_rolls_back(self):
        self.upgrade()
        self.commit("v2")
        states = iter([False, True])
        answer = releases.upgrade(self.cfg, self.repo, "main", lambda: next(states), check=lambda target: True)
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["restarted"])
        self.assertEqual(self.version(), "v1")

    def test_rollback_goes_to_the_other_version(self):
        self.upgrade()
        time.sleep(0.02)
        self.commit("v2")
        self.upgrade()
        self.assertTrue(releases.rollback(self.cfg, lambda: True)["ok"])
        self.assertEqual(self.version(), "v1")

    def test_only_committed_files_are_installed(self):
        (self.repo / "scratch.txt").write_text("not committed")
        self.upgrade()
        self.assertFalse((self.cfg.home / "current" / "scratch.txt").exists())


class QuickConfiguration(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        import tempfile
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def test_background_grace_default_and_configuration_validation(self):
        from super_super_harness.ops import config
        from tests.support import write_config
        write_config(self.home, 1, 2)
        self.assertEqual(config.load(self.home).limits["background_grace_seconds"], 20)
        for value in ("1", "45", "0", "-1", "true", "1.5", '"20"', "nan", "inf"):
            with self.subTest(value=value):
                write_config(self.home, 1, 2, limits="background_grace_seconds = " + value)
                if value in ("1", "45"):
                    self.assertEqual(config.load(self.home).limits["background_grace_seconds"], int(value))
                else:
                    with self.assertRaisesRegex(ValueError, "background_grace_seconds"):
                        config.load(self.home)

    def test_renamed_home_precedence_without_creating_default_state(self):
        from unittest.mock import patch
        from super_super_harness.ops import config
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True):
            self.assertEqual(config.home_path(), self.home / ".super-super-harness")
            with patch.dict(os.environ, SSHARNESS_HOME=str(self.home / "isolated")):
                self.assertEqual(config.home_path(), self.home / "isolated")
                self.assertEqual(config.home_path(self.home / "explicit"), self.home / "explicit")
        self.assertEqual(list(self.home.iterdir()), [])

    def args(self, **overrides):
        from pathlib import Path
        from types import SimpleNamespace
        defaults = dict(migrate_from=None, provider="custom", profile="default", base_url="http://127.0.0.1:1",
                        auth_env="ANTHROPIC_AUTH_TOKEN", model="declared-model", context_window=123456,
                        binary=str(Path(__file__).with_name("fake_claude.py")), key_env=None)
        return SimpleNamespace(**dict(defaults, **overrides))

    def test_init_hidden_key_atomic_permissions_and_no_key_in_config_or_output(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch
        from super_super_harness import cli
        from super_super_harness.ops import config, setup
        with patch.dict(os.environ, SSHARNESS_HOME=str(self.home)):
            args = self.args()
            argv = ["supersuperharness", "init", "--provider", args.provider, "--base-url", args.base_url,
                    "--auth-env", args.auth_env, "--model", args.model, "--context-window", str(args.context_window),
                    "--binary", args.binary]
            with patch("sys.argv", argv), patch("super_super_harness.ops.setup.getpass.getpass", return_value="fake-key-not-real"), \
                 redirect_stdout(io.StringIO()) as initialized:
                self.assertEqual(cli.main(), 0)
            self.assertNotIn("fake-key-not-real", initialized.getvalue())
            path = self.home / "profiles.toml"
            cfg = config.load(self.home)
            provider = cfg.providers["custom"]
            self.assertEqual(provider.key(), "fake-key-not-real")
            self.assertNotIn("fake-key-not-real", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(provider.key_file.stat().st_mode & 0o777, 0o600)
            with redirect_stdout(io.StringIO()) as output, patch("sys.argv", ["supersuperharness", "doctor", "--timeout", "0.1"]):
                self.assertEqual(cli.main(), 1)
            self.assertNotIn("fake-key-not-real", output.getvalue())
            self.assertFalse(cfg.db.exists())
            self.assertFalse(cfg.sock.exists())

    def test_init_eof_is_actionable_without_traceback(self):
        import sys
        for args in ([], ["--provider", "deepseek", "--model", "deepseek-flash"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "init", *args],
                                        input="", capture_output=True, text=True, timeout=5, start_new_session=True,
                                        env=dict(os.environ, SSHARNESS_HOME=str(self.home)))
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertIn("--provider", result.stderr)
                self.assertIn("--key-env", result.stderr)
                self.assertFalse((self.home / "profiles.toml").exists())
                self.assertFalse((self.home / "keys").exists())

    def test_init_empty_hidden_key_has_specific_safe_error(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import patch
        from super_super_harness import cli
        args = self.args()
        argv = ["supersuperharness", "init", "--provider", args.provider, "--base-url", args.base_url,
                "--auth-env", args.auth_env, "--model", args.model, "--context-window", str(args.context_window)]
        with patch.dict(os.environ, SSHARNESS_HOME=str(self.home)), patch("sys.argv", argv), \
                patch("super_super_harness.ops.setup.getpass.getpass", return_value="   "), \
                redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(), 1)
        self.assertIn("API Key is empty", errors.getvalue())
        self.assertFalse((self.home / "profiles.toml").exists())
        self.assertFalse((self.home / "keys").exists())

    def test_init_rejects_home_in_checkout_for_file_and_environment_credentials(self):
        from unittest.mock import patch
        from super_super_harness.ops import setup
        # A worktree's .git is a file; a regular checkout's .git is a directory.
        for git_file in (False, True):
            checkout = self.home / ("worktree" if git_file else "checkout")
            checkout.mkdir()
            if git_file:
                (checkout / ".git").write_text("gitdir: unused-test-marker\n")
            else:
                (checkout / ".git").mkdir()
            for key_env in (None, "SSHARNESS_TEST_KEY"):
                with self.subTest(git_file=git_file, key_env=key_env):
                    home = checkout / ("env-home" if key_env else "file-home")
                    with patch.dict(os.environ, SSHARNESS_HOME=str(home), SSHARNESS_TEST_KEY="fake-env-key"), \
                            self.assertRaisesRegex(ValueError, "outside a git checkout"):
                        setup.initialize(home, self.args(key_env=key_env), ask_key=lambda _: "fake-file-key")
                    self.assertFalse((home / "profiles.toml").exists())
                    self.assertFalse((home / "keys").exists())

    def test_gitignore_covers_private_state_but_not_public_profiles(self):
        from pathlib import Path
        from super_super_harness.ops import config
        repo = self.home / "ignore-check"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        (repo / ".gitignore").write_bytes((config.REPO_ROOT / ".gitignore").read_bytes())
        private = ["owner-token", "web-session-key", "router.db", "router.db-wal", "router.db-shm",
                   "keys/fake-key", "run/session", "workspaces/task/brief.md", "service.pid", "service.log", "router.sock"]
        paths = private + ["private/" + name for name in private]
        ignored = subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--stdin"],
                                 cwd=repo, input="\n".join(paths) + "\n", capture_output=True, text=True, check=False)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(set(ignored.stdout.splitlines()), set(paths))
        public = subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "profiles.toml"],
                                cwd=repo, capture_output=True, text=True, check=False)
        self.assertEqual(public.returncode, 1, public.stdout + public.stderr)

    def test_declared_provider_overrides_builtin_endpoint_auth_models_and_window(self):
        from unittest.mock import patch
        from super_super_harness.ops import config, setup
        args = self.args(provider="stepplan", model="step-5-preview", context_window=333333,
                         auth_env="ANTHROPIC_API_KEY", key_env="SSHARNESS_TEST_KEY")
        with patch.dict(os.environ, SSHARNESS_TEST_KEY="fake-env-key", ANTHROPIC_AUTH_TOKEN="not-for-this-provider"):
            setup.initialize(self.home, args)
            cfg = config.load(self.home)
            profile = cfg.profile("default")
            provider = cfg.providers["stepplan"]
            self.assertEqual((provider.base_url, provider.auth_env), ("http://127.0.0.1:1", "ANTHROPIC_API_KEY"))
            self.assertEqual(provider.models["step-5-preview"], 1000000)
            self.assertEqual(profile.context_window, 333333)
            # Explicit user catalog declarations still override the built-in model window.
            raw = setup.read_raw(self.home / "profiles.toml")
            raw["providers"]["stepplan"]["models"] = {"step-5-preview": 444444}
            raw["profiles"]["default"].pop("context_window")
            (self.home / "profiles.toml").write_text(setup.toml_text(raw))
            overridden = config.load(self.home)
            self.assertEqual(overridden.providers["stepplan"].models["step-5-preview"], 444444)
            self.assertEqual(overridden.profile("default").context_window, 444444)
            env = harness.build_env(cfg, profile)
            self.assertEqual(env["ANTHROPIC_API_KEY"], "fake-env-key")
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertFalse((self.home / "keys").exists())
            self.assertNotIn("fake-env-key", (self.home / "profiles.toml").read_text())

    def test_init_existing_profiles_and_migration_are_never_overwritten(self):
        from super_super_harness.ops import setup
        setup.initialize(self.home, self.args(), ask_key=lambda _: "fake-original")
        original = (self.home / "profiles.toml").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            setup.initialize(self.home, self.args(), ask_key=lambda _: self.fail("must not ask for a key"))
        source = self.home / "different.toml"
        source.write_bytes(original + b"\n# different\n")
        with self.assertRaisesRegex(ValueError, "not overwrite"):
            setup.migrate(self.home, source)
        self.assertEqual((self.home / "profiles.toml").read_bytes(), original)
        setup.initialize(self.home, self.args(profile="another", provider="another-provider"), ask_key=lambda _: "fake-second")
        from super_super_harness.ops import config
        self.assertEqual(set(config.load(self.home).profiles), {"default", "another"})

    def test_existing_state_without_private_config_requires_migration_not_public_defaults(self):
        from super_super_harness.ops import config, setup
        # An intentionally invalid DB file proves this path checks existence only.
        (self.home / "router.db").write_bytes(b"not-a-database-do-not-open")
        with self.assertRaisesRegex(ValueError, "migrate"):
            config.load(self.home)
        with self.assertRaisesRegex(ValueError, "migrate"):
            setup.initialize(self.home, self.args(), ask_key=lambda _: self.fail("must not request a new key"))
        setup.migrate(self.home, config.REPO_ROOT / "profiles.toml")
        self.assertIn("default", config.load(self.home).profiles)
        self.assertEqual((self.home / "router.db").read_bytes(), b"not-a-database-do-not-open")

    def test_legacy_migration_survives_install_upgrade_and_rollback_without_keys_or_db(self):
        from pathlib import Path
        from unittest.mock import patch
        from super_super_harness.ops import config, setup
        from tests.support import write_config
        old = self.home / "old-release"
        old.mkdir()
        write_config(old, 1, 2)
        raw = setup.read_raw(old / "profiles.toml")
        raw["providers"] = {"legacy-one": raw["providers"]["one"], "legacy-two": raw["providers"]["two"]}
        raw["profiles"] = {"legacy-main": raw["profiles"]["main"], "legacy-backup": raw["profiles"]["spare"], "legacy-worker": raw["profiles"]["work"]}
        for name, profile in raw["profiles"].items():
            profile["provider"] = "legacy-one" if name == "legacy-main" else "legacy-two"
            profile["add_dirs"] = [str(self.home / "extra-workspace")]
        raw.update(default_profile="legacy-worker", source_repo=str(self.home / "source"))
        raw["router"] = {"primary": "legacy-main", "backup": "legacy-backup"}
        source = old / "profiles.toml"
        source.write_text(setup.toml_text(raw))
        # Migration structurally validates, but must never read these existing key files.
        for provider in raw["providers"].values():
            Path(provider["key_file"]).unlink(missing_ok=True)
        private_home = self.home / "private-home"
        with patch.dict(os.environ, SSHARNESS_HOME=str(private_home)):
            destination = setup.migrate(config.home_path(), source)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(setup.migrate(private_home, source), destination)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            public = self.home / "public-repo"
            public.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
            (public / "profiles.toml").write_bytes((config.REPO_ROOT / "profiles.toml").read_bytes())
            for version in ("first", "second"):
                (public / "version").write_text(version)
                subprocess.run(["git", "add", "."], cwd=public, check=True)
                subprocess.run(["git", "-c", "user.email=test@example.invalid", "-c", "user.name=test", "commit", "-qm", version], cwd=public, check=True)
                cfg = config.load()
                def restart():
                    with patch.object(config, "REPO_ROOT", releases.current(cfg)):
                        restored = config.load()
                    self.assertEqual(set(restored.profiles), {"legacy-main", "legacy-backup", "legacy-worker"})
                    self.assertEqual((restored.primary, restored.backup, restored.default_profile), ("legacy-main", "legacy-backup", "legacy-worker"))
                    self.assertEqual(restored.source_repo, self.home / "source")
                    return True
                self.assertTrue(releases.upgrade(cfg, public, "main", restart, check=lambda _: True)["ok"])
            self.assertTrue(releases.rollback(cfg, restart)["ok"])
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertFalse(cfg.db.exists())
            self.assertFalse(cfg.sock.exists())

    def test_init_reuses_shared_provider_credentials_and_rejects_implicit_rotation(self):
        from super_super_harness.ops import config, setup
        setup.initialize(self.home, self.args(), ask_key=lambda _: "fake-original")
        original_key_file = config.load(self.home).providers["custom"].key_file
        setup.initialize(self.home, self.args(profile="second"), ask_key=lambda _: self.fail("shared key must not be requested"))
        cfg = config.load(self.home)
        self.assertEqual(cfg.providers["custom"].key_file, original_key_file)
        self.assertEqual(cfg.providers["custom"].key(), "fake-original")
        with self.assertRaisesRegex(ValueError, "shared"):
            setup.initialize(self.home, self.args(profile="third", base_url="http://127.0.0.1:2"),
                             ask_key=lambda _: self.fail("must validate before requesting a key"))

    def test_concurrent_init_serializes_read_modify_write_without_losing_profiles(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from super_super_harness.ops import config, setup
        entered, release = threading.Event(), threading.Event()
        def key_prompt(_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release init")
            return "fake-initial-key"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(setup.initialize, self.home, self.args(), ask_key=key_prompt)
            self.assertTrue(entered.wait(2))
            second = pool.submit(setup.initialize, self.home, self.args(profile="second"),
                                 ask_key=lambda _: self.fail("second init must reuse the first key"))
            try:
                time.sleep(.05)
                self.assertFalse(second.done())
            finally:
                release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(set(config.load(self.home).profiles), {"default", "second"})

    def test_new_profile_window_does_not_change_existing_inherited_model_default(self):
        from super_super_harness.ops import config, setup
        setup.initialize(self.home, self.args(), ask_key=lambda _: "fake-original")
        path = self.home / "profiles.toml"
        raw = setup.read_raw(path)
        raw["profiles"]["default"].pop("context_window")
        path.write_text(setup.toml_text(raw))
        self.assertEqual(config.load(self.home).profile("default").context_window, 123456)
        setup.initialize(self.home, self.args(profile="different-window", context_window=333333),
                         ask_key=lambda _: self.fail("shared key must not be requested"))
        cfg = config.load(self.home)
        self.assertEqual(cfg.profile("default").context_window, 123456)
        self.assertEqual(cfg.profile("different-window").context_window, 333333)

    def test_symlinked_home_or_keys_cannot_store_credentials_inside_a_checkout(self):
        from super_super_harness.ops import setup
        checkout = self.home / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        (checkout / "private-home").mkdir()
        alias = self.home / "home-alias"
        alias.symlink_to(checkout / "private-home", target_is_directory=True)
        actual_home = self.home / "outside-home"
        actual_home.mkdir()
        (actual_home / "keys").symlink_to(checkout, target_is_directory=True)
        for home in (alias, actual_home):
            with self.subTest(home=home), self.assertRaisesRegex(ValueError, "outside a git checkout"):
                setup.initialize(home, self.args(), ask_key=lambda _: "fake-not-to-be-written")
            self.assertFalse((home / "profiles.toml").exists())
        for path in checkout.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"fake-not-to-be-written", path.read_bytes())

    def test_invalid_endpoint_and_auth_are_rejected_before_reading_or_writing_credentials(self):
        from super_super_harness.ops import setup
        for changes in ({"base_url": "https://fake-secret@example.invalid"}, {"auth_env": "OTHER_AUTH"},
                        {"context_window": 0}, {"base_url": "https://example.invalid/?key=fake-secret"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                setup.initialize(self.home, self.args(**changes), ask_key=lambda _: self.fail("must not request a key"))
        self.assertFalse((self.home / "profiles.toml").exists())
        self.assertFalse((self.home / "keys").exists())

    def test_presets_and_public_defaults_have_no_personal_configuration(self):
        from super_super_harness.ops import config
        from super_super_harness.ops.catalog import PRESETS, PLANNED
        cfg = config.load(self.home)
        self.assertEqual(set(PRESETS), {"anthropic", "deepseek", "glm", "stepplan", "kimi"})
        self.assertIn("建设中", PLANNED["gpt"])
        self.assertEqual(cfg.providers["kimi"].base_url, "https://api.moonshot.cn/anthropic")
        for name, provider in cfg.providers.items():
            self.assertEqual(provider.auth_env, "ANTHROPIC_API_KEY" if name == "anthropic" else "ANTHROPIC_AUTH_TOKEN")
        raw = (config.REPO_ROOT / "profiles.toml").read_text()
        for private in ("/users/", "/home/", "source_repo"):
            self.assertNotIn(private, raw.lower())
        self.assertIsNone(cfg.source_repo)
        self.assertTrue(all(not profile.add_dirs for profile in cfg.profiles.values()))

    def test_doctor_fake_endpoint_validates_both_auth_modes_and_model_failures_without_leaks(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from unittest.mock import patch
        from super_super_harness.ops import config, setup
        from tests.support import free_port
        seen = []
        expected = {"auth_env": "ANTHROPIC_AUTH_TOKEN", "model": "declared-model", "status": 200}
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)  # reachable != valid key
                self.end_headers()
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                auth = self.headers.get("Authorization") if expected["auth_env"] == "ANTHROPIC_AUTH_TOKEN" else self.headers.get("x-api-key")
                seen.append((self.path, body["model"], auth, self.headers.get("x-api-key"), self.headers.get("Authorization")))
                self.send_response(expected["status"])
                self.end_headers()
                self.wfile.write(b'{"type":"message","content":[]}' if expected["status"] == 200 else b'{"error":"fake-key-not-real"}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", free_port()), Endpoint)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for auth_env in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            with self.subTest(auth_env=auth_env):
                folder = self.home / auth_env
                args = self.args(base_url="http://127.0.0.1:%d" % server.server_port, auth_env=auth_env)
                setup.initialize(folder, args, ask_key=lambda _: "fake-key-not-real")
                cfg = config.load(folder)
                expected["auth_env"], expected["status"] = auth_env, 200
                result = setup.doctor(cfg, timeout=1)
                self.assertTrue(result[0]["ok"], result)
                self.assertEqual(seen[-1][0:2], ("/v1/messages", "declared-model"))
                self.assertEqual(seen[-1][2], "Bearer fake-key-not-real" if auth_env == "ANTHROPIC_AUTH_TOKEN" else "fake-key-not-real")
                self.assertIsNone(seen[-1][3] if auth_env == "ANTHROPIC_AUTH_TOKEN" else seen[-1][4])
                for status in (401, 404, 400):
                    expected["status"] = status
                    failure = setup.doctor(cfg, timeout=1)
                    self.assertFalse(failure[0]["ok"])
                    self.assertFalse(failure[0]["checks"]["model"])
                    self.assertNotIn("fake-key-not-real", json.dumps(failure))
                provider = cfg.providers["custom"]
                provider.key_file.unlink()
                before = len(seen)
                self.assertFalse(setup.doctor(cfg, timeout=1)[0]["checks"]["credential"])
                self.assertEqual(len(seen), before)
                self.assertFalse(cfg.db.exists())

    def test_probes_return_false_for_an_endpoint_that_does_not_speak_http(self):
        """http.client raises HTTPException, not OSError, for this; it must not escape into the monitor loop."""
        import socket
        import threading
        from types import SimpleNamespace
        from super_super_harness.adapters import claude_code
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.addCleanup(server.close)

        def answer():
            for _ in range(2):
                client, _ = server.accept()
                client.recv(65536)
                client.sendall(b"\x00garbage\r\n\r\n")
                client.close()
        threading.Thread(target=answer, daemon=True).start()
        provider = SimpleNamespace(base_url="http://127.0.0.1:%d" % server.getsockname()[1], auth_headers=lambda: {"x-api-key": "k"})
        self.assertFalse(claude_code.shallow_probe(provider, 2))
        ok, reason = claude_code.deep_check(provider, "m", 2)
        self.assertFalse(ok)
        self.assertIn("BadStatusLine", reason)

    def test_doctor_passes_a_model_that_answers_only_after_thinking_and_says_why_a_probe_failed(self):
        """Like a gateway in front of a thinking model: max_tokens is ignored and a whole reply comes back only after
        the model has thought, past doctor's 5 second default; a stream starts at once."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import time
        from super_super_harness.ops import config, setup
        from tests.support import free_port
        mode = {"status": 200, "stream_error": False}
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if mode["status"] != 200:
                    self.send_response(mode["status"])
                    self.end_headers()
                    self.wfile.write(b'{"type":"error","error":{"type":"authentication_error","message":"bad key fake-key-not-real"}}')
                    return
                if not body.get("stream"):
                    time.sleep(1.5)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"type":"message","content":[{"type":"text","text":"Hi!"}]}')
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                first = ('{"type":"error","error":{"type":"overloaded_error","message":"fake-key-not-real"}}' if mode["stream_error"]
                         else '{"type":"message_start","message":{"type":"message","content":[]}}')
                self.wfile.write(("event: %s\ndata: %s\n\n" % ("error" if mode["stream_error"] else "message_start", first)).encode())
                self.wfile.flush()
                time.sleep(1.5)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", free_port()), Endpoint)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        setup.initialize(self.home, self.args(base_url="http://127.0.0.1:%d" % server.server_port), ask_key=lambda _: "fake-key-not-real")
        cfg = config.load(self.home)
        result = setup.doctor(cfg, timeout=1)[0]
        self.assertTrue(result["checks"]["model"], result)
        self.assertTrue(result["ok"])
        self.assertNotIn("reasons", result)
        for status, stream_error, reason in ((401, False, "HTTP 401"), (200, True, "overloaded_error")):
            with self.subTest(status=status, stream_error=stream_error):
                mode.update(status=status, stream_error=stream_error)
                failure = setup.doctor(cfg, timeout=1)[0]
                self.assertFalse(failure["checks"]["model"])
                self.assertIn(reason, failure["reasons"]["model"])
                self.assertNotIn("fake-key-not-real", json.dumps(failure))
        server.shutdown()
        server.server_close()
        failure = setup.doctor(cfg, timeout=1)[0]
        self.assertFalse(failure["checks"]["endpoint"])
        self.assertIn("endpoint", failure["reasons"])
        self.assertNotIn("fake-key-not-real", json.dumps(failure))


class PrivateFiles(unittest.TestCase):
    def test_atomic_no_clobber_publication_does_not_overwrite_a_racing_destination(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from super_super_harness.core.private_files import atomic_write
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profiles.toml"
            original = os.link
            def racing_link(source, destination):
                path.write_bytes(b"created-by-another-writer")
                return original(source, destination)
            with patch("super_super_harness.core.private_files.os.link", side_effect=racing_link):
                with self.assertRaises(FileExistsError):
                    atomic_write(path, b"must-not-overwrite", replace=False)
            self.assertEqual(path.read_bytes(), b"created-by-another-writer")
            self.assertFalse(list(Path(folder).glob(".profiles.toml.*")))

    def test_atomic_private_write_interruption_preserves_existing_and_recovers_empty_tokens(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from super_super_harness.ops import config
        from super_super_harness.daemon import Service
        from super_super_harness.core.private_files import atomic_write
        from tests.support import write_config
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            cfg = config.load(home)
            paths = [cfg.owner_token_file, home / "web-session-key"]
            for path in paths:
                path.write_bytes(b"")  # left by the old non-atomic writer
            for target in paths:
                def replace_failure(source, destination):
                    if Path(destination) == target:
                        self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                        self.assertTrue(Path(source).read_bytes())
                        raise OSError("injected interruption before rename")
                    return original(source, destination)
                original = os.replace
                with patch("super_super_harness.core.private_files.os.replace", side_effect=replace_failure):
                    with self.assertRaises(OSError):
                        Service(cfg)
                self.assertEqual(target.read_bytes(), b"")
                service = Service(cfg)
                self.assertTrue(service.owner_token)
                self.assertEqual(len(service.web.session_key), 32)
                keys = [p.read_bytes() for p in paths]
                service.store.close()
                again = Service(cfg)
                self.assertEqual([p.read_bytes() for p in paths], keys)
                again.store.close()
                # Exercise the next target's legacy-empty recovery too.
                if target == paths[0]:
                    paths[1].write_bytes(b"")
            existing = home / "private"
            atomic_write(existing, b"original")
            with patch("super_super_harness.core.private_files.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    atomic_write(existing, b"replacement")
            self.assertEqual(existing.read_bytes(), b"original")
            for path in paths + [existing]:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(home.glob(".owner-token.*")))
            self.assertFalse(list(home.glob(".web-session-key.*")))

    def test_private_writes_reject_symlinks_and_do_not_replace_nonempty_corrupt_keys(self):
        from pathlib import Path
        import tempfile
        from super_super_harness.core.private_files import atomic_write, persistent_secret
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target, link = root / "target", root / "link"
            target.write_bytes(b"wrong-length")
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                atomic_write(link, b"new")
            with self.assertRaises(OSError):
                persistent_secret(link, 32, lambda: b"x" * 32)
            with self.assertRaises(ValueError):
                persistent_secret(target, 32, lambda: b"x" * 32)
            self.assertEqual(target.read_bytes(), b"wrong-length")


class Pieces(unittest.TestCase):
    def test_transcript_only_splits_newlines_not_unicode_separators(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proj").mkdir()
            text = "before middle nextafter"
            row = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            data = json.dumps(row, ensure_ascii=False) + "\n"
            (root / "proj" / "unicode.jsonl").write_text(data)
            cfg = SimpleNamespace(session_root=root)
            for bounds in ({}, {"max_bytes": len(data.encode()) + 1, "limit": 20, "text_limit": 100}):
                with self.subTest(bounds=bounds):
                    self.assertEqual(harness.transcript(cfg, "unicode", **bounds)[0]["text"], text)

    def test_transcript_is_normalized(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        root = Path(tempfile.mkdtemp(dir="/tmp"))
        (root / "proj").mkdir()
        rows = [
            {"type": "user", "timestamp": "2026-11-01T10:00:00Z", "message": {"content": "do it"}},
            {"type": "queue-operation", "operation": "enqueue", "content": "x"},
            {"type": "assistant", "timestamp": "2026-11-01T10:00:01Z", "message": {"content": [
                {"type": "thinking", "thinking": "plan"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "timestamp": "2026-11-01T10:00:02Z", "message": {"content": [
                {"type": "tool_result", "content": [{"type": "text", "text": "a.txt"}]}]}},
            {"type": "assistant", "timestamp": "2026-11-01T10:00:03Z", "message": {"content": [{"type": "text", "text": "done"}]}},
        ]
        (root / "proj" / "s1.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
        cfg = SimpleNamespace(session_root=root)
        kinds = [(item["kind"], item.get("tool")) for item in harness.transcript(cfg, "s1")]
        self.assertEqual(kinds, [("received", None), ("thinking", None), ("tool_call", "Bash"), ("tool_result", None), ("said", None)])
        self.assertTrue(harness.session_has(cfg, "s1", "do it"))
        self.assertFalse(harness.session_has(cfg, "s1", "enqueue"))

    def test_child_env_is_clean_and_blocks_push(self):
        import tempfile
        from pathlib import Path
        from super_super_harness.ops import config
        from tests.support import write_config
        home = Path(tempfile.mkdtemp(dir="/tmp"))
        write_config(home, 1, 2)
        cfg = config.load(home)
        os.environ["CLAUDE_CODE_CHILD_SESSION"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "leaked"
        try:
            env = harness.build_env(cfg, cfg.profile("spare"), "/tmp/x")
        finally:
            del os.environ["CLAUDE_CODE_CHILD_SESSION"]
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "dummy-for-tests")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "500000")
        self.assertIn("pushinsteadof", env["GIT_CONFIG_PARAMETERS"])

    def test_session_has_matches_decoded_user_text_not_json_escaping(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proj").mkdir()
            text = '<message id="m17">hello</message>'
            path = root / "proj" / "session.jsonl"
            path.write_text(json.dumps({"type": "queue-operation", "content": text}) + "\n")
            cfg = SimpleNamespace(session_root=root)
            self.assertFalse(harness.session_has(cfg, "session", 'id="m17"'))
            path.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
            self.assertTrue(harness.session_has(cfg, "session", 'id="m17"'))

    def test_store_task_ids(self):
        store = Store(":memory:")
        first = store.add_task("a", "b", "p", None, "s", "", "")
        second = store.add_task("c", "d", "p", None, "s2", "", "")
        self.assertEqual((first, second), ("t1", "t2"))
