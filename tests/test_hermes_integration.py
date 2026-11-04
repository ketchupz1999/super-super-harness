"""Real service entry points against the fake ACP executable, never real Hermes."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from super_super_harness import cli
from super_super_harness.adapters import hermes
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
        await super().boot()
        await self.until(lambda: self.service.router.ready)
        self.service.tasks.caller_wait = .1

    async def finished(self, task_id, status="completed"):
        await self.until(lambda: self.status(task_id) == status and task_id not in self.service.tasks.live,
                         label="Hermes Worker " + status)

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
        self.assertEqual(self.store.task(waiting)["report"], "fixture report two")
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
