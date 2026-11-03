"""Runs a real Service against the fake Harness in a temporary SSHARNESS_HOME."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest

from super_super_harness import config
from super_super_harness.daemon import Service

HERE = Path(__file__).resolve().parent
OWNER = ("owner", None)
ROUTER = ("router", None)


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self._answer()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self._answer()

    def log_message(self, *args):
        pass


def free_port():
    base = int(os.environ.get("SSHARNESS_PORT_BASE", "0"))
    for port in range(base, base + 100) if base else [0]:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port in test range")


class Gateway:
    """A provider endpoint that can be taken down."""

    def __init__(self):
        self.port = free_port()
        self.server = None
        self.up()

    def up(self):
        ThreadingHTTPServer.allow_reuse_address = True
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def down(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = None


def write_config(home, primary_port, backup_port, limits="", failover=""):
    key = home / "key"
    key.write_text("dummy-for-tests")
    root = home / "sessions" / "proj"
    fake = HERE / "fake_claude.py"
    os.chmod(fake, 0o755)
    (home / "profiles.toml").write_text(f"""
default_profile = "work"
session_root = "{home}/sessions"
[web]
port = 0
[limits]
max_workers = 2
busy_interrupt_seconds = 1
stall_minutes = 30
worker_retries = 1
{limits}
[router]
primary = "main"
backup = "spare"
[failover]
silent_seconds = 1
deep_probe_seconds = 1
retries = 5
probe_timeout = 1
wake_grace_seconds = 0
primary_check_seconds = 1
{failover}
[harness.claude-code]
binary = "{fake}"
[providers.one]
base_url = "http://127.0.0.1:{primary_port}"
key_file = "{key}"
[providers.two]
base_url = "http://127.0.0.1:{backup_port}"
key_file = "{key}"
[profiles.main]
harness = "claude-code"
provider = "one"
model = "main-model"
env = {{ SSHARNESS_FAKE_ROOT = "{root}" }}
[profiles.spare]
harness = "claude-code"
provider = "two"
model = "spare-model"
context_window = 500000
env = {{ SSHARNESS_FAKE_ROOT = "{root}" }}
[profiles.work]
harness = "claude-code"
provider = "two"
model = "work-model"
env = {{ SSHARNESS_FAKE_ROOT = "{root}" }}
[profiles.other]
harness = "claude-code"
provider = "two"
model = "other-model"
env = {{ SSHARNESS_FAKE_ROOT = "{root}" }}
""")


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    limits = ""
    failover = ""

    def _guard(self):
        async def watchdog():
            await asyncio.sleep(40)
            print("\nHUNG:", self.id(), flush=True)
            for task in asyncio.all_tasks():
                print("  task", task.get_coro().__qualname__, [(f.f_code.co_name, f.f_lineno) for f in task.get_stack(limit=3)], flush=True)
            os._exit(9)
        return asyncio.create_task(watchdog())

    async def asyncSetUp(self):
        self.addAsyncCleanup(self._stop_guard, self._guard())
        self.home = Path(tempfile.mkdtemp(prefix="ssharness-test-")).resolve()
        self.primary = Gateway()
        self.backup = Gateway()
        write_config(self.home, self.primary.port, self.backup.port, self.limits, self.failover)
        self.cfg = config.load(self.home)
        await self.boot()

    async def _stop_guard(self, guard):
        guard.cancel()

    async def boot(self):
        self.service = Service(self.cfg)
        self.running = asyncio.create_task(self.service.run())
        await self.until(lambda: self.cfg.sock.exists() and self.service.router.process)

    async def halt(self):
        self.service.stopping.set()
        await self.running

    async def asyncTearDown(self):
        if not self.running.done():
            await self.halt()
        for gateway in (self.primary, self.backup):
            if gateway.server:
                gateway.down()
        shutil.rmtree(self.home, ignore_errors=True)

    async def until(self, predicate, timeout=15, label="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            await asyncio.sleep(0.03)
        self.fail("timed out waiting for %s" % label)

    @property
    def store(self):
        return self.service.store

    def say(self, text):
        return self.service.router.submit("user", text, "terminal")

    async def tool(self, tool_name, /, **args):
        return await self.service.call_tool(ROUTER, tool_name, args)

    def start_task(self, brief, name="任务", **extra):
        return self.service.tasks.start(name, brief, **extra)["id"]

    def status(self, task_id):
        return self.store.task(task_id)["status"]

    def session_path(self, session_id):
        return self.home / "sessions" / "proj" / (session_id + ".jsonl")

    async def worker_ready(self, task_id):
        await self.until(lambda: self.session_path(self.store.task(task_id)["session_id"]).exists()
                         and '"type": "user"' in self.session_path(self.store.task(task_id)["session_id"]).read_text(), label="worker took the brief")

    async def wait_status(self, task_id, status, timeout=15):
        await self.until(lambda: self.status(task_id) == status, timeout, "%s to be %s" % (task_id, status))

    def argv_log(self):
        path = self.home / "sessions" / "proj" / "argv.log"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def outbox(self):
        return [row["text"] for row in self.store.all("SELECT text FROM outbox ORDER BY id")]

    def statuses(self):
        return [row["status"] for row in self.store.all("SELECT status FROM messages ORDER BY id")]
