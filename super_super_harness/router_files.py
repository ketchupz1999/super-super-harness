"""Fixed Router documents shared by the web editor and Router memory tools."""
import hashlib
import os
import stat

from .private_files import atomic_write, private_lock

FILE_LIMIT = 16 * 1024
MEMORY_STUB = "# Memory\n\nStanding preferences the user has stated.\n"


class DocumentError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class RouterFiles:
    names = ('soul', 'memory')
    label = 'Router document'

    def __init__(self, directory):
        self.directory = directory

    def _path(self, name):
        if name not in self.names:
            raise DocumentError(404, 'unknown %s' % self.label)
        if not stat.S_ISDIR(self.directory.lstat().st_mode):
            raise DocumentError(400, '%s directory must not be a symlink' % self.label.split()[0])
        return self.directory / (name + '.md')

    def _read(self, name):
        path = self._path(name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {'content': '', 'version': 'missing', 'exists': False}
        except OSError:
            raise DocumentError(400, '%s is unavailable' % self.label) from None
        with os.fdopen(fd, 'rb') as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise DocumentError(400, '%s must be a regular file' % self.label)
            data = file.read(FILE_LIMIT + 1)
        if len(data) > FILE_LIMIT:
            raise DocumentError(413, '%s exceeds 16384 UTF-8 bytes' % self.label)
        try:
            content = data.decode('utf-8')
        except UnicodeError:
            raise DocumentError(400, '%s must be UTF-8' % self.label) from None
        return {'content': content, 'version': hashlib.sha256(data).hexdigest(), 'exists': True}

    def read(self, name):
        self._path(name)
        with private_lock(self.directory / '.documents.lock'):
            return self._read(name)

    def write(self, name, content, version):
        path = self._path(name)
        if not isinstance(content, str) or not isinstance(version, str) or not version:
            raise DocumentError(400, 'content and version must be strings')
        try:
            data = content.encode('utf-8')
        except UnicodeError:
            raise DocumentError(400, '%s must be UTF-8' % self.label) from None
        if len(data) > FILE_LIMIT:
            raise DocumentError(413, '%s exceeds 16384 UTF-8 bytes' % self.label)
        with private_lock(self.directory / '.documents.lock'):
            if self._read(name)['version'] != version:
                raise DocumentError(409, '%s conflict: read the latest version and merge before saving' % self.label)
            atomic_write(path, data)
            return {'content': content, 'version': hashlib.sha256(data).hexdigest(), 'exists': True}

    def ensure_memory(self):
        path = self._path('memory')
        with private_lock(self.directory / '.documents.lock'):
            # Do not rewrite existing user memory or create a Soul file.
            if not path.exists() and not path.is_symlink():
                atomic_write(path, MEMORY_STUB, replace=False)


class ItemNotes(RouterFiles):
    """SSHARNESS_HOME/items/<code>/notes.md with the same atomic write and version check."""
    names = ('notes',)
    label = 'Item notes'
