"""Claude Code adapter: stream-json process control for Workers and the Router."""
import asyncio
from collections import deque
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid as uuidlib

from .. import procs

# Only these reach a child. Inheriting CLAUDE_CODE_CHILD_SESSION makes interactive sessions unsaved.
ENV_ALLOW = ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
             "SSH_AUTH_SOCK", "__CF_USER_TEXT_ENCODING", "GOPROXY")


# Prevent accidental pushes, not deliberate bypasses (防误操作，不是防绕过).
# GIT_CONFIG_PARAMETERS works with git 2.30 too. Explicit pushurl bypasses this guard;
# a user's longer matching pushInsteadOf prefix also wins and can push to a real remote.
# A prefix-based guard cannot fix that limitation; it is not a security boundary.
PUSH_BLOCK = " ".join("'url.ssharness-push-blocked://x/.pushinsteadof=%s'" % prefix
                      for prefix in ("https://", "http://", "git@", "ssh://", "/", "file://", ""))


def build_env(cfg, profile, tmpdir=None):
    provider = cfg.providers[profile.provider]
    env = {name: os.environ[name] for name in ENV_ALLOW if name in os.environ}
    bare = profile.model.replace("[1m]", "")
    env.update({
        "ANTHROPIC_BASE_URL": provider.base_url,
        provider.auth_env: provider.key(),
        "ANTHROPIC_MODEL": profile.model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": profile.model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": profile.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": profile.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": provider.small_model or bare,
        "CLAUDE_CODE_SUBAGENT_MODEL": profile.model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_PARAMETERS": PUSH_BLOCK,
    })
    if profile.context_window:
        # Claude Code assumes a small window for models it does not know and would compact too early.
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(profile.context_window)
    if tmpdir:
        env["TMPDIR"] = str(tmpdir)
    env.update(profile.env)
    return env


_ensured = {}


def ensure_provider(provider):
    """Optional trusted local preparation; never log its command or output."""
    cache_key = (provider.name, provider.base_url, provider.ensure)
    if not provider.ensure or time.time() - _ensured.get(cache_key, 0) < 60:
        return
    try:
        done = subprocess.run(os.path.expanduser(provider.ensure).split(), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("provider preparation failed") from None
    if done.returncode:
        raise ValueError("provider preparation failed")
    _ensured[cache_key] = time.time()


def shallow_probe(provider, timeout=5):
    """Does the gateway answer at all? A connection that hangs is the failure Claude Code stays silent about."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(provider.base_url + "/", timeout=timeout).read(16)
        return True
    except urllib.error.HTTPError:
        return True
    except (OSError, http.client.HTTPException):   # an endpoint that does not speak HTTP raises HTTPException
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Credentials must never follow an endpoint's redirect to another service.
        return None


def deep_probe(provider, model, timeout=30):
    """One real, tiny model call, for when the gateway answers but the model behind it may not."""
    return deep_check(provider, model, timeout)[0]


ERROR_TYPE = re.compile(r"[a-z_]{1,60}")


def _error_type(payload):
    """None unless payload is an Anthropic error object; then its type, if that is a plain identifier, else "unknown".
    Never the message: it may echo a secret."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "error":
        return None
    kind = data["error"].get("type") if isinstance(data.get("error"), dict) else None
    return kind if isinstance(kind, str) and ERROR_TYPE.fullmatch(kind) else "unknown"


def deep_check(provider, model, timeout=30):
    """deep_probe with a reason: (ok, reason). The reason holds only status codes, error types and exception class names,
    never the key, the URL or an upstream message.

    The call streams. A gateway in front of a thinking model (for example a proxy for a non-Anthropic model) ignores max_tokens, and
    without a stream it answers only once the model has thought it through, which can outlast a short timeout;
    the stream's first event comes at once."""
    body = json.dumps({"model": model.replace("[1m]", ""), "max_tokens": 1, "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        request = urllib.request.Request(provider.base_url + "/v1/messages", body, method="POST", headers={
            "content-type": "application/json", "anthropic-version": "2023-06-01", **provider.auth_headers()})
        with opener.open(request, timeout=timeout) as response:
            if "text/event-stream" not in response.headers.get("content-type", ""):
                response.read(16)   # a gateway that does not stream answered with the whole message
                return True, ""
            for _ in range(20):
                line = response.readline(65536)
                if not line:
                    break
                if line.startswith(b"data:"):
                    kind = _error_type(line[5:])
                    return (False, "the model answered with an error event (%s)" % kind) if kind else (True, "")
            return False, "the model's stream ended before its first event"
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return False, "the endpoint redirected (HTTP %d); credentials are never sent on" % exc.code
        try:
            kind = _error_type(exc.read(65536))
        except OSError:
            kind = None
        return False, "HTTP %d%s" % (exc.code, " " + kind if kind else "")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return False, "no answer within %g seconds" % timeout
        return False, "connection failed (%s)" % type(exc.reason).__name__
    except TimeoutError:
        return False, "no answer within %g seconds" % timeout
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return False, "request failed (%s)" % type(exc).__name__


def binary(cfg, profile):
    return os.path.expanduser(cfg.harnesses[profile.harness]["binary"])


def available(cfg, profile):
    return shutil.which(binary(cfg, profile)) is not None


def common_args(profile, mcp_config, system_prompt, hooks=None):
    # Our hooks only work if the user's own stay out, so a session with hooks reads project settings alone.
    sources, settings = ("project", {"hooks": hooks}) if hooks else (profile.settings, {"disableAllHooks": True})
    args = ["--model", profile.model, "--dangerously-skip-permissions", "--setting-sources", sources,
            "--settings", json.dumps(settings), "--strict-mcp-config"]
    if mcp_config:
        args += ["--mcp-config", str(mcp_config)]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if profile.tools != "all":
        args += ["--tools", profile.tools]
    for path in profile.add_dirs:
        args += ["--add-dir", path]
    return args


def headless_argv(cfg, profile, session_id, resume, mcp_config, system_prompt):
    args = [binary(cfg, profile), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--replay-user-messages"] + common_args(profile, mcp_config, system_prompt)
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    return args


def interactive_argv(cfg, profile, session_id, mcp_config, system_prompt, fork=False, hooks=None):
    # Going through /usr/bin/env matters on this Mac: direct launches from Python under a TTY get SIGKILLed.
    args = ["/usr/bin/env", binary(cfg, profile)] + common_args(profile, mcp_config, system_prompt, hooks) + ["--resume", session_id]
    return args + ["--fork-session"] if fork else args


def session_file(cfg, session_id):
    for folder in cfg.session_root.glob("*"):
        path = folder / (session_id + ".jsonl")
        if path.exists():
            return path
    return None


def session_has(cfg, session_id, marker):
    """True only when marker is in a real user turn. A queue-operation line is not seen by the model after resume."""
    path = session_file(cfg, session_id)
    if not path:
        return False
    with path.open(errors="ignore") as lines:
        for line in lines:
            try:
                row = json.loads(line)
                content = (row.get("message") or {}).get("content", "")
                texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content
                                                                            if isinstance(b, dict) and b.get("type") == "text"]
                if row.get("type") == "user" and (not marker or any(marker in text for text in texts)):
                    return True
            except ValueError:
                pass
    return False


def prepare_session(cfg, session_id):
    """Returns (session_id, resume). --session-id refuses an id whose file exists and --resume needs a real turn,
    so a session whose first message never landed starts over under a new id."""
    if not session_file(cfg, session_id):
        return session_id, False
    if session_has(cfg, session_id, ""):
        return session_id, True
    return str(uuidlib.uuid4()), False


def transcript(cfg, session_id, *, max_bytes=None, limit=None, text_limit=None):
    """Normalize the Harness log. Web callers can bound IO, retained items and text.

    The default remains the full transcript for CLI / Router callers. A bounded
    read keeps the tail and discards its partial first JSON line.
    """
    path = session_file(cfg, session_id)
    if not path:
        return []
    items = deque(maxlen=limit)
    with path.open("rb") as source:
        if max_bytes:
            size = source.seek(0, os.SEEK_END)
            source.seek(max(0, size - max_bytes))
            if size > max_bytes:
                source.readline(max_bytes)
            rows = source.read(max_bytes).decode("utf-8", errors="replace").split("\n")
        else:
            rows = source.read().decode("utf-8", errors="replace").split("\n")
    for line in rows:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") not in ("user", "assistant"):
            continue
        stamp = row.get("timestamp", "")
        content = (row.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                items.append({"time": stamp, "kind": "received" if row["type"] == "user" else "said", "text": block.get("text", "")})
            elif kind == "thinking":
                items.append({"time": stamp, "kind": "thinking", "text": block.get("thinking", "")})
            elif kind == "tool_use":
                items.append({"time": stamp, "kind": "tool_call", "tool": block.get("name"),
                              "text": json.dumps(block.get("input"), ensure_ascii=False)})
            elif kind == "tool_result":
                body = block.get("content")
                text = body if isinstance(body, str) else " ".join(b.get("text", "") for b in body or [] if isinstance(b, dict))
                items.append({"time": stamp, "kind": "tool_result", "error": bool(block.get("is_error")), "text": text})
    if text_limit:
        for item in items:
            if len(item["text"]) > text_limit:
                item["text"] = item["text"][:text_limit] + "\n… [truncated]"
                item["truncated"] = True
    return list(items)


class Process:
    """One stream-json Harness process. Calls on_event for every message and on_exit once at the end."""

    def __init__(self, argv, env, cwd, on_event, on_exit, log_path=None):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.on_event, self.on_exit = on_event, on_exit
        self.log_path = log_path
        self.proc = None
        self.pid = None
        self.closing = False
        self.exited = asyncio.Event()

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=str(self.cwd), env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=2 ** 27, start_new_session=True)
        self.pid = self.proc.pid
        self._reader = asyncio.create_task(self._read())
        return self.pid

    async def _read(self):
        log = open(self.log_path, "ab") if self.log_path else None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    if log:
                        log.write(line)
                        log.flush()
                    continue
                await self.on_event(msg)
        except asyncio.CancelledError:
            # Waiting below on a live child would hang shutdown forever.
            self.closing = True
            self.proc.kill()
            raise
        finally:
            if log:
                log.close()
            code = await self.proc.wait()
            self.exited.set()
            await self.on_exit(code)

    def _write(self, obj):
        if not self.proc or self.proc.returncode is not None or self.proc.stdin.is_closing():
            return False
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        return True

    def send(self, text, uuid=None):
        uuid = uuid or str(uuidlib.uuid4())
        ok = self._write({"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}})
        return uuid if ok else None

    def interrupt(self):
        return self._write({"type": "control_request", "request_id": str(uuidlib.uuid4()), "request": {"subtype": "interrupt"}})

    async def close(self, wait=5):
        """End the process and everything it started."""
        self.closing = True
        if not self.proc or self.exited.is_set():
            return
        tree = await asyncio.to_thread(procs.descendants, self.pid)
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.exited.wait(), wait)
        except asyncio.TimeoutError:
            pass
        await asyncio.to_thread(procs.kill, tree + [self.pid])
        await self.exited.wait()
