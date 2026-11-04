"""One Workspace per task. The path must stay stable: Claude Code stores sessions by working directory."""
import os
import json
import subprocess
import shutil
from pathlib import Path

from .worker_names import check_code_name
from ..core.router_files import WorkerNotes
from ..core.private_files import atomic_write, read_private


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def is_git(path):
    return _git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def slug(task_id, code_name):
    return "%s-%s" % (task_id, check_code_name(code_name))


def create(cfg, task_id, name, repo, brief):
    """Returns (workspace, cwd, branch)."""
    root = cfg.workspaces / slug(task_id, name)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    if not (root / "notes.md").exists():
        WorkerNotes(root).write("notes", "", "missing")
    (root / "brief.md").write_text(brief)
    cwd, branch = root / "repo", None
    if repo:
        source = os.path.expanduser(repo)
        if not os.path.isdir(source):
            raise ValueError("repo %s does not exist" % source)
        if not is_git(source):
            raise ValueError("%s is not a git repository" % source)
        branch = "w/%s" % name.lower()
        marker = root / ".workspace-owner"
        owner = {"task_id": task_id, "repo": str(Path(source).resolve()), "branch": branch}
        try:
            owned = json.loads(read_private(marker)) == owner
        except FileNotFoundError:
            owned = False
        if _git(source, "show-ref", "--verify", "--quiet", "refs/heads/" + branch).returncode == 0:
            registered = _git(source, "worktree", "list", "--porcelain").stdout.split("\n\n")
            if owned and cwd.is_dir() and any("worktree " + str(cwd.resolve()) in block.splitlines()
                                             and "branch refs/heads/" + branch in block.splitlines() for block in registered):
                return root, cwd, branch   # created by us before a crash, but not yet saved in tasks
            raise ValueError("branch %s already exists; refusing to overwrite it" % branch)
        if cwd.exists():
            raise ValueError("workspace checkout already exists; refusing to overwrite it")
        if not owned:
            atomic_write(marker, json.dumps(owner), replace=False)
        done = _git(source, "worktree", "add", "-q", "-b", branch, str(cwd))
        if done.returncode:
            raise ValueError("worktree failed: %s" % done.stderr.strip()[:300])
    else:
        cwd.mkdir(exist_ok=True)
    return root, cwd, branch


def expire(task):
    """Remove the checkout and scratch files, keeping only durable notes. Never force a dirty worktree away."""
    root, cwd = Path(task["workspace"]), Path(task["cwd"])
    if task["branch"] and cwd.exists():
        done = _git(os.path.expanduser(task["repo"]), "worktree", "remove", str(cwd))
        if done.returncode:
            raise ValueError("worktree cleanup refused: %s" % done.stderr.strip()[:300])
    if root.exists():
        for path in root.iterdir():
            if path.name in ("notes.md", ".documents.lock"):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def wake(task):
    """Restore the original path and branch, even if the Worker was renamed meanwhile."""
    root, cwd = Path(task["workspace"]), Path(task["cwd"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    if not cwd.exists():
        if task["branch"]:
            done = _git(os.path.expanduser(task["repo"]), "worktree", "add", "-q", str(cwd), task["branch"])
            if done.returncode:
                raise ValueError("worktree wake failed: %s" % done.stderr.strip()[:300])
        else:
            cwd.mkdir()
    (root / "brief.md").write_text(task["brief"])


def status(cwd):
    done = _git(cwd, "status", "--short")
    return done.stdout.strip() if done.returncode == 0 else ""


def recent_commits(cwd, count=5):
    done = _git(cwd, "log", "--oneline", "-%d" % count)
    return done.stdout.strip() if done.returncode == 0 else ""
