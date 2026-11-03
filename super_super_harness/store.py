"""SQLite store. The database is the source of truth; notifications are only reminders."""
from contextlib import contextmanager
import json
import sqlite3
import time

from .worker_names import named_task

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT NOT NULL,
  role TEXT NOT NULL,            -- user | event | system
  channel TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  task_id TEXT,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued | written | confirmed | done
  turn_id INTEGER,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS messages_turn ON messages(turn_id);

CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'open',    -- open | done
  sources TEXT NOT NULL DEFAULT '[]',
  channels TEXT NOT NULL DEFAULT '[]',
  replied INTEGER NOT NULL DEFAULT 0,
  started REAL NOT NULL,
  ended REAL
);

CREATE TABLE IF NOT EXISTS effects (
  turn_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  tool TEXT NOT NULL,
  result TEXT NOT NULL,
  created REAL NOT NULL,
  PRIMARY KEY (turn_id, key)
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  turn_id INTEGER,
  created REAL NOT NULL,
  sent REAL
);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status);

CREATE TABLE IF NOT EXISTS tasks (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE,
  name TEXT NOT NULL,
  repo TEXT,
  profile TEXT NOT NULL,
  status TEXT NOT NULL,          -- queued | running | waiting | interrupted | completed | failed | stopped
  workspace TEXT,
  cwd TEXT,
  branch TEXT,
  session_id TEXT NOT NULL,
  started_once INTEGER NOT NULL DEFAULT 0,
  brief TEXT NOT NULL,
  progress TEXT NOT NULL DEFAULT '',
  question TEXT NOT NULL DEFAULT '',
  report TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  pending TEXT NOT NULL DEFAULT '[]',
  origin_channel TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  updated REAL NOT NULL,
  ended REAL
);

CREATE TABLE IF NOT EXISTS worker_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  session_id TEXT NOT NULL,
  pid INTEGER,
  pid_started TEXT,
  started REAL NOT NULL,
  ended REAL,
  exit_code INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'info',
  data TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS events_type ON events(type, id);

CREATE TABLE IF NOT EXISTS worker_notices (
  outbox_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  turn_id INTEGER,
  created REAL NOT NULL,
  UNIQUE(role, source_id)
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  text TEXT NOT NULL,
  kind TEXT NOT NULL,
  cron TEXT,
  next_at REAL,
  channel TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, next_at);
CREATE TABLE IF NOT EXISTS reminder_fires (
  reminder_id INTEGER NOT NULL,
  scheduled REAL NOT NULL,
  created REAL NOT NULL,
  PRIMARY KEY (reminder_id, scheduled)
);
-- Emitted events in full and where they went. Append-only; the events table stays the internal log.
CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  event_id TEXT NOT NULL,
  type TEXT NOT NULL,
  thread TEXT NOT NULL,
  severity TEXT NOT NULL,
  text TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}',
  rule TEXT NOT NULL,
  route TEXT NOT NULL,           -- drop | record | direct | wake
  created REAL NOT NULL,
  UNIQUE(source, event_id)
);
-- Items and what hangs off them. New tables only, so an older release still opens this database.
CREATE TABLE IF NOT EXISTS items (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- delivery | standing
  status TEXT NOT NULL,          -- active | confirm | delivered | parked | closed
  permission TEXT NOT NULL,      -- read | act
  progress TEXT NOT NULL DEFAULT '',
  next TEXT NOT NULL DEFAULT '',
  follow_up TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD local date or ''
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS item_tasks (task_id TEXT PRIMARY KEY, item TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS item_tasks_item ON item_tasks(item);
CREATE TABLE IF NOT EXISTS subscriptions (thread TEXT NOT NULL, task_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (thread, task_id));
CREATE TABLE IF NOT EXISTS deliveries (trigger_id INTEGER NOT NULL, task_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (trigger_id, task_id));
-- satisfied is set once, when the upstream completes; later work on the upstream does not block again.
CREATE TABLE IF NOT EXISTS task_deps (task_id TEXT NOT NULL, upstream TEXT NOT NULL, satisfied REAL, PRIMARY KEY (task_id, upstream));
CREATE INDEX IF NOT EXISTS task_deps_upstream ON task_deps(upstream);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,                  -- a Worker's question, or NULL for the Router's
  outbox_id INTEGER,             -- the Router's question as sent
  question TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]',
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_task ON questions(task_id, id);
CREATE INDEX IF NOT EXISTS questions_outbox ON questions(outbox_id);
CREATE TABLE IF NOT EXISTS web_requests (key TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate_web()

    def _migrate_web(self):
        for table, fields in {"tasks": {"state_since": "REAL", "started": "REAL", "base_commit": "TEXT"},
                              "events": {"turn_id": "INTEGER"}}.items():
            columns = {row["name"] for row in self.all("PRAGMA table_info(%s)" % table)}
            for name, kind in fields.items():
                if name not in columns:
                    self.run("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
        self.run("CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id, source, id)")
        self.run("UPDATE tasks SET state_since=updated WHERE state_since IS NULL")
        self.run("UPDATE tasks SET started=COALESCE((SELECT MIN(started) FROM worker_runs WHERE task_id=tasks.id), created)"
                 " WHERE started IS NULL AND started_once=1")
        if self.get("web_history_migrated"):
            return
        self.run("UPDATE events SET turn_id=(SELECT id FROM turns WHERE started<=events.created "
                 "AND (ended>=events.created OR ended IS NULL) ORDER BY id DESC LIMIT 1) "
                 "WHERE source='router' AND turn_id IS NULL")
        # Old outbox entries were one row per channel. Coalesce replies in the same turn.
        self.run("INSERT OR IGNORE INTO conversation(role, source_id, channel, kind, text, turn_id, created) "
                 "SELECT role, source_id, channel, kind, text, turn_id, created FROM ("
                 "SELECT 'user' role, id source_id, channel, kind, text, turn_id, created FROM messages WHERE role='user' "
                 "UNION ALL SELECT 'router', MIN(id), group_concat(channel), kind, text, turn_id, MIN(created) FROM outbox "
                 "GROUP BY COALESCE(turn_id, -id), kind, text) ORDER BY created, source_id")
        self.set("web_history_migrated", "1")

    @contextmanager
    def atomic(self):
        # Savepoints also work when a caller groups several store operations.
        self.run("SAVEPOINT web_write")
        try:
            yield
            self.run("RELEASE SAVEPOINT web_write")
        except BaseException:
            self.run("ROLLBACK TO SAVEPOINT web_write")
            self.run("RELEASE SAVEPOINT web_write")
            raise

    def close(self):
        self.db.close()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def run(self, sql, *args):
        return self.db.execute(sql, args)

    # key-value state
    def get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", key)
        return row["value"] if row else default

    def set(self, key, value):
        self.run("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, str(value))

    def unset(self, key):
        self.run("DELETE FROM kv WHERE key=?", key)

    # events
    def event(self, source, type, task_id=None, subject="", severity="info", **data):
        turn = self.current_turn() if source == "router" else None
        cursor = self.run("INSERT INTO events(task_id, source, type, subject, severity, data, created, turn_id) VALUES(?,?,?,?,?,?,?,?)",
                          task_id, source, type, subject, severity, json.dumps(data, ensure_ascii=False), time.time(), turn["id"] if turn else None)
        return cursor.lastrowid

    # tasks
    def task(self, task_id):
        row = self.one("SELECT * FROM tasks WHERE id=?", task_id)
        return named_task(row) if row else None

    def update_task(self, task_id, **fields):
        fields["updated"] = time.time()
        if "status" in fields:
            task = self.task(task_id)
            if task and task["status"] != fields["status"]:
                fields["state_since"] = fields["updated"]
                if fields["status"] in ("queued", "running"):
                    fields.setdefault("ended", None)
                if fields["status"] == "running" and task["started"] is None:
                    fields["started"] = fields["updated"]
        names = ", ".join("%s=?" % name for name in fields)
        self.run("UPDATE tasks SET %s WHERE id=?" % names, *fields.values(), task_id)

    def add_task(self, name, brief, profile, repo, session_id, reason, origin_channel):
        now = time.time()
        cursor = self.run("INSERT INTO tasks(name, brief, profile, repo, status, session_id, reason, origin_channel, created, updated)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?)", name, brief, profile, repo, "queued", session_id, reason, origin_channel, now, now)
        task_id = "t%d" % cursor.lastrowid
        self.run("UPDATE tasks SET id=?, state_since=? WHERE seq=?", task_id, now, cursor.lastrowid)
        return task_id

    # messages
    def add_message(self, uuid, role, text, channel="", kind="", task_id=None):
        with self.atomic():
            cursor = self.run("INSERT INTO messages(uuid, role, text, channel, kind, task_id, created) VALUES(?,?,?,?,?,?,?)",
                              uuid, role, text, channel, kind, task_id, time.time())
            if role == "user":
                self.run("INSERT INTO conversation(role, source_id, channel, kind, text, created) "
                         "SELECT role, id, channel, kind, text, created FROM messages WHERE id=?", cursor.lastrowid)
            return cursor.lastrowid

    def messages(self, status):
        return self.all("SELECT * FROM messages WHERE status=? ORDER BY id", status)

    # turns
    def open_turn(self):
        cursor = self.run("INSERT INTO turns(started) VALUES(?)", time.time())
        return cursor.lastrowid

    def current_turn(self):
        return self.one("SELECT * FROM turns WHERE status='open' ORDER BY id DESC LIMIT 1")

    def effect(self, turn_id, key):
        row = self.one("SELECT result FROM effects WHERE turn_id=? AND key=?", turn_id, key)
        return json.loads(row["result"]) if row else None

    def add_effect(self, turn_id, key, tool, result):
        self.run("INSERT OR IGNORE INTO effects(turn_id, key, tool, result, created) VALUES(?,?,?,?,?)",
                 turn_id, key, tool, json.dumps(result, ensure_ascii=False), time.time())
