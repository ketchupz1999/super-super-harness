"""super-super-harness: talk to the service, or read what happened. Reading works with the service down."""
import argparse
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time

from .ops import config
from . import daemon
from .ops import releases, setup
from .ops.config import REPO_ROOT
from .adapters import get_harness, session_harness
from .router.router import prompt_text
from .workers.tasks import LABELS, clip


class Client:
    def __init__(self, cfg):
        self.token = cfg.owner_token_file.read_text().strip()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(cfg.sock))
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self.lock = threading.Lock()

    def send(self, method, **params):
        with self.lock:
            self.sock.sendall((json.dumps({"method": method, "token": self.token, "params": params}, ensure_ascii=False) + "\n").encode())

    def call(self, method, **params):
        self.send(method, **params)
        answer = json.loads(self.reader.readline())
        if "error" in answer:
            raise SystemExit("error: %s" % answer["error"])
        return answer["result"]

    def emit(self, **event):
        """Python SDK: the same owner-only channel and the same bus.emit as the CLI."""
        return self.call("emit", event=event)


def cmd_watch(cfg, args):
    from .channels.chat import clean
    from .channels.watch import BUSY

    deadline = time.monotonic() + args.timeout if args.timeout is not None else None
    states = {}

    def output(record):
        if args.json:
            print(json.dumps(record, ensure_ascii=False), flush=True)
            return
        if record.get("event") == "snapshot":
            for worker in record["workers"]:
                output(dict(worker, type="snapshot"))
            if record["router"]:
                output(record["router"])
            if not record["workers"] and not record["router"]:
                print("watch: no matching active Workers", flush=True)
            return
        who = record.get("worker", "Router")
        detail = record.get("question") or record.get("report") or record.get("text", "")
        if record.get("options"):
            detail += " [" + " / ".join(record["options"]) + "]"
        line = "%s %s %s %s" % (who, record["type"], LABELS.get(record.get("status"), ""), detail)
        print(" ".join(clean(line).split()), flush=True)

    def remaining(setup=False):
        if deadline is None:
            return 5 if setup else None
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return min(left, 5) if setup else left

    try:
        token = cfg.owner_token_file.read_text().strip()
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(remaining(setup=True))
            sock.connect(str(cfg.sock))
            sock.sendall((json.dumps({"method": "watch", "token": token, "params": {
                "workers": args.workers, "item": args.item, "router": args.router}}, ensure_ascii=False) + "\n").encode())
            with sock.makefile("r", encoding="utf-8") as reader:
                initial = True
                while True:
                    sock.settimeout(remaining(setup=initial))
                    line = reader.readline()
                    if not line:
                        raise ConnectionError("watch connection closed before the condition was met")
                    packet = json.loads(line)
                    if "error" in packet:
                        raise ValueError(packet["error"])
                    if initial:
                        snapshot = packet["result"]
                        if snapshot["event"] != "snapshot":
                            raise ValueError("invalid watch snapshot")
                        states = {worker["task_id"]: worker["status"] for worker in snapshot["workers"]}
                        output(snapshot)
                        question = snapshot["router"] is not None or "waiting" in states.values()
                        changed = False
                        initial = False
                    else:
                        if packet["event"] != "watch" or not packet["changes"]:
                            raise ValueError("invalid watch event")
                        question = False
                        for record in packet["changes"]:
                            output(record)
                            if record["type"] == "worker.status":
                                states[record["task_id"]] = record["status"]
                            question |= record["type"] in ("ask_user", "router.ask_user")
                        question |= "waiting" in states.values()
                        changed = True
                    if question:
                        return 2
                    if args.until == "change" and changed:
                        return 0
                    if not states and not args.router:
                        return 0
                    if args.until == "done" and states and not any(state in BUSY for state in states.values()):
                        return 0
    except TimeoutError:
        if deadline is not None and time.monotonic() >= deadline:
            print("watch: timed out", file=sys.stderr)
            return 124
        print("watch: service connection timed out", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("watch: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def read_db(cfg):
    return sqlite3.connect("file:%s?mode=ro" % cfg.db, uri=True, timeout=5)


def find_task(db, needle):
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM tasks WHERE code_name=? COLLATE NOCASE", (needle,)).fetchone()
    if row is None:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (needle,)).fetchone()
    rows = [row] if row else db.execute("SELECT * FROM tasks WHERE name LIKE ? ORDER BY seq DESC", ("%" + needle + "%",)).fetchall()
    if not rows:
        raise SystemExit("no Worker matches %r" % needle)
    if len(rows) > 1:
        names = "；".join("%s %s" % (r["code_name"], r["name"]) for r in rows[:6])
        raise SystemExit("more than one match: %s" % names)
    return rows[0]


def cmd_chat(cfg, args):
    from .channels.chat import cmd_chat as chat
    return chat(cfg, args)


def cmd_history(cfg, args):
    from .channels.chat import CommandHistory, clean
    history = None
    try:
        history = CommandHistory(cfg.db)
        messages = history.recent(args.limit)
        for message in messages:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(message.created))
            print("%s %s %s\n%s" % (stamp, message.role, message.kind, clean(message.text)))
        if not messages:
            print("No messages.")
        sys.stdout.flush()
    except BrokenPipeError:
        # A reader such as head may exit early. Avoid another broken flush at shutdown.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
    except (OSError, sqlite3.Error):
        print("Cannot read history. Check database access and integrity; diagnostics suppressed to protect secrets.", file=sys.stderr)
        return 1
    finally:
        if history is not None:
            history.close()
    return 0


def cmd_web(cfg, args):
    try:
        client = Client(cfg)
    except OSError:
        print("后台服务没有运行", file=sys.stderr)
        return 1
    try:
        url = client.call("web_link")["url"]
    finally:
        client.reader.close()
        client.sock.close()
    if args.open:
        import webbrowser
        if webbrowser.open(url):
            print("已在浏览器打开")
        else:
            print("未能打开浏览器；请运行 supersuperharness web 获取新的登录链接")
    else:
        print(url)


def cmd_say(cfg, args):
    client = None
    try:
        client = Client(cfg)
        print(client.call("say", text=args.text)["id"])
    except OSError:
        print("后台服务没有运行", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.reader.close()
            client.sock.close()


def cmd_status(cfg, args):
    try:
        info = Client(cfg).call("status")
        router = info["router"]
        print("Router：%s，%s；运行中的 Worker %d/%d" % (router["profile"], router["mode"], info["live"], info["max_workers"]))
        web = info.get("web", {})
        print("Web：" + (web.get("error") or ("127.0.0.1:%s（supersuperharness web 获取登录链接）" % web["port"] if web.get("listening") else "未启用")))
    except (OSError, FileNotFoundError):
        print("后台服务没有运行")
    if not cfg.db.exists():
        return
    db = read_db(cfg)
    db.row_factory = sqlite3.Row
    for row in db.execute("SELECT * FROM tasks ORDER BY seq DESC LIMIT 20"):
        print("%-5s %-6s %-7s %s  %s" % (row["code_name"], LABELS[row["status"]], row["profile"], row["name"], clip(row["progress"], 60)))


def cmd_reminders(cfg, args):
    from .events.reminders import local_stamp
    if not cfg.db.exists():
        print("No reminders.")
        return
    with read_db(cfg) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminders'").fetchone():
            print("No reminders (start the updated service to enable them).")
            return
        rows = db.execute("SELECT * FROM reminders WHERE status='active' ORDER BY next_at,id").fetchall()
        for row in rows:
            print("r%-5d %-6s %s  %s%s" % (row["id"], row["kind"], local_stamp(row["next_at"]), row["name"],
                                              "  [" + row["cron"] + "]" if row["cron"] else ""))
        if not rows:
            print("No reminders.")


def cmd_emit(cfg, args):
    if os.environ.get("SSHARNESS_TASK_ID"):
        print("emit is for the local owner only; Workers cannot send events.", file=sys.stderr)
        return 1
    if args.json:
        try:
            with (sys.stdin if args.json == "-" else open(args.json, encoding="utf-8")) as file:
                event = json.load(file)
        except (OSError, ValueError):
            print("cannot read a JSON event from %s" % args.json, file=sys.stderr)
            return 1
    else:
        missing = [name for name in ("source", "type", "id", "thread", "text") if getattr(args, name) is None]
        if missing:
            print("missing --%s (or use --json FILE)" % ", --".join(missing), file=sys.stderr)
            return 1
        event = {name: getattr(args, name) for name in ("source", "type", "id", "thread", "text", "severity", "url") if getattr(args, name) is not None}
        if args.data is not None:
            try:
                event["data"] = json.loads(args.data)
            except ValueError:
                print("--data must be a JSON object", file=sys.stderr)
                return 1
    client = None
    try:
        client = Client(cfg)
    except OSError:
        print("后台服务没有运行", file=sys.stderr)
        return 1
    try:
        answer = client.call("emit", event=event)
    except (OSError, ValueError, TypeError, KeyError):
        # Closed early, cut off mid-line or not our protocol. Whether the event was recorded is unknown;
        # resending the same id is safe because (source, id) is accepted once.
        print("emit failed: the service closed the connection or gave no valid answer; resend the same id to retry", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.reader.close()
            client.sock.close()
    print(json.dumps(answer, ensure_ascii=False))
    return 0


def cmd_events(cfg, args):
    if not cfg.db.exists():
        print("No events.")
        return 0
    with read_db(cfg) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='triggers'").fetchone():
            print("No events (start the updated service to enable them).")
            return 0
        rows = db.execute("SELECT * FROM triggers ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    for row in rows:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(row["created"]))
        print("#%-5d %s %-6s %-8s %-10s %s %s  %s" % (row["id"], stamp, row["route"], row["severity"], row["source"],
                                                     row["type"], row["thread"], clip(row["text"], 100)))
    if not rows:
        print("No events.")
    return 0


def show_events(db, task_id, after):
    last = after
    for row in db.execute("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id", (task_id, after)):
        data = json.loads(row["data"])
        stamp = time.strftime("%H:%M:%S", time.localtime(row["created"]))
        print("%s %-16s %s" % (stamp, row["type"], clip(" ".join("%s" % value for value in data.values() if value is not None), 150)), flush=True)
        last = row["id"]
    return last


def cmd_log(cfg, args):
    db = read_db(cfg)
    task = find_task(db, args.task)
    if args.full:
        saved = db.execute("SELECT value FROM kv WHERE key=?", ("task_harness:" + task["id"],)).fetchone()
        harness = session_harness(cfg, task["profile"], saved[0] if saved else None)
        if harness is None:
            print("Profile/Harness removed; showing application events instead.")
            show_events(db, task["id"], 0)
            return
        for item in harness.transcript(cfg, task["session_id"]):
            if args.kinds and item["kind"] not in args.kinds.split(","):
                continue
            name = "%s %s" % (item["kind"], item.get("tool") or "")
            text = item["text"] if args.raw else clip(item["text"], 300)
            print("%s %-22s %s" % (item["time"][11:19], name.strip(), text))
        return
    print("%s %s（%s，%s）" % (task["code_name"], task["name"], LABELS[task["status"]], task["profile"]))
    print("目录：%s  分支：%s" % (task["cwd"], task["branch"]))
    print("为什么选这个模型：%s\n" % (task["reason"] or "默认"))
    last = show_events(db, task["id"], 0)
    while args.follow:
        time.sleep(1)
        last = show_events(db, task["id"], last)


def exec_interactive(cfg, profile_name, session, cwd, mcp, prompt, fork=False, hooks=None):
    profile = cfg.profile(profile_name)
    harness = get_harness(cfg, profile)
    argv = harness.interactive_argv(cfg, profile, session, mcp, prompt, fork=fork, hooks=hooks)
    env = harness.build_env(cfg, profile)
    env["SSHARNESS_HOME"] = str(cfg.home)
    env["PATH"] = "%s:%s" % (REPO_ROOT / "bin", env.get("PATH", ""))
    return subprocess.run(argv, env=env, cwd=str(cwd)).returncode


def cmd_peek(cfg, args):
    db = read_db(cfg)
    row = db.execute("SELECT value FROM kv WHERE key='router_session'").fetchone()
    profile = db.execute("SELECT value FROM kv WHERE key='router_profile'").fetchone()
    if not row:
        raise SystemExit("Router has no session yet")
    return exec_interactive(cfg, profile[0] if profile else cfg.primary, row[0], cfg.router_dir, None, None, fork=True)


def cmd_attach(cfg, args):
    client = None
    try:
        try:
            client = Client(cfg)
            info = client.call("router_detach")
        except (OSError, SystemExit, ValueError):
            print("接管失败；先运行 supersuperharness status 检查服务，再重试 supersuperharness attach。", file=sys.stderr)
            return 1
        hooks = {"Stop": [{"hooks": [{"type": "command", "command": "%s stop-check" % (REPO_ROOT / "bin" / "supersuperharness")}]}]}
        code = 1
        launch_failed = False
        handed_back = False
        try:
            if not info["started"]:
                print("Router 还没有会话，先发一条消息，再运行 supersuperharness attach。")
            else:
                note = (prompt_text(cfg) + "\n\n## A person is attached\n\nThe user is with you in the terminal. Talk to them directly here; "
                        "`reply` is for other channels. Worker results and messages from other channels no longer arrive by themselves: "
                        "after you start Workers, run `supersuperharness wait` in the background with Bash and end your turn. When it finishes, "
                        "call `inbox_read`, handle what it returns, and run `supersuperharness wait` again while Workers are out.")
                mcp = client.call("router_mcp")["path"]
                code = exec_interactive(cfg, info["profile"], info["session"], cfg.router_dir, mcp, note, hooks=hooks)
        except (OSError, SystemExit, ValueError):
            code = 1
            launch_failed = True
            print("接管启动失败；检查当前终端的凭据环境变量和 claude 可执行文件后，重试 supersuperharness attach。", file=sys.stderr)
        finally:
            try:
                client.call("router_attach")
                handed_back = True
            except (OSError, SystemExit, ValueError):
                print("交回后台失败；运行 supersuperharness status 检查服务，服务恢复后重新 supersuperharness attach 再退出。", file=sys.stderr)
        if not handed_back:
            return code or 1
        if code:
            if not launch_failed:
                print("接管未成功（退出码 %d）；后台已恢复。检查 Claude Code 首次引导和确认选项后，重试 supersuperharness attach。" % code, file=sys.stderr)
        else:
            print("Router 已交回后台。")
        return code
    finally:
        if client is not None:
            try:
                client.reader.close()
            finally:
                client.sock.close()


def cmd_open(cfg, args):
    client = Client(cfg)
    task = find_task(read_db(cfg), args.task)
    task = client.call("task_hold", task_id=task["id"])
    profile = cfg.profile(task["profile"])
    try:
        return exec_interactive(cfg, task["profile"], task["session_id"], task["cwd"], None, task.get("system_prompt"))
    finally:
        client.call("task_release", task_id=task["id"])
        print("%s 已交还，状态：%s。要继续就告诉 Router。" % (task["code_name"], task["status"]))


def cmd_wait(cfg, args):
    print("有 %d 条新消息，调用 inbox_read 读取。" % Client(cfg).call("wait")["queued"])


def cmd_stop_check(cfg, args):
    try:
        reason = Client(cfg).call("attach_check")["reason"]
    except (OSError, SystemExit):
        return 0
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def restarter(cfg):
    def status():
        try:
            return Client(cfg).call("status")
        except (OSError, SystemExit):
            return None

    def restart():
        if status():
            Client(cfg).call("stop")
            end = time.time() + 60
            while time.time() < end and cfg.sock.exists():
                time.sleep(0.3)
        return releases.healthy(status, releases.serve_detached(cfg))
    return restart


def cmd_upgrade(cfg, args):
    answer = releases.upgrade(cfg, args.repo or cfg.source_repo, args.ref, restarter(cfg))
    print(json.dumps(answer, ensure_ascii=False))
    return 0 if answer["ok"] else 1


def cmd_rollback(cfg, args):
    answer = releases.rollback(cfg, restarter(cfg))
    print(json.dumps(answer, ensure_ascii=False))
    return 0 if answer["ok"] else 1


def cmd_stop(cfg, args):
    Client(cfg).call("stop")


def cmd_router_reset(cfg, args):
    Client(cfg).call("router_reset")


def main():
    parser = argparse.ArgumentParser(prog="supersuperharness")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create private configuration or preserve legacy configuration before upgrading")
    init.add_argument("--provider")
    init.add_argument("--profile", help="new Profile name (existing Profiles are never overwritten)")
    init.add_argument("--base-url")
    init.add_argument("--auth-env", choices=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    init.add_argument("--key-env", help="name of an environment variable, never the key itself")
    init.add_argument("--model")
    init.add_argument("--context-window", type=int)
    init.add_argument("--binary", help="Claude Code executable (default: claude on PATH)")
    init.add_argument("--migrate-from", help="copy old profiles.toml to private SSHARNESS_HOME before upgrading; never overwrite")
    doctor = commands.add_parser("doctor", help="check Harness, credential and a tiny billable model call without starting the service")
    doctor.add_argument("--profile", action="append", help="repeat to check more Profiles; default: Router and default Worker")
    doctor.add_argument("--timeout", type=float, default=5)
    commands.add_parser("serve", help="run the service in the foreground")
    commands.add_parser("chat", help="talk to the Router")
    web = commands.add_parser("web", help="print a one-time local web login link")
    web.add_argument("--open", action="store_true", help="open the link in your browser")
    say = commands.add_parser("say", help="send one message")
    say.add_argument("text")
    commands.add_parser("status")
    history = commands.add_parser("history", help="read recent terminal conversation, even with the service stopped")
    history.add_argument("--limit", type=int, default=8, help="number of messages (default: 8)")
    commands.add_parser("reminders", help="list scheduled reminders, even with the service stopped")
    emit = commands.add_parser("emit", help="send one event to the bus (local owner only)")
    for name in ("source", "type", "id", "thread", "text", "url"):
        emit.add_argument("--" + name)
    emit.add_argument("--severity", choices=("info", "warn", "critical"))
    emit.add_argument("--data", help="JSON object with business fields")
    emit.add_argument("--json", help="read the whole event from a JSON file, or - for stdin")
    events = commands.add_parser("events", help="list recent events and where they went, even with the service stopped")
    events.add_argument("--limit", type=int, default=20, help="number of events (default: 20)")
    log = commands.add_parser("log", help="what a Worker did")
    log.add_argument("task", metavar="WORKER", help="code name (case-insensitive), internal id, or part of its name")
    log.add_argument("--full", action="store_true", help="everything the Harness recorded")
    log.add_argument("--kinds", help="received,said,thinking,tool_call,tool_result")
    log.add_argument("--raw", action="store_true", help="do not shorten")
    log.add_argument("--follow", "-f", action="store_true")
    commands.add_parser("peek", help="open a copy of the Router session")
    commands.add_parser("attach", help="take the Router over in the terminal")
    open_task = commands.add_parser("open", help="take a Worker over in the terminal")
    open_task.add_argument("task", metavar="WORKER", help="code name (case-insensitive), internal id, or part of its name")
    commands.add_parser("wait", help="block until the Router has a message")
    watch = commands.add_parser("watch", help="wait for Worker changes (owner-only server push)")
    watch.add_argument("workers", nargs="*", metavar="WORKER", help="code name or internal id; default: currently active Workers")
    watch.add_argument("--item", metavar="CODE", help="filter Workers by item")
    watch.add_argument("--until", choices=("done", "change"), default="done")
    watch.add_argument("--timeout", type=float, metavar="SECONDS")
    watch.add_argument("--json", action="store_true", help="one JSON object per line")
    watch.add_argument("--router", action="store_true", help="also watch Router questions")
    commands.add_parser("stop-check", help="Stop hook used by attach")
    upgrade = commands.add_parser("upgrade", help="install a commit, test it, switch to it, roll back if it is unhealthy")
    upgrade.add_argument("--repo")
    upgrade.add_argument("--ref", default="main")
    commands.add_parser("rollback", help="go back to the previous version")
    commands.add_parser("stop")
    commands.add_parser("router-reset", help="give the Router a new session")
    args = parser.parse_args()
    if args.command == "init":
        try:
            path = setup.initialize(config.home_path(), args)
        except (EOFError, setup.getpass.GetPassWarning):
            print("Interactive input is unavailable. Use non-interactive init --provider NAME --key-env ENV_NAME (add --base-url, --auth-env, --model and --context-window for a custom Provider); see docs/setup.md. Never pass the key itself as an argument.", file=sys.stderr)
            return 1
        except setup.EmptyKeyError:
            print("API Key is empty. Configuration was not installed; retry init with a non-empty hidden key or --key-env ENV_NAME.", file=sys.stderr)
            return 1
        except config.PublicConfigurationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except (ValueError, OSError, KeyError, TypeError):
            print("Configuration was not installed. Check inputs, private file permissions, and existing Profile names; see docs/setup.md.", file=sys.stderr)
            return 1
        print("Private configuration saved: %s\nNext: supersuperharness doctor (makes a tiny model call)." % path)
        return 0
    if args.command == "doctor":
        if not 0 < args.timeout <= 60:
            parser.error("--timeout must be between 0 and 60 seconds")
        try:
            results = setup.doctor(config.load(), args.profile, args.timeout)
        except config.PublicConfigurationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except (ValueError, OSError, KeyError, TypeError):
            print("Configuration is invalid; diagnostics suppressed to protect secrets.", file=sys.stderr)
            return 1
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(r["ok"] for r in results) else 1
    if args.command == "serve":
        return daemon.serve()
    if args.command == "watch":
        if args.timeout is not None and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 31536000):
            parser.error("--timeout must be greater than zero and at most 31536000 seconds (365 days)")
        return cmd_watch(config.Paths(config.home_path()), args)
    if args.command == "history":
        if not 0 < args.limit <= 2 ** 63 - 1:
            parser.error("--limit must be between 1 and 9223372036854775807")
        return cmd_history(config.Paths(config.home_path()), args)
    if args.command == "events":
        if not 0 < args.limit <= 10000:
            parser.error("--limit must be between 1 and 10000")
        return cmd_events(config.Paths(config.home_path()), args)
    if args.command == "emit":
        return cmd_emit(config.Paths(config.home_path()), args)
    if args.command in ("stop", "status"):
        cfg = config.Paths(config.home_path())
        try:
            return {"stop": cmd_stop, "status": cmd_status}[args.command](cfg, args)
        except OSError:
            print("后台服务没有运行", file=sys.stderr)
            return 1
    try:
        cfg = config.load()
    except config.PublicConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, TypeError):
        print("Configuration is unavailable or invalid. Existing installations must run init --migrate-from with the old configuration; new installations should run init. See docs/setup.md. Diagnostics suppressed to protect secrets.", file=sys.stderr)
        return 1
    return {"reminders": cmd_reminders, "web": cmd_web, "chat": cmd_chat, "say": cmd_say, "status": cmd_status, "log": cmd_log, "peek": cmd_peek, "attach": cmd_attach,
            "open": cmd_open, "wait": cmd_wait, "stop-check": cmd_stop_check, "upgrade": cmd_upgrade, "rollback": cmd_rollback, "stop": cmd_stop, "router-reset": cmd_router_reset}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
