"""Stdio MCP server that forwards to the running service. Standard library only: it starts with python -I."""
import json
import os
import socket
import sys

SOCK = os.environ["SSHARNESS_SOCK"]
TOKEN = os.environ["SSHARNESS_TOKEN"]


def call(method, **params):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(SOCK)
        sock.sendall((json.dumps({"method": method, "token": TOKEN, "params": params}) + "\n").encode())
        reader = sock.makefile("r", encoding="utf-8")
        return json.loads(reader.readline())


def reply(msg_id, payload):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": payload}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(msg_id, {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                       "serverInfo": {"name": "super-super-harness", "version": "0"}})
    elif method == "tools/list":
        reply(msg_id, {"tools": call("tools").get("result", [])})
    elif method == "tools/call":
        # Claude Code names the tool_use behind each call; the service matches it against the Worker's stream
        # to tell the Worker's own calls from its sub-agents'.
        tool_use_id = (msg["params"].get("_meta") or {}).get("claudecode/toolUseId")
        try:
            answer = call("tool", name=msg["params"]["name"], args=msg["params"].get("arguments") or {}, tool_use_id=tool_use_id)
        except OSError as exc:
            answer = {"error": "service unreachable: %s" % exc}
        failed = "error" in answer
        text = answer.get("error") if failed else json.dumps(answer.get("result"), ensure_ascii=False)
        reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": failed})
    elif msg_id is not None:
        reply(msg_id, {})


for line in sys.stdin:
    handle(json.loads(line))
