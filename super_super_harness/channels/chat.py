"""A small terminal chat client. Database access is read-only; the service stays running."""
import codecs
from collections import deque
from dataclasses import dataclass
import json
import os
import queue
import re
import select
import shutil
import socket
import sqlite3
import sys
import termios
import threading
import time
import tty
import unicodedata

from .notifications import worker_notice


@dataclass
class Message:
    role: str
    text: str
    created: float = 0
    kind: str = "answer"
    key: tuple = ()
    title: str = ""


class History:
    """Read either conversation (web releases) or messages/outbox (stage-2-core)."""

    def __init__(self, path):
        self.db = None
        self.after = 0
        self.pending = set()
        try:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
            self.db.row_factory = sqlite3.Row
            rows = self.query("SELECT MAX(id) AS last FROM outbox WHERE channel='terminal'")
            self.after = (rows[0]["last"] or 0) if rows else 0
            self.pending = {r["id"] for r in self.query(
                "SELECT id FROM outbox WHERE channel='terminal' AND status='pending'")}
        except (OSError, sqlite3.Error):
            pass

    def query(self, sql, args=()):
        if self.db is None:
            return []
        try:
            return [dict(r) for r in self.db.execute(sql, args)]
        except sqlite3.Error:
            return []

    def close(self):
        if self.db is not None:
            self.db.close()

    @staticmethod
    def terminal(row):
        return "channel" not in row or "terminal" in row["channel"].split(",")

    def message(self, row, role, key):
        kind, title = row.get("kind") or "answer", ""
        if role == "router":
            kind, title = worker_notice(self.db, kind, row.get("turn_id"), key[1])
        return Message(role, str(row["text"]), row.get("created") or 0, kind, key, title)

    def recent(self, limit=8):
        columns = {r["name"] for r in self.query("PRAGMA table_info(conversation)")}
        items = []
        if {"id", "role", "text", "created"} <= columns:
            rows = self.query("SELECT * FROM conversation ORDER BY id DESC LIMIT ?", (limit * 4,))
            for row in rows:
                if row["role"] not in ("user", "router") or not self.terminal(row):
                    continue
                key = (row["role"], row.get("source_id", row["id"]))
                if row["role"] == "router":
                    # A conversation row can represent several outbox channels.
                    matches = self.query("SELECT * FROM outbox WHERE channel='terminal' AND text=? "
                                         "AND turn_id IS ? AND id>=? AND created=? ORDER BY id LIMIT 1",
                                         (row["text"], row.get("turn_id"), row.get("source_id", 0), row["created"]))
                    if matches:
                        key = ("router", matches[0]["id"])
                items.append(self.message(row, row["role"], key))
        else:
            for row in self.query("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit * 4,)):
                if row.get("role") == "user" and "text" in row and self.terminal(row):
                    items.append(self.message(row, "user", ("user", row["id"])))
            for row in self.query("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit * 4,)):
                if "text" in row and self.terminal(row):
                    items.append(self.message(row, "router", ("router", row["id"])))
        return sorted(items, key=lambda m: m.created)[-limit:]

    def incoming(self, event):
        text, kind = str(event.get("text", "")), event.get("kind")
        # Older services push only text. Match in delivery order, never by keywords.
        ids = sorted(self.pending)
        where = "id>?"
        args = [text, self.after]
        if ids:
            where += " OR id IN (%s)" % ",".join("?" for _ in ids)
            args.extend(ids)
        sql = "SELECT * FROM outbox WHERE channel='terminal' AND text=? AND (%s)" % where
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = self.query(sql + " ORDER BY id LIMIT 1", args)
        if rows:
            row = rows[0]
            self.after = max(self.after, row["id"])
            self.pending.discard(row["id"])
            return self.message(row, "router", ("router", row["id"]))
        return Message("router", text, time.time(), kind or "unknown")


class CommandHistory(History):
    """Strict offline reads for the history command; chat keeps its tolerant reader."""

    def __init__(self, path):
        self.db = None
        try:
            path.stat()
        except FileNotFoundError:
            return
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        self.db.row_factory = sqlite3.Row

    def query(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def columns(self, table, required):
        columns = {row["name"] for row in self.query("PRAGMA table_info(%s)" % table)}
        if not required <= columns:
            raise sqlite3.DatabaseError("unsupported history schema")
        return columns

    def rows(self, table, columns, where, limit):
        # Match terminal(row) exactly: channel is a comma-separated list, not a substring.
        if "channel" in columns:
            where += " AND instr(',' || channel || ',', ',terminal,') > 0"
        return self.query("SELECT * FROM %s WHERE %s ORDER BY id DESC LIMIT ?" % (table, where), (limit,))

    def recent(self, limit=8):
        if self.db is None:
            return []
        messages = self.columns("messages", {"id", "role", "text", "created"})
        outbox = self.columns("outbox", {"id", "text", "created"})
        conversation = {row["name"] for row in self.query("PRAGMA table_info(conversation)")}
        items = []
        if conversation:
            self.columns("conversation", {"id", "role", "text", "created"})
            for row in self.rows("conversation", conversation, "role IN ('user','router')", limit):
                key = (row["role"], row.get("source_id", row["id"]))
                if row["role"] == "router" and {"channel", "turn_id"} <= outbox:
                    matches = self.query("SELECT * FROM outbox WHERE channel='terminal' AND text=? "
                                         "AND turn_id IS ? AND id>=? AND created=? ORDER BY id LIMIT 1",
                                         (row["text"], row.get("turn_id"), row.get("source_id", 0), row["created"]))
                    if matches:
                        key = ("router", matches[0]["id"])
                items.append(self.message(row, row["role"], key))
        else:
            for row in self.rows("messages", messages, "role='user'", limit):
                items.append(self.message(row, "user", ("user", row["id"])))
            for row in self.rows("outbox", outbox, "1", limit):
                items.append(self.message(row, "router", ("router", row["id"])))
        return sorted(items, key=lambda message: message.created)[-limit:]


# Terminal escape sequences and controls in remote text must not reach the terminal.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


def clean(text):
    text = ESCAPES.sub("", str(text)).replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    return "".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cs")
                   and c not in "‪‫‬‭‮⁦⁧⁨⁩")


def width(text):
    return sum(0 if unicodedata.combining(c) or c in "‍️" else
               2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def wrap(text, columns):
    result = []
    for paragraph in clean(text).split("\n"):
        line = ""
        for char in paragraph:
            if line and width(line + char) > columns:
                result.append(line)
                line = ""
            line += char
        result.append(line)
    return result


STYLES = {
    "answer": ("Router", "36"),
    "question": ("? Router 提问", "35"),
    "fallback": ("Router", "36"),
    "notice": ("• 通知", "33"),
    "reminder": ("⏰ 提醒", "35"),
    "reminder.run": ("⏰ 定时任务", "35"),
    "worker.done": ("✓ 完成", "32"),
    "worker.question": ("? 提问", "35"),
    "worker.failed": ("! 告警", "31"),
    "worker.stalled": ("! 卡住", "33"),
    "alert": ("! 告警", "31"),
    "unknown": ("Router · 未分类", "33"),
}


class Display:
    def __init__(self, output, interactive, color):
        self.output, self.interactive, self.color = output, interactive, color
        self.active = False
        self.waiting = False
        self.hint = ""
        self.text = ""
        self.cursor = 0
        self.transcript = deque(maxlen=40)

    def columns(self):
        try:
            return max(4, os.get_terminal_size(self.output.fileno()).columns)
        except (OSError, AttributeError, ValueError):
            return shutil.get_terminal_size((80, 24)).columns

    def tint(self, text, color):
        return "\x1b[%sm%s\x1b[0m" % (color, text) if self.color else text

    def write(self, text):
        self.output.write(text)
        self.output.flush()

    def erase(self):
        if self.active:
            self.write("\r\x1b[2K\x1b[1A\r\x1b[2K")
            self.active = False

    def prompt(self):
        if not self.interactive:
            return
        # Keep editing on one physical line, even after a narrow-window resize.
        room = max(1, self.columns() - 3)
        start = self.cursor
        while start and width(self.text[start - 1:self.cursor]) <= room:
            start -= 1
        visible = ""
        for char in self.text[start:]:
            if width(visible + char) > room:
                break
            visible += char
        before = width(self.text[start:self.cursor])
        status = self.hint or ("· 等待 Router 回复…" if self.waiting else "")
        status = wrap(status, self.columns() - 1)[0]
        self.write(self.tint(status, "33" if self.hint else "2") + "\n" + self.tint("› ", "36") + visible + "\r")
        self.write("\x1b[%dC" % (2 + before))
        self.active = True

    def redraw(self):
        self.erase()
        self.prompt()

    def repaint(self):
        # A resize may reflow the old prompt into several rows. Rebuild the viewport
        # rather than guessing its previous cursor position and leaving input ghosts.
        self.active = False
        self.write("\x1b[2J\x1b[H")
        interactive, self.interactive = self.interactive, False
        try:
            for kind, item in self.transcript:
                if kind == "message":
                    self.message(item, remember=False)
                else:
                    self.note(item, remember=False)
        finally:
            self.interactive = interactive
        self.prompt()

    def note(self, text, remember=True):
        if remember and self.interactive:
            self.transcript.append(("note", text))
        self.erase()
        for line in wrap(text, max(1, self.columns() - 1)):
            self.write(self.tint(line, "2") + "\n")
        self.write("\n")
        self.prompt()

    def message(self, message, remember=True):
        if remember and self.interactive:
            self.transcript.append(("message", message))
        self.erase()
        if message.role == "user" and message.kind != "reminder.run":
            label, color = "You", "34"
        else:
            label, color = STYLES.get(message.kind, ("• 通知", "33"))
            label = message.title or label
        try:
            stamp = time.strftime("%H:%M", time.localtime(message.created)) if message.created else "--:--"
        except (ValueError, OverflowError, TypeError):
            stamp = "--:--"
        columns = max(1, self.columns() - 1)
        for line in wrap("╭─ %s  %s" % (label, stamp), columns):
            self.write(self.tint(line, color) + "\n")
        prefix = "│ " if columns >= 4 else ""
        for line in wrap(message.text, max(1, columns - width(prefix))):
            self.write(self.tint(prefix, color) + line + "\n")
        self.write(self.tint("╰" + "─" * min(22, columns - 1), color) + "\n\n")
        self.prompt()


EXIT_HINT = "再按一次 Ctrl-C 退出"
EXIT_WINDOW = 2.0
EXIT_COMMANDS = ("/exit", "/quit")


class Editor:
    """A UTF-8 line editor; the display owns the echo, not the terminal driver."""

    def __init__(self, display, clock=time.monotonic):
        self.display = display
        self.clock = clock
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.escape = ""
        self.paste = False
        self.skip_lf = False
        self.armed = None

    def disarm(self):
        self.armed = None
        self.display.hint = ""

    def expire(self):
        """Drop a stale Ctrl-C exit hint; True when the prompt needs a redraw."""
        if self.armed is None or self.clock() - self.armed < EXIT_WINDOW:
            return False
        self.disarm()
        return True

    def feed(self, data):
        submitted = []
        d = self.display
        for char in self.decoder.decode(data):
            if char == "\x03" and not d.text and not self.escape:
                # Empty input: the first Ctrl-C only warns; a second within the window exits.
                if self.armed is not None and self.clock() - self.armed < EXIT_WINDOW:
                    return submitted, True
                self.armed = self.clock()
                d.hint = EXIT_HINT
                continue
            self.disarm()
            if char == "\x1b" or self.escape:
                self.escape += char
                if self.escape in ("\x1b", "\x1b[", "\x1bO"):
                    continue
                if self.escape.startswith("\x1b[") and not ("@" <= char <= "~"):
                    continue
                key, self.escape = self.escape, ""
                if key in ("\x1b[D", "\x1bOD"):
                    d.cursor = max(0, d.cursor - 1)
                elif key in ("\x1b[C", "\x1bOC"):
                    d.cursor = min(len(d.text), d.cursor + 1)
                elif key in ("\x1b[H", "\x1bOH", "\x1b[1~"):
                    d.cursor = 0
                elif key in ("\x1b[F", "\x1bOF", "\x1b[4~"):
                    d.cursor = len(d.text)
                elif key == "\x1b[3~":
                    d.text = d.text[:d.cursor] + d.text[d.cursor + 1:]
                elif key == "\x1b[200~":
                    self.paste = True
                elif key == "\x1b[201~":
                    self.paste = False
                continue
            if self.paste and char in "\r\n":
                char = " "
            elif char in "\r\n":
                if char == "\n" and self.skip_lf:
                    self.skip_lf = False
                    continue
                self.skip_lf = char == "\r"
                text = d.text.strip()
                d.text, d.cursor = "", 0
                if text:
                    submitted.append(text)
                continue
            else:
                self.skip_lf = False
            if char == "\x04":
                if not d.text:
                    return submitted, True
                d.text = d.text[:d.cursor] + d.text[d.cursor + 1:]
            elif char == "\x03":
                d.text, d.cursor = "", 0
            elif char == "\x01":
                d.cursor = 0
            elif char == "\x05":
                d.cursor = len(d.text)
            elif char == "\x15":
                d.text, d.cursor = d.text[d.cursor:], 0
            elif char == "\x17":
                start = d.cursor
                while start and d.text[start - 1].isspace():
                    start -= 1
                while start and not d.text[start - 1].isspace():
                    start -= 1
                d.text, d.cursor = d.text[:start] + d.text[d.cursor:], start
            elif char in ("\x7f", "\b"):
                if d.cursor:
                    d.text = d.text[:d.cursor - 1] + d.text[d.cursor:]
                    d.cursor -= 1
            elif char.isprintable():
                d.text = d.text[:d.cursor] + char + d.text[d.cursor:]
                d.cursor += 1
        return submitted, False


def connection_info(cfg, info):
    router = info.get("router", {})
    profile = router.get("profile", "未知")
    try:
        model = cfg.profile(profile).model
    except (KeyError, ValueError, AttributeError):
        model = profile
    return "已连接 Router · %s · Worker %s/%s · Ctrl-C 两次或 Ctrl-D 退出，后台不受影响" % (
        router.get("model") or model, info.get("live", "?"), info.get("max_workers", "?"))


def close_client(client):
    if client is None:
        return
    try:
        client.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    client.reader.close()
    client.sock.close()


def run(cfg, client_factory, input_stream=None, output=None):
    input_stream, output = input_stream or sys.stdin, output or sys.stdout
    interactive = input_stream.isatty() and output.isatty() and os.environ.get("TERM") != "dumb"
    display = Display(output, interactive, output.isatty() and "NO_COLOR" not in os.environ
                      and os.environ.get("TERM") != "dumb")
    history = History(cfg.db)
    client = listener = None
    events, requests = queue.Queue(), queue.Queue()
    threads = []
    stopped = threading.Event()
    saved = None
    try:
        client = client_factory(cfg)
        client.sock.settimeout(5)
        info = client.call("status")
        listener = client_factory(cfg)
        listener.sock.settimeout(5)
        listener.call("subscribe")
        listener.sock.settimeout(None)
        display.note(connection_info(cfg, info))
        recent = history.recent()
        seen = {m.key for m in recent if m.key}
        if recent:
            display.note("最近 %d 条消息" % len(recent))
            for message in recent:
                display.message(message)

        def listen():
            try:
                for line in listener.reader:
                    event = json.loads(line)
                    if event.get("event") == "message":
                        events.put(("message", event))
            except (OSError, ValueError):
                pass
            if not stopped.is_set():
                events.put(("disconnect", None))

        def send():
            while not stopped.is_set():
                text = requests.get()
                if text is None:
                    return
                try:
                    client.call("say", text=text)
                    events.put(("sent", None))
                except (OSError, ValueError, SystemExit) as exc:
                    events.put(("error", str(exc)))
                    return

        for target in (listen, send):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            threads.append(thread)
        if interactive:
            saved = termios.tcgetattr(input_stream.fileno())
            tty.setraw(input_stream.fileno(), termios.TCSANOW)
            # Retain LF -> CRLF output processing while taking ownership of input echo.
            raw = termios.tcgetattr(input_stream.fileno())
            raw[1] = saved[1]
            termios.tcsetattr(input_stream.fileno(), termios.TCSANOW, raw)
            display.write("\x1b[?2004h")
            display.redraw()
        editor = Editor(display)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        eof = False
        outstanding = 0
        last_columns = display.columns()
        while True:
            while True:
                try:
                    kind, event = events.get_nowait()
                except queue.Empty:
                    break
                if kind in ("disconnect", "error"):
                    display.waiting = False
                    display.note("连接断开。" if kind == "disconnect" else "发送失败（未自动重试）：%s" % event)
                    return 1
                if kind == "sent":
                    outstanding -= 1
                    continue
                message = history.incoming(event)
                if message.kind in ("answer", "question", "fallback", "unknown"):
                    display.waiting = False
                if not message.key or message.key not in seen:
                    display.message(message)
                    if message.key:
                        seen.add(message.key)
                else:
                    display.redraw()
            if eof:
                # A pipe may close immediately after its last line: finish queued sends.
                if outstanding == 0:
                    return 0
                time.sleep(0.02)
                continue
            if interactive and display.columns() != last_columns:
                last_columns = display.columns()
                display.repaint()
            if editor.expire():
                display.redraw()
            ready, _, _ = select.select([input_stream], [], [], 0.05)
            if not ready:
                continue
            data = os.read(input_stream.fileno(), 4096)
            if not data:
                lines = [pending.strip()] if pending.strip() else []
                eof = True
            elif interactive:
                lines, eof = editor.feed(data)
            else:
                pending += decoder.decode(data)
                parts = pending.split("\n")
                pending = parts.pop()
                lines = [line.strip() for line in parts if line.strip()]
            for text in lines:
                if text in EXIT_COMMANDS:
                    # Leave like Ctrl-D: earlier lines still finish sending, later ones are dropped.
                    eof = True
                    break
                display.waiting = True
                display.message(Message("user", text, time.time()))
                outstanding += 1
                requests.put(text)
            if interactive:
                display.redraw()
    except (OSError, ValueError, SystemExit) as exc:
        display.note("无法连接 Router：%s" % exc)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        display.erase()
        if saved is not None:
            display.write("\x1b[?2004l")
            termios.tcsetattr(input_stream.fileno(), termios.TCSANOW, saved)
        stopped.set()
        requests.put(None)
        close_client(listener)
        for thread in threads:
            thread.join(timeout=0.3)
        close_client(client)
        history.close()


def cmd_chat(cfg, args):
    from ..cli import Client
    return run(cfg, Client)
