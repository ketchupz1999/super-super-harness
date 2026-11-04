"""Watch observes a real control socket and fake Harness, never the user's service."""
import asyncio
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from super_super_harness.channels.watch import BUFFER_LIMIT
from super_super_harness.core.store import Store
from .support import ServiceCase


class WatchTests(ServiceCase):
    async def watch(self, *args, json_output=True):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "super_super_harness.cli", "watch", *args,
            *(["--json"] if json_output else []), env=dict(os.environ, SSHARNESS_HOME=str(self.home)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.close_process, process)
        return process

    async def close_process(self, process):
        if process.returncode is None:
            process.kill()
        await process.wait()

    async def snapshot(self, process):
        line = await asyncio.wait_for(process.stdout.readline(), 5)
        value = json.loads(line)
        self.assertEqual(value["event"], "snapshot")
        return value

    async def result(self, process, code):
        out, err = await asyncio.wait_for(process.communicate(), 8)
        self.assertEqual(process.returncode, code, (out.decode(), err.decode()))
        return [json.loads(line) for line in out.splitlines()], err.decode()

    async def busy(self, **kwargs):
        task = self.start_task("HANG", **kwargs)
        await self.worker_ready(task)
        await self.until(lambda: self.service.tasks.live[task].ready and not self.service.tasks.live[task].pending_uuids,
                         label="brief acknowledged")
        return task

    async def test_fake_completion_report_and_json(self):
        text = "报告" * 400
        with patch.object(self.service.tasks, "schedule"):
            task = self.start_task("DONE " + text + " ECHO fallback", code_name="Beacon")
            process = await self.watch("beacon")
            await self.snapshot(process)
        self.service.tasks.schedule()
        events, _ = await self.result(process, 0)
        report = next(row for row in events if row["type"] == "report.done")
        self.assertEqual(report["report"], text[:500])
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(events[-1]["report"], text[:500])
        self.assertEqual(events[-1]["task_id"], task)

    async def test_question_returns_two_with_options(self):
        task = await self.busy()
        process = await self.watch(task)
        await self.snapshot(process)
        self.service.tasks.ask_user(task, "Which path?", ["left", "right"])
        events, _ = await self.result(process, 2)
        question = next(row for row in events if row["type"] == "ask_user")
        self.assertEqual(question["question"], "Which path?")
        self.assertEqual(question["options"], ["left", "right"])
        self.assertEqual(events[0]["options"], ["left", "right"])

    async def test_existing_question_returns_two(self):
        task = await self.busy()
        self.service.tasks.ask_user(task, "Already waiting?", ["yes"])
        process = await self.watch(task, "--until", "change")
        snapshot = await self.snapshot(process)
        self.assertEqual(snapshot["workers"][0]["options"], ["yes"])
        await self.result(process, 2)

    async def test_change_ignores_progress_and_report_does_not_finish_done(self):
        task = await self.busy()
        change = await self.watch(task, "--until", "change")
        done = await self.watch(task)
        await self.snapshot(change)
        await self.snapshot(done)
        self.service.tasks.report_progress(task, "not a watched event")
        await asyncio.sleep(0.1)
        self.assertIsNone(change.returncode)
        self.service.tasks.report_done(task, "final report, background still running")
        events, _ = await self.result(change, 0)
        self.assertEqual([row["type"] for row in events], ["report.done"])
        await asyncio.sleep(0.1)
        self.assertIsNone(done.returncode)
        self.store.update_task(task, status="completed")
        events, _ = await self.result(done, 0)
        self.assertEqual(events[-1]["status"], "completed")

    async def test_timeout_is_overall_and_nonzero(self):
        task = await self.busy()
        process = await self.watch(task, "--timeout", "0.4")
        await self.snapshot(process)
        self.service.tasks.report_done(task, "report without completion")
        _, err = await self.result(process, 124)
        self.assertIn("timed out", err)

    async def test_shutdown_is_error(self):
        task = await self.busy()
        process = await self.watch(task)
        await self.snapshot(process)
        await self.halt()
        _, err = await self.result(process, 1)
        self.assertIn("closed", err)

    async def test_multiple_workers_wait_for_last(self):
        first, second = await self.busy(), await self.busy()
        process = await self.watch(first, second)
        await self.snapshot(process)
        self.store.update_task(first, status="failed")
        await asyncio.sleep(0.1)
        self.assertIsNone(process.returncode)
        self.store.update_task(second, status="stopped")
        events, _ = await self.result(process, 0)
        self.assertEqual([row["status"] for row in events], ["failed", "stopped"])

    async def test_item_filter_and_frozen_default_selection(self):
        item = self.service.items.create("focus", "Focus", "delivery", "act")["code"]
        first = await self.busy(item=item)
        other = await self.busy()
        process = await self.watch("--item", item)
        snapshot = await self.snapshot(process)
        self.assertEqual([row["task_id"] for row in snapshot["workers"]], [first])
        later = self.start_task("HANG", item=item)
        self.store.update_task(other, status="waiting", question="unrelated")
        self.store.update_task(first, status="dormant")
        events, _ = await self.result(process, 0)
        self.assertEqual([row["task_id"] for row in events], [first])
        self.assertNotEqual(later, first)

    async def test_explicit_selection_is_deduplicated_and_intersected(self):
        item = self.service.items.create("focus", "Focus", "delivery", "act")["code"]
        first = await self.busy(item=item, code_name="Beacon")
        other = await self.busy()
        process = await self.watch(first, "BEACON", other, "--item", item)
        snapshot = await self.snapshot(process)
        self.assertEqual(len(snapshot["workers"]), 1)
        self.store.update_task(first, status="interrupted")
        await self.result(process, 0)

    async def test_terminal_snapshot_is_not_a_change(self):
        task = await self.busy()
        self.store.update_task(task, status="completed", report="earlier")
        done = await self.watch(task)
        snapshot = await self.snapshot(done)
        self.assertEqual(snapshot["workers"][0]["report"], "earlier")
        await self.result(done, 0)
        change = await self.watch(task, "--until", "change", "--timeout", "0.3")
        await self.snapshot(change)
        await self.result(change, 124)

    async def test_empty_default_returns_but_router_only_waits(self):
        empty = await self.watch()
        self.assertEqual((await self.snapshot(empty))["workers"], [])
        await self.result(empty, 0)
        router = await self.watch("--router")
        await self.snapshot(router)
        self.service.router.ask_user("Approve?", ["go", "wait"])
        events, _ = await self.result(router, 2)
        self.assertEqual(events, [{"type": "router.ask_user", "question": "Approve?", "options": ["go", "wait"]}])

    async def test_router_question_snapshot_and_opt_in(self):
        self.service.router.ask_user("Pending?", ["yes"])
        ignored = await self.watch()
        self.assertIsNone((await self.snapshot(ignored))["router"])
        await self.result(ignored, 0)
        watched = await self.watch("--router")
        self.assertEqual((await self.snapshot(watched))["router"]["options"], ["yes"])
        await self.result(watched, 2)

    async def test_handoff_and_completion_deferred(self):
        task = await self.busy()
        for kind in ("worker.handoff", "worker.completion_deferred"):
            with self.subTest(kind=kind):
                process = await self.watch(task, "--until", "change")
                await self.snapshot(process)
                if kind == "worker.handoff":
                    await self.service.on_task_change(kind, task, "new session")
                else:
                    self.store.event("system", kind, task, severity="error")
                events, _ = await self.result(process, 0)
                self.assertEqual(events[0]["type"], kind)

    async def test_unknown_references_and_invalid_timeouts(self):
        for args in [("missing",), ("--item", "missing"), ("--timeout", "nan"), ("--timeout", "0"), ("--timeout", "inf"), ("--timeout", "1e12")]:
            with self.subTest(args=args):
                process = await self.watch(*args)
                _, err = await self.result(process, 2 if "--timeout" in args else 1)
                self.assertTrue(err)
                self.assertNotIn("Traceback", err)

    async def test_missing_service(self):
        await self.halt()
        process = await self.watch()
        _, err = await self.result(process, 1)
        self.assertTrue(err)

    async def test_owner_only_and_observation_does_not_consume_outbox(self):
        reader, writer = await asyncio.open_unix_connection(str(self.cfg.sock))
        token = self.service.issue_token(("worker", "unused"))
        writer.write((json.dumps({"token": token, "method": "watch"}) + "\n").encode())
        await writer.drain()
        self.assertIn("error", json.loads(await reader.readline()))
        writer.close()
        await writer.wait_closed()
        self.service.outbox.add("keep this message", "notice", ["terminal"])
        before = [dict(row) for row in self.store.all("SELECT * FROM outbox")]
        process = await self.watch()
        await self.snapshot(process)
        await self.result(process, 0)
        self.assertEqual(before, [dict(row) for row in self.store.all("SELECT * FROM outbox")])

    async def test_status_transaction_rollback_is_invisible(self):
        task = await self.busy()
        process = await self.watch(task, "--until", "change", "--timeout", "0.3")
        await self.snapshot(process)
        with self.assertRaises(ValueError):
            with self.store.atomic():
                self.store.update_task(task, status="completed")
                self.store.event("worker", "report.done", task, text="rolled back")
                raise ValueError("rollback")
        events, _ = await self.result(process, 124)
        self.assertEqual(events, [])

    async def test_overflow_disconnects_without_blocking_worker(self):
        task = await self.busy()
        process = await self.watch(task)
        await self.snapshot(process)
        with patch("super_super_harness.channels.watch.BUFFER_LIMIT", 100):
            self.service.tasks.report_done(task, "x" * BUFFER_LIMIT)
        await self.result(process, 1)
        self.assertEqual(self.store.task(task)["report"], "x" * BUFFER_LIMIT)
        self.assertEqual(len(self.service.watch.clients), 0)

    async def test_large_snapshot_is_rejected_before_buffering_for_nonreader(self):
        with patch.object(self.service.tasks, "schedule"):
            for _ in range(30):
                task = self.start_task("HANG")
                self.service.tasks.ask_user(task, "x" * 60000)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(3)
            sock.connect(str(self.cfg.sock))
            sock.sendall((json.dumps({"token": self.service.owner_token, "method": "watch"}) + "\n").encode())
            await self.until(lambda: bool(self.service.clients))
            await asyncio.sleep(0.1)  # Deliberately do not read from the socket.
            self.assertFalse(self.service.watch.clients)
            for writer in self.service.clients:
                self.assertLessEqual(writer.transport.get_write_buffer_size(), BUFFER_LIMIT)
            response = await asyncio.to_thread(sock.recv, 4096)
            self.assertIn("snapshot exceeds buffer limit", json.loads(response)["error"])

    async def test_plain_report_sanitizes_terminal_controls_but_json_preserves_text(self):
        report = "proof\x1b[2J\x1b]52;c;Y2hhbmdlZA==\x07"
        with patch.object(self.service.tasks, "schedule"):
            task = self.start_task("HANG")
            self.store.update_task(task, status="completed", report=report)
        process = await self.watch(task, json_output=False)
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 0, err.decode())
        self.assertIn(b"proof", out)
        self.assertNotIn(b"\x1b", out)
        self.assertNotIn(b"\x07", out)
        process = await self.watch(task)
        self.assertEqual((await self.snapshot(process))["workers"][0]["report"], report)
        await self.result(process, 0)

    async def test_subscribe_boundary_preserves_snapshot_then_change(self):
        task = await self.busy()
        reader, writer = await asyncio.open_unix_connection(str(self.cfg.sock))
        writer.write((json.dumps({"token": self.service.owner_token, "method": "watch", "params": {"workers": [task]}}) + "\n").encode())
        await writer.drain()
        await self.until(lambda: bool(self.service.watch.clients))
        self.store.update_task(task, status="completed", report="at subscription boundary")
        snapshot = json.loads(await asyncio.wait_for(reader.readline(), 2))["result"]
        changes = json.loads(await asyncio.wait_for(reader.readline(), 2))["changes"]
        self.assertEqual(snapshot["workers"][0]["status"], "running")
        self.assertEqual(changes[0]["status"], "completed")
        writer.close()
        await writer.wait_closed()
        await self.until(lambda: not self.service.watch.clients)

    async def test_quiet_router_does_not_delay_workers_and_waiting_wins_batch(self):
        first, second = await self.busy(), await self.busy()
        process = await self.watch(first, second, "--router")
        await self.snapshot(process)
        self.store.update_task(first, status="completed")
        self.service.tasks.ask_user(second, "Need input?", ["yes"])
        await self.result(process, 2)
        self.store.update_task(second, status="completed")
        process = await self.watch(first, second, "--router")
        await self.snapshot(process)
        await self.result(process, 0)

    async def test_duplicate_status_does_not_trigger_change_and_rename_keeps_id(self):
        task = await self.busy()
        process = await self.watch(task, "--until", "change")
        await self.snapshot(process)
        self.store.update_task(task, status="running", code_name="Renamed")
        await asyncio.sleep(0.1)
        self.assertIsNone(process.returncode)
        self.store.update_task(task, status="interrupted")
        events, _ = await self.result(process, 0)
        self.assertEqual(events[0]["worker"], "Renamed")
        self.assertEqual(events[0]["task_id"], task)

    async def test_human_output_is_one_line_and_flushed(self):
        task = await self.busy()
        process = await self.watch(task, json_output=False)
        initial = await asyncio.wait_for(process.stdout.readline(), 5)
        self.assertIn(self.worker_name(task), initial.decode())
        self.service.tasks.ask_user(task, "line one\nline two\x1b[2J", ["A\x1b]52;c;YQ==\x07", "B"])
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 2, err.decode())
        self.assertIn("line one line two [A / B]", out.decode())
        self.assertEqual(len(out.splitlines()), 2)


class StoreObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.seen = []
        self.store.observers.append(self.seen.append)

    def test_nested_commit_waits_for_outer_commit(self):
        with self.store.atomic():
            self.store.event("system", "first")
            with self.store.atomic():
                self.store.event("system", "second")
            self.assertEqual(self.seen, [])
        self.assertEqual([row["type"] for row in self.seen], ["first", "second"])

    def test_nested_rollback_discards_only_inner_events(self):
        with self.store.atomic():
            self.store.event("system", "first")
            with self.assertRaises(ValueError):
                with self.store.atomic():
                    self.store.event("system", "discard")
                    raise ValueError("rollback")
            self.store.event("system", "last")
        self.assertEqual([row["type"] for row in self.seen], ["first", "last"])

    def test_observer_failure_does_not_change_commit_result_or_other_observers(self):
        def broken(change):
            raise RuntimeError("observer failed")
        self.store.observers.insert(0, broken)
        with self.assertLogs("super_super_harness.core.store", level="ERROR"):
            with self.store.atomic():
                event_id = self.store.event("system", "committed")
        self.assertEqual(self.store.one("SELECT type FROM events WHERE id=?", event_id)["type"], "committed")
        self.assertEqual([row["type"] for row in self.seen], ["committed"])
        with self.assertLogs("super_super_harness.core.store", level="ERROR"):
            next_id = self.store.event("system", "outside")
        self.assertGreater(next_id, event_id)
        self.assertEqual([row["type"] for row in self.seen], ["committed", "outside"])

    def test_outer_rollback_discards_committed_inner_events(self):
        with self.assertRaises(ValueError):
            with self.store.atomic():
                with self.store.atomic():
                    self.store.event("system", "discard")
                raise ValueError("rollback")
        self.assertEqual(self.seen, [])
        self.store.event("system", "after")
        self.assertEqual([row["type"] for row in self.seen], ["after"])
