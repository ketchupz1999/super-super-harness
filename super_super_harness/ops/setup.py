"""Offline private configuration, explicit legacy migration, and bounded diagnostics."""
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import tomllib
import warnings

from . import config
from ..adapters import get_harness
from .catalog import AUTH_ENVS, PLANNED, declared_providers
from ..core.private_files import atomic_write, private_lock, read_private


NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class EmptyKeyError(ValueError):
    pass


def toml_text(raw):
    """Small TOML writer for the values supported by profiles.toml (no dependencies)."""
    def value(item):
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, (int, float)):
            return str(item)
        if isinstance(item, (list, tuple)):
            return "[" + ", ".join(value(i) for i in item) + "]"
        raise ValueError("unsupported TOML value")

    lines = []
    def table(item, path=()):
        if path:
            lines.extend(["", "[" + ".".join(json.dumps(k) for k in path) + "]"])
        for key, child in item.items():
            if not isinstance(child, dict):
                lines.append(json.dumps(key) + " = " + value(child))
        for key, child in item.items():
            if isinstance(child, dict):
                table(child, (*path, key))
    table(raw)
    return "# Private super-super-harness configuration. Do not put API keys here.\n" + "\n".join(lines) + "\n"


def read_raw(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except tomllib.TOMLDecodeError:
        raise ValueError("invalid configuration TOML (diagnostic suppressed to protect secrets)") from None


def migrate(home, source):
    """Run BEFORE replacing the old repository defaults. No service/DB/key access."""
    home = config.home_path(home)
    with private_lock(home / ".profiles.lock"):
        return _migrate(home, source)


def _migrate(home, source):
    source = config.expand(source).absolute()
    raw = read_raw(source)
    config.from_raw(raw, home)  # structural validation only; no credential reads or probes
    destination = home / "profiles.toml"
    data = source.read_bytes()
    if destination.exists() or destination.is_symlink():
        if read_private(destination) != data:
            raise ValueError("private profiles.toml already exists; migration will not overwrite it")
        return destination
    atomic_write(destination, data, replace=False)
    return destination


def initialize(home, args, *, ask=None, ask_key=None):
    home = config.home_path(home)
    if args.migrate_from:
        return migrate(home, args.migrate_from)
    with private_lock(home / ".profiles.lock"):
        return _initialize(home, args, ask or input, ask_key or getpass.getpass)


def _initialize(home, args, ask, ask_key):
    destination = home / "profiles.toml"
    if destination.is_symlink():
        raise ValueError("private configuration must not be a symlink")
    existing = destination.exists()
    if not existing and (home / "router.db").exists():
        raise ValueError("existing installation needs init --migrate-from, not a fresh configuration")
    raw = read_raw(destination) if existing else read_raw(config.REPO_ROOT / "profiles.toml")
    profile_name = args.profile or ("default" if not existing else ask("New Profile name: ").strip())
    if not NAME.fullmatch(profile_name):
        raise ValueError("Profile names must contain only letters, digits, _ and -")
    if existing and profile_name in raw.get("profiles", {}):
        raise ValueError("Profile already exists; edit its private configuration instead of overwriting it")
    provider_name = args.provider or ask("Provider [anthropic/deepseek/glm/stepplan/kimi/custom] (anthropic): ").strip() or "anthropic"
    if not NAME.fullmatch(provider_name):
        raise ValueError("invalid Provider name")
    if provider_name in PLANNED:
        raise ValueError(PLANNED[provider_name])
    declared = declared_providers(raw.get("providers", {})).get(provider_name, {})
    base_url = args.base_url or declared.get("base_url") or ask("Anthropic-compatible base URL (without /v1/messages): ").strip()
    auth_env = args.auth_env or declared.get("auth_env") or ask("Authentication variable [ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY]: ").strip()
    if auth_env not in AUTH_ENVS:
        raise ValueError("choose ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY")
    models = declared.get("models", {})
    model = args.model or next(iter(models), "") or ask("Model ID: ").strip()
    if not model or any(c in model for c in "\r\n"):
        raise ValueError("a model ID is required")
    window = args.context_window if args.context_window is not None else models.get(model)
    if window is None:
        window = int(ask("Context window (tokens): "))
    if type(window) is not int or window <= 0:
        raise ValueError("context window must be a positive integer")
    if args.key_env and not ENV_NAME.fullmatch(args.key_env):
        raise ValueError("invalid credential environment variable name")
    shared = existing and any(p["provider"] == provider_name for p in raw.get("profiles", {}).values())
    if shared and (base_url != declared.get("base_url") or auth_env != declared.get("auth_env") or
                   (args.key_env and args.key_env != declared.get("key_env"))):
        raise ValueError("Provider is shared by existing Profiles; use a new Provider name or edit private configuration")
    declaration = dict(raw.get("providers", {}).get(provider_name, {}))
    declaration.update(base_url=base_url, auth_env=auth_env)
    if shared:
        pass  # Preserve credentials used by other Profiles; do not rotate a live key during init.
    elif args.key_env:
        declaration.update(key_env=args.key_env)
        declaration.pop("key_file", None)
    else:
        # Never take a secret as a CLI argument or a visible input() value.
        # Store in a new private file, so an interrupted config write cannot rotate a live key.
        key_path = home / "keys" / (provider_name + "-" + secrets.token_hex(8))
        declaration.update(key_file=str(key_path), key_env="")
    # Window overrides belong to the new Profile. Never mutate another Profile's inherited default.
    if model not in declared.get("models", {}):
        declaration["models"] = dict(declaration.get("models", {}), **{model: window})
    raw.setdefault("providers", {})[provider_name] = declaration
    raw.setdefault("profiles", {})[profile_name] = {"harness": "claude-code", "provider": provider_name,
                                                   "model": model, "context_window": window}
    if not existing:
        raw["profiles"] = {profile_name: raw["profiles"][profile_name]}
        raw["default_profile"] = profile_name
        raw["router"] = {"primary": profile_name, "backup": profile_name}
    if args.binary:
        raw.setdefault("harness", {}).setdefault("claude-code", {})["binary"] = args.binary
    config.from_raw(raw, home)  # validate endpoint/auth/window before asking for or writing a secret
    content = toml_text(raw)
    # Do not permit a private home inside a checkout, even with environment-only credentials.
    private_dirs = (home, key_path.parent) if not args.key_env and not shared else (home,)
    for directory in private_dirs:
        actual_directory = directory.resolve()
        if any((p / ".git").exists() for p in (actual_directory, *actual_directory.parents)):
            raise config.PublicConfigurationError("Choose an SSHARNESS_HOME and keys directory outside a git checkout, including for environment-only credentials.")
    if not args.key_env and not shared:
        with warnings.catch_warnings():
            # Refuse getpass's visible-input fallback when no terminal is available.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = ask_key("API Key (hidden; stored only in SSHARNESS_HOME/keys): ").strip()
        if not key:
            raise EmptyKeyError("API Key is empty")
        if any(c in key for c in "\r\n"):
            raise ValueError("API Key is invalid")
        atomic_write(key_path, key + "\n")
    atomic_write(destination, content, replace=existing)
    return destination


def doctor(cfg, names=None, timeout=5):
    """No service startup and no DB access. Deep probes make a tiny billable model call."""
    results = []
    names = names or list(dict.fromkeys((cfg.primary, cfg.backup, cfg.default_profile)))
    for name in names:
        checks, reasons = {}, {}
        failure = "the Profile or its Provider is not usable; details are suppressed to protect secrets"
        try:
            profile = cfg.profile(name)
            harness = get_harness(cfg, profile)
            provider = cfg.providers[profile.provider]
            checks["harness"] = bool(harness.available(cfg, profile))
            if not checks["harness"]:
                reasons["harness"] = "the Harness binary was not found"
            elif hasattr(harness, "installation_check"):
                checks["harness_setup"], reason = harness.installation_check(cfg, profile, timeout)
                if not checks["harness_setup"]:
                    reasons["harness_setup"] = reason
            try:
                provider.key()
                checks["credential"] = True
            except (OSError, ValueError):
                checks["credential"] = False
                reasons["credential"] = "no API Key: its environment variable is empty and its key file is missing or unreadable"
            # Existing user key files are inspected, not chmod'ed behind the user's back.
            checks["key_file_private"] = not provider.key_file or bool(os.environ.get(provider.key_env, "")) or (
                provider.key_file.is_file() and not provider.key_file.is_symlink() and provider.key_file.stat().st_mode & 0o777 == 0o600)
            if not checks["key_file_private"]:
                reasons["key_file_private"] = "the key file must be a regular file, not a symlink, with mode 0600"
            failure = "the Provider's preparation command (ensure) failed"
            harness.ensure_provider(provider)
            failure = "the probe failed; details are suppressed to protect secrets"
            checks["endpoint"] = bool(harness.shallow_probe(provider, timeout))
            if not checks["endpoint"]:
                reasons["endpoint"] = ("the Provider's base_url gave no HTTP answer: connection refused, name not found, "
                                       "not HTTP, or nothing within %g seconds" % timeout)
            if not (checks["credential"] and checks["endpoint"]):
                checks["model"], reason = False, "not tried: it needs a credential and an endpoint that answers"
            elif hasattr(harness, "deep_check"):
                checks["model"], reason = harness.deep_check(provider, profile.model, timeout)
            else:
                checks["model"], reason = bool(harness.deep_probe(provider, profile.model, timeout)), "the model call failed"
            if not checks["model"]:
                reasons["model"] = reason
        except (OSError, ValueError, KeyError):
            # Never print exceptions: HTTP bodies and command diagnostics can contain secrets.
            checks["configuration"] = False
            reasons["configuration"] = failure
        result = {"profile": name, "ok": bool(checks) and all(checks.values()), "checks": checks}
        if reasons:
            result["reasons"] = reasons   # safe to show: no keys, URLs or upstream messages
        results.append(result)
    return results
