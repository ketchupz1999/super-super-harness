"""Router avatar state is derived from durable activity, never browser-local timers."""
import os
import stat
import time

STATES = ("error", "done", "waiting", "working", "thinking", "rest", "idle")
EXTENSIONS = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "gif": "image/gif"}
LIMIT = 2 * 1024 * 1024
FLASH_SECONDS = 3.5


def single_play_gif(data):
    """Remove GIF loop application extensions in memory, preserving image frames."""
    if len(data) < 13:
        raise ValueError("invalid GIF")
    offset = 13 + (3 * 2 ** ((data[10] & 7) + 1) if data[10] & 128 else 0)
    output = [data[:offset]]

    def subblocks(position):
        while position < len(data):
            size = data[position]
            position += 1
            if not size:
                return position
            position += size
        raise ValueError("invalid GIF blocks")

    while offset < len(data):
        start = offset
        marker = data[offset]
        if marker == 59:  # trailer
            return b"".join(output) + data[offset:]
        if marker == 33 and offset + 2 < len(data):  # extension
            label = data[offset + 1]
            offset = subblocks(offset + 2)
            if label == 255 and data[start + 2:start + 14] in (b"\x0bNETSCAPE2.0", b"\x0bANIMEXTS1.0"):
                continue
        elif marker == 44 and offset + 10 < len(data):  # image descriptor
            flags = data[offset + 9]
            offset += 10 + (3 * 2 ** ((flags & 7) + 1) if flags & 128 else 0)
            offset = subblocks(offset + 1)  # Skip LZW minimum code size.
        else:
            raise ValueError("invalid GIF structure")
        output.append(data[start:offset])
    raise ValueError("missing GIF trailer")


class Avatar:
    def __init__(self, service, clock=time.time):
        self.service, self.store, self.cfg = service, service.store, service.cfg
        self.clock = clock
        self.assets = {}
        self.scanned = float("-inf")

    def image(self, state, extension, read=True):
        if state not in STATES or extension not in EXTENSIONS:
            raise ValueError("unknown avatar")
        # Reject symlinks, directories, devices and FIFOs, including a symlinked
        # avatar directory. No client-supplied path ever reaches open().
        directory = os.open(self.cfg.home / "avatar", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(state + "." + extension, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= LIMIT:
                    raise ValueError("avatar unavailable or too large")
                prefix = os.read(fd, 16)
                valid = {"png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
                         "jpg": prefix.startswith(b"\xff\xd8\xff"),
                         "gif": prefix.startswith((b"GIF87a", b"GIF89a")),
                         "webp": prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"}
                if not valid[extension]:
                    raise ValueError("avatar format does not match its extension")
                version = "%x-%x-%x-%x" % (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
                if not read:
                    return version
                chunks, size = [prefix], len(prefix)
                while size <= LIMIT:
                    chunk = os.read(fd, min(65536, LIMIT + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                if size > LIMIT:
                    raise ValueError("avatar too large")
                data = b"".join(chunks)
                if state == "done" and extension == "gif":
                    data = single_play_gif(data)
                return data, EXTENSIONS[extension]
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def catalogue(self):
        # All SSE clients share this bounded scan. New/replaced/deleted files
        # propagate within one second without a service restart.
        now = time.monotonic()
        if now - self.scanned < 1:
            return self.assets
        assets = {}
        for state in STATES:
            for extension in EXTENSIONS:
                try:
                    version = self.image(state, extension, read=False)
                except (OSError, ValueError):
                    continue
                assets[state] = {"url": "/api/router-avatar/%s.%s?v=%s" % (state, extension, version),
                                 "animated": extension == "gif"}
                break
        self.scanned, self.assets = now, assets
        return assets

    def state(self):
        now = self.clock()
        router = self.service.router
        failed = self.store.one("SELECT id,created FROM events WHERE type='avatar.error' ORDER BY id DESC LIMIT 1")
        done = self.store.one("SELECT id,created FROM events WHERE type='avatar.done' ORDER BY id DESC LIMIT 1")
        deadline, token = None, ""
        if router.mode == "paused":
            state = "error"
        elif failed and failed["created"] + FLASH_SECONDS > now:
            state, deadline, token = "error", failed["created"] + FLASH_SECONDS, str(failed["id"])
        elif done and done["created"] + FLASH_SECONDS > now:
            state, deadline, token = "done", done["created"] + FLASH_SECONDS, str(done["id"])
        elif self.store.get("router_waiting"):
            state = "waiting"
        elif self.store.one("SELECT id FROM tasks WHERE status='running' LIMIT 1"):
            state = "working"
        elif self.store.current_turn():
            state = "thinking"
        else:
            user = self.store.one("SELECT MAX(created) stamp FROM messages WHERE role='user'")["stamp"] or 0
            work = self.store.one("SELECT MAX(created) stamp FROM events WHERE type IN ('task.started','task.message','worker.started')")["stamp"] or 0
            state = "rest" if done and done["created"] >= max(user, work) else "idle"
        assets = self.catalogue()
        asset = assets.get(state) or (assets.get("idle") if state in ("waiting", "rest") else None)
        return {"state": state, "name": self.cfg.web.get("router_name", "Router"), "until": deadline,
                "token": token, "asset": asset}
