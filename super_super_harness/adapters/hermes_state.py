"""Read-only projection of Hermes state schema 32; unknown schemas fail closed."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import sqlite3

COLUMNS = {"id", "session_id", "role", "content", "timestamp", "tool_calls", "reasoning", "reasoning_content",
           "active", "compacted", "display_kind"}
PAYLOAD = ("content", "tool_calls", "reasoning", "reasoning_content")


@contextmanager
def _open(path):
    if not path or path.is_symlink():
        raise sqlite3.DatabaseError("No readable Hermes state")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] != 32:
            raise sqlite3.DatabaseError("Unknown Hermes schema")
        if not COLUMNS <= {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
            raise sqlite3.DatabaseError("Unknown Hermes message layout")
        yield db


def content(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.startswith("\x00json:"):
        try:
            return json.loads(value[6:])
        except ValueError:
            # A truncated structured block is not plain text (it may contain image data).
            return None
    return value


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(b["text"] for b in value if isinstance(b, dict) and
                         b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def has(path, hid, marker, *, strict=False):
    if not hid:
        return False
    try:
        with _open(path) as db:
            # Inactive compacted turns still prove prior delivery. Never match tools or summaries.
            return any(not marker or marker in text(content(row[0])) for row in db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC", (hid,)))
    except (OSError, sqlite3.Error):
        if strict:
            raise ValueError("Hermes saved state is unreadable or unsupported; refusing to discard the session") from None
        return False


def compacted(path, hid):
    try:
        with _open(path) as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND compacted=1", (hid,)).fetchone()[0]
    except (OSError, sqlite3.Error):
        return None


def unknown(path, hid):
    try:
        with _open(path) as db:
            return [row[0] for row in db.execute("SELECT admission_id FROM session_admissions "
                    "WHERE target_session_id=? AND status='unknown' ORDER BY seq", (hid,))
                    if isinstance(row[0], str) and row[0] and not any(c.isspace() for c in row[0])]
    except (OSError, sqlite3.Error):
        return []


def _json(value, default):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _items(row):
    stamp = row["timestamp"]
    if isinstance(stamp, (float, int)):
        try:
            stamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            stamp = ""
    stamp = stamp if isinstance(stamp, str) else ""
    value = content(row["content"])
    body = text(value)
    role = row["role"]
    items = []
    if role == "assistant":
        thinking = row["reasoning_content"] or row["reasoning"]
        if thinking:
            items.append({"time": stamp, "kind": "thinking", "text": content(thinking)})
    if body and role in ("user", "assistant", "tool"):
        item = {"time": stamp, "kind": {"user": "received", "assistant": "said", "tool": "tool_result"}[role], "text": body}
        if role == "tool":
            # Hermes has no dedicated error column in this schema.
            item["error"] = False
        items.append(item)
    if role == "assistant":
        calls = _json(row["tool_calls"], None)
        if calls is None and row["tool_calls"] and row["clipped"]:
            items.append({"time": stamp, "kind": "tool_call", "tool": "", "text": content(row["tool_calls"])})
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            args = function.get("arguments", {})
            items.append({"time": stamp, "kind": "tool_call", "tool": function.get("name", ""),
                          "text": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})
    return items


def transcript(path, hid, *, max_bytes=None, limit=None, text_limit=None):
    """Read newest rows first, bound SQL payloads before decoding, return oldest first.

    max_bytes bounds retained text bytes and each fetched field; SQLite can still
    read whole storage pages. A bounded read never fetches an unlimited TEXT cell.
    """
    if not hid or any(value is not None and value <= 0 for value in (max_bytes, limit)):
        return []
    remaining = read_remaining = max_bytes
    # Empty/image-only rows produce no items; allow bounded look-behind past them.
    row_limit = max(128, limit * 8) if limit is not None else max_bytes
    if max_bytes is not None and row_limit is not None:
        row_limit = min(row_limit, max_bytes)
    # BLOB substr matters: SQLite TEXT substr stops at the NUL in Hermes's JSON sentinel.
    fields = ["substr(CAST(%s AS BLOB),1,?) AS %s" % (name, name) if max_bytes is not None else name for name in PAYLOAD]
    clipped = " OR ".join("length(CAST(%s AS BLOB))>%d" % (name, max_bytes) for name in PAYLOAD) if max_bytes is not None else "0"
    sql = "SELECT timestamp,role,(" + clipped + ") AS clipped," + ",".join(fields) + " FROM messages WHERE session_id=? "
    sql += "AND (active=1 OR compacted=1) AND COALESCE(display_kind,'') NOT IN ('hidden','model_only') "
    sql += "AND role IN ('user','assistant','tool') ORDER BY id DESC"
    params = [max_bytes] * len(PAYLOAD) if max_bytes is not None else []
    params.append(hid)
    if row_limit is not None:
        sql += " LIMIT ?"
        params.append(row_limit)
    newest = []
    try:
        with _open(path) as db:
            for row in db.execute(sql, params):
                if read_remaining is not None:
                    read_remaining -= sum(len(row[name] or b"") for name in PAYLOAD)
                for item in reversed(_items(row)):
                    value = item["text"]
                    if row["clipped"]:
                        item["truncated"] = True
                    if not isinstance(value, str):
                        continue
                    if text_limit is not None and len(value) > text_limit:
                        value = value[:max(0, text_limit)] + "\n… [truncated]"
                        item["truncated"] = True
                    if remaining is not None:
                        raw = value.encode("utf-8")
                        if len(raw) > remaining:
                            value = raw[:remaining].decode("utf-8", errors="ignore")
                            item["truncated"] = True
                        remaining -= len(value.encode("utf-8"))
                    item["text"] = value
                    newest.append(item)
                    if (limit is not None and len(newest) >= limit) or (remaining is not None and remaining <= 0):
                        return list(reversed(newest))
                if read_remaining is not None and (read_remaining <= 0 or remaining < 4):
                    break
    except (OSError, sqlite3.Error):
        return []
    return list(reversed(newest))
