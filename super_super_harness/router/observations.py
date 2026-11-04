"""Durable observation attribution and read-only listing, shared by Router, CLI and web."""
import json
import re
import time

from . import tools
from ..core.store import OBSERVATION_MIXED_TURN

# Only this service's known Router MCP names are exempt, bare or stream-prefixed.
# Native reads, other MCP services and unknown names still belong in the audit.
AUDIT_EXEMPT_TOOLS = frozenset(name for spec in tools.ROUTER
                               for name in (spec["name"], "mcp__router__" + spec["name"]))
READ_TOOLS = frozenset({"event_read", "item_read", "item_list", "worker_read", "worker_detail", "worker_search",
                        "message_search", "memory_read", "reminder_list"})
DENIED = "Observation-only turn: only read-only MCP tools and observe_note are allowed; do not act or reply to the user."


def observe_only(db, turn_id=None):
    if turn_id is None:
        row = db.execute("SELECT id FROM turns WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return False
        turn_id = row[0]
    rows = db.execute("SELECT m.role, o.trigger_id FROM messages m LEFT JOIN observations o ON o.message_id=m.id "
                      "WHERE m.turn_id=? AND m.role!='system'", (turn_id,)).fetchall()
    return bool(rows) and all(row[1] is not None for row in rows)


def since_time(value, now=None):
    match = re.fullmatch(r"(\d+)([mhd])", value)
    if not match:
        raise ValueError("since must be a duration such as 30m, 12h or 1d")
    return (time.time() if now is None else now) - int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]


def listing(db, since=0, source=None, limit=200):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'").fetchone():
        return []
    # Offline readers must also work before the service migrates an older database.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(observations)")}
    mixed = "o.mixed_turn" if "mixed_turn" in columns else OBSERVATION_MIXED_TURN + " AS mixed_turn"
    rows = db.execute("SELECT t.id AS event_id, t.source, t.type, t.thread, t.text, t.url, o.status, o.would_do, o.route, o.created, o.noted, "
                      "o.used_builtin_tools, o.builtin_tools, " + mixed + " FROM observations o JOIN triggers t ON t.id=o.trigger_id "
                      "WHERE o.created>=? AND (? IS NULL OR t.source=?) ORDER BY o.created DESC, t.id DESC LIMIT ?",
                      (since, source, source, limit)).fetchall()
    return [dict(row, used_builtin_tools=bool(row["used_builtin_tools"]), builtin_tools=json.loads(row["builtin_tools"]),
                 mixed_turn=bool(row["mixed_turn"])) for row in rows]
