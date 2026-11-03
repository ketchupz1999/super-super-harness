"""One Workspace per task. The path must stay stable: Claude Code stores sessions by working directory."""
import os
from pathlib import Path
import re
import subprocess


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def is_git(path):
    return _git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def slug(task_id, name):
    clean = re.sub(r"[^\w一-鿿-]+", "-", name).strip("-")[:24]
    return "%s-%s" % (task_id, clean) if clean else task_id


def create(cfg, task_id, name, repo, brief):
    """Returns (workspace, cwd, branch)."""
    root = cfg.workspaces / slug(task_id, name)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "brief.md").write_text(brief)
    cwd, branch = root / "repo", None
    if repo:
        source = os.path.expanduser(repo)
        if not os.path.isdir(source):
            raise ValueError("repo %s does not exist" % source)
        if not is_git(source):
            raise ValueError("%s is not a git repository" % source)
        branch = "task/%s" % task_id
        if not cwd.exists():
            done = _git(source, "worktree", "add", "-q", "-b", branch, str(cwd))
            if done.returncode:
                raise ValueError("worktree failed: %s" % done.stderr.strip()[:300])
    else:
        cwd.mkdir(exist_ok=True)
    return root, cwd, branch


def status(cwd):
    done = _git(cwd, "status", "--short")
    return done.stdout.strip() if done.returncode == 0 else ""


def recent_commits(cwd, count=5):
    done = _git(cwd, "log", "--oneline", "-%d" % count)
    return done.stdout.strip() if done.returncode == 0 else ""
