"""Real-model acceptance on an isolated instance: python3 tests/e2e.py <scenario...>.

SSHARNESS_E2E_PROFILES must point to a dedicated test config with opus, sonnet, worker
and worker-alt profiles. Fault scenarios also require SSHARNESS_E2E_PRIMARY_PORT and
SSHARNESS_E2E_BACKUP_PORT for the test provider endpoints on localhost. Never use a
production config: these scenarios deliberately interrupt and restart processes.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("SSHARNESS_E2E_HOME", "/tmp/ssharness-e2e"))
REPO = Path("/tmp/ssharness-e2e-repo")
BARE = Path("/tmp/ssharness-e2e-bare")
sys.path.insert(0, str(ROOT))
os.environ["SSHARNESS_HOME"] = str(HOME)
from super_super_harness import config, procs  # noqa: E402
from super_super_harness.cli import Client  # noqa: E402
from tests.reply_helpers import MessageStamp, reply_after  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def profiles_text():
    path = os.environ.get("SSHARNESS_E2E_PROFILES")
    if not path:
        raise SystemExit("Set SSHARNESS_E2E_PROFILES to a dedicated test profiles.toml first")
    return Path(path).expanduser().read_text()


def cheap():
    """Router on Sonnet: most scenarios test the plumbing, not Opus."""
    return profiles_text().replace('primary = "opus"', 'primary = "sonnet"')


def fault_profiles():
    primary_port = int(os.environ["SSHARNESS_E2E_PRIMARY_PORT"])
    backup_port = int(os.environ["SSHARNESS_E2E_BACKUP_PORT"])
    primary, backup = Hole(primary_port), Hole(backup_port)
    text = profiles_text()
    for port, gateway in ((primary_port, primary), (backup_port, backup)):
        text = text.replace('base_url = "http://127.0.0.1:%d"' % port,
                            'base_url = "http://127.0.0.1:%d"' % gateway.port)
    text = re.sub(r"(?m)^ensure\s*=.*\n?", "", text)
    return primary, backup, text


class Instance:
    def __init__(self, profiles=None):
        self.profiles = profiles
        self.proc = None
        self.cfg = None

    def fresh(self):
        shutil.rmtree(HOME, ignore_errors=True)
        HOME.mkdir(parents=True)
        (HOME / "workspaces").mkdir()
        (HOME / "profiles.toml").write_text(self.profiles or cheap())
        return self

    def start(self):
        log = open(HOME / "service.log", "a")
        self.proc = subprocess.Popen([str(ROOT / "bin" / "supersuperharness"), "serve"], stdout=log, stderr=log,
                                     env=dict(os.environ, SSHARNESS_NO_CAFFEINATE="1"), start_new_session=True)
        self.cfg = config.load(HOME)
        end = time.time() + 40
        while time.time() < end:
            if self.cfg.sock.exists() and self.cfg.owner_token_file.exists():
                try:
                    self.client = Client(self.cfg)
                    self.client.call("status")
                    return self
                except OSError:
                    pass
            time.sleep(0.2)
        raise SystemExit("service did not start: %s" % (HOME / "service.log").read_text()[-400:])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=30)

    def kill9(self):
        self.proc.kill()
        self.proc.wait()
        time.sleep(0.3)

    def db(self):
        conn = sqlite3.connect("file:%s?mode=ro" % self.cfg.db, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def say(self, text):
        stamp = time.time()
        message = self.client.call("say", text=text)
        return MessageStamp(stamp, message["id"])

    def rows(self, sql, *args):
        with self.db() as conn:
            return conn.execute(sql, args).fetchall()

    def tasks(self):
        return self.rows("SELECT * FROM tasks ORDER BY seq")

    def replies(self, after=0):
        return self.rows("SELECT * FROM outbox WHERE created>? ORDER BY id", after)

    def wait(self, predicate, timeout, label):
        end = time.time() + timeout
        while time.time() < end:
            value = predicate()
            if value:
                return value
            time.sleep(1)
        print("  timed out: %s" % label, flush=True)
        return None

    def reply_after(self, stamp, timeout=120, final=False):
        return reply_after(self, stamp, timeout, final)

    def task_named(self, needle):
        for row in self.tasks():
            if needle in row["name"] or needle in row["brief"]:
                return row
        return None


class Hole:
    """Forwards to a real gateway until told to accept connections and never answer, like a dead tunnel."""

    def __init__(self, upstream):
        self.upstream, self.hole = upstream, False
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        self.port = listener.getsockname()[1]
        self.listener = listener
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            client, _ = self.listener.accept()
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client):
        if self.hole:
            time.sleep(3600)
            return
        upstream = socket.create_connection(("127.0.0.1", self.upstream))
        threading.Thread(target=self.pump, args=(upstream, client), daemon=True).start()
        self.pump(client, upstream)

    def pump(self, source, target):
        try:
            while True:
                data = source.recv(65536)
                if not data or self.hole:
                    if self.hole:
                        time.sleep(3600)
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try:
                    sock.close()
                except OSError:
                    pass


class Terminal:
    """A real terminal running super-super-harness, answering what Claude Code's UI asks at startup."""

    def __init__(self, *args):
        self.pid, self.master = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execv(str(ROOT / "bin" / "supersuperharness"), ["supersuperharness", *args])
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        self.buffer = b""

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    self.buffer += os.read(self.master, 65536)
                except OSError:
                    return

    def text(self):
        cleaned = re.sub(r"\x1b\[[0-9;?<>]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]", " ", self.buffer[-6000:].decode(errors="ignore"))
        return re.sub(r"\s+", " ", cleaned)

    def line(self, line):
        os.write(self.master, line.encode())
        self.drain(0.8)
        os.write(self.master, b"\r")

    def ready(self):
        self.drain(10)
        os.write(self.master, b"\x1b[?62;c\x1bP>|xterm 1\x1b\\")
        self.drain(2)
        if "No, exit" in self.text():
            os.write(self.master, b"\x1b[B")
            self.drain(1.5)
            os.write(self.master, b"\r")
            self.drain(8)


def make_repo():
    for path in (REPO, BARE):
        shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["git", "init", "-q", "--bare", str(BARE)], check=True)
    REPO.mkdir()
    commands = [["init", "-q", "-b", "main"], ["config", "user.email", "e2e@test"], ["config", "user.name", "e2e"],
                ["remote", "add", "origin", str(BARE)]]
    for args in commands:
        subprocess.run(["git", *args], cwd=REPO, check=True)
    (REPO / "app.py").write_text("print('app')\n")
    subprocess.run(["git", "add", "."], cwd=REPO, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=REPO, check=True)


def core():
    """Acceptance 1, 2, 4, 7: parallel tasks on three models, a free entry point, natural references, merge."""
    make_repo()
    box = Instance(profiles_text()).fresh().start()
    try:
        first = box.say("开三个任务，都很小：\n"
                        "1. 名字“hello 脚本”：在 %s 仓库里新建 hello.py，内容是打印 hello，提交。用 sonnet。\n"
                        "2. 名字“斐波那契说明”：写 fib.txt 解释斐波那契数列，三句话。用 worker。\n"
                        "3. 名字“素数说明”：写 prime.txt 解释素数，三句话。用 worker-alt。" % REPO)
        tasks = box.wait(lambda: len(box.tasks()) == 3 and box.tasks(), 120, "3 tasks")
        check("派了 3 个任务", tasks and {t["profile"] for t in tasks} == {"sonnet", "worker", "worker-alt"},
              str([(t["id"], t["profile"]) for t in tasks or []]))
        latency, text = box.reply_after(first)
        check("派活后 Router 回话", latency is not None, "%.1fs %s" % (latency or -1, text[:60]))
        asked = box.say("顺便问下，1+1 等于几？只回答数字")
        latency, text = box.reply_after(asked, 90)
        check("Worker 跑着时 Router 能回话", latency is not None and latency < 30 and "2" in text, "%.1fs %s" % (latency or -1, text[:40]))
        done = box.wait(lambda: all(t["status"] == "completed" for t in box.tasks()), 240, "all completed")
        check("3 个任务完成", done, str([(t["id"], t["status"]) for t in box.tasks()]))
        notice = box.wait(lambda: [r for r in box.replies(first) if "hello" in r["text"]], 90, "completion notice")
        check("主动通知完成", notice)
        hello = box.task_named("hello")
        branch_log = subprocess.run(["git", "log", "--oneline", hello["branch"]], cwd=REPO, capture_output=True, text=True).stdout
        check("Worker 提交到任务分支", "init" in branch_log and len(branch_log.splitlines()) >= 2, branch_log[:80])
        check("主分支没动", not (REPO / "hello.py").exists())
        stamp = box.say("把 hello 那个任务的分支合并到本地 main。")
        merged = box.wait(lambda: (REPO / "hello.py").exists(), 120, "merge")
        check("确认后本地合并", merged)
        pushed = subprocess.run(["git", "--git-dir", str(BARE), "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip()
        check("没有 push", pushed == "0", pushed)
        latency, text = box.reply_after(stamp, 60, final=True)
        check("合并最终回复已到", latency is not None, text[:80])
        if latency is None:
            return  # Do not send the next request while the previous turn is unresolved.
        before = [dict(task) for task in box.tasks()]
        stamp = box.say("把那个说明任务换成 sonnet")
        latency, text = box.reply_after(stamp, 60, final=True)
        check("有歧义时只问一句", latency is not None and "斐波那契" in text and "素数" in text
              and len(text) < 120 and [dict(task) for task in box.tasks()] == before, text[:80])
    finally:
        box.stop()


def control():
    """Acceptance 2, 3: change direction, interrupt, resume and switch model, all by name."""
    box = Instance().fresh().start()
    try:
        box.say("开个任务，名字“慢任务”，用 worker：先用 Bash 前台运行 sleep 40，然后写 result.txt，内容是 ALPHA。")
        task = box.wait(lambda: [t for t in box.tasks() if t["status"] == "running" and t["cwd"]], 90, "running")
        task_id = task[0]["id"]
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE task_id=? AND type='tool'", task_id), 60, "tool started")
        box.say("慢任务改一下，result.txt 的内容改成 BETA。")
        check_cwd = Path(task[0]["cwd"]) / "result.txt"
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE type='task.message' AND task_id=?", task_id), 60, "forwarded")
        done = box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id=? AND status='completed'", task_id), 120, "completed")
        check("中途改方向生效", done and check_cwd.exists() and "BETA" in check_cwd.read_text(), check_cwd.read_text() if check_cwd.exists() else "no file")

        box.say("再开个任务，名字“长任务”，用 worker：用 Bash 前台运行 sleep 200，然后写 end.txt。")
        task = box.wait(lambda: [t for t in box.tasks() if "长任务" in t["name"] and t["status"] == "running" and t["cwd"]], 90, "long running")
        task_id = task[0]["id"]
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE task_id=? AND type='tool'", task_id), 60, "tool started")
        time.sleep(3)
        stamp = box.say("长任务先停一下")
        stopped = box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id=? AND status='interrupted'", task_id), 60, "interrupted")
        check("打断", stopped, "%.1fs" % (time.time() - stamp))
        check("打断后命令被杀", not procs.in_dir(task[0]["cwd"]) and not [1 for line in subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout.splitlines() if line.strip() == "sleep 200"])
        time.sleep(2)
        box.say("长任务继续吧，不用再 sleep 了，直接写 end.txt，内容 RESUMED。")
        done = box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id=? AND status='completed'", task_id), 120, "resumed completed")
        end = Path(task[0]["cwd"]) / "end.txt"
        check("恢复后接着干", done and end.exists(), end.read_text()[:30] if end.exists() else "no file")

        box.say("长任务再做一件事，换成 sonnet 跑：把 end.txt 的内容追加一行 AGAIN。")
        ok = box.wait(lambda: "AGAIN" in end.read_text() and box.rows("SELECT 1 FROM tasks WHERE id=? AND profile='sonnet' AND status='completed'", task_id), 180, "switched")
        check("按名字换模型并保留会话", ok, end.read_text()[:40] if end.exists() else "")
        runs = box.rows("SELECT profile, session_id FROM worker_runs WHERE task_id=?", task_id)
        check("同一会话", len({r["session_id"] for r in runs}) == 1 and "sonnet" in {r["profile"] for r in runs}, str([r["profile"] for r in runs]))
    finally:
        box.stop()


def busy():
    """Acceptance 9: Router running a long command still answers within ten seconds or so."""
    box = Instance().fresh().start()
    try:
        box.say("用你自己的 Bash 工具前台运行 sleep 90，这次不要派 Worker，跑完告诉我。")
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE source='router' AND type='tool' AND data LIKE '%sleep 90%'"), 60, "router running sleep")
        time.sleep(2)
        stamp = box.say("2+2 等于几？只回答数字")
        latency, text = box.reply_after(stamp, 60)
        check("Router 忙时回话", latency is not None and latency < 25, "%.1fs %s" % (latency or -1, text[:60]))
        check("发生了打断", box.rows("SELECT 1 FROM events WHERE type='router.interrupted'"))
    finally:
        box.stop()


def memory():
    """Acceptance 8: a standing preference survives compaction."""
    box = Instance().fresh().start()
    try:
        stamp = box.say("记住：调研类任务都用 opus。")
        box.reply_after(stamp, 60)
        memory_file = HOME / "router" / "memory.md"
        check("写进记忆文件", "opus" in memory_file.read_text().lower(), memory_file.read_text()[-60:])
        box.client.call("router_compact")
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE type='compact'"), 120, "compact event")
        check("压缩发生", box.rows("SELECT 1 FROM events WHERE type='compact'"))
        stamp = box.say("开个任务：调研一下 tomllib 是哪个 Python 版本加入的，写进 note.txt。")
        task = box.wait(lambda: box.tasks(), 90, "task")
        check("压缩后按记忆选模型", task and task[0]["profile"] == "opus", task[0]["profile"] if task else "")
        box.client.call("task_stop_owner", task_id=task[0]["id"]) if task else None
    finally:
        box.stop()


def early():
    """The primary is unreachable from the very first message: no session has started yet."""
    primary, backup, text = fault_profiles()
    text = text.replace("silent_seconds = 20 ", "silent_seconds = 8 ")
    primary.hole = True
    box = Instance(text).fresh().start()
    try:
        stamp = box.say("3+5 等于几？只回答数字")
        answer = box.wait(lambda: [r for r in box.replies(stamp) if "8" in r["text"]], 120, "answer")
        check("第一条消息就碰上故障也能回答", answer, answer[0]["text"][:30] if answer else "")
        check("Router 没有崩溃", not box.rows("SELECT 1 FROM events WHERE type='router.exited'"))
        check("只回答一次", box.rows("SELECT COUNT(*) c FROM outbox WHERE text LIKE '%8%' AND kind!='notice'")[0]["c"] == 1)
    finally:
        box.stop()


def failover():
    """Acceptance 6: a hung primary, then both down. Workers keep running throughout."""
    primary, backup, text = fault_profiles()
    text = text.replace("primary_check_seconds = 300", "primary_check_seconds = 10")
    text = text.replace("silent_seconds = 20 ", "silent_seconds = 8 ")
    box = Instance(text).fresh().start()
    try:
        stamp = box.say("记住暗号 WOLF-2，回复收到。")
        latency, _ = box.reply_after(stamp, 60)
        check("主模型正常", latency is not None, "%.1fs" % (latency or -1))
        box.client.call("start_many", count=1, profile="worker", brief="用 Bash 前台运行 sleep 60，然后写 survived.txt，内容 ok。")
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE type='tool' AND task_id='t1'"), 60, "worker tool")
        primary.hole = True
        stamp = box.say("暗号是什么？只回答暗号。")
        notice = box.wait(lambda: [r for r in box.replies(stamp) if "顶着" in r["text"]], 90, "switch notice")
        check("主模型挂起后自动切到备用", notice, "%.0fs" % (time.time() - stamp))
        answer = box.wait(lambda: [r for r in box.replies(stamp) if "WOLF-2" in r["text"]], 90, "answer")
        check("备用模型接着回答，上下文还在", answer, answer[0]["text"][:40] if answer else "")
        count = box.rows("SELECT COUNT(*) c FROM outbox WHERE text LIKE '%WOLF-2%' AND created>?", stamp)[0]["c"]
        check("没有重复回答", count == 1, str(count))
        check("Worker 没受影响", box.rows("SELECT 1 FROM tasks WHERE id='t1' AND status='running'"))
        primary.hole = False
        back = box.wait(lambda: [r for r in box.replies(stamp) if "恢复了" in r["text"]], 40, "primary back")
        check("主模型恢复后询问", back)
        done = box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id='t1' AND status='completed'"), 120, "worker done")
        check("Worker 全程跑完", done)
        primary.hole = backup.hole = True
        stamp = box.say("现在 3+4 等于几？只回答数字")
        paused = box.wait(lambda: [r for r in box.replies(stamp) if "两个模型都不可用" in r["text"]], 120, "paused")
        check("两边都挂时暂停并告知", paused, "%.0fs" % (time.time() - stamp))
        primary.hole = backup.hole = False
        answer = box.wait(lambda: [r for r in box.replies(stamp) if "7" in r["text"]], 120, "answer after pause")
        check("恢复后接着处理排队的消息", answer, answer[0]["text"][:40] if answer else "")
    finally:
        box.stop()


def attach():
    """Acceptance 11: take the Router over in a real terminal, run Workers from there, hand back."""
    box = Instance().fresh().start()
    try:
        stamp = box.say("记住暗号 RAVEN-8，回复收到。")
        box.reply_after(stamp, 60)
        term = Terminal("attach")
        term.ready()
        pid, drain, text, type_line = term.pid, term.drain, term.text, term.line
        check("attach 期间 Router 已让出", box.client.call("status")["router"]["mode"] == "attached")
        outside = box.say("终端外发来的消息：暗号后面加个 X")
        target = HOME / "router" / "woke.txt"
        type_line("用 task_start 开一个任务，名字“接管测试”，用 worker，内容是用 Bash 运行 echo hi-from-worker 并报告输出。"
                  "Worker 的结果到了之后，把结论写进当前目录的 woke.txt。")
        end = time.time() + 150
        while time.time() < end and not target.exists():
            drain(1)
        check("交互界面里派出 Worker", box.task_named("接管测试"), text()[-120:] if not box.task_named("接管测试") else "")
        written = target.read_text() if target.exists() else ""
        check("Worker 结果叫醒了界面并写入文件", "hi-from-worker" in written, written[:100] or text()[-200:])
        check("外部消息没有丢", box.rows("SELECT 1 FROM messages WHERE text LIKE '终端外发来%' AND status='done'"))
        type_line("/exit")
        drain(6)
        os.kill(pid, signal.SIGTERM) if procs.alive(pid) else None
        drain(1)
        box.wait(lambda: box.client.call("status")["router"]["mode"] == "running", 20, "reattached")
        check("退出后交回后台", box.client.call("status")["router"]["mode"] == "running")
        stamp = box.say("刚才你在终端里创建了哪个文件？暗号是什么？一句话回答。")
        # A Worker result can be reported first in the same turn; judge the turn's final reply.
        latency, reply = box.reply_after(stamp, 90, final=True)
        check("交回后什么都记得", "woke" in reply and "RAVEN-8" in reply, reply[:80])
    finally:
        box.stop()


def takeover():
    """A person takes one Worker over in the terminal; it stays held until they leave."""
    box = Instance().fresh().start()
    try:
        box.client.call("start_many", count=1, profile="worker", brief="用 Bash 前台运行 sleep 100，然后写 end.txt。")
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE type='tool' AND task_id='t1'"), 60, "worker tool")
        session = box.tasks()[0]["session_id"]
        term = Terminal("open", "t1")
        term.ready()
        check("接管时后台进程已停", box.client.call("status")["live"] == 0)
        check("任务标成已打断", box.tasks()[0]["status"] == "interrupted")
        term.line("用 Bash 写 taken.txt，内容 TAKEN。不要再运行 sleep。")
        target = Path(box.tasks()[0]["cwd"]) / "taken.txt"
        end = time.time() + 60
        while time.time() < end and not target.exists():
            term.drain(1)
        check("人工在终端里继续这个 Worker", target.exists(), term.text()[-120:] if not target.exists() else "")
        term.line("/exit")
        term.drain(5)
        box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id='t1' AND status='interrupted'"), 10, "released")
        box.client.call("tool_owner", name="task_resume", args={"task_id": "t1", "text": "不用再 sleep 了。说出你写过哪个文件，然后结束。"})
        done = box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id='t1' AND status='completed'"), 90, "done")
        task = box.tasks()[0]
        check("交还后 Router 能接着用，同一会话", done and task["session_id"] == session and "taken" in task["report"], task["report"][:80])
    finally:
        box.stop()


def upgrade():
    """A tested commit goes live, a failing one never does, an unhealthy one rolls back."""
    source = Path("/tmp/ssharness-e2e-src")
    shutil.rmtree(source, ignore_errors=True)
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "tmp"))
    git = lambda *args: subprocess.run(["git", "-c", "user.email=e@t", "-c", "user.name=e", *args], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "v1")
    v1 = git("rev-parse", "--short", "HEAD")
    shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True)
    (HOME / "profiles.toml").write_text(cheap())
    env = dict(os.environ, SSHARNESS_NO_CAFFEINATE="1", SSHARNESS_HOME=str(HOME))

    def run(*args):
        installed = HOME / "current" / "bin" / "supersuperharness"
        cli = installed if installed.exists() else source / "bin" / "supersuperharness"
        done = subprocess.run([str(cli), *args], env=env, capture_output=True, text=True, timeout=400)
        print("  %s -> %s" % (" ".join(args), done.stdout.strip()[-200:]), flush=True)
        return done

    def active():
        return os.path.basename(os.readlink(HOME / "current"))

    box = Instance()
    box.cfg = config.load(HOME)
    try:
        check("第一次升级成功", run("upgrade", "--repo", str(source)).returncode == 0 and active() == v1)
        (source / "tests" / "test_workers.py").write_text((source / "tests" / "test_workers.py").read_text().replace(
            "class Pieces(unittest.TestCase):\n", "class Pieces(unittest.TestCase):\n    def test_broken(self):\n        self.fail()\n\n"))
        git("add", ".")
        git("commit", "-q", "-m", "broken tests")
        done = run("upgrade", "--repo", str(source))
        check("测试没过不切换", done.returncode == 1 and active() == v1
              and "tests failed, nothing changed" in done.stdout, (done.stdout + done.stderr)[-200:])
        git("revert", "--no-edit", "HEAD")
        entry = source / "bin" / "supersuperharness"
        entry.write_text(entry.read_text().replace("#!/usr/bin/env bash\n",
                        '#!/usr/bin/env bash\nif [ "${1:-}" = "serve" ]; then exit 42; fi\n', 1))
        git("add", ".")
        git("commit", "-q", "-m", "serve exits before startup")
        done = run("upgrade", "--repo", str(source))
        check("启动不了就回滚", done.returncode == 1 and active() == v1 and '"rolled_back_to"' in done.stdout)
        status = subprocess.run([str(HOME / "current" / "bin" / "supersuperharness"), "status"], env=env, capture_output=True, text=True).stdout
        check("回滚后服务在跑", "Router" in status, status[:80])
    finally:
        pid_file = HOME / "service.pid"
        pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
        started = procs.start_time(pid) if pid else None
        run("stop")  # Owner control must not depend on valid Profile configuration.
        end = time.time() + 20
        while time.time() < end and ((HOME / "router.sock").exists() or (pid and procs.alive(pid, started))):
            time.sleep(0.3)
        check("升级测试服务已退出", not (HOME / "router.sock").exists() and not (pid and procs.alive(pid, started)))


def restart():
    """Acceptance 5: SIGKILL the service mid-task, restart, the task carries on with one process."""
    box = Instance().fresh().start()
    try:
        box.say("开个任务，名字“重启测试”，用 worker：用 Bash 前台运行 sleep 45，然后写 w1.txt，内容 once（用 >> 追加）。")
        task = box.wait(lambda: [t for t in box.tasks() if t["cwd"] and t["status"] == "running"], 90, "running")
        task_id, cwd = task[0]["id"], task[0]["cwd"]
        box.wait(lambda: box.rows("SELECT 1 FROM events WHERE task_id=? AND type='tool'", task_id), 60, "tool")
        time.sleep(3)
        box.kill9()
        check("杀掉后旧 Worker 还活着（要清理）", procs.in_dir(cwd), str(procs.in_dir(cwd)))
        box.start()
        box.wait(lambda: box.rows("SELECT COUNT(*) c FROM worker_runs WHERE task_id=?", task_id)[0]["c"] == 2, 40, "relaunch")
        time.sleep(2)
        pids = procs.in_dir(cwd)
        print("  in workspace after restart: %s" % pids, flush=True)
        done = box.wait(lambda: box.rows("SELECT 1 FROM tasks WHERE id=? AND status='completed'", task_id), 150, "completed")
        target = Path(cwd) / "w1.txt"
        lines = target.read_text().split() if target.exists() else []
        check("重启后完成，没有重复执行", done and lines == ["once"], str(lines))
        check("告诉你中断过", box.rows("SELECT 1 FROM outbox WHERE text LIKE '%接着跑%'"))
    finally:
        box.stop()


def scale():
    """Acceptance 10: more tasks than the limit: all finish, ten at most at a time."""
    box = Instance().fresh().start()
    try:
        box.client.call("start_many", count=12, profile="worker", brief="用 Bash 运行 sleep 12，再写 done.txt，内容 ok，然后结束。")
        peak = 0
        end = time.time() + 240
        while time.time() < end:
            running = box.client.call("status")["live"]
            peak = max(peak, running)
            if all(t["status"] in ("completed", "failed") for t in box.tasks()):
                break
            time.sleep(1)
        states = [t["status"] for t in box.tasks()]
        check("12 个任务全部完成", states.count("completed") == 12, str(states))
        check("同时运行不超过 10 个", 8 <= peak <= 10, "peak=%d" % peak)
    finally:
        box.stop()


def isolation():
    """Two Workers on one Profile and one repo stay out of each other's way; pushes are refused."""
    make_repo()
    box = Instance().fresh().start()
    try:
        box.client.call("start_many", count=2, profile="worker", repo=str(REPO), brief=(
            "在仓库里新建 note-$SSHARNESS_TASK_ID.txt，内容是 $SSHARNESS_TASK_ID 和 TMPDIR 的值，提交。再把 app.py 第一行改成打印你的任务号并提交。"
            "然后运行 git push origin HEAD，把输出写进你的 report_done。"))
        done = box.wait(lambda: len(box.tasks()) == 2 and all(t["status"] == "completed" for t in box.tasks()), 200, "both done")
        tasks = box.tasks()
        check("两个 Worker 都完成", done, str([t["status"] for t in tasks]))
        check("目录和分支各自独立", len({t["cwd"] for t in tasks}) == 2 and len({t["branch"] for t in tasks}) == 2)
        check("临时目录各自独立", all(t["workspace"] in (Path(t["cwd"]) / ("note-%s.txt" % t["id"])).read_text() for t in tasks if (Path(t["cwd"]) / ("note-%s.txt" % t["id"])).exists()))
        count = subprocess.run(["git", "--git-dir", str(BARE), "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip()
        check("push 被拦下", count == "0", "remote commits=%s" % count)
        for task in tasks:
            print("  report: %s" % task["report"][:160].replace("\n", " "), flush=True)
    finally:
        box.stop()


def inspect():
    """The inside of a Worker can be read: what it received, thought, called and got back."""
    box = Instance().fresh().start()
    try:
        box.client.call("start_many", count=1, profile="worker", brief="用 Bash 运行 echo INSPECT-ME，然后调用 report_done 汇报结果。")
        box.wait(lambda: box.tasks() and box.tasks()[0]["status"] == "completed", 90, "done")
        task = box.tasks()[0]
        out = subprocess.run([str(ROOT / "bin" / "supersuperharness"), "log", task["id"], "--full", "--raw"], capture_output=True, text=True,
                             env=dict(os.environ)).stdout
        check("看得到收到的消息", "received" in out and "INSPECT-ME" in out)
        check("看得到工具调用和结果", "tool_call Bash" in out and "tool_result" in out and "report_done" in out)
        print(out[-500:], flush=True)
    finally:
        box.stop()


if __name__ == "__main__":
    names = sys.argv[1:]
    for name in names:
        print("=== %s" % name, flush=True)
        began = time.time()
        globals()[name]()
        print("--- %s %.0fs" % (name, time.time() - began), flush=True)
    failed = [name for name, ok in results if not ok]
    print("%d checks, %d failed %s" % (len(results), len(failed), failed), flush=True)
    sys.exit(1 if failed else 0)
