"""Chat tests use disposable databases, a fake socket service, and actual PTYs only."""
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from super_super_harness.channels import chat
from super_super_harness import cli
from super_super_harness.core.store import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
# Keep pre-web fixtures pre-web even when the current Store schema evolves.
LEGACY_SCHEMA = SCHEMA.split("CREATE TABLE IF NOT EXISTS conversation", 1)[0]
# The web release adds conversation and migration columns to the legacy schema.
WEB_SCHEMA = """
CREATE TABLE conversation (
 id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, source_id INTEGER NOT NULL,
 channel TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
 turn_id INTEGER, created REAL NOT NULL, UNIQUE(role, source_id)
);
CREATE TABLE web_requests (key TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
ALTER TABLE tasks ADD COLUMN state_since REAL;
ALTER TABLE tasks ADD COLUMN started REAL;
ALTER TABLE tasks ADD COLUMN base_commit TEXT;
ALTER TABLE events ADD COLUMN turn_id INTEGER;
"""


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssharness-chat-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "router #?.db"

    def fixture(self, web=False):
        db = sqlite3.connect(self.path)
        db.executescript(LEGACY_SCHEMA + (WEB_SCHEMA if web else ""))
        db.execute("INSERT INTO messages(uuid,role,channel,text,created) VALUES('u','user','terminal','你好',1)")
        db.execute("INSERT INTO outbox(channel,kind,text,status,created) VALUES('terminal','answer','你好，正在处理。','sent',2)")
        if web:
            db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) "
                       "VALUES('user',1,'terminal','','你好',1)")
            db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) "
                       "VALUES('router',1,'terminal,web','answer','你好，正在处理。',2)")
        db.commit()
        self.addCleanup(db.close)
        return db

    def history(self):
        history = chat.History(self.path)
        self.addCleanup(history.close)
        return history

    def test_stage_2_core_history_is_read_only(self):
        db = self.fixture()
        before = self.path.read_bytes()
        history = self.history()
        self.assertEqual([(m.role, m.text) for m in history.recent()], [("user", "你好"), ("router", "你好，正在处理。")])
        with self.assertRaises(sqlite3.OperationalError):
            history.db.execute("CREATE TABLE forbidden(x)")
        history.close()
        history.db = None
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='conversation'").fetchone())

    def test_web_history_uses_conversation_without_duplicate_channels(self):
        db = self.fixture(web=True)
        db.execute("INSERT INTO outbox(channel,kind,text,status,created) VALUES('web','answer','你好，正在处理。','sent',2)")
        db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) "
                   "VALUES('router',3,'web','answer','仅网页',3)")
        db.commit()
        before = self.path.read_bytes()
        self.assertEqual([m.text for m in self.history().recent()], ["你好", "你好，正在处理。"])
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_database_is_not_created(self):
        self.assertEqual(self.history().recent(), [])
        self.assertFalse(self.path.exists())

    def test_missing_tables_columns_and_invalid_conversation_degrade(self):
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        db.executescript("CREATE TABLE conversation(id INTEGER, text TEXT); "
                         "CREATE TABLE messages(id INTEGER,role TEXT,text TEXT); "
                         "INSERT INTO messages VALUES(1,'user','旧版本'); "
                         "CREATE TABLE outbox(id INTEGER,text TEXT); INSERT INTO outbox VALUES(1,'旧回复');")
        db.commit()
        self.assertEqual([m.text for m in self.history().recent()], ["旧版本", "旧回复"])
        self.assertEqual(self.history().incoming({"text": "未分类"}).kind, "unknown")

    def test_corrupt_database_does_not_break_chat(self):
        self.path.write_bytes(b"not sqlite")
        self.assertEqual(self.history().recent(), [])

    def test_text_only_live_notifications_match_in_delivery_order(self):
        db = self.fixture()
        history = self.history()
        for kind in ("notice", "alert", "answer"):
            db.execute("INSERT INTO outbox(channel,kind,text,created) VALUES('terminal',?,'同样的文本',3)", (kind,))
        db.commit()
        results = [history.incoming({"text": "同样的文本"}) for _ in range(3)]
        self.assertEqual([m.kind for m in results], ["notice", "alert", "answer"])
        self.assertEqual([m.key for m in results], [("router", 2), ("router", 3), ("router", 4)])
        self.assertEqual(history.incoming({"text": "同样的文本"}).kind, "unknown")

    def test_pending_replayed_message_has_same_history_key(self):
        db = self.fixture()
        db.execute("INSERT INTO outbox(channel,kind,text,created) VALUES('terminal','notice','积压',3)")
        db.commit()
        history = self.history()
        key = history.recent()[-1].key
        self.assertEqual(history.incoming({"text": "积压"}).key, key)

    def test_notice_enriched_from_turn_events_not_text_guessing(self):
        db = self.fixture()
        history = self.history()
        for index, kind in enumerate(("worker.done", "worker.question", "worker.failed"), 1):
            db.execute("INSERT INTO messages(uuid,role,kind,text,turn_id,status,created) VALUES(?,'event',?,'结果',?,'confirmed',3)",
                       (str(index), kind, index))
            db.execute("INSERT INTO outbox(channel,kind,text,turn_id,created) VALUES('terminal','notice',?,?,4)",
                       ("通知%d" % index, index))
        db.commit()
        self.assertEqual([history.incoming({"text": "通知%d" % i}).kind for i in (1, 2, 3)],
                         ["worker.done", "worker.question", "worker.failed"])
        self.assertEqual(history.incoming({"text": "完成了？告警？", "kind": "answer"}).kind, "answer")

    def test_worker_titles_match_history_and_live_with_multiple_tasks(self):
        from super_super_harness.core.store import Store
        db = Store(self.path)
        self.addCleanup(db.close)
        history = self.history()
        turn = db.open_turn()
        for index in range(5):
            task_id = db.add_task("Example %d" % index, "brief", "work", None, "s", "", "")
            db.add_message(str(index), "event", "body without task name", kind="worker.done", task_id=task_id)
        db.run("UPDATE messages SET turn_id=?,status='confirmed'", turn)
        db.run("INSERT INTO outbox(channel,kind,text,turn_id,created) VALUES('terminal','notice','All done',?,2)", turn)
        db.run("INSERT INTO conversation(role,source_id,channel,kind,text,turn_id,created) "
               "VALUES('router',1,'terminal','notice','All done',?,2)", turn)
        live = history.incoming({"text": "All done", "kind": "notice"})
        saved = history.recent()[-1]
        display = chat.Display(io.StringIO(), False, False)
        display.message(live)
        live_text = display.output.getvalue()
        self.assertIn("✓ Nova · Example 0", live_text)
        self.assertIn("Echo · Example 2", live_text)
        self.assertIn("5", live_text)
        self.assertNotIn("Atlas · Example 3", live_text)
        display.output = io.StringIO()
        display.message(saved)
        self.assertEqual(display.output.getvalue(), live_text)

    def test_worker_failed_and_question_titles_and_missing_task_fallback(self):
        from super_super_harness.core.store import Store
        db = Store(self.path)
        self.addCleanup(db.close)
        history = self.history()
        task_id = db.add_task("Neutral task", "brief", "work", None, "s", "", "")
        for index, (kind, label) in enumerate((("worker.failed", "告警"), ("worker.question", "提问")), 1):
            turn = db.open_turn()
            message_id = db.add_message(str(index), "event", "no task names", kind=kind, task_id=task_id)
            db.run("UPDATE messages SET turn_id=?,status='confirmed' WHERE id=?", turn, message_id)
            db.run("INSERT INTO outbox(channel,kind,text,turn_id,created) VALUES('terminal','notice',?,?,?)", kind, turn, index)
            db.run("INSERT INTO conversation(role,source_id,channel,kind,text,turn_id,created) "
                   "VALUES('router',?,'terminal','notice',?,?,?)", index, kind, turn, index)
            display = chat.Display(io.StringIO(), False, False)
            display.message(history.incoming({"text": kind}))
            self.assertIn(label + " Nova · Neutral task", display.output.getvalue())
        db.run("DELETE FROM tasks")
        self.assertTrue(history.recent())  # Missing names must not break old history.

    def test_worker_titles_respect_message_kind_and_group_latest_events(self):
        from super_super_harness.core.store import Store
        from tests.notification_helpers import CASES, populate
        db = Store(self.path)
        self.addCleanup(db.close)
        history = self.history()
        for name, kind, events, expected_kind, title in CASES:
            with self.subTest(name=name):
                turn, ids = populate(db, events)
                self.service_reply(db, name, kind, turn)
                live = history.incoming({'text': name, 'kind': kind})
                saved = history.recent()[-1]
                self.assertEqual((live.kind, live.title), (expected_kind, title.format(*(db.task(task_id)["code_name"] for task_id in ids))))
                display = chat.Display(io.StringIO(), False, False)
                display.message(live)
                live_text = display.output.getvalue()
                display.output = io.StringIO()
                display.message(saved)
                self.assertEqual(display.output.getvalue(), live_text)

    @staticmethod
    def service_reply(db, text, kind, turn):
        cursor = db.run("INSERT INTO outbox(channel,kind,text,turn_id,created) VALUES('terminal',?,?,?,?)",
                        kind, text, turn, time.time())
        db.run("INSERT INTO conversation(role,source_id,channel,kind,text,turn_id,created) "
               "SELECT 'router',id,channel,kind,text,turn_id,created FROM outbox WHERE id=?", cursor.lastrowid)

    def test_no_matching_row_keeps_kind_or_marks_unknown(self):
        self.assertEqual(self.history().incoming({"text": "hi", "kind": "alert"}).kind, "alert")
        self.assertEqual(self.history().incoming({"text": "hi"}).kind, "unknown")


class HistoryCommandTests(unittest.TestCase):
    def setUp(self):
        from super_super_harness.ops import config
        self.temporary = tempfile.TemporaryDirectory(prefix="ssharness-history-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.cfg = config.Paths(self.home)

    def command(self, *args):
        from contextlib import redirect_stderr, redirect_stdout
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, SSHARNESS_HOME=str(self.home)), \
                patch("sys.argv", ["supersuperharness", "history", *args]), \
                patch("super_super_harness.cli.config.load", side_effect=AssertionError("history needs only paths")), \
                patch("super_super_harness.core.store.Store", side_effect=AssertionError("history must not migrate")), \
                patch("super_super_harness.cli.Client", side_effect=AssertionError("history must work offline")), \
                redirect_stdout(output), redirect_stderr(errors):
            result = cli.main()
        return result, output.getvalue(), errors.getvalue()

    def fixture(self, web):
        db = sqlite3.connect(self.cfg.db)
        db.executescript(LEGACY_SCHEMA + (WEB_SCHEMA if web else ""))
        for i in range(10):
            role = "user" if i % 2 == 0 else "router"
            text = "full body %d\nsecond line" % i
            if web:
                db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) VALUES(?,?,'terminal','answer',?,?)",
                           (role, i + 1, text, i + 1))
            elif role == "user":
                db.execute("INSERT INTO messages(uuid,role,channel,text,created) VALUES(?,'user','terminal',?,?)",
                           (str(i), text, i + 1))
            else:
                db.execute("INSERT INTO outbox(channel,kind,text,created) VALUES('terminal','answer',?,?)", (text, i + 1))
        db.commit()
        db.close()

    def test_default_eight_and_limit_use_history_read_only_without_service_or_config(self):
        for web in (False, True):
            with self.subTest(web=web):
                self.fixture(web)
                before = self.cfg.db.read_bytes()
                original = chat.CommandHistory
                opened = []
                def history(path):
                    result = original(path)
                    opened.append(result)
                    with self.assertRaises(sqlite3.OperationalError):
                        result.db.execute("CREATE TABLE forbidden(x)")
                    return result
                with patch("super_super_harness.channels.chat.CommandHistory", side_effect=history):
                    result, output, errors = self.command()
                self.assertEqual(result, 0, errors)
                self.assertEqual(len(opened), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    opened[0].db.execute("SELECT 1")
                self.assertEqual(output.count("full body"), 8)
                self.assertNotIn("full body 1\n", output)
                self.assertIn("user", output)
                self.assertIn("router", output)
                self.assertIn("answer", output)
                self.assertIn(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(3)), output)
                self.assertIn("full body 9\nsecond line", output)
                result, output, errors = self.command("--limit", "2")
                self.assertEqual(result, 0, errors)
                self.assertEqual(output.count("full body"), 2)
                self.assertLess(output.index("full body 8"), output.index("full body 9"))
                self.assertEqual(self.cfg.db.read_bytes(), before)
                self.cfg.db.unlink()

    def test_missing_database_is_not_created(self):
        result, output, errors = self.command()
        self.assertEqual(result, 0, errors)
        self.assertIn("No messages.", output)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_nonpositive_limit_is_rejected(self):
        for limit in ("0", "-1"):
            with self.subTest(limit=limit), self.assertRaises(SystemExit) as error:
                self.command("--limit", limit)
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(list(self.home.iterdir()), [])

    def wal_fixture(self, web=False):
        self.fixture(web)
        db = sqlite3.connect(self.cfg.db)
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.addCleanup(db.close)
        return db

    def test_terminal_filter_runs_before_limit_on_real_wal_databases(self):
        for web in (False, True):
            with self.subTest(web=web):
                db = self.wal_fixture(web)
                for role, channel, text, stamp in (("user", ",web,terminal,", "comma-list user", 11),
                                                    ("router", "web,terminal", "comma-list router", 12)):
                    if web:
                        db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) VALUES(?,100,?,'answer',?,?)",
                                   (role, channel, text, stamp))
                    elif role == "user":
                        db.execute("INSERT INTO messages(uuid,role,channel,text,created) VALUES('comma','user',?,?,?)", (channel, text, stamp))
                    else:
                        db.execute("INSERT INTO outbox(channel,kind,text,created) VALUES(?,'answer',?,?)", (channel, text, stamp))
                for i in range(40):
                    channel = ("web", "notterminal", "terminal-extra", "terminal ", "web, terminal")[i % 5]
                    if web:
                        db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) VALUES('router',?,?,'answer','hidden web row',?)",
                                   (200 + i, channel, 100 + i))
                    else:
                        db.execute("INSERT INTO messages(uuid,role,channel,text,created) VALUES(?,'user',?,'hidden web row',?)", (str(200 + i), channel, 100 + i))
                        db.execute("INSERT INTO outbox(channel,kind,text,created) VALUES(?,'answer','hidden web row',?)", (channel, 100 + i))
                db.commit()
                wal = Path(str(self.cfg.db) + "-wal")
                self.assertTrue(wal.is_file())
                before = (self.cfg.db.read_bytes(), wal.read_bytes())
                result, output, errors = self.command("--limit", "2")
                self.assertEqual(result, 0, errors)
                self.assertIn("comma-list user", output)
                self.assertIn("comma-list router", output)
                self.assertNotIn("hidden web row", output)
                self.assertEqual((self.cfg.db.read_bytes(), wal.read_bytes()), before)
                # The interactive chat reader retains its existing tolerant/windowed behavior.
                history = chat.History(self.cfg.db)
                try:
                    self.assertEqual(history.recent(2), [])
                finally:
                    history.close()
                db.close()
                self.cfg.db.unlink()

    def test_missing_required_tables_is_a_read_failure_not_an_empty_history(self):
        for web in (False, True):
            with self.subTest(web=web):
                db = self.wal_fixture(web)
                db.execute("DROP TABLE outbox")
                db.commit()
                self.assertTrue(Path(str(self.cfg.db) + "-wal").is_file())
                result, output, errors = self.command()
                self.assertEqual(result, 1)
                self.assertNotIn("No messages.", output)
                self.assertIn("Cannot read history", errors)
                self.assertNotIn("outbox", errors)
                db.close()
                self.cfg.db.unlink()

    def test_corrupt_database_is_a_safe_read_failure(self):
        self.cfg.db.write_bytes(b"not sqlite: fake-private-diagnostic")
        before = self.cfg.db.read_bytes()
        result, output, errors = self.command()
        self.assertEqual(result, 1)
        self.assertNotIn("No messages.", output)
        self.assertIn("Cannot read history", errors)
        self.assertNotIn("fake-private-diagnostic", errors)
        self.assertEqual(self.cfg.db.read_bytes(), before)
        history = chat.History(self.cfg.db)
        try:
            self.assertEqual(history.recent(), [])
        finally:
            history.close()

    def test_unopenable_database_is_a_safe_read_failure(self):
        self.cfg.db.mkdir()
        result, output, errors = self.command()
        self.assertEqual(result, 1)
        self.assertNotIn("No messages.", output)
        self.assertIn("Cannot read history", errors)
        self.assertNotIn(str(self.home), errors)

    def test_oversized_limit_is_an_argument_error(self):
        for limit in (str(2 ** 63), str(10 ** 100)):
            with self.subTest(limit=limit):
                self.fixture(False)
                try:
                    with self.assertRaises(SystemExit) as error:
                        self.command("--limit", limit)
                    self.assertEqual(error.exception.code, 2)
                finally:
                    self.cfg.db.unlink()

    def test_closed_history_pipe_does_not_report_service_stopped(self):
        db = self.wal_fixture(True)
        db.execute("INSERT INTO conversation(role,source_id,channel,kind,text,created) VALUES('router',100,'terminal','answer',?,100)",
                   ("full output\n" * 100000,))
        db.commit()
        process = subprocess.Popen([sys.executable, "-m", "super_super_harness.cli", "history"], cwd=ROOT,
                                   env=dict(os.environ, SSHARNESS_HOME=str(self.home)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(process.stdout.readline())
            process.stdout.close()
            process.wait(timeout=5)
            errors = process.stderr.read().decode()
            self.assertEqual(process.returncode, 0, errors)
            self.assertNotIn("后台服务没有运行", errors)
            self.assertNotIn("Traceback", errors)
            self.assertNotIn("BrokenPipeError", errors)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


class DisplayTests(unittest.TestCase):
    def display(self, columns=36, color=False, interactive=False):
        output = io.StringIO()
        display = chat.Display(output, interactive, color)
        display.columns = lambda: columns
        return display, output

    def test_chinese_wrapping_and_message_boundaries(self):
        display, output = self.display()
        display.message(chat.Message("user", "中文和 English " * 15, 1))
        display.message(chat.Message("router", "长段落\n第二段" * 12, 2))
        text = output.getvalue()
        self.assertIn("You", text)
        self.assertIn("Router", text)
        self.assertEqual(text.count("╰"), 2)
        self.assertIn("\n\n╭", text)
        self.assertTrue(all(chat.width(line) < 36 for line in text.splitlines()))
        self.assertNotIn("\x1b", text)

    def test_scheduled_run_history_is_not_labelled_you(self):
        display, output = self.display()
        display.message(chat.Message("user", "Scheduled project review", kind="reminder.run"))
        self.assertIn("⏰ 定时任务", output.getvalue())
        self.assertNotIn("You", output.getvalue())

    def test_notifications_have_different_labels_and_colors(self):
        display, output = self.display(color=True)
        for kind in ("answer", "notice", "worker.done", "worker.question", "alert"):
            display.message(chat.Message("router", "内容", kind=kind))
        for label in ("Router", "通知", "完成", "提问", "告警"):
            self.assertIn(label, output.getvalue())
        for color in ("36", "33", "32", "35", "31"):
            self.assertIn("\x1b[%sm" % color, output.getvalue())

    def test_remote_terminal_controls_are_removed(self):
        display, output = self.display()
        display.message(chat.Message("router", "\x1b[2J安全\x1b]0;bad\x07\x00\b\r\n下一行"))
        self.assertIn("安全", output.getvalue())
        self.assertNotIn("bad", output.getvalue())
        self.assertTrue(all(c in "\n" or ord(c) >= 32 for c in output.getvalue()))

    def test_model_comes_from_running_profile_not_primary(self):
        cfg = SimpleNamespace(profile=lambda name: SimpleNamespace(model="backup-model" if name == "backup" else "wrong"))
        self.assertIn("backup-model", chat.connection_info(cfg, {"router": {"profile": "backup"}, "live": 2, "max_workers": 5}))
        self.assertIn("Worker 2/5", chat.connection_info(cfg, {"router": {"profile": "backup"}, "live": 2, "max_workers": 5}))

    def test_editor_preserves_utf8_editing_and_paste(self):
        display, _ = self.display(interactive=True)
        editor = chat.Editor(display)
        data = "你好".encode()
        for byte in data:
            self.assertEqual(editor.feed(bytes([byte])), ([], False))
        editor.feed(b"\x1b[D")
        editor.feed("真".encode())
        self.assertEqual(display.text, "你真好")
        editor.feed(b"\x1b[F\x7f")
        self.assertEqual(display.text, "你真")
        editor.feed(b"\x1b[200~one\ntwo\x1b[201~")
        lines, eof = editor.feed(b"\r\n")
        self.assertEqual(lines, ["你真one two"])
        self.assertFalse(eof)
        self.assertEqual(editor.feed(b"\x04"), ([], True))

    def exit_editor(self):
        display, output = self.display(interactive=True)
        now = [100.0]
        editor = chat.Editor(display, clock=lambda: now[0])
        return display, output, editor, now

    def test_ctrl_c_clears_typed_input_without_arming_exit(self):
        display, _, editor, _ = self.exit_editor()
        editor.feed("草稿".encode())
        self.assertEqual(editor.feed(b"\x03"), ([], False))
        self.assertEqual((display.text, display.cursor), ("", 0))
        self.assertEqual(display.hint, "")

    def test_ctrl_c_twice_within_two_seconds_on_empty_input_exits(self):
        display, output, editor, now = self.exit_editor()
        self.assertEqual(editor.feed(b"\x03"), ([], False))
        self.assertEqual(display.hint, "再按一次 Ctrl-C 退出")
        display.redraw()
        self.assertIn("再按一次 Ctrl-C 退出", output.getvalue())
        now[0] += 1.9
        self.assertFalse(editor.expire())
        self.assertEqual(editor.feed(b"\x03"), ([], True))

    def test_ctrl_c_hint_expires_after_two_seconds_and_rearms(self):
        display, output, editor, now = self.exit_editor()
        editor.feed(b"\x03")
        now[0] += 2.0
        self.assertTrue(editor.expire())
        self.assertEqual(display.hint, "")
        output.seek(0)
        output.truncate()
        display.redraw()
        self.assertNotIn("再按一次", output.getvalue())
        self.assertFalse(editor.expire())
        self.assertEqual(editor.feed(b"\x03"), ([], False))
        self.assertEqual(display.hint, "再按一次 Ctrl-C 退出")
        now[0] += 0.5
        self.assertEqual(editor.feed(b"\x03"), ([], True))

    def test_ctrl_d_on_empty_input_exits_even_with_ctrl_c_hint(self):
        display, _, editor, _ = self.exit_editor()
        editor.feed(b"\x03")
        self.assertEqual(editor.feed(b"\x04"), ([], True))
        display, _, editor, _ = self.exit_editor()
        editor.feed(b"x\x01\x04")
        self.assertEqual(display.text, "")
        self.assertEqual(editor.feed(b"\x04"), ([], True))

    def test_connection_info_names_both_exit_keys(self):
        cfg = SimpleNamespace(profile=lambda name: SimpleNamespace(model="m"))
        info = chat.connection_info(cfg, {"router": {"profile": "p"}, "live": 0, "max_workers": 1})
        self.assertIn("Ctrl-C 两次或 Ctrl-D 退出", info)
        self.assertIn("后台不受影响", info)

    def test_long_input_stays_on_one_row(self):
        display, output = self.display(columns=20, interactive=True)
        display.text = "中文正在输入" * 20
        display.cursor = len(display.text)
        display.waiting = True
        display.prompt()
        rows = output.getvalue().split("\n")
        self.assertEqual(len(rows), 2)
        visible = chat.ESCAPES.sub("", rows[1]).replace("\r", "")
        self.assertLess(chat.width(visible), 20)

    def test_resize_repaints_history_and_keeps_one_input_line(self):
        display, output = self.display(columns=70, interactive=True)
        display.message(chat.Message("router", "中文历史段落" * 5, 1))
        display.text = "正在输入的中文内容"
        display.cursor = len(display.text)
        display.waiting = True
        display.redraw()
        display.columns = lambda: 18
        output.seek(0)
        output.truncate()
        display.repaint()
        # Model output processing (ONLCR) just as the actual PTY does.
        screen = TerminalScreen(18)
        screen.feed(output.getvalue().replace("\n", "\r\n"))
        visible = screen.lines()
        self.assertEqual(sum(line.startswith("› ") for line in visible), 1)
        self.assertTrue(any("等待 Router" in line for line in visible))
        self.assertEqual(display.text, "正在输入的中文内容")
        self.assertEqual(len(display.transcript), 1)
        self.assertEqual(sum("Router  " in line for line in visible), 1)

    def test_cli_delegates_only_chat(self):
        with patch.object(chat, "cmd_chat", return_value=7) as target:
            self.assertEqual(cli.cmd_chat("cfg", "args"), 7)
            target.assert_called_once_with("cfg", "args")


class FakeService:
    """The same owner socket protocol, without any Harness or real SSHARNESS_HOME access."""

    def __init__(self, home):
        self.home = home
        self.methods = []
        self.said = []
        self.clients = []
        self.subscriber = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.subscribed = threading.Event()
        (home / "owner-token").write_text("test-token")
        (home / "profiles.toml").write_text('''default_profile = "test"
session_root = "TEST_ONLY"
[router]
primary = "test"
backup = "test"
[harness.fake]
binary = "unused"
[providers.unused]
base_url = "http://127.0.0.1:1"
[profiles.test]
harness = "fake"
provider = "unused"
model = "test-model"
''')
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(home / "router.sock"))
        self.server.listen()
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.clients.append(conn)
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            with conn.makefile("r", encoding="utf-8") as reader:
                for line in reader:
                    request = json.loads(line)
                    assert request["token"] == "test-token"
                    method = request["method"]
                    self.methods.append(method)
                    if method == "status":
                        result = {"router": {"profile": "test", "mode": "running"}, "live": 2, "max_workers": 4}
                    elif method == "subscribe":
                        self.subscriber = conn
                        result = {"ok": True}
                    elif method == "stop":
                        result = {"ok": True}
                    elif method == "say":
                        time.sleep(0.02)
                        self.said.append(request["params"]["text"])
                        result = {"id": len(self.said)}
                    else:
                        raise AssertionError("unexpected method %s" % method)
                    self.write(conn, {"result": result})
                    if method == "subscribe":
                        self.subscribed.set()
        except (OSError, ValueError):
            pass

    def write(self, conn, data):
        with self.lock:
            conn.sendall((json.dumps(data, ensure_ascii=False) + "\n").encode())

    def push(self, text, kind="answer"):
        assert self.subscribed.wait(3)
        event = {"event": "message", "text": text}
        if kind is not None:
            event["kind"] = kind
        self.write(self.subscriber, event)

    def close(self):
        self.stopped.set()
        self.server.close()
        for conn in self.clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()


class TerminalScreen:
    """Interpret this client's ANSI subset to assert the visible PTY, not its echo log."""

    def __init__(self, columns, rows=36):
        self.columns, self.rows = columns, rows
        self.cells = [[(" ", "0") for _ in range(columns)] for _ in range(rows)]
        self.x = self.y = 0
        self.color = "0"

    def feed(self, text):
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\x1b":
                match = chat.ESCAPES.match(text, index)
                if not match:
                    index += 1
                    continue
                sequence = match.group()
                index = match.end()
                if sequence.startswith("\x1b["):
                    param, command = sequence[2:-1], sequence[-1]
                    if param.startswith("?"):
                        continue
                    amount = int(param or "1")
                    if command == "A":
                        self.y = max(0, self.y - amount)
                    elif command == "C":
                        self.x = min(self.columns - 1, self.x + amount)
                    elif command == "K" and param == "2":
                        self.cells[self.y] = [(" ", "0") for _ in range(self.columns)]
                    elif command == "J" and param == "2":
                        self.cells = [[(" ", "0") for _ in range(self.columns)] for _ in range(self.rows)]
                    elif command == "H":
                        self.x = self.y = 0
                    elif command == "m":
                        self.color = param or "0"
                continue
            index += 1
            if char == "\r":
                self.x = 0
            elif char == "\n":
                self.y += 1
                if self.y >= self.rows:
                    self.cells.pop(0)
                    self.cells.append([(" ", "0") for _ in range(self.columns)])
                    self.y = self.rows - 1
            else:
                size = chat.width(char)
                if not size:
                    continue
                if self.x + size > self.columns:
                    raise AssertionError("client wrote past terminal edge")
                self.cells[self.y][self.x] = (char, self.color)
                if size == 2:
                    self.cells[self.y][self.x + 1] = ("", self.color)
                self.x += size

    def lines(self):
        return ["".join(char for char, _ in row).rstrip() for row in self.cells]


class PTYSession:
    def __init__(self, home, columns=70, no_color=False):
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        self.resize(columns)
        env = dict(os.environ, SSHARNESS_HOME=str(home), TERM="xterm-256color", PYTHONPATH=str(ROOT))
        if no_color:
            env["NO_COLOR"] = ""
        else:
            env.pop("NO_COLOR", None)
        self.process = subprocess.Popen([str(ROOT / "bin" / "supersuperharness"), "chat"], cwd=ROOT,
                                        stdin=self.slave, stdout=self.slave, stderr=self.slave, env=env)
        self.output = bytearray()

    def resize(self, columns):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, columns, 0, 0))

    def read_until(self, text, timeout=4, count=1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.output.count(text.encode()) >= count:
                return self.output.decode("utf-8", "replace")
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                self.output.extend(os.read(self.master, 65536))
        raise AssertionError("PTY never showed %r: %r" % (text, self.output.decode("utf-8", "replace")))

    def type(self, text):
        os.write(self.master, text.encode())

    def screen(self, columns):
        while select.select([self.master], [], [], 0.1)[0]:
            self.output.extend(os.read(self.master, 65536))
        screen = TerminalScreen(columns)
        text = self.output.decode("utf-8", "replace")
        # Repainting on resize explicitly replaces the previous-width viewport.
        if "\x1b[2J" in text:
            text = text[text.rfind("\x1b[2J"):]
        screen.feed(text)
        return screen

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=3)
        os.close(self.master)
        os.close(self.slave)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssharness-chat-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.service = FakeService(self.home)
        self.addCleanup(self.service.close)

    def session(self, **kwargs):
        session = PTYSession(self.home, **kwargs)
        self.addCleanup(session.close)
        session.read_until("后台不受影响")
        # Let raw-mode setup finish before typing.
        session.read_until("\x1b[?2004h")
        return session

    def test_pty_chinese_partial_input_survives_async_message(self):
        session = self.session()
        session.type("正在输入中文")
        session.read_until("正在输入中文")
        self.service.push("消息在输入过程中到达。", "notice")
        session.read_until("消息在输入过程中到达。")
        visible = session.screen(70).lines()
        self.assertIn("› 正在输入中文", visible)
        self.assertEqual(sum("正在输入中文" in line for line in visible), 1)
        session.type("，没有丢失\r")
        session.read_until("You")
        self.assertTrue(self.wait_for(lambda: self.service.said))
        self.assertEqual(self.service.said, ["正在输入中文，没有丢失"])
        self.service.push("收到，继续吧。")
        session.read_until("收到，继续吧。")
        visible = session.screen(70).lines()
        self.assertEqual(sum("正在输入中文，没有丢失" in line for line in visible), 1)
        self.assertFalse(any("等待 Router" in line for line in visible))
        session.type("\x04")
        self.assertEqual(session.process.wait(timeout=3), 0)
        restored = termios.tcgetattr(session.slave)
        # macOS sets PENDIN when echo is restored; this is a transient kernel flag.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        original = list(session.original)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(restored, original)
        self.assertEqual(set(self.service.methods), {"status", "subscribe", "say"})

    @staticmethod
    def wait_for(predicate):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def assert_left_chat_only(self, session):
        self.assertEqual(session.process.wait(timeout=3), 0)
        restored = termios.tcgetattr(session.slave)
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        original = list(session.original)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(restored, original)
        # Leaving chat closes this client only: the service is never asked to stop.
        self.assertNotIn("stop", self.service.methods)
        probe = socket.socket(socket.AF_UNIX)
        self.addCleanup(probe.close)
        probe.connect(str(self.home / "router.sock"))

    def test_pty_ctrl_c_twice_on_empty_input_leaves_chat(self):
        session = self.session()
        session.type("草稿")
        session.read_until("草稿")
        session.type("\x03")
        time.sleep(0.2)
        self.assertNotIn("再按一次 Ctrl-C 退出", session.output.decode("utf-8", "replace"))
        self.assertIsNone(session.process.poll())
        session.type("\x03")
        session.read_until("再按一次 Ctrl-C 退出")
        visible = session.screen(70).lines()
        self.assertTrue(any("再按一次 Ctrl-C 退出" in line for line in visible))
        self.assertIsNone(session.process.poll())
        session.type("\x03")
        self.assert_left_chat_only(session)
        self.assertEqual(self.service.said, [])

    def test_pty_ctrl_c_hint_disappears_after_two_seconds(self):
        session = self.session()
        session.type("\x03")
        session.read_until("再按一次 Ctrl-C 退出")
        time.sleep(2.4)
        visible = session.screen(70).lines()
        self.assertFalse(any("再按一次" in line for line in visible))
        self.assertIsNone(session.process.poll())
        session.type("\x03")
        session.read_until("再按一次 Ctrl-C 退出", count=2)
        time.sleep(0.2)
        self.assertIsNone(session.process.poll())
        session.type("\x03")
        self.assert_left_chat_only(session)

    def test_pty_exit_commands_leave_without_sending(self):
        for index, command in enumerate(("/exit", "/quit")):
            with self.subTest(command=command):
                session = self.session()
                session.type("消息%d\r" % index)
                self.assertTrue(self.wait_for(lambda: len(self.service.said) == index + 1))
                session.type(command + "\r")
                self.assert_left_chat_only(session)
                self.assertEqual(self.service.said, ["消息%d" % i for i in range(index + 1)])

    def test_pipe_exit_command_stops_reading_without_sending(self):
        env = dict(os.environ, SSHARNESS_HOME=str(self.home), PYTHONPATH=str(ROOT))
        result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "chat"],
                                input="第一条\n /quit \n第三条\n", capture_output=True, text=True,
                                cwd=ROOT, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.service.said, ["第一条"])
        self.assertNotIn("/quit", result.stdout)
        self.assertNotIn("stop", self.service.methods)

    def test_question_clears_waiting_indicator(self):
        session = self.session()
        session.type("需要确认\r")
        self.assertTrue(self.wait_for(lambda: self.service.said))
        session.read_until("等待 Router")
        self.service.push("选择哪个例子？", "question")
        session.read_until("选择哪个例子？")
        visible = session.screen(70).lines()
        self.assertTrue(any("Router 提问" in line for line in visible))
        self.assertFalse(any("等待 Router" in line for line in visible))

    def test_pty_narrow_window_long_chinese_and_no_color(self):
        session = self.session(columns=24, no_color=True)
        session.type("窄窗口中输入很长的中文消息" * 5)
        time.sleep(0.1)
        self.service.push("这是一个很长的中文段落，需要在窄窗口里正确换行。" * 4, "alert")
        session.read_until("告警")
        session.resize(18)
        session.read_until("\x1b[2J")
        visible = session.screen(18).lines()
        self.assertEqual(sum(line.startswith("› ") for line in visible), 1)
        session.type("\r")
        self.assertTrue(self.wait_for(lambda: self.service.said))
        self.assertEqual(self.service.said, ["窄窗口中输入很长的中文消息" * 5])
        session.type("\x04")
        self.assertEqual(session.process.wait(timeout=3), 0)
        text = session.output.decode("utf-8", "replace")
        self.assertNotRegex(text, r"\x1b\[[0-9;]*m")

    def test_text_only_live_event_uses_read_only_database_metadata(self):
        db = sqlite3.connect(self.home / "router.db")
        self.addCleanup(db.close)
        db.executescript(LEGACY_SCHEMA)
        session = self.session()
        db.execute("INSERT INTO messages(uuid,role,kind,text,turn_id,status,created) "
                   "VALUES('q','event','worker.question','需要确认',5,'done',1)")
        db.execute("INSERT INTO outbox(channel,kind,text,turn_id,created) "
                   "VALUES('terminal','notice','请确认下一步。',5,2)")
        db.commit()
        self.service.push("请确认下一步。", kind=None)
        session.read_until("提问")
        visible = session.screen(70).lines()
        self.assertTrue(any("? 提问" in line for line in visible))
        self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='conversation'").fetchone())
        session.type("\x04")
        self.assertEqual(session.process.wait(timeout=3), 0)

    def test_stop_and_status_work_without_valid_profile_configuration(self):
        from super_super_harness.core.store import Store
        Store(self.home / "router.db").close()
        profile = self.home / "profiles.toml"
        profile.unlink()
        for contents in (None, 'secret-invalid-toml = = ='):
            if contents is not None:
                profile.write_text(contents)
            for command in ("status", "stop"):
                with self.subTest(config=contents, command=command):
                    result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", command],
                                            capture_output=True, text=True, cwd=ROOT,
                                            env=dict(os.environ, SSHARNESS_HOME=str(self.home)), timeout=5)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    self.assertNotIn("secret-invalid", result.stdout + result.stderr)
                    if command == "status":
                        self.assertIn("Router", result.stdout)
        self.assertEqual(self.service.methods, ["status", "stop", "status", "stop"])

    def test_serve_without_private_config_refuses_before_starting_or_writing(self):
        from contextlib import redirect_stderr
        from unittest.mock import AsyncMock, Mock
        from super_super_harness import daemon
        (self.home / "profiles.toml").unlink()
        service = Mock()
        service.return_value.run = AsyncMock()
        stderr = io.StringIO()
        with patch.dict(os.environ, SSHARNESS_HOME=str(self.home), SSHARNESS_NO_CAFFEINATE="1"), \
                patch("super_super_harness.daemon.Service", service), redirect_stderr(stderr):
            result = daemon.serve()
        self.assertEqual(result, 1)
        self.assertIn("init", stderr.getvalue())
        service.assert_not_called()
        self.assertFalse((self.home / "router.db").exists())
        self.assertFalse((self.home / "service.pid").exists())

    def test_missing_private_config_fails_closed_without_database_or_socket_access(self):
        database = self.home / "router.db"
        database.write_bytes(b"existence check only; not a database")
        before = database.read_bytes()
        (self.home / "profiles.toml").unlink()
        env = dict(os.environ, SSHARNESS_HOME=str(self.home), PYTHONPATH=str(ROOT))
        result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "chat"], input="never send this",
                                capture_output=True, text=True, cwd=ROOT, env=env, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("init --migrate-from", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("never send this", result.stdout + result.stderr)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(self.service.methods, [])
        self.assertFalse((self.home / "profiles.toml").exists())

    def test_plain_pipe_no_controls_and_final_unterminated_line_sent(self):
        env = dict(os.environ, SSHARNESS_HOME=str(self.home), PYTHONPATH=str(ROOT))
        result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "chat"], input="第一条\n第二条",
                                capture_output=True, text=True, cwd=ROOT, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.service.said, ["第一条", "第二条"])
        self.assertIn("test-model", result.stdout)
        self.assertIn("You", result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("\r", result.stdout)
        self.assertFalse((self.home / "router.db").exists())

    def test_disconnect_restores_terminal_and_returns_failure(self):
        session = self.session()
        self.service.subscriber.shutdown(socket.SHUT_RDWR)
        session.read_until("连接断开")
        self.assertEqual(session.process.wait(timeout=3), 1)
        restored = termios.tcgetattr(session.slave)
        # macOS sets PENDIN when echo is restored; this is a transient kernel flag.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        original = list(session.original)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
