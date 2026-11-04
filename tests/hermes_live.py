"""OPT-IN ONLY: real Hermes, fake loopback Provider, disposable service data.

Do not run during ordinary development or unittest discovery. A person must set
SSHARNESS_HERMES_LIVE=1 and supply a separate clean source-tree installation:

    TMPDIR=/tmp SSHARNESS_HERMES_LIVE=1 python3 -m tests.hermes_live \
        --binary /path/to/clean/hermes-agent/.venv/bin/hermes

The model route uses no real API key or external Provider. This is not an OS
network sandbox: use an isolated host if all external traffic must be forbidden.
The endpoint deterministically asks Hermes to run a local-only git push probe and report_done. It also checks cancel,
cold resume/model selection, unknown-execution recovery and both auth headers.
The private temporary home is retained with verification.json for inspection.
This script has NOT been executed as part of the adapter's fixture acceptance.
"""
import argparse
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

REVISION = "6b8fa991464d56680fc3ddd93755c8669412a44c"
SYSTEM_MARKER = "HERMES_LIVE_SYSTEM_" + secrets.token_hex(8)


class Endpoint(ThreadingHTTPServer):
    def __init__(self, port, key):
        super().__init__(("127.0.0.1", port), Handler)
        self.key, self.phase, self.plan = key, "idle", []
        self.observations = []
        self.lock = threading.Lock()

    def phase_plan(self, phase, *steps):
        with self.lock:
            self.phase, self.plan = phase, list(steps)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/count_tokens"):
                data = b'{"input_tokens":1}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            with self.server.lock:
                phase = self.server.phase
                step = self.server.plan.pop(0) if self.server.plan else ("text", "fixture complete")
                self.server.observations.append({
                    "phase": phase, "model": body.get("model"),
                    "api_key_only": self.headers.get("x-api-key") == self.server.key and not self.headers.get("Authorization"),
                    "bearer_only": self.headers.get("Authorization") == "Bearer " + self.server.key and not self.headers.get("x-api-key"),
                    "system_prompt": SYSTEM_MARKER in json.dumps(body.get("system")),
                    "prior_history": "LIVE_HISTORY_MARKER" in json.dumps(body.get("messages")),
                })
            if step[0] == "tool":
                _, name, arguments = step
                if name not in {tool.get("name") for tool in body.get("tools", [])}:
                    raise ValueError("required fixture tool is not advertised")
                block = {"type": "tool_use", "id": "live-" + secrets.token_hex(8), "name": name, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
                reason = "tool_use"
            else:
                block, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": step[1]}
                reason = "end_turn"
            events = [
                {"type": "message_start", "message": {"id": "msg-" + secrets.token_hex(8), "type": "message",
                    "role": "assistant", "model": body.get("model"), "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            data = "".join("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event)) for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, KeyError, TypeError):
            self.send_error(400, "Invalid fixture request")
        except (BrokenPipeError, ConnectionResetError):
            pass


def git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}


def local_probe(home):
    """A failed guard can push ONLY to this throwaway file:// remote."""
    env = git_env()
    repo, remote = home / "push-source", home / "push-target.git"
    for args in (["init", "-q", str(repo)], ["init", "-q", "--bare", str(remote)],
                 ["-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "fixture", "--allow-empty"]):
        subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], env=env, check=True, capture_output=True, timeout=10)
    probe = home / "terminal_probe.py"
    probe.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
if len(sys.argv) > 1:
    (root / sys.argv[1]).touch()
    time.sleep(60)
else:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0")
    if "GIT_CONFIG_PARAMETERS" in os.environ:
        env["GIT_CONFIG_PARAMETERS"] = os.environ["GIT_CONFIG_PARAMETERS"]
    done = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root / "push-source"), "push", (root / "push-target.git").as_uri(),
                           "HEAD:refs/heads/guard-probe"], env=env, capture_output=True, text=True, timeout=10)
    (root / "terminal-observation.json").write_text(json.dumps({
        "push_blocked": done.returncode != 0 and "ssharness-push-blocked" in done.stderr,
        "guard_present": "pushinsteadof" in os.environ.get("GIT_CONFIG_PARAMETERS", ""),
        "tmpdir": os.environ.get("TMPDIR", ""), "cwd": os.getcwd()}))
''')
    return shlex.join([sys.executable, str(probe)])


async def wait_until(predicate, timeout=60):
    async def poll():
        while not predicate():
            await asyncio.sleep(.05)
    await asyncio.wait_for(poll(), timeout)


async def exercise(args, home, report):
    # Imported only after the explicit opt-in and clean-install/revision checks in main.
    from super_super_harness.adapters import hermes, hermes_state, procs, register
    from super_super_harness.daemon import Service
    from super_super_harness.ops import config
    from tests.fake_harness import FakeHarness
    from tests.support import free_port

    key = "fixture-" + secrets.token_hex(16)
    port = free_port()
    endpoint, thread = None, None
    register("hermes-live-router", FakeHarness())
    raw = {"default_profile": "work", "router": {"primary": "router", "backup": "router"},
           "web": {"port": 0}, "session_root": str(home / "unused"),
           "limits": {"max_workers": 1, "worker_retries": 0},
           "harness": {"hermes": {"binary": args.binary}, "hermes-live-router": {}},
           "providers": {"local-api": {"base_url": "http://127.0.0.1:%d" % port, "key_env": "SSHARNESS_LIVE_FIXTURE_KEY"},
                         "local-bearer": {"base_url": "http://127.0.0.1:%d" % port, "key_env": "SSHARNESS_LIVE_FIXTURE_KEY",
                                          "auth_env": "ANTHROPIC_AUTH_TOKEN"}},
           "profiles": {"router": {"harness": "hermes-live-router", "provider": "local-api", "model": "fixture"},
                        "work": {"harness": "hermes", "provider": "local-api", "model": "live-model", "context_window": 200000},
                        "bearer": {"harness": "hermes", "provider": "local-bearer", "model": "live-model", "context_window": 200000}}}
    cfg = config.from_raw(raw, home)
    processes = []
    original_process = hermes.Process
    def record_process(argv, env, cwd, on_event, on_exit, log_path=None):
        events = []
        async def event(message):
            events.append(message)
            await on_event(message)
        process = original_process(argv, env, cwd, event, on_exit, log_path=log_path)
        process.live_check_events = events
        processes.append(process)
        return process
    command = local_probe(home)
    os_home = home / "os-home"
    os_home.mkdir(mode=0o700)
    service, running = None, None
    teardowns, teardown_errors = {}, set()
    def mark(name, condition):
        report["checks"][name] = bool(condition)
        if not condition:
            raise AssertionError(name)
    def report_plan(phase, text):
        endpoint.phase_plan(phase, ("tool", "mcp__router__report_done", {"text": text}), ("text", "finished"))
    async def finished(task_id, statuses=("completed",)):
        await wait_until(lambda: service.store.task(task_id)["status"] in {*statuses, "failed", "interrupted"}
                         and task_id not in service.tasks.live)
        # _on_exit removes live before _end_process finishes its workspace sweep.
        await wait_until(lambda: all(done.is_set() for done in teardowns.get(task_id, [])))
        if task_id in teardown_errors or service.store.task(task_id)["status"] not in statuses:
            raise AssertionError("Worker did not reach the expected fixture state with complete teardown")
    try:
        endpoint = Endpoint(port, key)
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        with patch.dict(os.environ, SSHARNESS_HOME=str(home), SSHARNESS_LIVE_FIXTURE_KEY=key, SSHARNESS_NO_CAFFEINATE="1",
                        HOME=str(os_home), SSH_AUTH_SOCK=""), \
                patch.object(hermes, "Process", side_effect=record_process):
            report["phase"] = "installation"
            mark("acp_dependencies", hermes.installation_check(cfg, cfg.profile("work"), timeout=20)[0])
            service = Service(cfg)
            end_process = service.tasks._end_process
            def track_teardown(task_id):
                finish = end_process(task_id)
                done = asyncio.Event()
                teardowns.setdefault(task_id, []).append(done)
                async def tracked():
                    try:
                        await finish
                    except (Exception, asyncio.CancelledError):
                        teardown_errors.add(task_id)
                        raise
                    finally:
                        done.set()
                return tracked()
            service.tasks._end_process = track_teardown
            prompt = service.tasks.system_prompt
            service.tasks.system_prompt = lambda task: prompt(task) + "\n" + SYSTEM_MARKER
            running = asyncio.create_task(service.run())
            await wait_until(lambda: cfg.sock.exists() and service.router.ready)

            report["phase"] = "report_done_and_terminal"
            endpoint.phase_plan("probe", ("tool", "terminal", {"command": command, "timeout": 20}),
                                ("tool", "mcp__router__report_done", {"text": "live parent report"}), ("text", "finished"))
            started = time.monotonic()
            task_id = service.tasks.start("Hermes live fixture", "LIVE_HISTORY_MARKER: execute the fixture plan.")["id"]
            await wait_until(lambda: service.store.task(task_id)["started_once"] or service.store.task(task_id)["status"] == "failed")
            report["startup_seconds"] = round(time.monotonic() - started, 3)
            await finished(task_id)
            mark("parent_report_done", service.store.task(task_id)["report"] == "live parent report" and
                 service.store.one("SELECT id FROM events WHERE task_id=? AND type='report.done'", task_id) is not None)
            observed = json.loads((home / "terminal-observation.json").read_text())
            mark("terminal_push_guard", observed["push_blocked"] and observed["guard_present"])
            report["terminal_tmpdir"] = observed["tmpdir"]
            mark("terminal_tmpdir_isolated", bool(observed["tmpdir"]) and Path(observed["tmpdir"]).resolve().is_relative_to(home))
            sid = service.store.task(task_id)["session_id"]
            mark("transcript", any(i["kind"] == "received" and "LIVE_HISTORY_MARKER" in i["text"] for i in hermes.transcript(cfg, sid)))
            settings = json.loads((home / "hermes" / "homes" / sid / "config.yaml").read_text())
            mark("update_settings", not settings["updates"]["check"] and not settings["plugins"]["auto_apply"] and
                 settings["plugins"]["auto_update_check_hours"] == 0)

            report["phase"] = "interrupt"
            endpoint.phase_plan("interrupt", ("tool", "terminal", {"command": command + " interrupt-ready", "timeout": 120}),
                                ("text", "interrupted fixture"))
            service.tasks.send(task_id, "Run the interrupt fixture.")
            await wait_until((home / "interrupt-ready").exists)
            interrupted_process = service.tasks.live[task_id].process
            event_offset = len(interrupted_process.live_check_events)
            interrupt_started = time.monotonic()
            mark("interrupt_requested", service.tasks.interrupt(task_id)["ok"])
            # Require a native cancellation result before the service's 10s kill fallback.
            await wait_until(lambda: any(e.get("type") == "result" and e.get("subtype") == "error_during_execution"
                                         for e in interrupted_process.live_check_events[event_offset:]), timeout=8)
            report["interrupt_seconds"] = round(time.monotonic() - interrupt_started, 3)
            mark("interrupt_rpc_result", True)
            await finished(task_id, ("interrupted",))
            mark("interrupt_settled", True)

            report["phase"] = "resume_and_model"
            cfg.profiles["work"] = replace(cfg.profile("work"), model="live-model-2")
            report_plan("resume", "live resumed report")
            mark("resume_requested", service.tasks.resume(task_id, "Continue the fixture after interruption.")["ok"])
            await finished(task_id)
            mark("resume_same_session", service.store.task(task_id)["session_id"] == sid)
            mark("resume_report", service.store.task(task_id)["report"] == "live resumed report")
            resumed = [row for row in endpoint.observations if row["phase"] == "resume"]
            mark("model_selection", bool(resumed) and all(row["model"] == "live-model-2" for row in resumed))
            mark("resume_history", any(row["prior_history"] for row in resumed))

            report["phase"] = "unknown_execution"
            endpoint.phase_plan("crash", ("tool", "terminal", {"command": command + " crash-ready", "timeout": 120}))
            service.tasks.send(task_id, "Run the crash recovery fixture.")
            await wait_until((home / "crash-ready").exists)
            process = service.tasks.live[task_id].process
            await process._remember_children()
            mark("gateway_descendants_observed", bool(process._children))
            for pid, identity in list(process._children.items()):
                if await asyncio.to_thread(procs.alive, pid, identity):
                    await asyncio.to_thread(procs.kill_tree, pid)
            await finished(task_id, ("failed", "interrupted"))
            hid = hermes._hermes_id(cfg, sid)
            mark("unknown_admission_observed", bool(hermes_state.unknown(hermes.session_file(cfg, sid), hid)))
            report_plan("recovery", "live recovered report")
            mark("recovery_requested", service.tasks.resume(task_id, "Recover the fixture; do not repeat the terminal probe.")["ok"])
            await finished(task_id)
            mark("unknown_admission_discarded", not hermes_state.unknown(hermes.session_file(cfg, sid), hid))
            mark("recovery_report", service.store.task(task_id)["report"] == "live recovered report")

            report["phase"] = "bearer_auth"
            report_plan("bearer", "live bearer report")
            bearer_id = service.tasks.start("Hermes Bearer fixture", "Run the auth fixture.", profile="bearer")["id"]
            await finished(bearer_id)
            api_rows = [row for row in endpoint.observations if row["phase"] != "bearer"]
            bearer_rows = [row for row in endpoint.observations if row["phase"] == "bearer"]
            mark("api_key_header", bool(api_rows) and all(row["api_key_only"] for row in api_rows))
            mark("bearer_header", bool(bearer_rows) and all(row["bearer_only"] for row in bearer_rows))
            mark("system_prompt", all(row["system_prompt"] for row in endpoint.observations))
            mark("bearer_report", service.store.task(bearer_id)["report"] == "live bearer report")
            report["phase"] = "complete"
    finally:
        cleanup_errors = []
        async def cleanup(label, action, timeout=5):
            try:
                await asyncio.wait_for(action, timeout)
                return True
            except (Exception, asyncio.CancelledError):
                cleanup_errors.append(label)
                return False
        if service and running:
            service.stopping.set()
            if not await cleanup("service shutdown", asyncio.shield(running), 30):
                running.cancel()
                await cleanup("cancelled service shutdown", asyncio.shield(running))
        await cleanup("worker teardown", asyncio.gather(*(done.wait() for group in teardowns.values() for done in group)), 30)
        if teardown_errors:
            cleanup_errors.append("worker teardown failure")
        for process in processes:
            if not await cleanup("process shutdown", process.close(wait=.2)) and process.pid:
                await cleanup("forced process shutdown", asyncio.to_thread(process._kill_children))
        if endpoint:
            if thread and thread.is_alive():
                await cleanup("endpoint shutdown", asyncio.to_thread(endpoint.shutdown))
            await cleanup("endpoint close", asyncio.to_thread(endpoint.server_close))
            if thread:
                await cleanup("endpoint thread", asyncio.to_thread(thread.join, 2))
        report["checks"]["process_cleanup"] = False
        try:
            def inspect_processes():
                known = [pid for process in processes for pid, identity in process._children.items() if procs.alive(pid, identity)]
                return known, procs.in_dir(home)
            known_alive, cwd_alive = await asyncio.to_thread(inspect_processes)
            report["checks"]["process_cleanup"] = not known_alive and not cwd_alive and not cleanup_errors
            for pid in set(known_alive + cwd_alive):
                # Observed identities or a freshly-created private fixture cwd.
                await cleanup("orphan cleanup", asyncio.to_thread(procs.kill_tree, pid))
        except (OSError, subprocess.TimeoutExpired):
            cleanup_errors.append("process inspection")
        report["cleanup_errors"] = cleanup_errors
        report["observations"] = endpoint.observations if endpoint else []


def main():
    if os.environ.get("SSHARNESS_HERMES_LIVE") != "1":
        print("NOT RUN: explicitly set SSHARNESS_HERMES_LIVE=1 to allow real isolated Hermes processes.")
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, help="absolute path to a separate clean source-tree Hermes executable")
    parser.add_argument("--expected-revision", default=REVISION)
    args = parser.parse_args()
    if not Path(args.binary).is_absolute():
        parser.error("--binary must be absolute")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from super_super_harness.adapters import hermes, procs
    hermes._check_install(args.binary, {})
    source = next(p for p in Path(args.binary).resolve().parents if (p / "hermes_cli" / "main.py").is_file())
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], env=git_env(), text=True, timeout=5).strip()
    clean = subprocess.run(["git", "-C", str(source), "diff", "HEAD", "--quiet"], env=git_env(), capture_output=True, timeout=5)
    if clean.returncode:
        parser.error("Hermes installation has tracked changes; use a clean checkout for reproducible verification")
    if revision != args.expected_revision:
        parser.error("Hermes revision differs from the requested contract; inspect it before explicitly selecting another revision")
    home = Path(tempfile.mkdtemp(prefix="hhl-", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
    if len(str(home / "router.sock").encode()) > 100:
        parser.error("temporary socket path is too long; set TMPDIR=/tmp")
    report = {"hermes_revision": revision, "home": str(home), "phase": "setup", "checks": {}, "ok": False,
              "scope": "real Hermes + fake loopback Provider; external Provider compatibility is not tested"}
    def bounded_proc_command(*a, **kw):
        kw["timeout"] = 5
        return subprocess.run(*a, **kw)
    try:
        # Keep real ps/lsof deadlines installed until asyncio.run has joined its
        # default executor: timing out an await does not stop its worker thread.
        with patch.object(procs, "subprocess", SimpleNamespace(run=bounded_proc_command)):
            asyncio.run(exercise(args, home, report))
        report["ok"] = bool(report["checks"]) and all(report["checks"].values())
    except (Exception, KeyboardInterrupt) as exc:
        report["error_type"] = type(exc).__name__   # never upstream bodies or credentials
    finally:
        destination = home / "verification.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        destination.chmod(0o600)
        print("PASS" if report["ok"] else "FAILED", "—", destination)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
