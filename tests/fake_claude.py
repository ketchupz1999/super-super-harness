#!/usr/bin/env python3
"""Deterministic stand-in for Claude Code's stream-json mode. Commands in the message text drive its behaviour.

  SLEEP n     stay inside the turn for n seconds, as a foreground tool would
  TOOLSLEEP n same, but emits a Bash tool_use first
  FAIL        finish the turn with is_error=true
  CRASH       exit without a result
  ALWAYSFAIL, ALWAYSCRASH  same, and for every later turn of this session too
  HANG        accept the message and never answer
  SILENTHANG  same, with no replay: the message is in the session but nobody was told
  RETRY n     emit n api_retry events, then finish
  ECHO text   reply with text
  DONE word     call report_done(word) through the router MCP server, as Claude Code does
  SUBDONE word  same, from inside a sub-agent: the tool_use carries parent_tool_use_id
  GHOSTDONE word  same, for a tool_use id that never appears on stdout
  OLDDONE word, OLDSUBDONE word  DONE and SUBDONE from a Claude Code that sends no _meta
"""
import json
import os
import select
import subprocess
import sys
import time

args = sys.argv[1:]


def option(name):
    return args[args.index(name) + 1] if name in args else None


session = option("--session-id") or option("--resume")
model = option("--model")
root = os.environ["SSHARNESS_FAKE_ROOT"]
os.makedirs(root, exist_ok=True)
path = os.path.join(root, session + ".jsonl")
if "--session-id" in args and os.path.exists(path):
    print("Error: Session ID %s is already in use." % session, flush=True)
    sys.exit(1)
if "--resume" in args and not (os.path.exists(path) and '"type": "user"' in open(path).read()):
    print("No conversation found with session ID: %s" % session, flush=True)
    sys.exit(1)
with open(os.path.join(root, "argv.log"), "a") as log:
    log.write(json.dumps({"pid": os.getpid(), "argv": args, "tmpdir": os.environ.get("TMPDIR"), "model": model}) + "\n")


def emit(**msg):
    msg.setdefault("session_id", session)
    if msg.get("type") == "assistant":
        record("assistant", timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message=msg["message"])
    print(json.dumps(msg), flush=True)


def record(kind, **fields):
    with open(path, "a") as out:
        out.write(json.dumps(dict(type=kind, **fields)) + "\n")


def result(text, error=False, subtype="success"):
    emit(type="result", subtype=subtype, is_error=error, result=text, usage={})


mcp = None


def rpc(method, params):
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": time.time(), "method": method, "params": params}) + "\n")
    mcp.stdin.flush()
    return json.loads(mcp.stdout.readline())["result"]


def call_router(word, text):
    """One router tool call the way Claude Code makes it: the tool_use on stdout, then tools/call with its id in _meta."""
    global mcp
    if mcp is None:
        server = json.load(open(option("--mcp-config")))["mcpServers"]["router"]
        mcp = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                               env=dict(os.environ, **server.get("env", {})))
        rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake"}})
    tool_id = "toolu_%s_%d" % (word.lower(), time.time() * 1000)
    parent = "toolu_agent" if "SUB" in word else None
    if word != "GHOSTDONE":
        emit(type="assistant", parent_tool_use_id=parent, message={"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": "mcp__router__report_done", "input": {"text": text}}]})
    params = {"name": "report_done", "arguments": {"text": text}}
    if not word.startswith("OLD"):
        params["_meta"] = {"claudecode/toolUseId": tool_id}
    answer = rpc("tools/call", params)
    record("tool_result", tool_use_id=tool_id, is_error=answer["isError"], text=answer["content"][0]["text"])
    emit(type="user", parent_tool_use_id=parent, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": answer["content"], "is_error": answer["isError"]}]})


queue = []
interrupted = False


buffered = b""


def pump(timeout):
    """Read stdin for up to timeout seconds. Returns False on EOF."""
    global interrupted, buffered
    if b"\n" not in buffered:
        ready, _, _ = select.select([0], [], [], timeout)
        if not ready:
            return True
        chunk = os.read(0, 65536)
        if not chunk:
            return False
        buffered += chunk
    if b"\n" not in buffered:
        return True
    line, buffered = buffered.split(b"\n", 1)
    msg = json.loads(line)
    if msg.get("type") == "control_request":
        interrupted = True
        emit(type="control_response", response={"subtype": "success", "request_id": msg["request_id"]})
    else:
        record("queue-operation", operation="enqueue", content=msg["message"]["content"])
        queue.append(msg)
    return True


def wait(seconds):
    global interrupted
    end = time.time() + seconds
    while time.time() < end:
        if not pump(0.05):
            sys.exit(0)
        if interrupted:
            interrupted = False
            return False
    return True


def run(msg):
    text = msg["message"]["content"]
    record("user", uuid=msg.get("uuid"), message=msg["message"])
    if "SILENTHANG" in text:
        while pump(1):
            pass
        sys.exit(0)
    emit(type="user", uuid=msg.get("uuid"), isReplay=True, message=msg["message"])
    words = text.split()
    for word in ("ALWAYSFAIL", "ALWAYSCRASH"):
        if word in words:
            open(path + "." + word, "w").close()
    if os.path.exists(path + ".ALWAYSFAIL"):
        words = ["FAIL"]
    if os.path.exists(path + ".ALWAYSCRASH"):
        words = ["CRASH"]
    for index, word in enumerate(words):
        if word in ("SLEEP", "TOOLSLEEP"):
            seconds = float(words[index + 1])
            if word == "TOOLSLEEP":
                emit(type="assistant", message={"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tool-%d" % time.time(), "name": "Bash", "input": {"command": "sleep"}}]})
            if not wait(seconds):
                result(None, True, "error_during_execution")
                return
            if word == "TOOLSLEEP":
                emit(type="user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]})
        elif word in ("DONE", "SUBDONE", "GHOSTDONE", "OLDDONE", "OLDSUBDONE"):
            call_router(word, words[index + 1])
        elif word == "FAIL":
            result("API Error: 503", True)
            return
        elif word == "CRASH":
            os._exit(3)
        elif word == "HANG":
            while pump(1):
                pass
            sys.exit(0)
        elif word == "RETRY":
            for attempt in range(int(words[index + 1])):
                emit(type="system", subtype="api_retry", attempt=attempt + 1, max_retries=10)
        elif word == "ECHO":
            reply = " ".join(words[index + 1:])
            emit(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": reply}]})
            result(reply)
            return
    emit(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": "done"}]})
    result("done")


emit(type="system", subtype="init", model=model, tools=[], skills=[])
while True:
    if not queue:
        if not pump(0.1):
            break
        continue
    run(queue.pop(0))
