"""Harness protocol and registry. Upper layers select an adapter through a Profile."""
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class HarnessProcess(Protocol):
    pid: int | None
    closing: bool
    exited: object                 # asyncio.Event
    on_exit: Callable

    async def start(self) -> int | None: ...
    def send(self, text: str, uuid: str | None = None) -> str | None: ...
    def interrupt(self) -> bool: ...
    async def close(self, wait: float = 5) -> None: ...


@runtime_checkable
class Harness(Protocol):
    """Synchronous setup / reads run in a thread; process callbacks run on the event loop.

    Adapters normalize their own wire format into the v1 events documented in the
    "Harness protocol" section of docs/design.md. Process is a factory, not necessarily an OS child.
    """
    def build_env(self, cfg, profile, tmpdir=None) -> dict: ...
    def headless_argv(self, cfg, profile, session_id, resume, mcp_config, system_prompt) -> list: ...
    def interactive_argv(self, cfg, profile, session_id, mcp_config, system_prompt, fork=False, hooks=None) -> list: ...
    def prepare_session(self, cfg, session_id) -> tuple[str, bool]: ...
    def session_has(self, cfg, session_id, marker) -> bool: ...
    def session_file(self, cfg, session_id) -> Path | None: ...
    def transcript(self, cfg, session_id, *, max_bytes=None, limit=None, text_limit=None) -> list[dict]: ...
    def ensure_provider(self, provider) -> None: ...
    def shallow_probe(self, provider, timeout=5) -> bool: ...
    def deep_probe(self, provider, model, timeout=30) -> bool: ...
    def available(self, cfg, profile) -> bool: ...
    def Process(self, argv, env, cwd, on_event, on_exit, log_path=None) -> HarnessProcess: ...


_registry: dict[str, Harness] = {}


def register(name: str, implementation: Harness):
    """Register trusted application code (also usable by an embedding application)."""
    if not isinstance(implementation, Harness):
        raise TypeError("Harness %r does not implement the protocol" % name)
    _registry[name] = implementation


def get_harness(cfg, profile) -> Harness:
    """Resolve a Profile's harness; aliases may declare an implementation explicitly."""
    declaration = cfg.harnesses.get(profile.harness, {})
    name = declaration.get("implementation", profile.harness)
    if name == "claude-code" and name not in _registry:
        from . import claude_code
        register(name, claude_code)
    if name == "hermes" and name not in _registry:
        from . import hermes
        register(name, hermes)
    if name not in _registry:
        raise ValueError("unknown Harness %r; register an implementation first" % name)
    return _registry[name]


def session_harness(cfg, profile_name, saved_name=None):
    """Read via the session's original Harness, not a renamed/reconfigured Profile.

    Legacy tasks without a saved identity use their surviving Profile. If both
    are gone, callers fall back to application events rather than guess an adapter.
    """
    profile = SimpleNamespace(harness=saved_name) if saved_name else cfg.profiles.get(profile_name)
    if profile is None:
        return None
    try:
        return get_harness(cfg, profile)
    except ValueError:
        return None
