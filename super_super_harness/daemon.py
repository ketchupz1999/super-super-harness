"""super-super-harnessd: holds the Router and the Workers, serves a local socket. No model runs here."""
import asyncio
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid

from . import config, procs, tools
from .adapters import session_harness
from .bus import LIMITS, Bus
from .items import Items, brief_header
from .outbox import Outbox
from .private_files import persistent_secret
from .reminders import Reminders
from .router import ProviderPreparationError, Router
from .store import Store
from .tasks import Tasks, clip
from .web import Web

HEARTBEAT_SECONDS = 30
TASK_NAME = 200  # well under the bus data limit, so a long name cannot roll back a Worker result


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cfg.home.stat().st_mode & 0o7777 != 0o700:
            cfg.home.chmod(0o700)
        cfg.run_dir.mkdir(exist_ok=True)
        cfg.workspaces.mkdir(exist_ok=True)
        self.store = Store(cfg.db)
        self.tokens = {}
        self.outbox = Outbox(self.store)
        self.items = Items(cfg, self.store)
        self.tasks = Tasks(cfg, self.store, self.on_task_change, self.issue_token, self.items)
        self.router = Router(cfg, self.store, self.tasks, self.outbox, self.issue_token)
        self.bus = Bus(self.store, self.router, self.outbox, cfg.sources, cfg.rules, self.tasks)
        self.reminders = Reminders(self.store, self.router, self.outbox, bus=self.bus)
        self.owner_token = self._owner_token()
        self.stopping = asyncio.Event()
        self.tasks_kept = []
        self.clients = set()
        self.web = Web(self)

    def _owner_token(self):
        token = persistent_secret(self.cfg.owner_token_file, 48, lambda: secrets.token_hex(24).encode()).decode("ascii")
        self.tokens[token] = ("owner", None)
        return token

    def issue_token(self, identity):
        token = secrets.token_hex(24)
        self.tokens[token] = identity
        return token

    async def on_task_change(self, kind, task_id, text):
        task = self.store.task(task_id)
        with self.store.atomic():
            if kind in ("worker.done", "worker.failed"):
                self.store.event("system", "avatar.done" if kind == "worker.done" else "avatar.error")
            if kind == "worker.done":
                self.items.record_done(task_id, text)
            # Built-in rule worker.* wakes the Router with the same summary as before.
            self.bus.emit({"source": "worker", "type": kind, "id": uuid.uuid4().hex, "thread": "task:" + task_id,
                           "severity": "warn" if kind in ("worker.failed", "worker.stalled", "worker.blocked") else "info",
                           "text": text[:LIMITS["text"]], "data": {"task_id": task_id, "name": task["name"][:TASK_NAME]}})

    # tools
    def tool_specs(self, identity):
        return {"router": tools.ROUTER, "worker": tools.WORKER}.get(identity[0], [])

    async def call_tool(self, identity, name, args, tool_use_id=None):
        role, task_id = identity
        if name not in {spec["name"] for spec in self.tool_specs(identity)}:
            raise ValueError("%s cannot use %s" % (role, name))
        if role == "worker":
            if name == "report_done":
                return await self.tasks.hand_in(task_id, args["text"], tool_use_id)
            return self.worker_tool(task_id, name, args)
        key = tools.chat_call_key(name, args)
        try:
            result = await self.router_tool(name, args)
        except (ValueError, KeyError):
            if key:
                self.store.event("router", "tool.completed", call_key=key, ok=False)
            raise
        if key:
            self.store.event("router", "tool.completed", call_key=key, ok=result.get("ok") is not False)
        return result

    def worker_tool(self, task_id, name, args):
        if name == "report_progress":
            return self.tasks.report_progress(task_id, args["text"])
        if name == "ask_user":
            return self.tasks.ask_user(task_id, args["question"], args.get("options"))
        if name in ("subscribe", "unsubscribe"):
            return self.bus.subscribe(task_id, args["thread"], on=name == "subscribe")
        if name == "hand_back":
            return self.bus.hand_back(task_id, args["event_id"], args["reason"])
        # A Worker reads and updates only its own item, and only next and notes.
        code = self.items.of(task_id)
        if not code:
            raise ValueError("this task is not attached to an item")
        if name == "item_read":
            return self.items.read(code)
        if set(args) - {"next", "notes", "notes_version"}:
            raise ValueError("a Worker may update only next and notes")
        return self.items.update(code, next=args.get("next"), notes=args.get("notes"), notes_version=args.get("notes_version"))

    async def router_tool(self, name, args):
        tasks, router = self.tasks, self.router
        if name in ('memory_read', 'memory_write'):
            required = set() if name == 'memory_read' else {'content', 'version'}
            if set(args) != required:
                raise ValueError('invalid memory tool arguments')
            if name == 'memory_read':
                return await asyncio.to_thread(router.files.read, 'memory')
            return await asyncio.to_thread(router.files.write, 'memory', args['content'], args['version'])
        if name == "reminder_list":
            return self.reminders.list()
        if name == "reminder_add":
            def add_reminder():
                reminder = self.reminders.add(args["name"], args["text"], args["kind"], args.get("at"), args.get("cron"), self._origin())
                self.store.event("router", "reminder.added", **{key: reminder[key] for key in ("id", "next_time", "text", "kind", "cron")})
                return reminder
            # The effect and reminder commit together, including crash recovery.
            with self.store.atomic():
                return router.once(name, name + json.dumps(args, sort_keys=True), add_reminder,
                                   reusable=lambda saved: self.store.one("SELECT 1 FROM reminders WHERE id=? AND status!='cancelled'",
                                                                        int(saved["id"][1:])))
        if name == "reminder_cancel":
            return self.reminders.cancel(args["reminder_id"])
        if name == "ask_user":
            with self.store.atomic():
                latest = self.store.one("SELECT id FROM messages WHERE role='user' AND status IN ('confirmed','done') "
                                        "AND kind!='reminder.run' ORDER BY id DESC LIMIT 1")
                identity = json.dumps([name, args["question"], latest["id"] if latest else None])
                return router.once(name, identity, lambda: router.ask_user(args["question"], args.get("options")))
        if name == "reply":
            return router.once("reply", "reply" + args["text"], lambda: router.reply(args["text"]))
        if name == "task_start":
            def start():
                task = tasks.start(args["name"], args["brief"], args.get("profile"), args.get("repo"), args.get("reason", ""), self._origin(),
                                   args.get("item"), args.get("wait_for"))
                result = {"task_id": task["id"], "worker_name": task["worker_name"], "status": task["status"], "profile": task["profile"]}
                if args.get("item"):
                    result["item"] = args["item"]
                if task["status"] == "waiting_deps":
                    waiting = self.store.all("SELECT d.upstream FROM task_deps d JOIN tasks t ON t.id=d.upstream "
                                             "WHERE d.task_id=? AND d.satisfied IS NULL ORDER BY t.seq", task["id"])
                    result["waiting_for"] = [row["upstream"] for row in waiting]
                    if not waiting:
                        result["note"] = "every upstream has completed; it starts in a moment"
                return result
            return router.once("task_start", "start" + args["brief"], start)
        if name == "task_wait_for":
            return tasks.wait_for(args["task_id"], args["wait_for"])
        if name == "item_create":
            return router.once(name, name + json.dumps(args, sort_keys=True, ensure_ascii=False),
                               lambda: self.items.create(args["code"], args["name"], args["kind"], args["permission"],
                                                         args.get("next", ""), args.get("follow_up", "")))
        if name == "item_update":
            fields = ("status", "progress", "next", "follow_up", "name", "permission", "notes", "notes_version")
            return self.items.update(args["item"], **{key: args.get(key) for key in fields})
        if name == "item_read":
            return self.items.read(args["item"])
        if name == "item_list":
            return self.items.list(args.get("query"), bool(args.get("include_closed")))
        if name == "item_close":
            return self.items.close(args["item"], args.get("reason", ""))
        if name == "item_attach":
            result = self.items.attach_existing(args["item"], args["task_id"], args.get("move", False))
            if result["ok"] and "note" not in result:
                moved = " It was moved from item %s, which is no longer yours." % result["moved_from"] if "moved_from" in result else ""
                self.tasks.tell(args["task_id"], "The Router attached this task to an item.%s\n%s"
                                % (moved, brief_header(self.items.get(result["item"]))))
            return result
        if name == "task_send":
            return router.once("task_send", "send" + args["task_id"] + args["text"],
                               lambda: tasks.send(args["task_id"], args["text"], args.get("expected_status")))
        if name == "task_interrupt":
            return tasks.interrupt(args["task_id"])
        if name == "task_resume":
            return tasks.resume(args["task_id"], args.get("text"))
        if name == "task_stop":
            return await tasks.stop(args["task_id"])
        if name == "task_switch_model":
            return await tasks.switch_model(args["task_id"], args["profile"])
        if name == "task_read":
            return tasks.read(args["task_id"])
        if name == "task_detail":
            return self.task_detail(args["task_id"], args.get("kinds"), args.get("limit", 40))
        if name == "task_search":
            return tasks.search(args["query"])
        if name == "event_read":
            if "trigger_id" in args:
                row = self.store.one("SELECT * FROM triggers WHERE id=?", args["trigger_id"])
                return dict(row, data=json.loads(row["data"])) if row else {"error": "no such trigger"}
            row = self.store.one("SELECT * FROM events WHERE id=?", args["event_id"])
            return dict(row) if row else {"error": "no such event"}
        if name == "message_search":
            rows = self.store.all("SELECT id, role, channel, text, created FROM messages WHERE text LIKE ? ORDER BY id DESC LIMIT 10", "%" + args["query"] + "%")
            return [dict(row, text=clip(row["text"], 300)) for row in rows]
        if name == "router_switch_model":
            router.check_profile(args["profile"])
            asyncio.create_task(self._switch_after_turn(args["profile"]))
            return {"ok": True, "when": "after this turn"}
        if name == "inbox_read":
            return router.read_inbox()
        raise ValueError(name)

    def _origin(self):
        turn = self.store.current_turn()
        channels = json.loads(turn["channels"]) if turn else []
        return channels[0] if channels else ""

    def task_detail(self, task_id, kinds, limit):
        task = self.tasks._require(task_id)
        harness = session_harness(self.cfg, task["profile"], self.store.get("task_harness:" + task_id))
        items = harness.transcript(self.cfg, task["session_id"]) if harness else []
        if not harness:
            items = [{"kind": "event", "time": row["created"], "text": row["data"], "tool": row["type"]}
                     for row in self.store.all("SELECT type, data, created FROM events WHERE task_id=? ORDER BY id", task_id)]
        wanted = set(kinds.split(",")) if kinds else None
        items = [item for item in items if not wanted or item["kind"] in wanted]
        return [dict(item, text=clip(item["text"], 400)) for item in items[-limit:]]

    async def _switch_after_turn(self, profile):
        while self.store.current_turn():
            await asyncio.sleep(0.5)
        await self.router.switch(profile)

    # socket
    async def dispatch(self, identity, method, params, writer):
        if method == "tools":
            return self.tool_specs(identity)
        if method == "tool":
            return await self.call_tool(identity, params["name"], params.get("args") or {}, params.get("tool_use_id"))
        if identity[0] != "owner":
            raise ValueError("not allowed")
        if method == "say":
            message_id = self.router.submit("user", params["text"], params.get("channel", "terminal"))
            return {"id": message_id}
        if method == "emit":
            event = params["event"]
            if isinstance(event, dict) and event.get("source") in tuple(config.BUILTIN_SOURCES):
                raise ValueError("source %s is internal" % event["source"])
            return self.bus.emit(event)
        if method == "subscribe":
            self.outbox.channels["terminal"].clients.add(writer)
            asyncio.create_task(self.outbox.flush())
            return {"ok": True}
        if method == "status":
            return {"router": {"mode": self.router.mode, "profile": self.router.profile_name, "pid": self.router.process.pid if self.router.process else None,
                               "ready": bool(self.router.process and self.router.ready)},
                    "live": len(self.tasks.live), "max_workers": self.tasks.max_workers, "web": self.web.status()}
        if method == "web_link":
            return {"url": self.web.issue_link()}
        if method == "stop":
            self.stopping.set()
            return {"ok": True}
        if method == "router_detach":
            return await self.router.detach()
        if method == "router_attach":
            await self.router.reattach()
            return {"ok": True}
        if method == "router_mcp":
            return {"path": str(self.router.mcp_config())}
        if method == "wait":
            return {"queued": await self.router.wait_inbox()}
        if method == "attach_check":
            return {"reason": self.router.attach_check()}
        if method == "router_reset":
            await self.router._kill()
            self.store.unset("router_session")
            self.store.unset("router_waiting")
            self.router.mode = "running"
            await self.router.spawn()
            return {"ok": True}
        if method == "router_compact":
            self.router.process.send("/compact")
            return {"ok": True}
        if method == "start_many":
            created = [self.tasks.start("批量任务", params["brief"], params.get("profile"), params.get("repo"))["id"] for _ in range(params["count"])]
            return created
        if method == "tool_owner":
            return await self.router_tool(params["name"], params["args"])
        if method == "task_stop_owner":
            return await self.tasks.stop(params["task_id"])
        if method == "task_hold":
            return await self.tasks.hold(params["task_id"])
        if method == "task_release":
            self.tasks.release(params["task_id"])
            return {"ok": True}
        raise ValueError("unknown method %s" % method)

    async def serve_client(self, reader, writer):
        self.clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                request = json.loads(line)
                identity = self.tokens.get(request.get("token"))
                if not identity:
                    answer = {"error": "bad token"}
                else:
                    try:
                        answer = {"result": await self.dispatch(identity, request["method"], request.get("params") or {}, writer)}
                    except (ValueError, KeyError) as exc:
                        answer = {"error": str(exc)}
                writer.write((json.dumps(answer, ensure_ascii=False, default=str) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.outbox.channels["terminal"].clients.discard(writer)
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    # lifecycle
    async def monitor(self):
        beat = 0.0
        while not self.stopping.is_set():
            await asyncio.sleep(1)
            await self.router.tick()
            await self.tasks.tick()
            if time.time() - beat >= HEARTBEAT_SECONDS:
                beat = time.time()
                self.store.set("last_alive", beat)

    async def monitor_reminders(self):
        # Separate from health probes/model failover: reminders never wait on a model.
        while not self.stopping.is_set():
            try:
                await self.reminders.tick()
            except Exception:
                logging.getLogger(__name__).warning("reminder tick failed; retrying on the next clock check")
            await asyncio.sleep(1)

    def startup_notice(self, recovered):
        clean = self.store.get("clean_exit") == "1"
        last = float(self.store.get("last_alive", "0"))
        self.store.set("clean_exit", "0")
        self.store.set("last_alive", time.time())
        if not recovered and (clean or not last):
            return
        gap = max(1, int((time.time() - last) / 60)) if last else 1
        head = "刚才中断了约 %d 分钟" % gap if clean else "上次异常退出，中断了约 %d 分钟" % gap
        self.outbox.add("%s，%d 个任务已接着跑。" % (head, len(recovered)), "notice")

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        if self.cfg.sock.exists():
            self.cfg.sock.unlink()
        server = await asyncio.start_unix_server(self.serve_client, str(self.cfg.sock), limit=2 ** 24, start_serving=False)
        os.chmod(self.cfg.sock, 0o600)
        recovered = await self.tasks.recover()
        await server.start_serving()
        await self.web.start()
        try:
            await self.router.start()
        except ProviderPreparationError:
            await self.web.close()
            server.close()
            for writer in list(self.clients):
                writer.close()
            await server.wait_closed()
            await self.router.shutdown()
            await self.tasks.shutdown()
            self.outbox.closed = True
            self.store.close()
            self.cfg.sock.unlink(missing_ok=True)
            raise
        reminder_monitor = asyncio.create_task(self.monitor_reminders())
        self.startup_notice(recovered)
        self.store.set("service_pid", os.getpid())
        monitor = asyncio.create_task(self.monitor())
        try:
            await self.stopping.wait()
        except asyncio.CancelledError:
            # Tests model a host crash by cancelling run(): leave the children
            # for recovery, but do not leave listeners/monitor on this live loop.
            monitor.cancel()
            reminder_monitor.cancel()
            await asyncio.gather(monitor, reminder_monitor, return_exceptions=True)
            await self.reminders.close()
            await self.web.close()
            server.close()
            for writer in list(self.clients):
                writer.close()
            await server.wait_closed()
            raise
        running = len(self.tasks.live)
        self.outbox.add("后台服务要停了，%d 个任务会暂停，恢复后接着跑。" % running, "notice")
        await self.outbox.flush()
        monitor.cancel()
        reminder_monitor.cancel()
        await asyncio.gather(monitor, reminder_monitor, return_exceptions=True)
        await self.reminders.close()
        await self.web.close()
        server.close()
        for writer in list(self.clients):
            writer.close()
        await server.wait_closed()
        self.router.closed = True
        self.tasks.closed = True
        await self.router.shutdown()
        await self.tasks.shutdown()
        self.outbox.closed = True
        self.store.set("clean_exit", "1")
        self.store.set("last_alive", time.time())
        self.store.close()
        if self.cfg.sock.exists():
            self.cfg.sock.unlink()


def serve():
    home = config.home_path()
    if not (home / "profiles.toml").is_file():
        command = "init --migrate-from /path/to/old/profiles.toml" if (home / "router.db").exists() else "init"
        print("Private configuration is missing; run supersuperharness %s before serve." % command, file=sys.stderr)
        return 1
    try:
        cfg = config.load()
    except config.PublicConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, TypeError):
        print("Private configuration is invalid; check it or run supersuperharness init. Diagnostics suppressed to protect secrets.", file=sys.stderr)
        return 1
    cfg.home.mkdir(parents=True, exist_ok=True)
    lock = cfg.home / "service.pid"
    if lock.exists():
        pid = lock.read_text().strip()
        if pid and procs.alive(int(pid)):
            print("already running as %s" % pid, file=sys.stderr)
            return 1
    lock.write_text(str(os.getpid()))
    awake = None
    if sys.platform == "darwin" and not os.environ.get("SSHARNESS_NO_CAFFEINATE"):
        # Only holds while plugged in, ends with this process and changes no system setting.
        awake = subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
    try:
        asyncio.run(Service(cfg).run())
    except ProviderPreparationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)
        if awake:
            awake.terminate()
    return 0
