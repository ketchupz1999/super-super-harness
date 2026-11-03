"""Private files are written completely before an atomic, same-directory rename."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import tempfile


def atomic_write(path, data, *, replace=True):
    """Install a complete 0600 file; replace=False atomically refuses an existing destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(mode):
            raise ValueError("private file must be a regular file")
    data = data.encode("utf-8") if isinstance(data, str) else data
    fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic no-clobber publication; the temp link is removed below.
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def private_lock(path):
    """Serialize private configuration read/modify/write across init/migration processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb+") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("private lock must be a regular file")
        os.fchmod(file.fileno(), 0o600)
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        yield


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("private file must be a regular file")
        os.fchmod(file.fileno(), 0o600)
        return file.read()


def persistent_secret(path, size, generate):
    """Repair an empty file left by an older interrupted writer; preserve valid keys."""
    try:
        key = read_private(path)
    except FileNotFoundError:
        key = b""
    if not key:
        key = generate()
        atomic_write(path, key)
    if len(key) != size:
        raise ValueError("invalid private key file")
    return key
