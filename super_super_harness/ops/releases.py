"""Installed versions. Workers change this code, so the service only ever runs a tested copy of a commit."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def _git(repo, *args):
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if done.returncode:
        raise ValueError(done.stderr.strip()[:300])
    return done.stdout.strip()


def link(cfg):
    return cfg.home / "current"


def current(cfg):
    path = link(cfg)
    return Path(os.readlink(path)) if path.is_symlink() else None


def install(cfg, repo, ref):
    sha = _git(repo, "rev-parse", "--short", ref)
    target = cfg.home / "releases" / sha
    if target.exists():
        return target
    partial = target.with_name(sha + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    archive = subprocess.Popen(["git", "archive", sha], cwd=str(repo), stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(partial)], stdin=archive.stdout, check=True)
    archive.stdout.close()
    if archive.wait():
        raise ValueError("git archive failed")
    partial.rename(target)
    return target


def run_tests(target):
    with tempfile.TemporaryDirectory(prefix="ssharness-release-test-", dir="/tmp") as home:
        env = dict(os.environ, SSHARNESS_HOME=home, SSHARNESS_NO_CAFFEINATE="1", TMPDIR="/tmp")
        # Short, disposable paths keep tests away from both real state and macOS AF_UNIX limits.
        try:
            done = subprocess.run([sys.executable, "-m", "unittest", "tests.test_workers", "tests.test_router", "tests.test_web",
                                   "tests.test_harness", "tests.test_chat", "tests.test_reminders", "tests.test_avatar",
                                   "tests.test_bus", "tests.test_items", "tests.test_suber", "tests.test_c1", "tests.test_c2",
                                   "tests.test_c5", "tests.test_ui_v2",
                                   "tests.test_ui_v2_1", "tests.test_routing_graph", "tests.test_worker_identity"], cwd=str(target), env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        except subprocess.TimeoutExpired:
            return False
        return done.returncode == 0


def switch(cfg, target):
    temporary = cfg.home / "current.new"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link(cfg))


def upgrade(cfg, repo, ref, restart, check=run_tests):
    """restart() brings the service up on the current link and says whether it is healthy."""
    target = install(cfg, repo, ref)
    if not check(target):
        return {"ok": False, "error": "tests failed, nothing changed", "release": target.name}
    previous = current(cfg)
    switch(cfg, target)
    if restart():
        return {"ok": True, "release": target.name, "previous": previous.name if previous else None}
    if not previous:
        link(cfg).unlink()
        return {"ok": False, "error": "new version is unhealthy and there is no earlier one", "release": target.name}
    switch(cfg, previous)
    return {"ok": False, "error": "new version is unhealthy, rolled back", "release": target.name, "rolled_back_to": previous.name,
            "restarted": restart()}


def rollback(cfg, restart):
    active = current(cfg)
    others = [path for path in (cfg.home / "releases").iterdir() if path != active and not path.name.endswith(".partial")]
    if not others:
        return {"ok": False, "error": "no earlier version"}
    target = max(others, key=lambda path: path.stat().st_mtime)
    switch(cfg, target)
    return {"ok": restart(), "release": target.name}


def serve_detached(cfg):
    log = open(cfg.home / "service.log", "a")
    return subprocess.Popen([str(link(cfg) / "bin" / "supersuperharness"), "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            env=dict(os.environ, SSHARNESS_HOME=str(cfg.home)), start_new_session=True)


def healthy(status, process, seconds=30, settle=5):
    """Up means the socket answers and the Router process is still there a few seconds later."""
    end = time.time() + seconds
    while time.time() < end and process.poll() is None:
        info = status()
        if info and info["router"].get("ready", bool(info["router"].get("pid"))):
            time.sleep(settle)
            router = (status() or {"router": {}})["router"]
            return process.poll() is None and bool(router.get("ready", bool(router.get("pid"))))
        time.sleep(0.3)
    return False
