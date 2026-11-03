"""Reply correlation for acceptance tests, importable without starting e2e.py."""


class MessageStamp(float):
    def __new__(cls, stamp, message_id):
        value = super().__new__(cls, stamp)
        value.message_id = message_id
        return value


def reply_after(box, stamp, timeout=120, final=False):
    def replies():
        rows = box.rows("SELECT o.* FROM outbox o JOIN turns t ON t.id=o.turn_id "
                        "WHERE o.turn_id=(SELECT turn_id FROM messages WHERE id=?) AND o.created>=? "
                        "AND o.kind IN ('answer','fallback','question') "
                        + ("AND t.status='done' " if final else "") + "ORDER BY o.id",
                        stamp.message_id, float(stamp))
        return rows
    rows = box.wait(replies, timeout, "final reply" if final else "reply")
    row = rows[-1] if rows and final else rows[0] if rows else None
    return (row["created"] - stamp, row["text"]) if row else (None, "")
