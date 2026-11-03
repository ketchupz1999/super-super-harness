"""Local web channel. HTTP and SSE share the daemon's asyncio loop; no HTTP threads.

SSE carries invalidations, not a consumable mailbox. On every connection the
browser reconciles state and the durable, monotonically numbered conversation.
This also works across daemon restarts and for several tabs at once.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

from . import usage
from .adapters import get_harness, session_harness
from .avatar import Avatar, EXTENSIONS as AVATAR_EXTENSIONS, STATES as AVATAR_STATES
from .config import REPO_ROOT, expand
from .private_files import persistent_secret
from .notifications import worker_notice
from .router_files import DocumentError
from .worker_names import worker_name

BODY_LIMIT = 64 * 1024
HEADER_LIMIT = 16 * 1024
DIFF_LIMIT = 256 * 1024
TRANSCRIPT_BYTES = 2 * 1024 * 1024
TRANSCRIPT_ITEMS = 500
TEXT_LIMIT = 16000
USAGE_CACHE_SECONDS = 3
USAGE_CACHE_WINDOWS = 4
STATES = ("waiting", "running", "queued", "waiting_deps", "interrupted", "completed", "failed", "stopped")
ITEMS_LIMIT = 200
COOKIE = "ssharness_web_session"
CHARACTER_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
CHARACTER_ANIMATIONS = ("idle", "working", "waiting", "complete")
CHARACTER_LIMIT = 8 * 1024 * 1024
CHARACTER_TYPES = {".png": ("image/png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
                   ".webp": ("image/webp", lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP")}
LOG = logging.getLogger(__name__)
HARNESS_NOTICE = re.compile(
    r"\[(?:Your previous response had no visible output\b[^\]]*|Request interrupted by user(?: for tool use)?)\]")


def timeline_visible(item):
    """Hide empty speech and known Harness control prompts, not tool output."""
    text = item.get("text", "")
    if item["kind"] == "event" and item.get("tool") == "text":
        try:
            text = json.loads(text).get("text", "")
        except (ValueError, AttributeError):
            return True
    elif item["kind"] not in ("received", "said"):
        return True
    text = text.strip()
    return bool(text) and not HARNESS_NOTICE.fullmatch(text)


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def number(query, key, default, maximum):
    try:
        value = int(query.get(key, [str(default)])[0])
    except (ValueError, TypeError):
        raise HTTPError(400, "invalid " + key)
    if not 0 <= value <= maximum:
        raise HTTPError(400, "invalid " + key)
    return value


def text_field(data, key, required=True):
    value = data.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise HTTPError(400, "invalid " + key)
    return value


class Web:
    def __init__(self, service):
        self.service = service
        self.cfg, self.store = service.cfg, service.store
        self.port = self.cfg.web["port"]
        self.cookie_name = "%s_%d" % (COOKIE, self.port)
        self.server = None
        self.closing = False
        self.error = ""
        self.connections = set()
        self.streams = set()
        self.handlers = set()
        self.ticket = None
        self.session_key = self._session_key()
        self.snapshots = {}
        self.epoch = secrets.token_hex(8)
        self.sequence = 0
        self.probes = None
        self.probed_at = 0
        self.probe_lock = asyncio.Lock()
        self.usage_lock = asyncio.Lock()
        self.usage_cache = {}
        self.started = time.time()
        self.router_avatar = Avatar(service)
        self.page = (Path(__file__).with_name("web") / "index.html").read_bytes()
        # Public product artwork only: fixed names, never a client-derived file path.
        artwork = Path(__file__).with_name("web") / "suber"
        self.artwork = {"/assets/suber/" + name + ".png": (artwork / (name + ".png")).read_bytes()
                        for name in ("mark", "idle", "idle-blink", "typing", "typing-clean",
                                     "typing-blink", "waiting", "waiting-blink", "done")}
        self.characters_scanned, self.characters_cache, self.character_atlas = float("-inf"), [], {}
        script = re.search(rb"<script>(.*?)</script>", self.page, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
        self.csp = ("default-src 'none'; script-src 'sha256-%s'; style-src 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" % digest)
        self.version = "unknown"

    def _session_key(self):
        return persistent_secret(self.cfg.home / "web-session-key", 32, lambda: secrets.token_bytes(32))

    def session_cookie(self):
        value = "%s.%d" % (secrets.token_urlsafe(32), int(time.time()) + 43200)
        return value + "." + hmac.new(self.session_key, value.encode(), "sha256").hexdigest()

    async def start(self):
        # Older releases persisted unused login tickets. Never keep live tokens on disk.
        (self.cfg.home / "web-token").unlink(missing_ok=True)
        if not self.port:
            return
        try:
            self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port, limit=HEADER_LIMIT)
        except OSError as exc:
            self.error = "Web unavailable on port %d: %s" % (self.port, exc.strerror)
            LOG.warning(self.error)
        self.version = await asyncio.to_thread(self._version)

    def _version(self):
        try:
            done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                  capture_output=True, text=True, timeout=2)
            if done.returncode == 0:
                return done.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return REPO_ROOT.name if REPO_ROOT.parent.name == "releases" else "unknown"

    def status(self):
        return {"enabled": bool(self.port), "listening": self.server is not None,
                "port": self.port, "error": self.error}

    def issue_link(self):
        if not self.server:
            raise ValueError(self.error or "Web is disabled (web.port = 0)")
        token = secrets.token_urlsafe(32)
        # Issuing another link invalidates the previous unredeemed link, not cookies.
        self.ticket = (token, time.monotonic() + 300)
        return "http://127.0.0.1:%d/?token=%s" % (self.port, token)

    def authorized(self, headers):
        # Ignore other localhost applications' cookies, even if they are malformed.
        values = []
        for field in headers.get("cookie", "").split(";"):
            key, separator, value = field.strip().partition("=")
            if separator and key == self.cookie_name:
                values.append(value.strip())
        if len(values) != 1:
            return False
        value = values[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[0-9]{10}\.[0-9a-f]{64}", value):
            return False
        payload, signature = value.rsplit(".", 1)
        expiry = int(payload.rsplit(".", 1)[1])
        expected = hmac.new(self.session_key, payload.encode(), "sha256").hexdigest()
        return expiry > time.time() and secrets.compare_digest(signature, expected)

    async def close(self):
        self.closing = True
        server, self.server = self.server, None
        if server:
            server.close()
        # Server.wait_closed() also waits for accepted connections on Python 3.12.
        for writer in list(self.connections):
            writer.close()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)
        if server:
            await server.wait_closed()

    async def response(self, writer, status, body=b"", content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        reasons = {200: "OK", 303: "See Other", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                   404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
                   415: "Unsupported Media Type", 429: "Too Many Requests", 500: "Internal Server Error"}
        headers = {"Content-Type": content_type, "Content-Length": str(len(body)), "Connection": "close",
                   "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": self.csp, **(extra or {})}
        writer.write(("HTTP/1.1 %d %s\r\n" % (status, reasons[status]) +
                      "".join("%s: %s\r\n" % item for item in headers.items()) + "\r\n").encode() + body)
        await asyncio.wait_for(writer.drain(), 10)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.connections.add(writer)
        streaming = False
        try:
            if self.closing:
                return
            if len(self.connections) > 64:
                raise HTTPError(429, "too many connections")
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except (asyncio.LimitOverrunError, ValueError):
                raise HTTPError(400, "headers too large")
            if len(raw) > HEADER_LIMIT:
                raise HTTPError(400, "headers too large")
            try:
                lines = raw.split(b"\r\n")
                method, target, version = lines[0].decode("ascii").split(" ")
                headers = {}
                for line in lines[1:-2]:
                    key, value = line.split(b":", 1)
                    key = key.decode("ascii").lower()
                    if not re.fullmatch(r"[a-z0-9-]+", key) or key in headers:
                        raise ValueError()
                    # HTTP field values allow obs-text, including unrelated cookies.
                    headers[key] = value.decode("latin-1").strip()
            except (ValueError, UnicodeError):
                raise HTTPError(400, "malformed request")
            if version != "HTTP/1.1" or not target.startswith("/") or target.startswith("//"):
                raise HTTPError(400, "invalid request target")
            if headers.get("host") not in {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}:
                raise HTTPError(403, "invalid Host")
            if "transfer-encoding" in headers:
                raise HTTPError(400, "transfer encoding is not supported")
            if not re.fullmatch(r"[0-9]+", headers.get("content-length", "0")):
                raise HTTPError(400, "invalid content length")
            size = int(headers.get("content-length", "0"))
            if size > BODY_LIMIT:
                raise HTTPError(413, "request body too large")
            url = urlsplit(target)
            path, query = url.path, parse_qs(url.query, keep_blank_values=True)
            # No request path is ever used as a filesystem path.
            if "%" in path or ".." in path or "\\" in path:
                raise HTTPError(400, "invalid path")
            if method not in ("GET", "POST"):
                raise HTTPError(405, "method not allowed")
            if method == "POST" and headers.get("origin") != "http://" + headers["host"]:
                raise HTTPError(403, "invalid Origin")
            if path == "/" and method == "GET" and "token" in query:
                value = query["token"][0]
                if not value.isascii() or not self.ticket or self.ticket[1] < time.monotonic() or not secrets.compare_digest(value, self.ticket[0]):
                    raise HTTPError(401, "link expired or already used; run supersuperharness web again")
                self.ticket = None
                session = self.session_cookie()
                await self.response(writer, 303, b"", extra={"Location": "/", "Set-Cookie":
                                    "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200" % (self.cookie_name, session)})
                return
            if path.startswith("/api") and not self.authorized(headers):
                raise HTTPError(401, "Open a fresh link with supersuperharness web")
            if method == "GET" and path == "/":
                await self.response(writer, 200, self.page, "text/html; charset=utf-8")
                return
            if method == "GET" and path in self.artwork:
                await self.response(writer, 200, self.artwork[path], "image/png")
                return
            if method == "GET" and path == "/api/stream":
                if len(self.streams) >= 16:
                    raise HTTPError(429, "too many streams")
                streaming = True
                await self.stream(reader, writer, headers)
                return
            data = {}
            if method == "POST":
                if headers.get("content-type", "").split(";")[0] != "application/json":
                    raise HTTPError(415, "JSON required")
                try:
                    data = json.loads(await asyncio.wait_for(reader.readexactly(size), 10))
                except (ValueError, UnicodeError):
                    raise HTTPError(400, "invalid JSON")
                if not isinstance(data, dict):
                    raise HTTPError(400, "JSON object required")
            if method == "GET" and path == "/api/characters/harness-claude-code.png":
                try:
                    body, _ = await asyncio.to_thread(self._character_file, "harness-claude-code.png", CHARACTER_LIMIT)
                    if not CHARACTER_TYPES[".png"][1](body):
                        raise ValueError("invalid icon")
                except (OSError, ValueError):
                    raise HTTPError(404, "icon unavailable")
                await self.response(writer, 200, body, "image/png")
                return
            if method == "GET" and path.startswith("/api/characters/"):
                match = re.fullmatch(r"/api/characters/([a-z][a-z0-9-]{0,31})\.atlas", path)
                if not match or not any(c["id"] == match.group(1) for c in await asyncio.to_thread(self.characters)):
                    raise HTTPError(404, "character unavailable")
                try:
                    body, mime = await asyncio.to_thread(self.character_image, match.group(1))
                except (OSError, ValueError):
                    raise HTTPError(404, "character unavailable")
                await self.response(writer, 200, body, mime)
                return
            if method == "GET" and path.startswith("/api/router-avatar/"):
                filename = path.removeprefix("/api/router-avatar/")
                parts = filename.split(".")
                if len(parts) != 2 or parts[0] not in AVATAR_STATES or parts[1] not in AVATAR_EXTENSIONS:
                    raise HTTPError(404, "avatar unavailable")
                try:
                    body, mime = await asyncio.to_thread(self.router_avatar.image, *parts)
                except (OSError, ValueError):
                    raise HTTPError(404, "avatar unavailable")
                await self.response(writer, 200, body, mime)
                return
            if method == "GET" and path == "/api/avatar":
                body, mime = await asyncio.to_thread(self.avatar)
                await self.response(writer, 200, body, mime)
                return
            result = await self.dispatch(method, path, query, data)
            await self.response(writer, 200, result)
        except HTTPError as exc:
            await self.response(writer, exc.status, {"error": exc.message})
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            if self.server is not None:
                raise
        except Exception:
            # Never log request targets, cookies, bodies or login tokens.
            LOG.exception("web request failed")
            if not streaming:
                await self.response(writer, 500, {"error": "internal server error"})
        finally:
            self.streams.discard(writer)
            self.connections.discard(writer)
            self.handlers.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass

    def _character_file(self, name, limit=None):
        """Read (or, without a limit, only check) one regular file in SSHARNESS_HOME/characters; never a link."""
        directory = os.open(self.cfg.home / "characters", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                cap = limit or CHARACTER_LIMIT
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= cap:
                    raise ValueError("character file unavailable or too large")
                version = "%x-%x" % (info.st_mtime_ns, info.st_size)
                if limit is None:
                    return None, version
                chunks, size = [], 0
                while size <= cap:
                    chunk = os.read(fd, min(1 << 20, cap + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                if size > cap:
                    raise ValueError("character file too large")
                return b"".join(chunks), version
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def characters(self):
        """Optional private sprite sheets for Worker avatars, rescanned at most once a second."""
        now = time.monotonic()
        if now - self.characters_scanned < 1:
            return self.characters_cache
        found, atlases = [], {}
        try:
            names = sorted(os.listdir(self.cfg.home / "characters"))
        except OSError:
            names = []
        def count(value, low, high):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("bad number")
            return value
        for name in names:
            stem, _, extension = name.rpartition(".")
            if extension != "json" or not CHARACTER_ID.fullmatch(stem) or len(found) >= 16:
                continue
            try:
                manifest = json.loads(self._character_file(name, 65536)[0])
                atlas = Path(str(manifest["atlas"])).name
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", atlas) or Path(atlas).suffix.lower() not in CHARACTER_TYPES:
                    continue
                width, height = count(manifest["frameWidth"], 8, 1024), count(manifest["frameHeight"], 8, 1024)
                columns, rows = count(manifest["columns"], 1, 32), count(manifest["rows"], 1, 32)
                animations = {}
                for key in CHARACTER_ANIMATIONS:
                    item = manifest["animations"][key]
                    frames = count(item["frames"], 1, columns)
                    durations = [count(value, 20, 10000) for value in item["frameDurationsMs"]]
                    if len(durations) != frames:
                        raise ValueError("durations do not match frames")
                    animations[key] = {"row": count(item["row"], 0, rows - 1), "frames": frames, "durations": durations}
                _, version = self._character_file(atlas)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            atlases[stem] = atlas
            found.append({"id": stem, "frameWidth": width, "frameHeight": height, "columns": columns, "rows": rows,
                          "pixel": manifest.get("pixel") is True, "animations": animations,
                          "atlas": "/api/characters/%s.atlas?v=%s" % (stem, version)})
        self.characters_scanned, self.characters_cache, self.character_atlas = now, found, atlases
        return found

    def harness_icon(self):
        try:
            body, version = self._character_file("harness-claude-code.png", CHARACTER_LIMIT)
            if CHARACTER_TYPES[".png"][1](body):
                return "/api/characters/harness-claude-code.png?v=" + version
        except (OSError, ValueError):
            pass
        return None

    def character_image(self, character):
        name = self.character_atlas.get(character)
        if not name:
            raise ValueError("character unavailable")
        body, _ = self._character_file(name, CHARACTER_LIMIT)
        mime, signature = CHARACTER_TYPES[Path(name).suffix.lower()]
        if not signature(body):
            raise ValueError("image format does not match its extension")
        return body, mime

    def router_question(self):
        """Pending identity is global, not inferred from a history page or avatar flash."""
        pending = self.store.get("router_waiting")
        if not pending:
            return None
        row = self.store.one("SELECT c.id, c.text, q.options FROM conversation c "
                             "LEFT JOIN questions q ON q.outbox_id=c.source_id "
                             "WHERE c.role='router' AND c.kind='question' "
                             "ORDER BY c.id DESC, q.id DESC LIMIT 1")
        if not row or row["text"] != pending:
            return None
        return {"id": row["id"], "text": row["text"][:TEXT_LIMIT],
                "options": json.loads(row["options"]) if row["options"] else []}

    def router_state(self):
        router = self.service.router
        return {"mode": router.mode, "switching": router.switching or (router.mode == "running" and (not router.ready or not router.process)),
                "profile": router.profile_name, "model": self.cfg.profile(router.profile_name).model,
                "backup": self.cfg.profile(self.cfg.backup).model, "busy": bool(self.store.current_turn()),
                "pending": self.store.one("SELECT COUNT(*) n FROM messages WHERE status!='done'")["n"],
                "last_event": router.last_event, "pid": router.process.pid if router.process else None,
                "avatar": self.router_avatar.state(), "question": self.router_question()}

    def task_links(self):
        """Item and upstreams of every task, for the page's run view."""
        items = {row["task_id"]: row["item"] for row in self.store.all("SELECT task_id, item FROM item_tasks")}
        deps = {}
        for row in self.store.all("SELECT d.task_id, d.upstream, d.satisfied FROM task_deps d LEFT JOIN tasks t ON t.id=d.upstream ORDER BY t.seq"):
            deps.setdefault(row["task_id"], []).append({"task_id": row["upstream"], "satisfied": row["satisfied"] is not None})
        return items, deps, self.task_activity()

    def task_activity(self):
        """Read-only display hints: current run start, the Worker's own words, last tool and last exchange."""
        def latest(types, extra=""):
            marks = ",".join("?" * len(types))
            return {row["task_id"]: row for row in self.store.all(
                "SELECT e.task_id, e.type, e.data, e.created FROM events e JOIN (SELECT task_id, MAX(id) id FROM events "
                "WHERE type IN (%s) AND task_id IS NOT NULL %s GROUP BY task_id) m ON m.id=e.id" % (marks, extra), *types)}
        def data(row):
            try:
                value = json.loads(row["data"])
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}
        runs = {row["task_id"]: row for row in self.store.all(
            "SELECT r.task_id, r.started, r.ended FROM worker_runs r JOIN "
            "(SELECT task_id, MAX(id) id FROM worker_runs GROUP BY task_id) last ON last.id=r.id")}
        reported = latest(("report.progress",))
        # Router tools (report_progress, ask_user...) are messages, not work; they have their own rows.
        tools = latest(("tool",), "AND data NOT LIKE '{\"tool\": \"mcp__router__%'")
        exchanges = latest(("task.started", "task.message", "report.progress", "ask_user", "report.done"))
        activity = {}
        for task_id in set(runs) | set(reported) | set(tools) | set(exchanges):
            tool, exchange, report = tools.get(task_id), exchanges.get(task_id), reported.get(task_id)
            run = runs.get(task_id)
            started = run["started"] if run else 0
            if tool and tool["created"] < started:
                tool = None
            if report and report["created"] < started:
                report = None
            activity[task_id] = {
                "run_started": run["started"] if run else None,
                "run_ended": run["ended"] if run else None,
                "reported": str(data(report).get("text", ""))[:2000] if report else "",
                "tool": {"name": str(data(tool).get("tool", ""))[:80], "input": str(data(tool).get("input", ""))[:200]} if tool else None,
                "exchange": {"type": exchange["type"], "text": str(data(exchange).get("text", ""))[:500],
                             "time": exchange["created"]} if exchange else None}
        return activity

    def task_summary(self, row, links=None):
        task = dict(row)
        profile = self.cfg.profiles.get(task["profile"])
        items, deps, activity = links or self.task_links()
        asked = self.store.one("SELECT id, question, options FROM questions WHERE task_id=? ORDER BY id DESC LIMIT 1", task["id"]) \
            if task["status"] == "waiting" and task["question"] else None
        if asked and asked["question"] != task["question"]:
            asked = None
        options = json.loads(asked["options"]) if asked else []
        return {key: task[key] for key in ("id", "name", "status", "profile", "repo", "branch", "created", "updated",
                                          "started", "state_since", "ended")} | {
            "worker_name": worker_name(task["seq"]),
            "model": profile.model if profile else task["profile"] + " (profile removed)",
            "harness": self.store.get("task_harness:" + task["id"]) or (profile.harness if profile else ""),
            "where": Path(task["repo"]).name if task["repo"] else "Workspace",
            "progress": task["progress"][:2000], "question": task["question"][:TEXT_LIMIT],
            "item": items.get(task["id"]), "wait_for": deps.get(task["id"], []), "options": options,
            "question_id": asked["id"] if asked else None,
            "activity": activity.get(task["id"], {"run_started": None, "run_ended": None, "reported": "", "tool": None, "exchange": None})}

    def items(self):
        rows = self.store.all("SELECT * FROM items ORDER BY status IN ('delivered','closed'), updated DESC LIMIT ?", ITEMS_LIMIT + 1)
        attached = {}
        for row in self.store.all("SELECT i.item, i.task_id FROM item_tasks i JOIN tasks t ON t.id=i.task_id ORDER BY t.seq"):
            attached.setdefault(row["item"], []).append(row["task_id"])
        return [self.service.items.describe(row) | {"tasks": attached.get(row["code"], [])} for row in rows[:ITEMS_LIMIT]], len(rows) > ITEMS_LIMIT

    def item_detail(self, code):
        try:
            item = self.service.items.read(code)
        except ValueError:
            raise HTTPError(404, "no such item")
        notes, version = item.pop("notes"), item.pop("notes_version")
        links = self.task_links()
        tasks = [self.task_summary(row, links) for row in self.store.all(
            "SELECT t.* FROM item_tasks i JOIN tasks t ON t.id=i.task_id WHERE i.item=? ORDER BY t.seq", code)]
        item["tasks"] = [task["id"] for task in tasks]
        return {"item": item, "tasks": tasks, "notes": notes[:TEXT_LIMIT], "notes_version": version,
                "thread": "item:" + code}

    def state(self):
        counts = dict.fromkeys(STATES, 0)
        counts.update({r["status"]: r["n"] for r in self.store.all("SELECT status, COUNT(*) n FROM tasks GROUP BY status")})
        rows = self.store.all("SELECT seq, id, name, status, profile, repo, branch, created, updated, started, state_since, ended, "
                              "substr(progress,1,2000) progress, substr(question,1,16000) question FROM tasks "
                              "ORDER BY status IN ('completed','failed','stopped'), seq DESC LIMIT 1000")
        links = self.task_links()
        items, items_truncated = self.items()
        return {"reminders": self.service.reminders.list(), "router": self.router_state(), "tasks": [self.task_summary(t, links) for t in rows], "counts": counts,
                "items": items, "items_truncated": items_truncated, "characters": self.characters(),
                "harness_icon": self.harness_icon(),
                "tasks_truncated": sum(counts.values()) > len(rows), "max_workers": self.service.tasks.max_workers,
                "live": len(self.service.tasks.live), "time": time.time(),
                "profiles": [{"name": p.name, "model": p.model} for p in self.cfg.profiles.values()],
                "user": {"name": self.cfg.web["user_name"], "avatar": bool(self.cfg.web["user_avatar"])}}

    def reminder_history(self, turn_id):
        # Creation events are append-only. The idempotency cache can replace an effect after cancellation.
        rows = self.store.all("SELECT data result, created FROM events WHERE turn_id=? AND source='router' AND type='reminder.added' ORDER BY id", turn_id)
        snapshots = {}
        for row in rows + self.store.all("SELECT result, created FROM effects WHERE turn_id=? AND tool='reminder_add' ORDER BY created, key", turn_id):
            saved = json.loads(row["result"])
            snapshots.setdefault(saved["id"], (row["created"], {key: saved.get(key) for key in ("id", "next_time", "text", "kind", "cron")}))
        return sorted(snapshots.values(), key=lambda pair: pair[0])

    def conversation(self, query):
        limit = max(1, number(query, "limit", 50, 100))
        before = number(query, "before", 2**63 - 1, 2**63 - 1)
        after = number(query, "after", 0, 2**63 - 1)
        order = "ASC" if "after" in query else "DESC"
        rows = self.store.all("SELECT c.*, COALESCE(m.turn_id,c.turn_id) actual_turn, m.status message_status "
                              "FROM conversation c LEFT JOIN messages m ON c.role='user' AND c.source_id=m.id "
                              "WHERE c.id<? AND c.id>? ORDER BY c.id " + order + " LIMIT ?", before, after, limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        items = []
        links = self.task_links()
        pending_question = self.router_question()
        for row in sorted(rows, key=lambda r: r["id"]):
            turn_id = row["actual_turn"]
            events = self.store.all("SELECT id, type, task_id, subject, data FROM events WHERE turn_id=? AND source='router' "
                                    "AND (type='tool' OR type LIKE 'task.%' OR type IN ('item.created','item.updated')) "
                                    "ORDER BY id LIMIT 101", turn_id) if turn_id else []
            reminders = []
            snapshots = self.reminder_history(turn_id) if turn_id else []
            if row["role"] == "router" and turn_id:
                previous = self.store.one("SELECT MAX(created) created FROM conversation WHERE role='router' AND turn_id=? AND id<?",
                                          turn_id, row["id"])["created"] or 0
                last = self.store.one("SELECT MAX(id) id FROM conversation WHERE role='router' AND turn_id=?", turn_id)["id"]
                for created, saved in snapshots:
                    # Calls after the final reply still belong to it, even when the turn ends without more speech.
                    if previous < created and (created <= row["created"] or row["id"] == last):
                        reminders.append(saved)
            outcomes = {}
            for completed in self.store.all("SELECT data FROM events WHERE turn_id=? AND source='router' AND type='tool.completed' ORDER BY id", turn_id):
                outcome = json.loads(completed["data"])
                outcomes.setdefault(outcome["call_key"], []).append(outcome["ok"])
            linked = {r["task_id"] for r in events if r["task_id"]}
            if turn_id:
                linked.update(r["task_id"] for r in self.store.all("SELECT task_id FROM messages WHERE turn_id=? AND task_id IS NOT NULL", turn_id))
            steps = []
            for event in events[:100]:
                data = json.loads(event["data"])
                name = data.get("tool", event["type"])
                step = {"id": event["id"], "name": name, "text": event["data"][:2000]}
                task = self.store.task(event["task_id"]) if event["task_id"] else None
                if event["type"] in ("task.started", "task.message") and task:
                    step["summary"] = ("Started " if event["type"] == "task.started" else "Sent a message to ") + task["worker_name"]
                elif event["type"] in ("item.created", "item.updated"):
                    item = self.store.one("SELECT name FROM items WHERE code=?", event["subject"])
                    step["summary"] = ("Created item " if event["type"] == "item.created" else "Updated item ") + (item["name"] if item else event["subject"])
                # Only a matching completed call may be replaced by its action/card. Failed or legacy calls remain visible.
                matched = outcomes.get(data.get("call_key"), []) if event["type"] == "tool" else []
                succeeded = matched.pop(0) if matched else False
                tool = name.removeprefix("mcp__router__")
                success_type = {"task_start": "task.started", "task_send": "task.message", "item_create": "item.created", "item_update": "item.updated"}.get(tool)
                replacement = bool(snapshots) if tool == "reminder_add" else any(e["type"] == success_type for e in events[:100])
                step["hidden"] = tool == "reply" or (succeeded and replacement)
                steps.append(step)
                # Tool events carry task_send/interrupt ids in their serialized input.
                try:
                    inp = json.loads(data.get("input", "{}"))
                    if isinstance(inp, dict) and re.fullmatch(r"t[0-9]+", str(inp.get("task_id", ""))):
                        linked.add(inp["task_id"])
                except (ValueError, TypeError):
                    pass
            kind, title = worker_notice(self.store.db, row["kind"], turn_id, row["source_id"]) if row["role"] == "router" else (row["kind"], "")
            asked = self.store.one("SELECT options FROM questions WHERE outbox_id=?", row["source_id"]) \
                if row["role"] == "router" and row["kind"] == "question" else None
            items.append({"id": row["id"], "role": row["role"], "text": row["text"], "channel": row["channel"],
                          "kind": kind, "title": title, "created": row["created"], "turn_id": turn_id,
                          "status": row["message_status"], "tasks": sorted(linked), "steps": steps, "reminders": reminders,
                          "workers": [self.task_summary(t, links) for task_id in sorted(linked) if (t := self.store.task(task_id))],
                          "steps_truncated": len(events) > 100, "options": json.loads(asked["options"]) if asked else [],
                          "current_question": bool(pending_question and pending_question["id"] == row["id"])})
        return {"items": items, "has_more": more, "next_before": items[0]["id"] if items else None,
                "next_after": items[-1]["id"] if items else after}

    def task_relations(self, task_id):
        """Read both directions, including dependencies outside the bounded state list."""
        result = {}
        for name, owner, peer in (("upstream", "task_id", "upstream"), ("downstream", "upstream", "task_id")):
            rows = self.store.all("SELECT d.%s id, t.seq, t.name, t.status, d.satisfied FROM task_deps d "
                                  "LEFT JOIN tasks t ON t.id=d.%s WHERE d.%s=? ORDER BY t.seq, d.%s" %
                                  (peer, peer, owner, peer), task_id)
            result[name] = [{"id": row["id"], "name": row["name"], "status": row["status"],
                             "worker_name": worker_name(row["seq"]) if row["seq"] else row["id"],
                             "satisfied": row["satisfied"] is not None} for row in rows]
        return result

    async def detail(self, task_id, query):
        task = dict(self.service.tasks._require(task_id))
        summary = self.task_summary(task) | {"cwd": task["cwd"], "brief": task["brief"][:TEXT_LIMIT],
                                             "report": task["report"][:TEXT_LIMIT], "reason": task["reason"][:TEXT_LIMIT],
                                             "session_id": task["session_id"]} | self.task_relations(task_id)
        if query.get("metadata") == ["1"]:
            return {"task": summary}
        offset = number(query, "offset", 0, TRANSCRIPT_ITEMS)
        limit = max(1, number(query, "limit", 100, 200))
        snapshot = query.get("snapshot", [""])[0]
        self.snapshots = {key: value for key, value in self.snapshots.items() if value["expires"] > time.monotonic()}
        if snapshot:
            saved = self.snapshots.get(snapshot)
            if not saved or saved["task_id"] != task_id:
                raise HTTPError(409, "Timeline snapshot expired; use Refresh timeline")
            saved["pinned"] = True
        else:
            if offset:
                raise HTTPError(400, "snapshot is required when paging a timeline")
            harness = session_harness(self.cfg, task["profile"], self.store.get("task_harness:" + task_id))
            items, path = [], None
            if harness:
                items = await asyncio.to_thread(harness.transcript, self.cfg, task["session_id"],
                                                max_bytes=TRANSCRIPT_BYTES, text_limit=TEXT_LIMIT)
                path = await asyncio.to_thread(harness.session_file, self.cfg, task["session_id"])
            truncated = bool(path and path.stat().st_size > TRANSCRIPT_BYTES)
            source = "transcript"
            if not items:
                source = "events"
                rows = self.store.run("SELECT type, substr(data,1,16000) data, created FROM events WHERE task_id=? ORDER BY id DESC", task_id)
                used = 0
                try:
                    for row in rows:
                        used += len(row["data"].encode("utf-8"))
                        if used > TRANSCRIPT_BYTES:
                            truncated = True
                            break
                        item = {"kind": "event", "time": row["created"], "text": row["data"], "tool": row["type"]}
                        if timeline_visible(item):
                            items.append(item)
                            if len(items) > TRANSCRIPT_ITEMS:
                                break
                finally:
                    rows.close()
                items.reverse()
            items = [item for item in items if timeline_visible(item)]
            truncated |= len(items) > TRANSCRIPT_ITEMS
            saved = {"task_id": task_id, "items": items[-TRANSCRIPT_ITEMS:], "truncated": truncated,
                     "source": source, "expires": time.monotonic() + 300, "pinned": False}
            refresh = query.get("refresh", [""])[0]
            previous = self.snapshots.get(refresh)
            if previous and previous["task_id"] == task_id:
                # A live tab replaces its own slot, not other readers' pages.
                snapshot = refresh
            else:
                while len(self.snapshots) >= 4:
                    candidates = [key for key, value in self.snapshots.items() if not value["pinned"]]
                    if not candidates:
                        raise HTTPError(429, "Four timelines are being read; retry after a snapshot expires")
                    self.snapshots.pop(candidates[0])
                snapshot = secrets.token_urlsafe(16)
            self.snapshots[snapshot] = saved
        items = saved["items"]
        if query.get("tail") == ["1"]:
            offset = max(0, len(items) - limit)
        page = items[offset:offset + limit]
        return {"task": summary, "timeline": page, "source": saved["source"], "truncated": saved["truncated"],
                "snapshot": snapshot, "total": len(items), "offset": offset,
                "previous_offset": max(0, offset - limit) if offset else None,
                "next_offset": offset + len(page) if offset + len(page) < len(items) else None}

    async def diff(self, task_id):
        task = self.service.tasks._require(task_id)
        if not task["branch"] or not task["cwd"]:
            raise HTTPError(409, "task has no git branch")
        # Ref names come from our DB, not the URL. --no-ext-diff/--no-textconv
        # prevent repo configuration from running programs just to view a diff.
        async def git(*args, cap=DIFF_LIMIT):
            proc = await asyncio.create_subprocess_exec("git", "--no-pager", *args, cwd=task["cwd"],
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                output = await asyncio.wait_for(proc.stdout.read(cap + 1), 10)
                # StreamReader.read(n) may return less than n before EOF.
                while len(output) <= cap:
                    chunk = await asyncio.wait_for(proc.stdout.read(cap + 1 - len(output)), 10)
                    if not chunk:
                        break
                    output += chunk
                if len(output) > cap:
                    proc.kill()
                code = await asyncio.wait_for(proc.wait(), 3)
                if code and len(output) <= cap:
                    raise HTTPError(409, "git diff unavailable")
                return output[:cap].decode("utf-8", errors="replace"), len(output) > cap
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        base = task["base_commit"]
        if not base:
            # Existing tasks: the oldest branch reflog entry records its creation.
            reflog, cut = await git("reflog", "show", "--format=%H", task["branch"], "--", cap=65536)
            if cut or not reflog.strip():
                raise HTTPError(409, "fork point unavailable for this older task")
            base = reflog.splitlines()[-1]
        if not re.fullmatch(r"[0-9a-f]{40,64}", base):
            raise HTTPError(409, "invalid fork point")
        output, truncated = await git("diff", "--no-ext-diff", "--no-textconv", base + ".." + task["branch"], "--")
        return {"text": output, "truncated": truncated, "base": base, "limit": DIFF_LIMIT,
                "scope": "committed branch changes since fork (working tree excluded)"}

    def avatar(self):
        path = expand(self.cfg.web["user_avatar"])
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower())
        if not mime:
            raise HTTPError(404, "avatar unavailable")
        try:
            with path.open("rb") as file:
                data = file.read(2 * 1024 * 1024 + 1)
        except OSError:
            raise HTTPError(404, "avatar unavailable")
        if len(data) > 2 * 1024 * 1024:
            raise HTTPError(413, "avatar too large")
        return data, mime

    def swap(self):
        if sys.platform != "darwin":
            return "Unavailable on this platform"
        try:
            return subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True,
                                  text=True, timeout=2).stdout.strip() or "Unavailable"
        except (OSError, subprocess.TimeoutExpired):
            return "Unavailable"

    async def provider_probes(self):
        """Shallow probe per Provider and Harness, shared by every tab for 30 seconds."""
        async with self.probe_lock:
            if self.probes is None or time.monotonic() - self.probed_at >= 30:
                async def probe(provider_name, harness_name):
                    profiles = [p for p in self.cfg.profiles.values()
                                if p.provider == provider_name and p.harness == harness_name]
                    harness = get_harness(self.cfg, profiles[0])
                    provider = self.cfg.providers[provider_name]
                    start = time.monotonic()
                    healthy = await asyncio.to_thread(harness.shallow_probe, provider, self.cfg.failover["probe_timeout"])
                    return {"name": provider.name, "harness": harness_name, "healthy": healthy,
                            "latency": round(time.monotonic() - start, 3), "models": [p.model for p in profiles]}
                pairs = dict.fromkeys((p.provider, p.harness) for p in self.cfg.profiles.values())
                self.probes = await asyncio.gather(*(probe(*pair) for pair in pairs))
                self.probed_at = time.monotonic()
            return self.probes

    async def models(self):
        """Read-only model configuration: names only, never endpoints, credentials or paths."""
        healthy = {(p["name"], p["harness"]): p["healthy"] for p in await self.provider_probes()}
        router, cfg = self.router_state(), self.cfg
        role = lambda name: {"profile": name, "model": cfg.profile(name).model}
        return {"router": {"primary": role(cfg.primary), "backup": role(cfg.backup),
                           "active": "primary" if router["profile"] == cfg.primary else
                                     "backup" if router["profile"] == cfg.backup else "other",
                           "current": role(router["profile"]), "mode": router["mode"], "switching": router["switching"]},
                "profiles": [{"name": p.name, "harness": p.harness, "model": p.model, "provider": p.provider,
                              "default": p.name == cfg.default_profile, "healthy": healthy[(p.provider, p.harness)]}
                             for p in cfg.profiles.values()],
                "version": self.version}

    async def developer(self):
        await self.provider_probes()
        return {"providers": self.probes, "cache_age": round(time.monotonic() - self.probed_at, 1),
                "router": self.router_state(), "inbox": self.store.one("SELECT COUNT(*) n FROM messages WHERE status!='done'")["n"],
                "queued": self.store.one("SELECT COUNT(*) n FROM tasks WHERE status='queued'")["n"],
                "outbox": [dict(r) for r in self.store.all("SELECT channel, status, COUNT(*) count FROM outbox GROUP BY channel, status")],
                "live": len(self.service.tasks.live), "max_workers": self.service.tasks.max_workers,
                "swap": await asyncio.to_thread(self.swap), "version": self.version, "started": self.started,
                "home": str(self.cfg.home), "web": self.status()}

    async def dispatch(self, method, path, query, data):
        match = re.fullmatch(r'/api/router-files/(soul|memory)', path)
        if match:
            if query or (method == 'POST' and set(data) != {'content', 'version'}):
                raise HTTPError(400, 'invalid Router document arguments')
            files, name = self.service.router.files, match[1]
            try:
                if method == 'GET':
                    return await asyncio.to_thread(files.read, name)
                return await asyncio.to_thread(files.write, name, data['content'], data['version'])
            except DocumentError as exc:
                raise HTTPError(exc.status, str(exc)) from None
        if method == "GET":
            if path == "/api/state":
                return self.state()
            if path == "/api/usage":
                try:
                    start, end = usage.window(query)
                except ValueError as exc:
                    raise HTTPError(400, str(exc))
                key = (start, end)
                # Share a short cache across tabs and serialize scans across windows.
                # All SQL uses a separate read-only connection in the background.
                async with self.usage_lock:
                    now = time.monotonic()
                    self.usage_cache = {k: v for k, v in self.usage_cache.items() if now - v[0] < USAGE_CACHE_SECONDS}
                    if key not in self.usage_cache:
                        workers = {'running': len(self.service.tasks.live), 'limit': self.service.tasks.max_workers}
                        result = await asyncio.to_thread(usage.read_report, self.cfg, workers, start, end)
                        while len(self.usage_cache) >= USAGE_CACHE_WINDOWS:
                            self.usage_cache.pop(next(iter(self.usage_cache)))
                        self.usage_cache[key] = (time.monotonic(), result)
                    result = self.usage_cache[key][1]
                    return result | {'workers': result['workers'] | {
                        'running': len(self.service.tasks.live), 'limit': self.service.tasks.max_workers}}
            if path == "/api/conversation":
                return self.conversation(query)
            if path == "/api/developer":
                return await self.developer()
            if path == "/api/models":
                return await self.models()
            if path == "/api/items":
                items, truncated = self.items()
                return {"items": items, "truncated": truncated}
            match = re.fullmatch(r"/api/items/([a-z][a-z0-9-]*-[0-9]+)", path)
            if match:
                return self.item_detail(match[1])
        if method == "GET" and path == "/api/reminders":
            return self.service.reminders.list()
        if method == "POST" and path == "/api/reminders/cancel":
            try:
                result = self.service.reminders.cancel(text_field(data, "reminder_id"))
            except ValueError as exc:
                raise HTTPError(400, str(exc))
            if not result["ok"]:
                raise HTTPError(404, result["error"])
            return result
        if method == "POST" and path == "/api/timeline/release":
            self.snapshots.pop(text_field(data, "snapshot"), None)
            return {"ok": True}
        if method == "POST" and path == "/api/messages":
            text = text_field(data, "text")
            key = text_field(data, "request_id")
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
                raise HTTPError(400, "invalid request_id")
            old = self.store.one("SELECT m.id, m.text FROM web_requests w JOIN messages m ON m.id=w.message_id WHERE w.key=?", key)
            if old:
                if old["text"] != text:
                    raise HTTPError(409, "request_id already used for different text")
                return {"id": old["id"]}
            with self.store.atomic():
                message_id = self.service.router.submit("user", text, "web")
                self.store.run("INSERT INTO web_requests(key, message_id) VALUES(?,?)", key, message_id)
            return {"id": message_id}
        match = re.fullmatch(r"/api/tasks/(t[0-9]+)(?:/(send|interrupt|resume|stop|model|diff))?", path)
        if match:
            task_id, action = match.groups()
            if not self.store.task(task_id):
                raise HTTPError(404, "no such task")
            if method == "GET" and not action:
                return await self.detail(task_id, query)
            if method == "GET" and action == "diff":
                return await self.diff(task_id)
            if method == "POST":
                tasks = self.service.tasks
                if action == "send":
                    result = tasks.send(task_id, text_field(data, "text"), text_field(data, "expected_status", False))
                elif action == "interrupt":
                    result = tasks.interrupt(task_id)
                elif action == "resume":
                    result = tasks.resume(task_id, text_field(data, "text", False) or None)
                elif action == "stop":
                    result = await tasks.stop(task_id)
                elif action == "model":
                    profile = text_field(data, "profile")
                    if profile not in self.cfg.profiles:
                        raise HTTPError(400, "unknown profile")
                    result = await tasks.switch_model(task_id, profile)
                else:
                    raise HTTPError(405, "method not allowed")
                if not result.get("ok"):
                    raise HTTPError(409, result.get("error", "operation failed"))
                return result
        raise HTTPError(404, "not found")

    async def stream(self, reader, writer, headers):
        self.streams.add(writer)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\nretry: 1000\n\n")
        async def emit(event, data):
            self.sequence += 1
            writer.write(("id: %s:%d\nevent: %s\ndata: %s\n\n" %
                          (self.epoch, self.sequence, event, json.dumps(data, ensure_ascii=False))).encode())
            await asyncio.wait_for(writer.drain(), 5)
        # Last-Event-ID is intentionally not a replay offset: DB IDs are the
        # durable cursor, and reset is valid even when the daemon epoch changed.
        await emit("reset", {"reconcile": True})
        previous, messages, state_stamp, activity = -1, -1, None, None
        heartbeat = time.monotonic()
        while not reader.at_eof() and not self.service.stopping.is_set() and self.authorized(headers):
            revision = self.store.db.total_changes
            router = self.router_state()
            if revision != previous or router != activity:
                latest = self.store.one("SELECT COALESCE(MAX(id),0) id FROM conversation")["id"]
                if latest != messages or revision != previous:
                    await emit("message", {"latest": latest})
                    messages = latest
                stamp = (tuple(self.store.one("SELECT COUNT(*), MAX(updated) FROM tasks")),
                         tuple(self.store.one("SELECT COUNT(*), MAX(updated), SUM(status='active'), SUM(next_at) FROM reminders")),
                         tuple(self.store.one("SELECT COUNT(*), MAX(updated) FROM items")),
                         tuple(self.store.one("SELECT COUNT(*), COUNT(satisfied) FROM task_deps")))
                if stamp != state_stamp:
                    await emit("state", {"changed": True})
                    state_stamp = stamp
                if router != activity:
                    await emit("activity", router)
                    activity = router
                previous = revision
            if time.monotonic() - heartbeat > 10:
                writer.write(b": keepalive\n\n")
                await asyncio.wait_for(writer.drain(), 5)
                heartbeat = time.monotonic()
            await asyncio.sleep(0.25)
