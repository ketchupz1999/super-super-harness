"""Fake-clock scheduling, transactional recovery and authenticated web integration."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from super_super_harness import cli, tools
from super_super_harness.outbox import Outbox
from super_super_harness.reminders import Cron, Reminders, local_stamp, parse_at, system_notify
from super_super_harness.store import Store
from tests import test_web
from tests.support import ServiceCase, free_port


class TimezoneFileTests(unittest.TestCase):
    def probe(self, mode):
        import json
        try:
            result = subprocess.run([sys.executable, '-m', 'tests.tzif_probe', mode],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired as error:
            output = error.stdout or b''
            if isinstance(output, bytes):
                output = output.decode(errors='replace')
            self.fail('TZif %s child exceeded 5s; tail of progress:\n%s' % (mode, output[-1800:]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['cuts_checked'], summary['source_bytes'])
        self.assertEqual(summary['last_cut'], summary['source_bytes'] - 1)
        cuts = [json.loads(line)['cut'] for line in result.stdout.splitlines()[:-1]
                if '"cut"' in line]
        self.assertEqual(cuts, list(range(summary['source_bytes'])))

    def test_absolute_tzif_every_truncated_prefix_in_timeout_child(self):
        self.probe('absolute')

    def test_relative_tzif_every_truncated_prefix_in_timeout_child(self):
        self.probe('relative')

    def test_timezone_parser_assertions_fall_back_to_libc(self):
        from super_super_harness.reminders import LibcZone, local_zone
        import time
        try:
            for name in (':/unreadable-test-zone', 'Unlisted/Zone'):
                with self.subTest(name=name), patch.dict(os.environ, TZ=name), \
                        patch('zoneinfo.TZPATH', ()), \
                        patch('super_super_harness.reminders.open', return_value=io.BytesIO(b'TZif2\n')), \
                        patch('super_super_harness.reminders.ZoneInfo') as parser:
                    parser.side_effect = AssertionError('invalid TZif header')
                    parser.from_file.side_effect = AssertionError('invalid TZif footer')
                    self.assertIsInstance(local_zone(), LibcZone)
        finally:
            time.tzset()

    def test_relative_name_missing_from_tzpath_keeps_zoneinfo_fallback(self):
        from super_super_harness.reminders import local_zone
        marker = object()
        with patch.dict(os.environ, TZ='Missing/Zone'), patch('zoneinfo.TZPATH', ()), \
                patch('super_super_harness.reminders.ZoneInfo', return_value=marker) as parser:
            self.assertIs(local_zone(), marker)
            parser.assert_called_once_with('Missing/Zone')


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "router.db"
        self.zone = patch.dict(os.environ, TZ="Asia/Shanghai")
        self.zone.start()
        self.now = parse_at("2026-01-01T08:00:00")
        self.store = Store(self.path)
        self.router = SimpleNamespace(pump=AsyncMock(), _wake_waiters=Mock())
        self.notifications = []
        self.boot()

    def boot(self):
        self.outbox = Outbox(self.store)
        self.reminders = Reminders(self.store, self.router, self.outbox, clock=lambda: self.now,
                                   notifier=lambda *args: self.notifications.append(args))

    async def asyncTearDown(self):
        await self.reminders.close()
        await self.outbox.flush()
        self.outbox.closed = True
        await asyncio.sleep(0)
        self.store.close()
        self.folder.cleanup()
        self.zone.stop()

    def add(self, kind="notify", **kwargs):
        return self.reminders.add("Review notes", "Review the project notes", kind,
                                  **(kwargs or {"at": local_stamp(self.now + 60)}))

    def count(self, table):
        return self.store.one("SELECT COUNT(*) n FROM " + table)["n"]

    async def test_one_time_notify_no_model_and_durable_delivery(self):
        item = self.add()
        self.now += 59
        await self.reminders.tick()
        self.assertEqual(self.count("outbox"), 0)
        self.now += 1
        await self.reminders.tick()
        await self.reminders.close()
        await self.outbox.flush()
        self.assertEqual(self.count("reminder_fires"), 1)
        self.assertEqual(self.count("outbox"), 2)
        self.assertEqual(self.count("conversation"), 1)
        self.assertEqual(self.store.one("SELECT kind FROM conversation")["kind"], "reminder")
        self.assertEqual(self.store.one("SELECT status FROM outbox WHERE channel='terminal'")["status"], "pending")
        self.assertEqual(len(self.notifications), 1)
        self.assertFalse(self.router.pump.called)
        self.assertEqual(self.reminders.list()["items"], [])
        await self.reminders.tick()
        self.assertEqual(self.count("reminder_fires"), 1)
        self.assertTrue(self.reminders.cancel(item["id"])["already_fired"])

    async def test_missed_once_and_cron_only_latest_without_burst(self):
        self.add()
        self.add(cron="*/5 * * * *")
        self.now += 3 * 3600 + 61
        await self.reminders.tick()
        rows = self.store.all("SELECT * FROM reminder_fires ORDER BY reminder_id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(local_stamp(rows[0]["scheduled"]), "2026-01-01T08:01:00+08:00")
        self.assertEqual(local_stamp(rows[1]["scheduled"]), "2026-01-01T11:00:00+08:00")
        texts = [r["text"] for r in self.store.all("SELECT text FROM conversation")]
        self.assertTrue(any("scheduled 2026-01-01T08:01:00+08:00" in t for t in texts))
        self.assertEqual(self.reminders.list()["items"][0]["next_time"], "2026-01-01T11:05:00+08:00")
        await self.reminders.tick()
        self.assertEqual(self.count("reminder_fires"), 2)

    async def test_restart_and_clock_rollback_do_not_repeat(self):
        self.add("run", cron="* * * * *")
        self.now += 61
        await self.reminders.tick()
        await self.outbox.flush()
        self.store.close()
        self.store = Store(self.path)
        self.boot()
        await self.reminders.tick()
        self.now -= 60
        await self.reminders.tick()
        self.now += 60
        await self.reminders.tick()
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("reminder_fires"), 1)
        row = self.store.one("SELECT * FROM messages")
        self.assertEqual(row["kind"], "reminder.run")
        self.assertIn("Reminder r1", row["text"])

    async def test_crash_before_commit_rolls_back_claim_and_enqueue(self):
        self.add("run")
        self.now += 61
        add = self.store.add_message
        def crash(*args, **kwargs):
            add(*args, **kwargs)
            raise RuntimeError("simulated crash")
        with patch.object(self.store, "add_message", side_effect=crash):
            with self.assertRaises(RuntimeError):
                await self.reminders.tick()
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("reminder_fires"), 0)
        self.assertEqual(len(self.reminders.list()["items"]), 1)
        await self.reminders.tick()
        await self.reminders.tick()
        self.assertEqual(self.count("messages"), 1)

    async def test_cancel_race_before_and_after_claim(self):
        early = self.add()
        self.now += 60
        self.assertTrue(self.reminders.cancel(early["id"])["ok"])
        await self.reminders.tick()
        self.assertEqual(self.count("reminder_fires"), 0)
        late = self.add("run")
        self.now += 60
        await self.reminders.tick()
        self.assertTrue(self.reminders.cancel(late["id"])["already_fired"])
        await self.reminders.tick()
        self.assertEqual(self.count("messages"), 1)
        self.assertFalse(self.reminders.cancel("r999")["ok"])

    async def test_cron_ranges_lists_steps_sunday_and_day_or(self):
        cron = Cron("15,45 9-10/1 * * 1-5")
        self.assertEqual(local_stamp(cron.occurrence(self.now)), "2026-01-01T09:15:00+08:00")
        self.assertEqual(local_stamp(Cron("0 9 * * 7").occurrence(self.now)), "2026-01-04T09:00:00+08:00")
        self.assertEqual(local_stamp(Cron("0 9 2 * 1").occurrence(self.now)), "2026-01-02T09:00:00+08:00")
        self.assertEqual(local_stamp(Cron("0 9 29 2 *").occurrence(self.now)), "2028-02-29T09:00:00+08:00")

    async def test_timezone_dst_gaps_folds_and_context(self):
        self.assertEqual(self.reminders.list()["timezone"], "Asia/Shanghai")
        self.assertTrue(self.reminders.list()["now"].endswith("+08:00"))
        with patch.dict(os.environ, TZ="America/New_York"):
            with self.assertRaises(ValueError):
                parse_at("2026-03-08T02:30:00")
            with self.assertRaises(ValueError):
                parse_at("2026-11-01T01:30:00")
            spring = parse_at("2026-03-08T01:59:00-05:00")
            self.assertEqual(local_stamp(Cron("30 2 * * *").occurrence(spring)), "2026-03-08T03:00:00-04:00")
            first = parse_at("2026-11-01T01:30:00-04:00")
            following = Cron("30 1 * * *").occurrence(first)
            self.assertEqual(local_stamp(following), "2026-11-02T01:30:00-05:00")

    async def test_timezone_file_errors_fall_back_to_libc(self):
        from super_super_harness.reminders import LibcZone, local_zone, time_context
        import time
        try:
            for failure in (OSError("unreadable zone"), ValueError("invalid TZif")):
                with self.subTest(failure=type(failure).__name__), patch.dict(os.environ, TZ=":/bad-zone"), \
                        patch("super_super_harness.reminders.open", side_effect=failure):
                    self.assertIsInstance(local_zone(), LibcZone)
                    self.assertIn("now", time_context(self.now))
            with patch.dict(os.environ, {}, clear=True), patch("super_super_harness.reminders.open", side_effect=FileNotFoundError):
                self.assertIsInstance(local_zone(), LibcZone)
        finally:
            time.tzset()

    async def test_truncated_tzif_file_falls_back_to_libc(self):
        # Keep the original five-byte prefix check, now inside the exhaustive child.
        result = subprocess.run([sys.executable, '-m', 'tests.tzif_probe', 'absolute'],
                                cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"cut": 5}', result.stdout)

    async def test_timezone_name_from_localtime_link(self):
        from super_super_harness.reminders import time_context
        with patch.dict(os.environ, {}, clear=True), \
                patch("super_super_harness.reminders.os.path.realpath", return_value="/var/db/timezone/zoneinfo/Asia/Shanghai"):
            self.assertEqual(time_context(self.now)["timezone"], "Asia/Shanghai")

    async def test_cron_dst_first_fold_and_gap_boundary(self):
        with patch.dict(os.environ, TZ="America/New_York"):
            cron = Cron("30 1 * * *")
            first = parse_at("2026-11-01T01:30:00-04:00")
            second = parse_at("2026-11-01T01:30:00-05:00")
            self.assertEqual(cron.occurrence(first - 1), first)
            self.assertEqual(local_stamp(cron.occurrence(first)), "2026-11-02T01:30:00-05:00")
            self.assertEqual(cron.occurrence(second, forward=False), first)
            spring = parse_at("2026-03-08T01:59:00-05:00")
            boundary = parse_at("2026-03-08T03:00:00-04:00")
            self.assertEqual(Cron("30 2 * * *").occurrence(spring), boundary)
            self.assertEqual(Cron("30 2 * * *").occurrence(boundary, forward=False), boundary)
            self.assertEqual(Cron("30 2 * * *").occurrence(boundary, forward=False, minimum=boundary+1), None)

    async def test_cron_dst_gap_collapses_and_never_repeats(self):
        with patch.dict(os.environ, TZ="America/New_York"):
            self.now = parse_at("2026-03-08T01:59:00-05:00")
            self.add("run", cron="* 2 * * *")
            self.now += 60
            await self.reminders.tick()
            await self.reminders.tick()
            self.assertEqual(self.count("messages"), 1)
            self.assertEqual(self.count("reminder_fires"), 1)
            self.assertEqual(self.store.one("SELECT scheduled FROM reminder_fires")["scheduled"], self.now)
            self.assertEqual(self.reminders.list()["items"][0]["next_time"], "2026-03-09T02:00:00-04:00")
        with patch.dict(os.environ, TZ="Australia/Lord_Howe"):
            before = parse_at("2026-10-04T01:59:00+10:30")
            self.assertEqual(local_stamp(Cron("15 2 * * *").occurrence(before)), "2026-10-04T02:30:00+11:00")

    async def test_run_message_uuid_is_standard_and_deterministic(self):
        import uuid
        self.add("run")
        self.now += 60
        await self.reminders.tick()
        row = self.store.one("SELECT * FROM messages")
        self.assertEqual(str(uuid.UUID(row["uuid"])), row["uuid"])
        due = self.store.one("SELECT scheduled FROM reminder_fires")["scheduled"]
        self.assertEqual(row["uuid"], str(uuid.uuid5(uuid.NAMESPACE_URL, "reminder:1:%.6f" % due)))
        await self.reminders.tick()
        self.assertEqual(self.count("messages"), 1)

    async def test_invalid_cron_and_inputs_do_not_create_reminders(self):
        for expression in ["", "* * * *", "* * * * * *", "60 * * * *", "0 24 * * *", "0 0 0 * *",
                           "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "5-2 * * * *", "x * * * *",
                           "0 0 31 2 *", "0 0 * * MON", "* * * * */99"]:
            with self.subTest(cron=expression), self.assertRaises(ValueError):
                self.add(cron=expression)
        for args in [dict(at="bad"), dict(at=local_stamp(self.now)), dict(at="2026-01-02"), dict(at=True),
                     dict(at=local_stamp(self.now+60), cron="* * * * *")]:
            with self.assertRaises(ValueError):
                self.add(**args)
        with self.assertRaises(ValueError):
            self.reminders.add("", "body", "notify", at=local_stamp(self.now+60))
        self.assertEqual(self.count("reminders"), 0)

    async def test_old_database_migration_preserves_all_existing_rows(self):
        old = Path(self.folder.name) / "old.db"
        # Model an existing web-release DB with no scheduler tables. This must
        # also run from a release archive without .git or the baseline commit.
        legacy = Store(old)
        legacy.run("DROP TABLE reminder_fires")
        legacy.run("DROP TABLE reminders")
        legacy.add_message("legacy", "user", "keep conversation", "terminal")
        legacy.add_task("keep task", "brief", "work", None, "session", "", "terminal")
        legacy.event("system", "keep.event")
        legacy.run("INSERT INTO outbox(channel,kind,text,created) VALUES('terminal','answer','keep reply',1)")
        tables = [r["name"] for r in legacy.all("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        before = {t: [tuple(r) for r in legacy.all("SELECT * FROM " + t)] for t in tables}
        legacy.close()
        upgraded = Store(old)
        for table, rows in before.items():
            self.assertEqual([tuple(r) for r in upgraded.all("SELECT * FROM " + table)], rows, table)
        self.assertEqual(upgraded.one("SELECT COUNT(*) n FROM reminders")["n"], 0)
        upgraded.close()
        Store(old).close()  # Migration is idempotent.

    async def test_posix_timezone_strings_and_timezone_file(self):
        import time
        try:
            for zone, offset in (("UTC0", "+00:00"), ("CST-8", "+08:00")):
                with patch.dict(os.environ, TZ=zone):
                    self.assertTrue(self.reminders.list()["now"].endswith(offset))
                    instant = parse_at("2026-01-02T09:00:00")
                    self.assertEqual(local_stamp(instant), "2026-01-02T09:00:00" + offset)
            with patch.dict(os.environ, TZ=":/etc/localtime"):
                self.assertIn("now", self.reminders.list())
            with patch.dict(os.environ, TZ="EST5EDT,M3.2.0,M11.1.0"):
                before = parse_at("2026-03-08T01:59:00-05:00")
                self.assertEqual(local_stamp(Cron("30 2 * * *").occurrence(before)), "2026-03-08T03:00:00-04:00")
        finally:
            time.tzset()

    async def test_timezone_change_preserves_saved_due_instant(self):
        self.add("run", cron="0 9 * * *")
        saved = self.store.one("SELECT next_at FROM reminders")["next_at"]
        with patch.dict(os.environ, TZ="UTC"):
            self.now = saved + 1
            await self.reminders.tick()
            self.assertEqual(self.store.one("SELECT scheduled FROM reminder_fires")["scheduled"], saved)
            self.assertEqual(self.reminders.list()["items"][0]["next_time"], "2026-01-01T09:00:00+00:00")
            await self.reminders.tick()
            self.assertEqual(self.count("messages"), 1)

    async def test_large_due_batch_yields_to_other_event_loop_work(self):
        for _ in range(100):
            self.add("run", cron="* * * * *")
        self.now += 61
        observed = []
        async def observer():
            await asyncio.sleep(0)
            observed.append(self.count("reminder_fires"))
        other = asyncio.create_task(observer())
        await self.reminders.tick()
        await other
        self.assertLess(observed[0], 100)
        self.assertEqual(self.count("messages"), 100)
        self.assertEqual(self.count("reminder_fires"), 100)

    async def test_one_invalid_stored_schedule_does_not_block_other_reminders(self):
        self.add("run", cron="* * * * *")
        self.add("run")
        self.store.run("UPDATE reminders SET cron='bad' WHERE id=1")
        self.now += 61
        with self.assertLogs("super_super_harness.reminders", level="WARNING"):
            await self.reminders.tick()
        self.assertEqual(self.count("messages"), 1)

    async def test_cli_read_only_and_terminal_style(self):
        self.add()
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_reminders(SimpleNamespace(db=self.path), None)
        self.assertIn("Review notes", output.getvalue())
        self.assertIn("notify", output.getvalue())
        from super_super_harness.chat import STYLES
        self.assertNotEqual(STYLES["reminder"], STYLES["answer"])

    def test_system_notification_uses_only_argv_and_failure_is_safe(self):
        title, body = '" & do shell script "bad"', 'line 1\nline 2'
        with patch("super_super_harness.reminders.sys.platform", "darwin"), patch("super_super_harness.reminders.subprocess.run") as run:
            system_notify(title, body)
            argv = run.call_args.args[0]
            self.assertNotIn(title, argv[2])
            self.assertEqual(argv[-3:], ["--", title, body])
            run.side_effect = subprocess.CalledProcessError(1, "osascript")
            with self.assertLogs("super_super_harness.reminders", level="WARNING") as logs:
                system_notify(title, body)
            self.assertNotIn(title, str(logs.output))
        with patch("super_super_harness.reminders.sys.platform", "linux"), patch("super_super_harness.reminders.subprocess.run") as run:
            with self.assertLogs("super_super_harness.reminders", level="INFO"):
                system_notify(title, body)
            run.assert_not_called()


class ReminderWebTests(ServiceCase):
    request = test_web.WebCase.request
    login = test_web.WebCase.login
    stream = test_web.WebCase.stream
    close_stream = test_web.WebCase.close_stream
    event = test_web.WebCase.event

    async def boot(self):
        self.cfg.web["port"] = free_port()
        await super().boot()
        self.port = self.cfg.web["port"]
        self.cookie = ""
        await self.login()

    async def test_reminder_tools_permissions_idempotency_and_pause(self):
        worker_names = {t["name"] for t in tools.WORKER}
        self.assertFalse({"reminder_add", "reminder_list", "reminder_cancel"} & worker_names)
        with self.assertRaises(ValueError):
            await self.service.call_tool(("worker", "t1"), "reminder_add", {})
        self.service.router.mode = "paused"
        self.service.router.switching = True
        clock = [parse_at("2026-01-01T00:00:00+00:00")]
        self.service.reminders.clock = lambda: clock[0]
        self.service.reminders.notifier = Mock()
        turn = self.store.open_turn()
        args = dict(name="Review notes", text="Summarize notes", kind="run", at=local_stamp(clock[0]+60))
        first = await self.tool("reminder_add", **args)
        self.assertEqual(first, await self.tool("reminder_add", **args))
        self.store.run("UPDATE turns SET status='done' WHERE id=?", turn)
        await self.tool("reminder_add", name="Stretch", text="Take a break", kind="notify", at=local_stamp(clock[0]+60))
        clock[0] += 61
        await self.service.reminders.tick()
        self.assertEqual(len(self.store.messages("queued")), 1)
        self.assertEqual(self.store.one("SELECT kind FROM conversation WHERE role='router'")["kind"], "reminder")
        self.service.router.mode = "running"
        await self.service.router.pump()
        self.assertEqual(len(self.store.messages("queued")), 1)  # Still switching.
        self.service.router.switching = False
        await self.service.router.pump()
        await self.until(lambda: not self.store.messages("queued") and not self.store.current_turn())
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM reminder_fires")["n"], 2)

    async def test_add_cancel_add_in_one_turn_creates_live_replacement(self):
        self.service.router.mode = "paused"
        self.store.open_turn()
        args = dict(name="Review", text="Review notes", kind="run", at=local_stamp(__import__('time').time()+60))
        first = await self.tool("reminder_add", **args)
        self.assertEqual(await self.tool("reminder_add", **args), first)
        await self.tool("reminder_cancel", reminder_id=first["id"])
        replacement = await self.tool("reminder_add", **args)
        self.assertNotEqual(replacement["id"], first["id"])
        self.assertEqual(await self.tool("reminder_add", **args), replacement)
        self.assertEqual([r["id"] for r in self.service.reminders.list()["items"]], [replacement["id"]])
        self.service.reminders.clock = lambda: __import__('time').time()+120
        await self.service.reminders.tick()
        self.assertEqual(len(self.store.messages("queued")), 1)
        self.assertIn(replacement["id"], self.store.messages("queued")[0]["text"])
        self.assertEqual(await self.tool("reminder_add", **args), replacement)  # Finished is still replayable.

    async def test_state_survives_unreadable_localtime(self):
        with patch.dict(os.environ, {}, clear=True), patch("super_super_harness.reminders.open", side_effect=FileNotFoundError):
            status, _, body = await self.request()
        self.assertEqual(status, 200)
        self.assertIn("timezone", body["reminders"])

    async def test_reminders_web_auth_csrf_sort_cancel_and_sse(self):
        at = local_stamp(__import__('time').time()+3600)
        first = await self.tool("reminder_add", name="Later", text="Review", kind="notify", at=at)
        await self.tool("reminder_add", name="Sooner", text="Review", kind="run", at=local_stamp(__import__('time').time()+1800))
        self.assertEqual((await self.request(path="/api/reminders", headers={"Cookie":""}))[0], 401)
        status, _, body = await self.request(path="/api/reminders")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in body["items"]], ["Sooner", "Later"])
        request = dict(method="POST", path="/api/reminders/cancel", data={"reminder_id":first["id"]})
        self.assertEqual((await self.request(**request, headers={"Origin":"http://other.invalid"}))[0], 403)
        self.assertEqual((await self.request(**request, headers={"Cookie":""}))[0], 401)
        reader, _ = await self.stream()
        await self.event(reader,"state")
        self.assertEqual((await self.request(**request))[0], 200)
        await self.event(reader,"state")
        self.assertEqual(len((await self.request())[2]["reminders"]["items"]), 1)
        self.assertEqual((await self.request(method="POST", path="/api/reminders/cancel", data={"reminder_id":"../x"}))[0], 400)
