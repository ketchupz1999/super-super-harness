#!/usr/bin/env python3
"""Hermes-shaped ACP peer and state.db fixture. Standard library only; no model calls."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

HOME = Path(os.environ["HERMES_HOME"])
LOCK = threading.Lock()
CANCEL = threading.Event()
ANSWERS = {}
SESSION = None
ACTIVE = False


def write(message):
    with LOCK:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()


def result(ident, value):
    global ACTIVE
    ACTIVE = False
    write({"id": ident, "result": value})


def update(kind, **data):
    write({"method": "session/update", "params": {"sessionId": SESSION, "update": {"sessionUpdate": kind, **data}}})


def text(value, message="answer"):
    update("agent_message_chunk", messageId=message, content={"type": "text", "text": value})


def save(role, value, **extra):
    with sqlite3.connect(HOME / "state.db") as db:
        values = {"session_id": SESSION, "role": role, "content": value, "timestamp": time.time(), **extra}
        db.execute("INSERT INTO messages(%s) VALUES(%s)" % (",".join(values), ",".join("?" for _ in values)), list(values.values()))


def request(method, **params):
    ident = "server-" + str(uuid.uuid4())
    ready, answer = threading.Event(), {}
    ANSWERS[ident] = (ready, answer)
    write({"id": ident, "method": method, "params": params})
    if not ready.wait(3):
        raise RuntimeError("client failed to answer server request")
    return answer


def mcp_call(name, arguments, *, emit=True):
    call_id = "mcp-" + str(uuid.uuid4())
    if emit:
        update("tool_call", toolCallId=call_id, title="mcp__router__" + name + ": fixture",
               rawInput=arguments, status="in_progress")
    server = json.loads((HOME / "config.yaml").read_text())["mcp_servers"]["router"]
    frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}]
    # Deliberately no _meta tool-use ID: Hermes uses name/argument attribution.
    with subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True) as child:
        output, _ = child.communicate("".join(json.dumps(f) + "\n" for f in frames), timeout=8)
    answer = json.loads(output.splitlines()[-1])["result"]
    if emit:
        update("tool_call_update", toolCallId=call_id, status="failed" if answer.get("isError") else "completed",
               rawOutput=answer)
    return answer


def turn(ident, prompt):
    global ACTIVE
    try:
        words = prompt.split()
        if prompt.startswith("[Session handoff]"):
            words = ["PAUSE"] if "HERMES_HANDOFF_PAUSE" in words else []
        if prompt.startswith("/discard "):
            with sqlite3.connect(HOME / "state.db") as db:
                db.execute("UPDATE session_admissions SET status='terminal', outcome='interrupted' WHERE admission_id=?", (prompt.split()[1],))
            text("discarded control message")
            result(ident, {"stopReason": "end_turn"})
            return
        once = HOME / "cancelled-once"
        if "CANCEL_BEFORE" in words or ("CANCEL_ONCE" in words and not once.exists()):
            once.touch()
            result(ident, {"stopReason": "cancelled"})
            return
        failed = HOME / "failed-before-once"
        if "FAIL_BEFORE_ONCE" in words and not failed.exists():
            failed.touch()
            ACTIVE = False
            write({"id": ident, "error": {"code": -32000, "message": "failed before persistence"}})
            return
        missing = HOME / "not-stored-once"
        skip_store = "NO_STORE_ONCE" in words and not missing.exists()
        if skip_store:
            missing.touch()
        if "NO_STORE" not in words and not skip_store:
            save("user", prompt)
        if "ORPHAN_STDERR" in words:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            (HOME / "child.pid").write_text(str(child.pid))
            os._exit(7)
        if "ORPHAN_PIPE" in words:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (HOME / "child.pid").write_text(str(child.pid))
            os._exit(7)
        if "CRASH" in words:
            os._exit(7)
        if "ERROR" in words:
            ACTIVE = False
            write({"id": ident, "error": {"code": -32000, "message": "fake-private-provider-secret"}})
            return
        if "PERMISSIONS" in words:
            permission = request("session/request_permission", sessionId=SESSION,
                toolCall={"toolCallId": "approval-1", "title": "terminal"},
                options=[{"optionId": "deny", "kind": "reject_once"}, {"optionId": "allow", "kind": "allow_once"}])
            assert permission["result"]["outcome"] == {"outcome": "selected", "optionId": "allow"}
            clarify = request("session/request_permission", sessionId=SESSION,
                toolCall={"toolCallId": "clarify-1", "title": "Which?"},
                options=[{"optionId": "wrong-answer", "kind": "allow_once"}, {"optionId": "skip", "kind": "reject_once"}])
            assert clarify["result"]["outcome"] == {"outcome": "cancelled"}
            unknown = request("future/unknown", sessionId=SESSION)
            assert unknown["error"]["code"] == -32601
        if "CHILD" in words:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (HOME / "child.pid").write_text(str(child.pid))
        if "TOOLS" in words:
            text("comment", "commentary")
            update("tool_call", toolCallId="call-1", title="mcp__router__report_progress: working",
                   rawInput={"text": "working"}, status="in_progress")
            update("tool_call_update", toolCallId="call-1", status="completed",
                   content=[{"type": "content", "content": {"type": "text", "text": "ok"}}])
            update("tool_call_update", toolCallId="call-1", status="completed", rawOutput="duplicate ignored")
            update("tool_call", toolCallId="call-2", title="terminal", rawInput={"command": "false"})
            update("tool_call_update", toolCallId="call-2", status="failed", rawOutput="failed")
        if "REPORT" in words or "REPORT2" in words:
            answer = mcp_call("report_done", {"text": "fixture report two" if "REPORT2" in words else "fixture report"})
            assert not answer.get("isError"), answer
        if "CHILD_REPORT" in words:
            answer = mcp_call("report_done", {"text": "unattributed child report"}, emit=False)
            assert answer.get("isError"), answer
        if "PAUSE" in words:
            deadline = time.monotonic() + 20
            while not (HOME / "continue").exists() and not CANCEL.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
        if "ASK_TOOL" in words:
            answer = mcp_call("ask_user", {"question": "fixture question?", "options": ["yes", "no"]})
            assert not answer.get("isError"), answer
        if "BUSY_TOOL" in words:
            update("tool_call", toolCallId="busy-1", title="terminal", rawInput={"command": "fixture only"})
        if "WAIT" in words:
            text("waiting", "wait")
            CANCEL.wait(20)
        if "COMPACT" in words:
            with sqlite3.connect(HOME / "state.db") as db:
                db.execute("UPDATE messages SET active=0, compacted=1 WHERE session_id=? AND role='user'", (SESSION,))
        if CANCEL.is_set():
            save("assistant", "cancelled")
            result(ident, {"stopReason": "cancelled"})
            return
        text("fake ")
        text("answer")
        save("assistant", "fake answer")
        result(ident, {"stopReason": "end_turn"})
    except Exception:
        ACTIVE = False
        write({"id": ident, "error": {"code": -32001, "message": "fixture assertion failed"}})


def main():
    global SESSION, ACTIVE
    if "--check" in sys.argv:
        print("Hermes ACP check OK")
        return
    HOME.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SSHARNESS_FAKE_REQUIRE_RUNTIME"):
        settings = json.loads((HOME / "config.yaml").read_text())
        assert settings["_config_version"] == 52
        assert settings["tools"]["tool_search"]["enabled"] == "off"
        assert settings["security"]["allow_lazy_installs"] is False
        assert settings["agent"]["gateway_startup_warmup_timeout"] == 0
        assert settings["agent"]["environment_probe"] is False
        assert os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1"
        assert Path(os.environ["HERMES_RUNTIME_DIR"]) == HOME.parent.parent / "runtime"
    if os.environ.get("SSHARNESS_FAKE_STDERR_FLOOD"):
        sys.stderr.write("x" * 100000 + "\n")
    if os.environ.get("SSHARNESS_FAKE_STDERR"):
        sys.stderr.write(os.environ["SSHARNESS_FAKE_STDERR"])
        sys.stderr.flush()
    with sqlite3.connect(HOME / "state.db") as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER);
            INSERT INTO schema_version SELECT 32 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                timestamp REAL, tool_calls TEXT, tool_call_id TEXT, reasoning TEXT, reasoning_content TEXT,
                active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, display_kind TEXT DEFAULT 'visible');
            CREATE TABLE IF NOT EXISTS session_admissions(seq INTEGER PRIMARY KEY,
                target_session_id TEXT, admission_id TEXT, status TEXT, outcome TEXT);
        """)
    for line in sys.stdin:
        frame = json.loads(line)
        with (HOME / "wire.jsonl").open("a") as log:
            log.write(json.dumps(frame) + "\n")
        method, ident, params = frame.get("method"), frame.get("id"), frame.get("params", {})
        if method and method == os.environ.get("SSHARNESS_FAKE_FAIL_METHOD"):
            write({"id": ident, "error": {"code": -32000, "message": "startup-private-secret"}})
        elif method and method == os.environ.get("SSHARNESS_FAKE_HANG_METHOD"):
            continue
        elif method == "initialize":
            time.sleep(float(os.environ.get("SSHARNESS_FAKE_INITIALIZE_DELAY", "0")))
            result(ident, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
        elif method == "session/new":
            SESSION = "hermes-" + str(uuid.uuid4())
            result(ident, {"sessionId": SESSION})
        elif method == "session/load":
            SESSION = params["sessionId"]
            with sqlite3.connect(HOME / "state.db") as db:
                db.execute("UPDATE session_admissions SET status='unknown' WHERE target_session_id=? AND status='started'", (SESSION,))
            text("historical replay: never deliver", "history")
            update("tool_call", toolCallId="old-call", title="old tool", rawInput={})
            result(ident, {})
        elif method in ("session/set_mode", "session/set_model"):
            result(ident, {})
        elif method == "session/prompt":
            if ACTIVE:
                write({"id": ident, "error": {"code": -32002, "message": "overlapping prompts"}})
            else:
                ACTIVE = True
                CANCEL.clear()
                prompt = "\n".join(p.get("text", "") for p in params["prompt"])
                threading.Thread(target=turn, args=(ident, prompt), daemon=True).start()
        elif method == "session/cancel":
            CANCEL.set()
        elif method is None and ident in ANSWERS:
            ready, answer = ANSWERS.pop(ident)
            answer.update(frame)
            ready.set()
        else:
            write({"id": ident, "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    main()
