"""Read-only Worker attribution shared by terminal history/live messages and web."""
import sqlite3

WORKER_LABELS = {"worker.failed": "! 告警", "worker.question": "? 提问",
                 "worker.stalled": "! 卡住", "worker.done": "✓"}


def worker_notice(db, kind, turn_id, outbox_id=None):
    if db is None or turn_id is None or kind not in ("notice", "question"):
        return kind, ""
    if outbox_id is not None:
        try:
            saved = db.execute("SELECT kind,title FROM worker_notices WHERE outbox_id=?", (outbox_id,)).fetchone()
        except sqlite3.Error:
            saved = None  # Legacy databases have no per-reply snapshots.
        if saved is not None:
            return saved[0], saved[1]
    try:
        # Written events can move to the next turn. Attribute only read events here,
        # so a live title cannot claim results that disappear from its saved history.
        events = db.execute("SELECT m.id, m.kind, m.task_id, t.seq, t.code_name, t.name FROM messages m "
                            "LEFT JOIN tasks t ON t.id=m.task_id WHERE m.role='event' AND m.turn_id=? "
                            "AND m.status IN ('confirmed','done') ORDER BY m.id", (turn_id,)).fetchall()
    except sqlite3.Error:
        # Old/corrupt/missing tables must not prevent displaying the message.
        return kind, ""
    latest = {}
    for row in events:
        if row[1] in WORKER_LABELS:
            latest[row[2] or ("event", row[0])] = row
    worker_kind = next((candidate for candidate in WORKER_LABELS
                        if any(row[1] == candidate for row in latest.values())), None)
    if not worker_kind:
        return kind, ""
    priorities = list(WORKER_LABELS)
    tasks = sorted((row for row in latest.values() if row[4] is not None),
                   key=lambda row: (priorities.index(row[1]), row[3]))
    original_kind = kind
    kind = worker_kind if kind == "notice" else kind
    if not tasks:
        return kind, ""
    groups = {}
    for row in tasks[:3]:
        label = "? 提问" if original_kind == "question" else WORKER_LABELS[row[1]]
        name = "%s · %s%s" % (row[4], row[5][:80], "…" if len(row[5]) > 80 else "")
        groups.setdefault(label, []).append(name)
    title = "；".join(label + " " + "、".join(names) for label, names in groups.items())
    suffix = " 等 %d 个 Worker" % len(tasks) if len(tasks) > 3 else ""
    return kind, title + suffix
