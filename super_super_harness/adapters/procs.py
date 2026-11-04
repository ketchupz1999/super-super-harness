"""Find and kill a Harness and its commands. Bash tool commands leave the process group, so groups are not enough."""
import getpass
import os
import signal
import subprocess
import time


def _table():
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            rows[int(parts[0])] = int(parts[1])
    return rows


def descendants(root):
    rows, found, frontier = _table(), [], [root]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in rows.items():
            if ppid == parent:
                found.append(pid)
                frontier.append(pid)
    return found


def in_dir(path):
    """Processes whose working directory is at or below path. Finds orphans too."""
    root = os.path.realpath(str(path))
    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-u", getpass.getuser(), "-Fpn"], capture_output=True, text=True).stdout
    found, pid = [], None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            cwd = line[1:]
            if cwd == root or cwd.startswith(root + os.sep):
                found.append(pid)
    return [p for p in found if p != os.getpid()]


def start_time(pid):
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout
    return out.strip()


def alive(pid, started=None):
    if not pid:
        return False
    current = start_time(pid)
    return bool(current) and (started is None or current == started)


def kill(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def kill_tree(pid):
    kill(descendants(pid) + [pid])


def sweep(path, settle=0.2):
    """Kill everything working inside path. Repeats once in case a command forked while we were killing."""
    killed = []
    for _ in range(2):
        pids = in_dir(path)
        if not pids:
            break
        kill(pids)
        killed += pids
        time.sleep(settle)
    return killed
