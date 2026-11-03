"""Items: the matters Workers are attached to. A delivery item gets delivered; a standing one lives until closed by hand."""
from datetime import date
from html import escape
import re
import time
import uuid as uuidlib

from .private_files import atomic_write
from .router_files import DocumentError, ItemNotes
from .worker_names import named_task, worker_name

KINDS = {"delivery": "交付型", "standing": "常设型"}
STATUSES = {"active": "进行中", "confirm": "待你确认", "delivered": "已交付", "parked": "搁置", "closed": "已关闭"}
OPEN = ("active", "confirm", "parked")
PERMISSIONS = {"read": "只查询分析", "act": "可执行"}
CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
CODE_LIMIT = 24
LIMITS = {"name": 80, "progress": 2000, "next": 2000, "reason": 500}
PROGRESS_FROM_REPORT = 300
TABLE_ROWS = 20
WORKERS_PER_ROW = 6


def today():
    return date.today().isoformat()


def clip(text, size):
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[:size - 1] + "…"


def data(text, size):
    """One line of stored text, safe inside the Router's tagged input."""
    return escape(clip(text, size), quote=False)


def check_text(field, value, required=False):
    if not isinstance(value, str) or len(value) > LIMITS[field] or (required and not value.strip()):
        raise ValueError("item %s must be %sa string of at most %d characters" % (field, "a nonempty " if required else "", LIMITS[field]))
    return value.strip()


def check_follow_up(value):
    if not isinstance(value, str):
        raise ValueError("follow_up must be a YYYY-MM-DD date or empty")
    if value:
        try:
            if len(value) != 10:
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("follow_up must be a YYYY-MM-DD date or empty") from None
    return value


def overdue(item, now=None):
    return bool(item["follow_up"]) and item["status"] in OPEN and item["follow_up"] < (now or today())


def brief_header(item):
    """What the Worker must know about its item before the brief. Permission is stated here, not left to each brief."""
    if item["permission"] == "read":
        rule = ("Permission: read only (只读). Query and analyze; do not change code, commit, send anything out "
                "or take any other action with side effects.")
    else:
        rule = "Permission: act (可执行). You may carry out the work."
    return ("[Item %s「%s」, %s]\n%s\nRead the item notes with item_read; update notes and next with item_update.\n\n"
            % (item["code"], clip(item["name"], LIMITS["name"]), item["kind"], rule))


class Items:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    # notes
    def _notes(self, code):
        root = self.cfg.home / "items"
        root.mkdir(mode=0o700, exist_ok=True)
        (root / code).mkdir(mode=0o700, exist_ok=True)
        return ItemNotes(root / code)

    def read_notes(self, code):
        try:
            return self._notes(code).read("notes")
        except (DocumentError, OSError):
            return {"content": "", "version": "missing", "exists": False}

    def write_notes(self, code, content, version):
        if not isinstance(version, str) or not version:
            raise ValueError("notes need notes_version from item_read")
        try:
            return self._notes(code).write("notes", content, version)
        except DocumentError as exc:
            raise ValueError(str(exc)) from None

    # records
    def get(self, code, require_open=False):
        row = self.store.one("SELECT * FROM items WHERE code=?", code) if isinstance(code, str) else None
        if not row:
            raise ValueError("no item %s" % code)
        if require_open and row["status"] == "closed":
            raise ValueError("item %s is closed" % code)
        return row

    def create(self, code, name, kind, permission, next="", follow_up=""):
        if not isinstance(code, str) or len(code) > CODE_LIMIT or not CODE.fullmatch(code):
            raise ValueError("item code must be lowercase English letters and digits joined by single '-', starting with a letter, "
                             "at most %d characters; a sequence number is added" % CODE_LIMIT)
        name = check_text("name", name, required=True)
        if kind not in KINDS:
            raise ValueError("item kind must be delivery or standing")
        if permission not in PERMISSIONS:
            raise ValueError("item permission must be read or act")
        next, follow_up = check_text("next", next), check_follow_up(follow_up)
        now = time.time()
        with self.store.atomic():
            cursor = self.store.run("INSERT INTO items(code, name, kind, status, permission, next, follow_up, created, updated)"
                                    " VALUES(?,?,?,?,?,?,?,?,?)", uuidlib.uuid4().hex, name, kind, "active", permission, next, follow_up, now, now)
            full = "%s-%d" % (code, cursor.lastrowid)
            self.store.run("UPDATE items SET code=? WHERE seq=?", full, cursor.lastrowid)
            path = self._notes(full).directory / "notes.md"
            if not path.exists():
                atomic_write(path, "# %s\n" % name)
            self.store.event("router", "item.created", subject=full, kind=kind, permission=permission)
        return self.read(full)

    def update(self, code, status=None, progress=None, next=None, follow_up=None, name=None, permission=None,
               notes=None, notes_version=None):
        item = self.get(code)
        fields = {}
        if status is not None:
            if status not in STATUSES:
                raise ValueError("item status must be one of %s" % ", ".join(STATUSES))
            if status == "delivered" and item["kind"] == "standing":
                raise ValueError("a standing item is never delivered; close it instead")
            fields["status"] = status
        for field, value in (("progress", progress), ("next", next), ("name", name)):
            if value is not None:
                fields[field] = check_text(field, value, required=field == "name")
        if follow_up is not None:
            fields["follow_up"] = check_follow_up(follow_up)
        if permission is not None:
            if permission not in PERMISSIONS:
                raise ValueError("item permission must be read or act")
            fields["permission"] = permission
        if notes is not None:
            self.write_notes(item["code"], notes, notes_version)
        if fields:
            fields["updated"] = time.time()
            with self.store.atomic():
                self.store.run("UPDATE items SET %s WHERE code=?" % ", ".join("%s=?" % key for key in fields), *fields.values(), item["code"])
                if fields.get("status") == "closed":
                    self._unsubscribe(item["code"])
            self.store.event("router", "item.updated", subject=item["code"], fields=sorted(fields))
        elif notes is not None:
            self.store.event("router", "item.updated", subject=item["code"], fields=["notes"])
        return self.read(item["code"])

    def _unsubscribe(self, code):
        # Its Workers stay attached for the record, but no longer receive the item's events.
        self.store.run("DELETE FROM subscriptions WHERE thread=?", "item:" + code)

    def close(self, code, reason=""):
        item = self.get(code)
        reason = check_text("reason", reason)
        progress = "%s 关闭：%s" % (today(), reason) if reason else item["progress"]
        with self.store.atomic():
            self.store.run("UPDATE items SET status='closed', progress=?, updated=? WHERE code=?", progress, time.time(), item["code"])
            self._unsubscribe(item["code"])
        self.store.event("router", "item.closed", subject=item["code"], reason=reason)
        return self.read(item["code"])

    def describe(self, item):
        return dict(item) | {"overdue": overdue(item)}

    def read(self, code):
        item = self.describe(self.get(code))
        item["tasks"] = [named_task(row) for row in self.store.all(
            "SELECT t.seq, t.id, t.name, t.status, t.branch FROM item_tasks i JOIN tasks t ON t.id=i.task_id WHERE i.item=? ORDER BY t.seq", code)]
        notes = self.read_notes(code)
        item["notes"], item["notes_version"] = notes["content"], notes["version"]
        return item

    def list(self, query=None, include_closed=False, limit=50):
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string")
        sql, args = "SELECT * FROM items WHERE 1", []
        if not include_closed:
            sql += " AND status!='closed'"
        if query:
            like = "%" + query + "%"
            sql += " AND (code LIKE ? OR name LIKE ? OR progress LIKE ? OR next LIKE ?)"
            args += [like] * 4
        rows = self.store.all(sql + " ORDER BY status IN ('delivered','closed'), updated DESC LIMIT ?", *args, limit)
        return [self.describe(row) for row in rows]

    # Workers
    def attach(self, code, task_id):
        now = time.time()
        self.store.run("INSERT INTO item_tasks(task_id, item, created) VALUES(?,?,?)", task_id, code, now)
        self.store.run("INSERT OR IGNORE INTO subscriptions(thread, task_id, created) VALUES(?,?,?)", "item:" + code, task_id, now)

    def attach_existing(self, code, task_id, move=False):
        """The Router hangs a task that already exists on an item. One on another item moves only with move=True."""
        item = self.get(code, require_open=True)
        task = self.store.task(task_id) if isinstance(task_id, str) else None
        if not task:
            raise ValueError("no task %s" % task_id)
        current = self.of(task_id)
        if current == item["code"]:
            return {"ok": True, "item": current, "task_id": task_id, "note": "already attached"}
        if current and move is not True:
            old = self.get(current)
            return {"ok": False, "attached_to": current, "error": "%s is attached to item %s「%s」. Tell the user, then pass move=true "
                    "to move it to %s." % (task_id, current, clip(old["name"], LIMITS["name"]), item["code"])}
        with self.store.atomic():
            if current:
                self.store.run("DELETE FROM item_tasks WHERE task_id=?", task_id)
                self.store.run("DELETE FROM subscriptions WHERE thread=? AND task_id=?", "item:" + current, task_id)
            self.attach(item["code"], task_id)
            self.store.event("router", "item.attached", task_id, subject=item["code"], **({"moved_from": current} if current else {}))
        return {"ok": True, "item": item["code"], "task_id": task_id, **({"moved_from": current} if current else {})}

    def of(self, task_id):
        row = self.store.one("SELECT item FROM item_tasks WHERE task_id=?", task_id)
        return row["item"] if row else None

    def record_done(self, task_id, report):
        """A Worker's conclusion becomes the item's progress, with the date and the Worker it came from."""
        code = self.of(task_id)
        if not code:
            return
        task = self.store.task(task_id)
        progress = "%s %s %s：%s" % (today(), task_id, clip(task["name"], 40), clip(report, PROGRESS_FROM_REPORT))
        self.store.run("UPDATE items SET progress=?, updated=? WHERE code=?", progress, time.time(), code)

    # the Router's view
    def table(self, labels):
        """Active items for the Router's <tasks> block. Every stored field is data and is escaped."""
        now = today()
        rows = self.store.all("SELECT * FROM items WHERE status IN ('active','confirm','parked') "
                              "ORDER BY status='parked', (follow_up!='' AND follow_up<?) DESC, updated DESC", now)
        if not rows:
            return []
        lines = ["事项（%d 个未关闭）：" % len(rows)]
        for item in rows[:TABLE_ROWS]:
            parts = ["%s %s" % (item["code"], data(item["name"], LIMITS["name"])), KINDS[item["kind"]], STATUSES[item["status"]],
                     "权限 " + PERMISSIONS[item["permission"]]]
            if item["progress"]:
                parts.append("进展：" + data(item["progress"], 160))
            if item["next"]:
                parts.append("接下来：" + data(item["next"], 120))
            if item["follow_up"]:
                parts.append("跟进 %s%s" % (item["follow_up"], " 已过期" if overdue(item, now) else ""))
            workers = self.store.all("SELECT t.seq, t.id, t.name, t.status FROM item_tasks i JOIN tasks t ON t.id=i.task_id "
                                     "WHERE i.item=? AND t.status!='stopped' ORDER BY t.seq DESC LIMIT ?", item["code"], WORKERS_PER_ROW + 1)
            if workers:
                shown = "、".join("%s %s · %s（%s）" % (row["id"], data(worker_name(row["seq"]), 80), data(row["name"], 40), labels[row["status"]])
                                 for row in reversed(workers[:WORKERS_PER_ROW]))
                parts.append("Worker：" + shown + ("，还有更早的" if len(workers) > WORKERS_PER_ROW else ""))
            lines.append(" | ".join(parts))
        if len(rows) > TABLE_ROWS:
            lines.append("另有 %d 个事项没有列出，用 item_list 查。" % (len(rows) - TABLE_ROWS))
        return lines
