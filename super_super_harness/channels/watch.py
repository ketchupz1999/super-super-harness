"""Owner-only, non-consuming Worker observations over the control socket."""
import asyncio
import json


BUSY = {"running", "queued", "waiting_deps"}
ACTIVE = BUSY | {"waiting"}
BUFFER_LIMIT = 1024 * 1024


class Watch:
    def __init__(self, store, tasks, items):
        self.store, self.tasks, self.items = store, tasks, items
        self.clients = {}
        store.observers.append(self.changed)

    def question_options(self, task_id, question):
        row = self.store.one("SELECT options FROM questions WHERE task_id IS ? AND question=? ORDER BY id DESC LIMIT 1",
                             task_id, question)
        return json.loads(row["options"]) if row else []

    def worker(self, task):
        result = {"task_id": task["id"], "worker": task["code_name"], "status": task["status"],
                  "report": task["report"][:500]}
        if task["status"] == "waiting":
            result.update(question=task["question"], options=self.question_options(task["id"], task["question"]))
        return result

    def subscribe(self, writer, workers=None, item=None, router=False):
        if not isinstance(workers, (list, type(None))) or not isinstance(router, bool):
            raise ValueError("invalid watch parameters")
        selected = [self.tasks.resolve(w) for w in workers] if workers else [
            self.store.task(row["id"]) for row in self.store.all("SELECT id FROM tasks ORDER BY seq")
            if self.store.task(row["id"])["status"] in ACTIVE]
        if item is not None:
            self.items.get(item)
            ids = {row["task_id"] for row in self.store.all("SELECT task_id FROM item_tasks WHERE item=?", item)}
            selected = [task for task in selected if task["id"] in ids]
        selected = {task["id"]: task for task in selected}
        question = self.store.get("router_waiting") if router else None
        snapshot = {"event": "snapshot", "workers": [self.worker(task) for task in selected.values()],
                    "router": {"type": "router.ask_user", "question": question,
                               "options": self.question_options(None, question)} if question else None,
                    "include_router": router}
        size = len((json.dumps({"result": snapshot}, ensure_ascii=False) + "\n").encode())
        if writer.transport.get_write_buffer_size() + size > BUFFER_LIMIT:
            raise ValueError("watch snapshot exceeds buffer limit; select fewer Workers")
        self.remove(writer)
        self.clients[writer] = {"ids": set(selected), "router": router, "pending": [], "size": 0, "handle": None}
        return snapshot

    def remove(self, writer):
        client = self.clients.pop(writer, None)
        if client and client["handle"]:
            client["handle"].cancel()

    def changed(self, change):
        if not self.clients:
            return
        kind = change["type"]
        if kind == "worker.status":
            record = dict(self.worker(change["task"]), type=kind)
        elif kind in ("report.done", "ask_user", "worker.handoff", "worker.completion_deferred") and change.get("task_id"):
            task = self.store.task(change["task_id"])
            if not task:
                return
            data = change["data"]
            record = {"type": kind, "task_id": task["id"], "worker": task["code_name"]}
            if kind == "report.done":
                record["report"] = data.get("text", "")[:500]
            elif kind == "ask_user":
                record.update(question=data.get("text", ""), options=data.get("options", []))
            else:
                record["text"] = data.get("text", "")
        elif kind == "router.ask_user":
            record = dict(change["data"], type=kind)
        else:
            return
        size = len(json.dumps(record, ensure_ascii=False).encode())
        for writer, client in list(self.clients.items()):
            if record.get("task_id") not in client["ids"] and not (kind == "router.ask_user" and client["router"]):
                continue
            if client["size"] + size > BUFFER_LIMIT:
                self.drop(writer)
                continue
            client["pending"].append(record)
            client["size"] += size
            if client["handle"] is None:
                client["handle"] = asyncio.get_running_loop().call_soon(self.flush, writer)

    def drop(self, writer):
        self.remove(writer)
        writer.transport.abort()

    def flush(self, writer):
        client = self.clients.get(writer)
        if not client:
            return
        # ask_user writes its status before its options, in the same synchronous operation.
        for record in client["pending"]:
            if record["type"] == "worker.status" and record["status"] == "waiting":
                record["options"] = self.question_options(record["task_id"], record["question"])
        packet = (json.dumps({"event": "watch", "changes": client["pending"]}, ensure_ascii=False) + "\n").encode()
        if writer.is_closing() or writer.transport.get_write_buffer_size() + len(packet) > BUFFER_LIMIT:
            self.drop(writer)
            return
        try:
            writer.write(packet)
        except (OSError, RuntimeError):
            self.drop(writer)
            return
        client.update(pending=[], size=0, handle=None)
