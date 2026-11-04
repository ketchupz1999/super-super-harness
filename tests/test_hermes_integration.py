"""Real service entry points against the fake ACP executable, never real Hermes."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from super_super_harness import cli
from super_super_harness.adapters import hermes, procs
from super_super_harness.daemon import Service
from super_super_harness.ops import config, setup
from tests.support import ServiceCase
from tests.test_hermes import configuration


class HermesIntegrationTests(ServiceCase):
    async def boot(self):
        cfg = configuration(self.home)
        raw = setup.read_raw(self.home / "profiles.toml")
        raw["harness"]["hermes"] = cfg.harnesses["hermes"]
        for p in raw["profiles"].values():
            p.update(harness="hermes", env={})
        raw["providers"]["one"]["base_url"] = "http://127.0.0.1:%d" % self.primary.port
        raw["providers"]["two"]["base_url"] = "http://127.0.0.1:%d" % self.backup.port
        (self.home / "profiles.toml").write_text(setup.toml_text(raw))
        self.cfg = config.load(self.home)
        self.service = Service(self.cfg)
        self.cleanups, self.cleanup_errors = {}, []
        end_process = self.service.tasks._end_process
        def tracked_end(task_id):
            finish = end_process(task_id)   # preserve synchronous run.ending/slot changes
            done = asyncio.Event()
            self.cleanups.setdefault(task_id, []).append(done)
            async def tracked():
                try:
                    await finish
                except BaseException as exc:
                    self.cleanup_errors.append(type(exc).__name__)
                    raise
                finally:
                    done.set()
            return tracked()
        self.service.tasks._end_process = tracked_end
        self.running = asyncio.create_task(self.service.run())
        await self.until(lambda: self.cfg.sock.exists() and self.service.router.ready)

    async def halt(self):
        await super().halt()
        await self.until(lambda: all(done.is_set() for group in self.cleanups.values() for done in group),
                         label="Hermes cleanup after service shutdown")
        self.assertEqual(self.cleanup_errors, [])

    def diagnostics(self, task_id):
        return json.dumps({"events": [dict(row) for row in self.store.all(
            "SELECT type,data FROM events WHERE task_id=? AND type IN ('report.done','report.unconfirmed','worker.retry','task.failed') ORDER BY id", task_id)],
            "runs": [dict(row) for row in self.store.all("SELECT pid,ended,exit_code FROM worker_runs WHERE task_id=? ORDER BY id", task_id)]}, ensure_ascii=False)

    async def finished(self, task_id, status="completed"):
        try:
            await self.until(lambda: self.status(task_id) == status and task_id not in self.service.tasks.live,
                             label="Hermes Worker " + status)
            # Removal from live precedes the old workspace sweep. Do not launch a
            # new process in that workspace until the original cleanup is done.
            await self.until(lambda: all(done.is_set() for done in self.cleanups.get(task_id, [])),
                             label="Hermes Worker cleanup")
        except AssertionError:
            self.fail("Hermes Worker did not settle: " + self.diagnostics(task_id))
        self.assertEqual(self.cleanup_errors, [])

    async def test_router_worker_interrupt_resume_cli_web_and_probe_paths(self):
        self.say("hello")
        await self.until(lambda: "fake answer" in self.outbox())
        self.assertTrue(all(s == "done" for s in self.statuses()))
        task_id = self.start_task("REPORT")
        await self.finished(task_id)
        self.assertEqual(self.store.task(task_id)["report"], "fixture report")
        self.assertEqual(self.store.one("SELECT result FROM assignments WHERE task_id=?", task_id)["result"], "fixture report")
        self.assertIsNotNone(self.store.one("SELECT id FROM events WHERE task_id=? AND type='report.done'", task_id))
        self.assertIsNone(self.store.one("SELECT id FROM events WHERE task_id=? AND type='report.unconfirmed'", task_id))
        waiting = self.start_task("WAIT", "interrupt fixture")
        sid = self.store.task(waiting)["session_id"]
        await self.until(lambda: hermes.session_has(self.cfg, sid, "WAIT"))
        self.assertTrue(self.service.tasks.interrupt(waiting)["ok"])
        await self.finished(waiting, "interrupted")
        self.cfg.profiles["work"] = replace(self.cfg.profile("work"), model="changed-model")
        self.service.tasks.resume(waiting, "REPORT2")
        await self.finished(waiting)
        self.assertEqual(self.store.task(waiting)["session_id"], sid)
        self.assertEqual(self.store.task(waiting)["report"], "fixture report two", self.diagnostics(waiting))
        wire = self.home / "hermes" / "homes" / sid / "wire.jsonl"
        frames = [json.loads(line) for line in wire.read_text().splitlines()]
        self.assertTrue(any(f.get("method") == "session/load" for f in frames))
        self.assertEqual([f["params"]["modelId"] for f in frames if f.get("method") == "session/set_model"][-1],
                         "custom:ssh:changed-model")
        self.assertTrue(await self.service.router._probe("main", deep=True))
        # Saved adapter identity survives a removed/reconfigured Profile.
        profile = self.cfg.profiles.pop("work")
        detail = await self.service.web.detail(task_id, {})
        self.assertEqual(detail["source"], "transcript")
        self.assertIn("REPORT", detail["timeline"][0]["text"])
        self.assertIn("REPORT", self.service.task_detail(task_id, None, 20)[0]["text"])
        self.cfg.profiles["work"] = profile
        developer = await self.service.web.developer()
        self.assertTrue(all(p["healthy"] for p in developer["providers"]))
        task = self.store.task(task_id)
        with patch("super_super_harness.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(cli.exec_interactive(self.cfg, "work", task["session_id"], task["cwd"], None, None), 0)
            self.assertIn("--resume", run.call_args.args[0])
            self.assertIn("HERMES_HOME=", run.call_args.args[0][1])
        with redirect_stdout(io.StringIO()) as output:
            cli.cmd_log(self.cfg, SimpleNamespace(task=task_id, full=True, kinds=None, raw=True))
        self.assertIn("REPORT", output.getvalue())
        with self.assertRaisesRegex(ValueError, "fork/peek"):
            cli.exec_interactive(self.cfg, "work", task["session_id"], task["cwd"], None, None, fork=True)

    async def test_immediate_resume_waits_for_old_workspace_sweep(self):
        tasks = self.service.tasks
        self.cfg.limits["worker_retries"] = 0
        task_id = self.start_task("WAIT")
        sid = self.store.task(task_id)["session_id"]
        await self.until(lambda: hermes.session_has(self.cfg, sid, "WAIT"))
        old = tasks.live[task_id]
        root = self.store.task(task_id)["workspace"]
        entered, release, swept = threading.Event(), threading.Event(), threading.Event()
        sweep = procs.sweep
        killed = []
        def paused_sweep(path):
            if path == root and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("fixture sweep barrier timed out")
                try:
                    killed.extend(sweep(path))
                    return killed
                finally:
                    swept.set()
            return sweep(path)
        with patch.object(procs, "sweep", side_effect=paused_sweep):
            try:
                self.assertTrue(tasks.interrupt(task_id)["ok"])
                # Deliberately do not use finished(): it hides this production race.
                await self.until(lambda: self.status(task_id) == "interrupted" and task_id not in tasks.live)
                await self.until(entered.is_set, label="old sweep entered")
                self.assertTrue(tasks.resume(task_id, "WAIT resumed-unique")["ok"])
                tasks.schedule()
                self.assertNotIn(task_id, tasks.live, "resume must wait for the old workspace sweep")
                self.assertEqual(self.status(task_id), "queued")
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM worker_runs WHERE task_id=?", task_id)["n"], 1)
            finally:
                release.set()
                await self.until(swept.is_set, label="old sweep finished")
            await self.until(lambda: hermes.session_has(self.cfg, sid, "WAIT resumed-unique"))
            new = tasks.live[task_id]
            self.assertIsNot(new, old)
            self.assertNotIn(new.process.proc.pid, killed)
            self.assertIsNone(new.process.proc.returncode)
            await tasks.stop(task_id)
            await self.finished(task_id, "stopped")
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE task_id=? AND type='worker.retry'", task_id))

    async def test_finished_waits_for_the_original_workspace_sweep(self):
        entered, release = threading.Event(), threading.Event()
        sweep = procs.sweep
        def paused_sweep(path, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture sweep barrier timed out")
            return sweep(path, *args, **kwargs)
        with patch.object(procs, "sweep", side_effect=paused_sweep):
            task_id = self.start_task("REPORT")
            try:
                await self.until(entered.is_set)
                waiter = asyncio.create_task(self.finished(task_id))
                await asyncio.sleep(.05)
                self.assertFalse(waiter.done(), "live removal must not stand in for complete cleanup")
                release.set()
                await waiter
            finally:
                release.set()
        self.service.tasks.send(task_id, "REPORT2")
        await self.finished(task_id)
        self.assertEqual(self.store.task(task_id)["report"], "fixture report two", self.diagnostics(task_id))
        self.assertIsNone(self.store.one("SELECT id FROM events WHERE task_id=? AND type='worker.retry'", task_id))

    async def test_parent_report_tolerates_delayed_stream_attribution(self):
        on_event = self.service.tasks._on_event
        async def delayed_event(task_id, run, message):
            blocks = (message.get("message") or {}).get("content", [])
            if message.get("type") == "assistant" and any(
                    b.get("type") == "tool_use" and b.get("name") == "mcp__router__report_done" for b in blocks):
                await asyncio.sleep(.6)
            await on_event(task_id, run, message)
        with patch.object(self.service.tasks, "_on_event", side_effect=delayed_event):
            task_id = self.start_task("REPORT2")
            await self.finished(task_id)
        self.assertEqual(self.store.task(task_id)["report"], "fixture report two", self.diagnostics(task_id))
        self.assertIsNone(self.store.one("SELECT id FROM events WHERE task_id=? AND type='report.unconfirmed'", task_id))

    async def test_late_receipts_keep_reports_bound_to_their_own_assignments(self):
        task_id = self.start_task("REPORT PAUSE")
        await self.until(lambda: self.store.task(task_id)["report"] == "fixture report")
        run = self.service.tasks.live[task_id]
        self.assertTrue(run.pending_uuids, "report_done precedes durable ACK in ACP")
        self.service.tasks.send(task_id, "REPORT2")
        sid = self.store.task(task_id)["session_id"]
        (self.home / "hermes" / "homes" / sid / "continue").touch()
        await self.finished(task_id)
        rows = self.store.all("SELECT status,result FROM assignments WHERE task_id=? ORDER BY id", task_id)
        self.assertEqual([(r["status"], r["result"]) for r in rows],
                         [("done", "fixture report"), ("done", "fixture report two")])

    async def test_assignment_failure_before_receipt_retries_original_text(self):
        task_id = self.start_task("REPORT")
        await self.finished(task_id)
        sid = self.store.task(task_id)["session_id"]
        self.service.tasks.send(task_id, "FAIL_BEFORE_ONCE unique-retry-body")
        await self.finished(task_id)
        self.assertTrue(hermes.session_has(self.cfg, sid, "unique-retry-body"))
        self.assertEqual(self.store.all("SELECT status FROM assignments WHERE task_id=? ORDER BY id", task_id)[-1]["status"], "done")
        self.assertIsNone(self.store.one("SELECT key FROM kv WHERE key LIKE 'assignment_unconfirmed:%'"))

    async def test_cold_recovery_replays_unpersisted_assignment_but_not_persisted_one(self):
        for persisted in (False, True):
            task_id = self.start_task("REPORT")
            await self.finished(task_id)
            sid = self.store.task(task_id)["session_id"]
            home = self.home / "hermes" / "homes" / sid
            body = "PAUSE unique-cold-%s" % persisted
            if not persisted:
                body += " NO_STORE_ONCE"
            self.service.tasks.send(task_id, body)
            await self.until(lambda: self.store.one("SELECT key FROM kv WHERE key LIKE 'assignment_unconfirmed:%'"))
            await self.until(lambda: hermes.session_has(self.cfg, sid, "unique-cold-True") if persisted else (home / "not-stored-once").exists())
            await self.halt()
            (home / "continue").touch()
            await self.boot()
            await self.finished(task_id)
            with sqlite3.connect(home / "state.db") as db:
                count = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user' AND content LIKE ?",
                                   (hermes._hermes_id(self.cfg, sid), "%%unique-cold-%s%%" % persisted)).fetchone()[0]
            self.assertEqual(count, 1, "original work must land once, whether or not its ACK survived")

    async def test_router_cancel_before_admission_requeues_only_the_started_input(self):
        first = self.say("CANCEL_ONCE")
        second = self.say("another queued message")
        await self.until(lambda: all(self.store.one("SELECT status FROM messages WHERE id=?", i)["status"] == "done"
                                    for i in (first, second)))
        sid = self.service.router.session_id
        self.assertTrue(hermes.session_has(self.cfg, sid, "CANCEL_ONCE"))
        with sqlite3.connect(hermes.session_file(self.cfg, sid)) as db:
            count = db.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND content LIKE '%another queued message%'").fetchone()[0]
        self.assertEqual(count, 1)
        self.say("ASK_TOOL")
        await self.until(lambda: self.store.get("router_waiting") == "fixture question?")

    async def test_context_handoff_before_late_ack_preserves_budget_and_owns_delivery(self):
        for reported in (False, True):
            with self.subTest(reported=reported):
                brief = ("REPORT " if reported else "") + "PAUSE HERMES_HANDOFF_PAUSE"
                task_id = self.start_task(brief)
                await self.until(lambda: task_id in self.service.tasks.live and self.service.tasks.live[task_id].current_assignment)
                old = self.service.tasks.live[task_id]
                old_session = self.store.task(task_id)["session_id"]
                assignment = old.current_assignment
                if reported:
                    await self.until(lambda: self.store.task(task_id)["report"] == "fixture report")
                self.assertTrue(old.pending_uuids)
                old_uuid = next(iter(old.pending_uuids))
                self.assertEqual(self.store.get("turn_assignment:" + task_id), str(assignment))
                if reported:
                    for _ in range(self.cfg.limits["compact_thrash"] + 1):
                        await self.service.tasks._on_event(task_id, old, {"type": "system", "subtype": "compact_boundary"})
                else:
                    await self.service.tasks._on_event(task_id, old, {"type": "result", "is_error": True, "result": "prompt is too long"})
                self.assertTrue(old.ending)
                await self.service.tasks._on_event(task_id, old, {"type": "user", "isReplay": True, "uuid": old_uuid})
                self.assertIn(old_uuid, old.pending_uuids, "old reader must stay frozen")
                await self.until(lambda: self.store.task(task_id)["session_id"] != old_session and
                                 task_id in self.service.tasks.live and self.service.tasks.live[task_id].handoff_uuid)
                new = self.service.tasks.live[task_id]
                session = self.store.task(task_id)["session_id"]
                await self.until(lambda: hermes.session_has(self.cfg, session, "[Session handoff]"))
                handoff = json.loads(self.store.get("handoff_pending:" + task_id))
                self.assertEqual(handoff["assignment_ids"], [assignment])
                self.assertEqual(self.store.get("assignment_handoffs:%s" % assignment), "1")
                self.assertIn(brief, handoff["message"])
                if reported:
                    self.assertIn("fixture report", handoff["message"])
                else:
                    self.assertEqual(self.store.one("SELECT status FROM assignments WHERE id=?", assignment)["status"], "doing")
                self.assertIsNone(self.store.get("assignment_unconfirmed:%s" % assignment))
                await self.service.tasks._on_event(task_id, old, {"type": "user", "isReplay": True, "uuid": old_uuid})
                self.assertIn(new.handoff_uuid, new.pending_uuids)
                self.assertIsNotNone(self.store.get("handoff_pending:" + task_id))
                (self.home / "hermes" / "homes" / session / "continue").touch()
                await self.finished(task_id)
                self.assertIsNone(self.store.get("handoff_pending:" + task_id))
                self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"], 1)
                with sqlite3.connect(hermes.session_file(self.cfg, session)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0], 1)

    async def test_unattributed_child_report_is_progress_not_final_report(self):
        with patch.object(self.service.tasks, "caller_wait", .1):
            task_id = self.start_task("CHILD_REPORT")
            await self.finished(task_id)
        self.assertEqual(self.store.task(task_id)["report"], "fake answer")
        self.assertIsNotNone(self.store.one("SELECT id FROM events WHERE task_id=? AND type='report.unconfirmed'", task_id))
        self.assertIsNone(self.store.one("SELECT id FROM events WHERE task_id=? AND type='report.done'", task_id))

    async def test_router_question_before_receipt_stays_waiting(self):
        self.say("ASK_TOOL")
        await self.until(lambda: self.store.get("router_waiting") == "fixture question?")
        await self.until(lambda: self.statuses() == ["done"])
        self.assertEqual(self.store.get("router_waiting"), "fixture question?")
        self.assertIn("fixture question?", self.outbox())

    async def test_busy_guard_ignores_started_input_but_interrupts_for_new_input(self):
        first = self.say("WAIT BUSY_TOOL")
        await self.until(lambda: bool(self.service.router.inflight))
        await asyncio.sleep(1.2)
        self.assertEqual(self.store.one("SELECT status FROM messages WHERE id=?", first)["status"], "written")
        self.assertIsNone(self.store.one("SELECT id FROM events WHERE type='router.interrupted'"))
        second = self.say("followup")
        await self.until(lambda: self.store.one("SELECT status FROM messages WHERE id=?", second)["status"] == "done")
        self.assertIsNotNone(self.store.one("SELECT id FROM events WHERE type='router.interrupted'"))

    async def test_recovery_uses_durable_user_rows_including_compacted_history(self):
        message_id = self.store.add_message("unconfirmed", "user", "already seen", "terminal")
        row = self.store.one("SELECT * FROM messages WHERE id=?", message_id)
        process = self.service.router.process
        with sqlite3.connect(Path(process.spec["home"]) / "state.db") as db:
            db.execute("INSERT INTO messages(session_id,role,content,active,compacted) VALUES(?,?,?,?,?)",
                       (process.spec["hermes_id"], "user", self.service.router._format(row), 0, 1))
        self.store.run("UPDATE messages SET status='written' WHERE id=?", message_id)
        await self.service.router._recover_inbox()
        self.assertEqual(self.store.one("SELECT status FROM messages WHERE id=?", message_id)["status"], "confirmed")
        self.store.run("UPDATE messages SET status='done' WHERE id=?", message_id)

    async def test_cold_worker_resume_refreshes_mcp_transport_without_changing_declaration(self):
        task_id = self.start_task("REPORT")
        await self.finished(task_id)
        sid = self.store.task(task_id)["session_id"]
        home = self.home / "hermes" / "homes" / sid
        before = json.loads((home / "config.yaml").read_text())["mcp_servers"]
        old_transport = (home / "mcp.json").read_text()
        await self.halt()
        await self.boot()
        self.assertTrue(self.service.tasks.send(task_id, "REPORT2")["ok"])
        await self.finished(task_id)
        self.assertEqual(self.store.task(task_id)["report"], "fixture report two")
        self.assertEqual(json.loads((home / "config.yaml").read_text())["mcp_servers"], before)
        self.assertNotEqual((home / "mcp.json").read_text(), old_transport)
