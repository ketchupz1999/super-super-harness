"""Everything said to the user goes through here: written first, sent after, retried until delivered."""
import asyncio
import json
import time

from .notifications import worker_notice


class Terminal:
    """Connected chat clients. With none connected, messages wait for the next one."""

    name = "terminal"

    def __init__(self):
        self.clients = set()

    async def deliver(self, row):
        sent = False
        for writer in list(self.clients):
            try:
                writer.write((json.dumps({"event": "message", "kind": row["kind"], "text": row["text"]}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                sent = True
            except (ConnectionError, RuntimeError):
                self.clients.discard(writer)
        return sent


class Web:
    """The durable conversation is the web mailbox, even with no browser connected.

    SSE is only an invalidation hint. Browsers catch up from conversation IDs, not
    from a global sent flag, so one tab cannot consume another tab's messages.
    """
    name = "web"

    async def deliver(self, row):
        return True


class Outbox:
    def __init__(self, store):
        self.store = store
        self.channels = {"terminal": Terminal(), "web": Web()}
        self.lock = asyncio.Lock()
        self.closed = False

    def add(self, text, kind="answer", channels=None, turn_id=None):
        channels = list(self.channels) if channels is None else list(dict.fromkeys(channels))
        now, first = time.time(), None
        with self.store.atomic():
            display_kind, title = worker_notice(self.store.db, kind, turn_id)
            for name in channels:
                cursor = self.store.run("INSERT INTO outbox(channel, kind, text, turn_id, created) VALUES(?,?,?,?,?)", name, kind, text, turn_id, now)
                first = first or cursor.lastrowid
                if kind in ("notice", "question"):
                    # Freeze even an empty title: later replay must not rewrite this reply.
                    self.store.run("INSERT INTO worker_notices(outbox_id,kind,title) VALUES(?,?,?)",
                                   cursor.lastrowid, display_kind, title)
            if first:
                self.store.run("INSERT INTO conversation(role, source_id, channel, kind, text, turn_id, created) VALUES(?,?,?,?,?,?,?)",
                               "router", first, ",".join(channels), kind, text, turn_id, now)
        asyncio.create_task(self.flush())
        return first

    async def flush(self):
        async with self.lock:
            if self.closed:
                return
            for row in self.store.all("SELECT * FROM outbox WHERE status='pending' ORDER BY id"):
                channel = self.channels.get(row["channel"])
                if channel and await channel.deliver(row):
                    self.store.run("UPDATE outbox SET status='sent', sent=? WHERE id=?", time.time(), row["id"])
