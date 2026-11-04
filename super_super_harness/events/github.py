"""Read-only GitHub notifications through gh's own authentication; no token handling."""
import asyncio
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
import hashlib
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlencode, urlsplit

from .bus import normalize

STATE_KEY = "source:github:notifications"
HEALTH_KEY = "source:github:health"


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def notification_event(row):
    """Keep useful fields only, with bounded strings; never use an API URL as a browser link."""
    repo = row["repository"]["full_name"]
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    subject = row["subject"]
    reason = row["reason"].lower()
    subject_url = subject.get("url") or ""
    path = urlsplit(subject_url).path
    match = re.fullmatch(r"/repos/" + re.escape(repo) + r"/(issues|pulls|discussions)/(\d+)", path)
    number = match[2] if match else ""
    url = "https://github.com/" + repo
    if match:
        url += "/%s/%s" % ({"pulls": "pull", "issues": "issues", "discussions": "discussions"}[match[1]], number)
    else:
        commit = re.fullmatch(r"/repos/" + re.escape(repo) + r"/commits/([0-9a-fA-F]+)", path)
        if commit:
            url += "/commit/" + commit[1]
    title = str(subject.get("title", ""))[:8000]
    kind = str(subject.get("type", ""))[:100]
    data = {"id": str(row["id"])[:200], "updated_at": row["updated_at"], "reason": reason,
            "repository": {"full_name": repo},
            "subject": {"title": title, "type": kind, "url": subject_url[:2000]},
            "unread": row.get("unread") is True}
    return normalize({"source": "github", "type": "github." + reason,
                      "id": "%s:%s" % (row["id"], row["updated_at"]),
                      "thread": "github:" + repo + ("#" + number if number else ""),
                      "text": " ".join(("%s · %s · %s · %s" % (repo, kind, title, reason)).split()),
                      "url": url, "data": data})


def response(output):
    # gh --include uses one HTTP status/header block followed by JSON.
    header, separator, body = output.replace("\r\n", "\n").partition("\n\n")
    lines = header.splitlines()
    if not separator or not lines or not re.match(r"HTTP/\S+ \d{3}\b", lines[0]):
        raise ValueError("invalid response")
    status = int(lines[0].split()[1])
    headers = {key.lower(): value.strip() for line in lines[1:] if ":" in line for key, value in [line.split(":", 1)]}
    return status, headers, body


class GitHubNotifications:
    def __init__(self, store, bus, options, clock=time.time):
        self.store, self.bus, self.options, self.clock = store, bus, options, clock
        self.task = None
        self.health = store.get(HEALTH_KEY, "healthy")
        self.health_pending = False
        saved = store.get(STATE_KEY)
        self.state = json.loads(saved) if saved else {"started": clock(), "since": clock(), "modified": "", "poll": 60}
        if not saved:
            # Persist before the first request, including when gh is unavailable at startup.
            self.state["since"] = self.state["started"]
            store.set(STATE_KEY, json.dumps(self.state))

    @property
    def interval(self):
        return max(self.options["interval_seconds"], self.state["poll"], math.ceil(self.state.get("retry_at", 0) - self.clock()))

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
                await asyncio.sleep(self.interval)
            except Exception:
                # Last-resort supervision, including failures outside the poll body. Cancellation still propagates.
                self._health("unavailable")
                await asyncio.sleep(max(60, self.options["interval_seconds"]))

    def _health(self, health):
        changed = health != self.health
        if not changed and not self.health_pending:
            return
        self.health, self.health_pending = health, True
        recovered = health == "healthy"
        message = ("GitHub notifications recovered (已恢复)." if recovered else
                   "GitHub notifications unavailable; check gh installation, gh auth login, network and local storage. Retrying at the polling interval.")
        try:
            with self.store.atomic():
                self.store.event("github", "source.recovered" if recovered else "source.warn",
                                 severity="info" if recovered else "warn", message=message)
                self.store.set(HEALTH_KEY, health)
        except Exception:
            # Retry the latest pending transition on later polls, without repeating fallback logs or leaking exceptions.
            if changed:
                logging.getLogger(__name__).warning("%s Status could not be stored; using the service log.", message)
        else:
            self.health_pending = False

    async def request(self, page):
        query = urlencode({"all": "true", "since": iso(self.state["since"] - 1), "per_page": 100, "page": page})
        args = ["gh", "api", "--hostname", "github.com", "--method", "GET", "notifications?" + query, "--include",
                "--header", "Accept: application/vnd.github+json"]
        if page == 1:
            args += ["--header", "If-Modified-Since: " + (self.state["modified"] or formatdate(self.state["since"] - 1, usegmt=True))]
        env = dict(os.environ, **self.options["env"], GH_PROMPT_DISABLED="1")
        env.pop("GH_DEBUG", None)
        env.pop("DEBUG", None)
        proc = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 30)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        # Neither stderr nor the raw response is ever logged (gh can include credentials).
        if not output:
            raise ValueError("gh failed")
        status, headers, body = response(output.decode("utf-8"))
        poll = headers.get("x-poll-interval", "")
        if poll.isdigit() and int(poll) > 0:
            self.state["poll"] = max(60, int(poll))
        retry = headers.get("retry-after", "")
        retry_at = self.clock() + int(retry) if retry.isdigit() else 0
        if retry and not retry.isdigit():
            try:
                retry_at = parsedate_to_datetime(retry).timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
        reset = headers.get("x-ratelimit-reset", "")
        if headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
            retry_at = max(retry_at, int(reset))
        if retry_at > self.clock():
            self.state["retry_at"] = max(retry_at, self.state.get("retry_at", 0))
        self.store.set(STATE_KEY, json.dumps(self.state))
        if status not in (200, 304) or (proc.returncode and status != 304):
            raise ValueError("gh failed")
        return status, headers, body

    async def tick(self):
        try:
            if self.clock() < self.state.get("retry_at", 0):
                return
            events, snapshot, newest, modified, page = [], [], self.state["since"], self.state["modified"], 1
            repos = {repo.casefold() for repo in self.options["repos"]}
            while True:
                if self.clock() < self.state.get("retry_at", 0):
                    raise ValueError("pagination rate limited")
                status, headers, body = await self.request(page)
                if status == 304:
                    if page != 1:
                        raise ValueError("unexpected not modified page")
                    self._health("healthy")
                    return
                rows = json.loads(body)
                if not isinstance(rows, list):
                    raise ValueError("invalid notifications")
                if page == 1:
                    modified = headers.get("last-modified", "")
                for row in rows:
                    updated = timestamp(row["updated_at"])
                    snapshot.append((str(row["id"]), row["updated_at"]))
                    newest = max(newest, updated)
                    # GitHub timestamps have second precision. Keep the startup second (at most 1s overlap).
                    if updated < math.floor(self.state["started"]):
                        continue
                    if repos and row["repository"]["full_name"].casefold() not in repos:
                        continue
                    events.append(notification_event(row))
                if not re.search(r'rel="next"', headers.get("link", "")):
                    break
                page += 1
            state = dict(self.state, since=newest, modified=modified)
            fingerprint = hashlib.sha256(json.dumps(sorted(snapshot)).encode()).hexdigest()
            if page > 1 and fingerprint != self.state.get("snapshot"):
                # Offset pages are not a snapshot: removal on page 1 can move an unread row off page 2.
                # Emit what we saw, but keep the lower bound and rescan next poll until two scans agree.
                state.update(since=self.state["since"], modified="", snapshot=fingerprint)
            else:
                state.pop("snapshot", None)
            # Do not advance the cursor if any page failed. Trigger dedupe and progress commit together.
            with self.store.atomic():
                for event in sorted(events, key=lambda event: timestamp(event["data"]["updated_at"])):
                    self.bus.emit(event)
                self.store.set(STATE_KEY, json.dumps(state))
            self.state = state
            self._health("healthy")
        except Exception:
            self._health("unavailable")
