"""Private per-home MCP launcher. Copied beside mcp.json; uses only the standard library.

Hermes freezes MCP command/args/env at session creation. Keep that declaration
stable and resolve our rotating service token only when the MCP process starts.
"""
import json
import os
from pathlib import Path
import sys


def main():
    try:
        servers = json.loads(Path(__file__).with_name("mcp.json").read_text())["mcpServers"]
        server = servers[sys.argv[1]]
        env = dict(os.environ, **server.get("env", {}))
        os.execvpe(server["command"], [server["command"], *server.get("args", [])], env)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        # Transport definitions and OS diagnostics may contain credentials.
        print("Hermes MCP launcher failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
