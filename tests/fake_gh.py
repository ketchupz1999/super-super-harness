#!/usr/bin/env python3
"""Offline gh fixture: consume HTTP responses from JSON, never contact GitHub.

FAKE_GH_SCENARIO names a JSON list of responses (status, headers, body, optional
stdout/stderr/returncode). FAKE_GH_CALLS is an append-only request log. Tests put
this executable and a Python symlink on an otherwise empty PATH.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    calls = Path(os.environ["FAKE_GH_CALLS"])
    previous = calls.read_text().splitlines() if calls.exists() else []
    args = sys.argv[1:]
    # Fail closed if the connector ever requests anything except notifications GET.
    valid = (len(args) >= 6 and args[:5] == ["api", "--hostname", "github.com", "--method", "GET"]
             and args[5].startswith("notifications?") and "--include" in args
             and all(arg == "--header" for arg in args[7::2]))
    entry = {"args": args, "valid": valid,
             "env": {key: os.environ.get(key) for key in
                     ("GH_PROMPT_DISABLED", "GH_DEBUG", "DEBUG", "https_proxy", "FAKE_GH_MARKER")}}
    # An independent connection proves the startup barrier was committed before gh ran.
    with sqlite3.connect("file:" + os.environ["FAKE_GH_DB"] + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM kv WHERE key='source:github:notifications'").fetchone()
        entry["state"] = json.loads(row[0]) if row else None
    with calls.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    if not valid:
        return 97
    responses = json.loads(Path(os.environ["FAKE_GH_SCENARIO"]).read_text())
    if len(previous) >= len(responses):
        return 98
    reply = responses[len(previous)]
    sys.stderr.write(reply.get("stderr", ""))
    if "stdout" in reply:
        sys.stdout.write(reply["stdout"])
    else:
        status = reply.get("status", 200)
        lines = ["HTTP/2.0 %d Test response" % status]
        lines.extend("%s: %s" % pair for pair in reply.get("headers", {}).items())
        body = reply.get("body", [])
        body = body if isinstance(body, str) else json.dumps(body)
        sys.stdout.write("\r\n".join(lines) + "\r\n\r\n" + body)
    return reply.get("returncode", 0)


if __name__ == "__main__":
    sys.exit(main())
