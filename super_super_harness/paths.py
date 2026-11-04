"""Stable paths to repository resources, independent of module layout."""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
MCP_SERVER = PACKAGE_DIR / "router" / "mcp_server.py"
