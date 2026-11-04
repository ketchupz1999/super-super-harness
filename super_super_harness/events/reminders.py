"""Durable local-time reminders. Claim and enqueue each occurrence in one transaction."""
import asyncio
from bisect import bisect_left, bisect_right
import calendar
from datetime import datetime, timedelta, tzinfo
from io import BytesIO
import logging
import os
import re
import struct
import subprocess
import sys
import time
import zoneinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .bus import Bus

LOG = logging.getLogger(__name__)
HORIZON_DAYS = 366 * 8
NOTIFICATION_SCRIPT = '''on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run'''


class LibcZone(tzinfo):
    """Use libc for POSIX TZ strings which are not IANA database keys."""
    @staticmethod
    def stamps(naive):
        fields = naive.timetuple()[:6]
        stamps = {time.mktime((*fields, -1, -1, dst)) for dst in (-1, 0, 1)}
        return sorted(s for s in stamps if time.localtime(s)[:6] == fields)

    def _stamp(self, date):
        naive = date.replace(tzinfo=None)
        stamps = self.stamps(naive)
        if stamps:
            return stamps[-1 if date.fold else 0] + date.microsecond / 1e6
        return time.mktime((*naive.timetuple()[:6], -1, -1, -1)) + date.microsecond / 1e6

    def utcoffset(self, date):
        if date is None:
            return None
        naive = calendar.timegm(date.replace(tzinfo=None).timetuple()) + date.microsecond / 1e6
        return timedelta(seconds=naive - self._stamp(date))

    def dst(self, date):
        if date is None:
            return None
        return self.utcoffset(date) + timedelta(seconds=time.timezone)

    def tzname(self, date):
        return time.localtime(self._stamp(date)).tm_zone if date else str(self)

    def fromutc(self, date):
        stamp = calendar.timegm(date.timetuple()) + date.microsecond / 1e6
        fields = time.localtime(stamp)
        naive = datetime(*fields[:6], microsecond=date.microsecond)
        stamps = self.stamps(naive)
        fold = int(len(stamps) > 1 and int(stamp) == int(stamps[-1]))
        return naive.replace(tzinfo=self, fold=fold)

    def __str__(self):
        return os.environ.get("TZ") or "/etc/localtime"


class _TimezoneFile(BytesIO):
    def read(self, size=-1):
        data = super().read(size)
        if size > 0 and not data:
            # Python 3.12 can loop on EOF after a footer's opening newline.
            raise ValueError("incomplete TZif file")
        return data


def _zone_from_file(path, key):
    with open(path, "rb") as file:
        data = file.read()
    if data[:4] == b"TZif" and data[4:5] >= b"2" and not data.endswith(b"\n"):
        raise ValueError("incomplete TZif footer")
    with _TimezoneFile(data) as file:
        return ZoneInfo.from_file(file, key=key)


def local_zone():
    name = os.environ.get("TZ", "").lstrip(":")
    try:
        if "TZ" in os.environ and not name:
            time.tzset()
            return LibcZone()
        if name and not name.startswith("/"):
            # Invalid relative keys must still go through ZoneInfo's validation.
            if not any(part in ("", ".", "..") for part in name.split("/")):
                for root in zoneinfo.TZPATH:
                    path = os.path.join(root, name)
                    if os.path.isfile(path):
                        return _zone_from_file(path, name)
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                time.tzset()
                return LibcZone()
        key = name or "local"
        if "TZ" not in os.environ:
            # macOS and Linux both link localtime into an IANA zoneinfo tree.
            try:
                target = os.path.realpath("/etc/localtime")
                if "/zoneinfo/" in target:
                    key = target.split("/zoneinfo/", 1)[1]
            except (OSError, ValueError):
                pass
        return _zone_from_file(name or "/etc/localtime", key)
    except (OSError, ValueError, struct.error, AssertionError):
        time.tzset()
        return LibcZone()


def time_context(now=None):
    local = datetime.fromtimestamp(time.time() if now is None else now, local_zone())
    return {"now": local.isoformat(timespec="seconds"), "timezone": str(local.tzinfo),
            "abbreviation": local.tzname()}


def local_stamp(stamp):
    return datetime.fromtimestamp(stamp, local_zone()).isoformat(timespec="seconds")


def local_candidates(naive, zone):
    # A spring gap has no instant; a fall fold has two. Never normalize a gap.
    values = set()
    for fold in (0, 1):
        stamp = naive.replace(tzinfo=zone, fold=fold).timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == naive:
            values.add(stamp)
    return sorted(values)


def cron_stamp(naive, zone):
    """First fold only; a missing wall minute maps to the end of its gap."""
    candidates = local_candidates(naive, zone)
    if candidates:
        return candidates[0]
    # Gaps may be half an hour or even a skipped calendar day. Locate their
    # boundary in wall seconds instead of normalizing 02:30 into 03:30.
    for hours in range(1, 49):
        if local_candidates(naive + timedelta(hours=hours), zone):
            break
    else:
        raise ValueError("timezone gap exceeds two days")
    low, high = 0, hours * 3600
    while low < high:
        middle = (low + high) // 2
        if local_candidates(naive + timedelta(seconds=middle), zone):
            high = middle
        else:
            low = middle + 1
    return local_candidates(naive + timedelta(seconds=low), zone)[0]


class Cron:
    """Numeric five-field cron: lists, inclusive ranges, steps, Sunday 0 or 7.

    As in Vixie cron, restricted day-of-month and day-of-week are ORed.
    Repeated minutes use the first instant; missing minutes use the gap end.
    """
    def __init__(self, expression):
        if not isinstance(expression, str) or len(expression) > 256:
            raise ValueError("cron must be a numeric five-field expression")
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("cron requires five fields: minute hour day month weekday")
        self.parts = parts
        self.fields = [self._field(p, low, high) for p, low, high in
                       zip(parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))]
        self.fields[4] = {v % 7 for v in self.fields[4]}
        self.minutes = [h * 60 + m for h in sorted(self.fields[1]) for m in sorted(self.fields[0])]

    @staticmethod
    def _field(field, low, high):
        values = set()
        for item in field.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", item)
            if not match:
                raise ValueError("invalid cron field")
            base, step = match.groups()
            step = int(step) if step else 1
            if not 1 <= step <= high - low + 1:
                raise ValueError("invalid cron step")
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = int(base)
                end = high if match.group(2) else start
            if not low <= start <= end <= high:
                raise ValueError("cron value out of range")
            values.update(range(start, end + 1, step))
        return values

    def day_matches(self, day):
        if day.month not in self.fields[3]:
            return False
        dom, dow = day.day in self.fields[2], (day.weekday() + 1) % 7 in self.fields[4]
        if self.parts[2].startswith("*") or self.parts[4].startswith("*"):
            return dom and dow
        return dom or dow

    def occurrence(self, bound, forward=True, minimum=None):
        zone = local_zone()
        local = datetime.fromtimestamp(bound, zone)
        day = local.date()
        for _ in range(HORIZON_DAYS):
            if self.day_matches(day):
                midnight = datetime(day.year, day.month, day.day, tzinfo=zone)
                transition = midnight.utcoffset() != (midnight + timedelta(days=1)).utcoffset()
                minutes = self.minutes
                if not transition:
                    if day == local.date():
                        boundary = local.hour * 60 + local.minute
                        minutes = minutes[bisect_left(minutes, boundary):] if forward else minutes[:bisect_right(minutes, boundary)]
                    if not forward:
                        minutes = reversed(minutes)
                eligible = []
                for minute in minutes:
                    naive = datetime(day.year, day.month, day.day, minute // 60, minute % 60)
                    stamp = cron_stamp(naive, zone)
                    if (stamp > bound if forward else stamp <= bound and (minimum is None or stamp >= minimum)):
                        eligible.append(stamp)
                    # On ordinary days minute ordering is instant ordering. Only
                    # a DST transition day needs the full scan to order folds.
                    if eligible and not transition:
                        break
                if eligible:
                    return min(eligible) if forward else max(eligible)
            day += timedelta(days=1 if forward else -1)
            if not forward and minimum is not None and day < datetime.fromtimestamp(minimum, zone).date():
                return None
        raise ValueError("cron has no occurrence within eight years")


def parse_at(value):
    if not isinstance(value, str):
        raise ValueError("at must be an ISO local date and time")
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("at must be an ISO local date and time") from None
    if "T" not in value and " " not in value:
        raise ValueError("at must include a time")
    if date.tzinfo is not None:
        return date.timestamp()
    candidates = local_candidates(date, local_zone())
    if len(candidates) != 1:
        raise ValueError("nonexistent or ambiguous local time; supply an explicit UTC offset")
    return candidates[0]


def system_notify(title, text):
    if sys.platform != "darwin":
        LOG.info("system notification skipped: platform unsupported")
        return
    try:
        subprocess.run(["osascript", "-e", NOTIFICATION_SCRIPT, "--", title, text],
                       check=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        # Do not log reminder bodies or osascript stderr (which may echo them).
        LOG.warning("system notification unavailable; reminder remains in outbox")


class Reminders:
    def __init__(self, store, router, outbox, clock=time.time, notifier=system_notify, bus=None):
        self.store, self.router, self.outbox = store, router, outbox
        self.bus = bus or Bus(store, router, outbox)
        self.clock, self.notifier = clock, notifier
        self.notifications = set()

    def add(self, name, text, kind, at=None, cron=None, channel=""):
        if kind not in ("notify", "run"):
            raise ValueError("kind must be notify or run")
        for key, value, maximum in (("name", name, 200), ("text", text, 16000)):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError("invalid reminder " + key)
        if (at is None) == (cron is None):
            raise ValueError("supply exactly one of at or cron")
        now = self.clock()
        due = Cron(cron).occurrence(now) if cron is not None else parse_at(at)
        if due <= now:
            raise ValueError("one-time reminder must be in the future")
        cursor = self.store.run("INSERT INTO reminders(name,text,kind,cron,next_at,channel,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                                name.strip(), text, kind, cron, due, channel, now, now)
        return self.describe(self.store.one("SELECT * FROM reminders WHERE id=?", cursor.lastrowid))

    @staticmethod
    def describe(row):
        result = dict(row)
        result["id"] = "r%d" % row["id"]
        result["next_time"] = local_stamp(row["next_at"]) if row["next_at"] is not None else None
        return result

    def list(self):
        return {**time_context(self.clock()), "items": [self.describe(row) for row in self.store.all(
            "SELECT * FROM reminders WHERE status='active' ORDER BY next_at,id")]}

    def cancel(self, reminder_id):
        if not isinstance(reminder_id, str) or not re.fullmatch(r"r[1-9][0-9]*", reminder_id):
            raise ValueError("invalid reminder_id")
        # tick has no await between claim, enqueue and commit. Whichever wins is final.
        row = self.store.one("SELECT * FROM reminders WHERE id=?", int(reminder_id[1:]))
        if not row:
            return {"ok": False, "error": "no such reminder"}
        if row["status"] == "active":
            self.store.run("UPDATE reminders SET status='cancelled',updated=? WHERE id=? AND status='active'",
                           self.clock(), row["id"])
        return {"ok": True, "already_fired": row["status"] == "finished",
                "note": "Any occurrence already queued is kept; cancellation prevents future occurrences."}

    async def tick(self):
        now = self.clock()
        queued = False
        for row in self.store.all("SELECT * FROM reminders WHERE status='active' AND next_at<=? ORDER BY next_at,id", now):
            try:
                cron = Cron(row["cron"]) if row["cron"] else None
                due = cron.occurrence(now, forward=False, minimum=row["next_at"]) if cron else row["next_at"]
                # A timezone change can leave no cron match between the saved
                # instant and now. That saved occurrence is still owed once.
                if due is None:
                    due = row["next_at"]
                following = cron.occurrence(now) if cron else None
            except (ValueError, OSError, OverflowError):
                LOG.warning("reminder schedule calculation failed; other reminders continue")
                continue
            text = "[Reminder r%d · %s · scheduled %s]\n%s" % (row["id"], row["name"], local_stamp(due), row["text"])
            route = None
            with self.store.atomic():
                claimed = self.store.run("UPDATE reminders SET next_at=?,status=?,updated=? WHERE id=? AND status='active' AND next_at=?",
                                         following, "active" if cron else "finished", now, row["id"], row["next_at"]).rowcount
                if not claimed:
                    continue
                inserted = self.store.run("INSERT OR IGNORE INTO reminder_fires(reminder_id,scheduled,created) VALUES(?,?,?)",
                                          row["id"], due, now).rowcount
                if not inserted:
                    continue
                # Built-in rules: notify goes straight to the user, run wakes the Router.
                result = self.bus.emit({"source": "reminder", "type": "reminder." + row["kind"], "id": "%d:%.6f" % (row["id"], due),
                                        "thread": "reminder:r%d" % row["id"], "text": text,
                                        "data": {"reminder_id": "r%d" % row["id"], "name": row["name"], "scheduled": due,
                                                 "channel": row["channel"]}})
                route = result["route"] if result["status"] == "accepted" else None
                queued = queued or route == "wake"
            if route == "direct" and row["kind"] == "notify":
                # External notifications are best effort and at-most-once: never replay
                # an uncertain osascript after a crash. The durable outbox is authoritative.
                task = asyncio.create_task(asyncio.to_thread(self.notifier, row["name"], text))
                self.notifications.add(task)
                task.add_done_callback(self.notifications.discard)
            await asyncio.sleep(0)  # Large batches must not monopolize HTTP/Worker events.
        if queued:
            self.router._wake_waiters()
            await self.router.pump()

    async def close(self):
        await asyncio.gather(*list(self.notifications), return_exceptions=True)
