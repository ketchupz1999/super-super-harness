"""Bounded, credential-redacted stderr diagnostics for the Hermes child only."""
from collections import deque
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote

LINES = 40
BYTES = 16384
LINE_BYTES = 4096
_SECRET_NAME = re.compile(r"key|token|secret|password|authorization", re.I)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_LABEL = re.compile(r"(?i)([\w-]*(?:key|token|secret|password|authorization)[\w-]*[\"']?\s*[:=]\s*)(?:[\"'][^\"']*[\"']|[^\s,;]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_AUTH = re.compile(r"(?i)([\w-]*authorization[\w-]*[\"']?\s*[:=]\s*).*$")
_URL_AUTH = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")


class StderrTail:
    def __init__(self, env, home):
        values = [value for name, value in env.items() if _SECRET_NAME.search(name)]
        try:
            servers = json.loads((Path(home) / "mcp.json").read_text()).get("mcpServers", {})
            for server in servers.values():
                values.extend(value for name, value in server.get("env", {}).items() if _SECRET_NAME.search(name))
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        self.secrets = set()
        for value in values:
            if isinstance(value, str) and value:
                self.secrets.update((value, json.dumps(value)[1:-1], quote(value, safe="")))
        self.lines = deque()
        self.size = 0

    def append(self, raw):
        line = _ANSI.sub("", raw.decode("utf-8", errors="replace"))
        line = "".join(c for c in line if c.isprintable() or c == "\t")
        for secret in sorted(self.secrets, key=len, reverse=True):
            line = line.replace(secret, "<redacted>")
        # Schemes such as Basic/Digest/Negotiate can carry credentials after a
        # space or comma. Redact the entire authorization value, not just its scheme.
        line = _AUTH.sub(r"\1<redacted>", line)
        line = _URL_AUTH.sub(r"\1<redacted>@", line)
        line = _BEARER.sub("Bearer <redacted>", line)
        line = _LABEL.sub(r"\1<redacted>", line)
        if not line:
            return
        size = len(line.encode("utf-8")) + 1
        if size > BYTES:
            line = "[stderr line omitted: too long]"
            size = len(line.encode("utf-8")) + 1
        self.lines.append(line)
        self.size += size
        while len(self.lines) > LINES or self.size > BYTES:
            self.size -= len(self.lines.popleft().encode("utf-8")) + 1

    async def read(self, stream):
        pending, dropping = b"", False
        try:
            while chunk := await stream.read(4096):
                pending += chunk.replace(b"\r", b"\n")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if dropping or len(line) > LINE_BYTES:
                        self.append(b"[stderr line omitted: too long]")
                    else:
                        self.append(line)
                    dropping = False
                if len(pending) > LINE_BYTES:
                    pending, dropping = b"", True
        finally:
            if dropping:
                self.append(b"[stderr line omitted: too long]")
            elif pending:
                self.append(pending)

    def text(self):
        return "\n".join(self.lines)

    def save(self, path):
        if not path or not self.lines:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as target:
            if not stat.S_ISREG(os.fstat(target.fileno()).st_mode):
                raise ValueError("Hermes log must be a regular file")
            os.fchmod(target.fileno(), 0o600)
            target.write("\n[Hermes stderr tail]\n" + self.text() + "\n")
