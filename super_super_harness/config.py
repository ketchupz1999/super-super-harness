"""Paths, providers and Profiles. Secrets stay in their existing key files."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .catalog import AUTH_ENVS, declared_providers

REPO_ROOT = Path(__file__).resolve().parent.parent
SEVERITIES = ("info", "warn", "critical")
PERMISSIONS = ("observe", "notify", "act")
ACTIONS = ("drop", "record", "direct", "wake")
RULE_CONDITIONS = ("source", "type_prefix", "thread_prefix", "severity", "keyword")
BUILTIN_SOURCES = {"worker": "act", "reminder": "notify"}
SOURCE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
TYPE_CHARS = re.compile(r"[a-z0-9._-]+")
THREAD_CHARS = re.compile(r"[A-Za-z0-9._:/#-]+")


class PublicConfigurationError(ValueError):
    """A fixed, actionable configuration diagnostic safe to show without secrets."""


def parse_routing(raw_sources, raw_rules):
    """[sources.<name>] permissions and ordered [[rules]]. Built-in sources are not configurable."""
    if not isinstance(raw_sources, dict) or not isinstance(raw_rules, list):
        raise PublicConfigurationError("[sources.<name>] must be tables and [[rules]] an array of tables. Edit the private profiles.toml.")
    sources = {}
    for name, item in raw_sources.items():
        if not SOURCE_NAME.fullmatch(name) or name in BUILTIN_SOURCES:
            raise PublicConfigurationError("Source names must be lowercase letters, digits, '-' or '_', and not worker or reminder. Edit [sources] in the private profiles.toml.")
        if not isinstance(item, dict) or set(item) - {"permission"} or item.get("permission", "observe") not in PERMISSIONS:
            raise PublicConfigurationError("[sources.%s] accepts only permission = observe, notify or act. Edit the private profiles.toml." % name)
        sources[name] = item.get("permission", "observe")
    rules = []
    for number, item in enumerate(raw_rules, 1):
        if (not isinstance(item, dict) or item.get("action") not in ACTIONS or set(item) - {"action", *RULE_CONDITIONS}
                or any(not isinstance(item[key], str) or not item[key] for key in RULE_CONDITIONS if key in item)
                or item.get("severity", "info") not in SEVERITIES):
            raise PublicConfigurationError("[[rules]] #%d needs action = drop, record, direct or wake, and only source, type_prefix, "
                                           "thread_prefix, severity (info, warn, critical) or keyword as nonempty strings. "
                                           "Edit the private profiles.toml." % number)
        # Name only the rule and field: the value is user text.
        if "source" in item and item["source"] not in sources and item["source"] not in BUILTIN_SOURCES:
            raise PublicConfigurationError("[[rules]] #%d: source is not declared in [sources.<name>] (or worker, reminder). "
                                           "Edit the private profiles.toml." % number)
        for key, chars in (("type_prefix", TYPE_CHARS), ("thread_prefix", THREAD_CHARS)):
            if key in item and not chars.fullmatch(item[key]):
                raise PublicConfigurationError("[[rules]] #%d: %s uses characters no event can contain, so it would never match. "
                                               "Edit the private profiles.toml." % (number, key))
        if item.get("severity") == "critical" and item["action"] == "drop":
            raise PublicConfigurationError("[[rules]] #%d: severity = critical cannot use action = drop; critical events are never dropped. "
                                           "Edit the private profiles.toml." % number)
        rules.append(dict(item))
    return sources, tuple(rules)


def expand(path):
    return Path(os.path.expanduser(str(path)))


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_file: Path | None = None
    small_model: str = ""
    ensure: str = ""
    auth_env: str = "ANTHROPIC_API_KEY"
    key_env: str = ""
    models: dict = field(default_factory=dict)

    def key(self):
        key = os.environ.get(self.key_env, "").strip() if self.key_env else ""
        if not key and self.key_file:
            try:
                key = self.key_file.read_text().strip()
            except OSError:
                raise ValueError("provider %s credential file is unavailable" % self.name) from None
        if not key or any(c in key for c in "\r\n"):
            raise ValueError("provider %s credential is missing or invalid; run supersuperharness init" % self.name)
        return key

    def auth_headers(self):
        key = self.key()
        return {"x-api-key": key} if self.auth_env == "ANTHROPIC_API_KEY" else {"Authorization": "Bearer " + key}


@dataclass(frozen=True)
class Profile:
    name: str
    harness: str
    provider: str
    model: str
    settings: str = "project"
    add_dirs: tuple = ()
    mcp: tuple = ("router",)
    tools: str = "all"
    context_window: int = 0
    env: dict = field(default_factory=dict)
    sandbox: str = "none"
    location: str = "local"

    def __post_init__(self):
        if self.sandbox != "none":
            raise PublicConfigurationError("Profile sandbox must be 'none'; sandboxing is not implemented. Edit the private profiles.toml.")
        if self.location != "local":
            raise PublicConfigurationError("Profile location must be 'local'; remote execution is not implemented. Edit the private profiles.toml.")


@dataclass
class Paths:
    """Owner control needs only private paths, not Provider/Profile parsing."""
    home: Path

    @property
    def db(self):
        return self.home / "router.db"

    @property
    def sock(self):
        return self.home / "router.sock"

    @property
    def owner_token_file(self):
        return self.home / "owner-token"


@dataclass
class Config(Paths):
    profiles: dict
    providers: dict
    harnesses: dict
    default_profile: str
    primary: str
    backup: str
    limits: dict
    failover: dict
    session_root: Path
    mcp_servers: dict = field(default_factory=dict)
    source_repo: Path = None
    web: dict = field(default_factory=lambda: {"port": 8765, "user_name": "You", "user_avatar": "", "router_name": "Router"})
    sources: dict = field(default_factory=dict)
    rules: tuple = ()

    @property
    def workspaces(self):
        return self.home / "workspaces"

    @property
    def router_dir(self):
        return self.home / "router"

    @property
    def run_dir(self):
        return self.home / "run"

    def profile(self, name):
        if name not in self.profiles:
            raise ValueError("unknown profile %r, have %s" % (name, sorted(self.profiles)))
        return self.profiles[name]


def home_path(home=None):
    return expand(home or os.environ.get("SSHARNESS_HOME") or "~/.super-super-harness").absolute()


def load(home=None):
    home = home_path(home)
    path = home / "profiles.toml"
    if not path.exists():
        if (home / "router.db").exists():
            raise ValueError("existing installation has no private profiles.toml; migrate the old active configuration with init --migrate-from before upgrading")
        path = REPO_ROOT / "profiles.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        # A TOML parser diagnostic may contain a line with a credential. Never echo it.
        raise ValueError("invalid profiles.toml; check TOML syntax without sharing secrets") from None
    return from_raw(raw, home)


def from_raw(raw, home):
    providers = {}
    for name, item in declared_providers(raw.get("providers", {})).items():
        base_url = item.get("base_url", "").rstrip("/")
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("provider %s needs an http(s) base_url without credentials, query or fragment" % name)
        auth_env = item.get("auth_env", "ANTHROPIC_API_KEY")
        if auth_env not in AUTH_ENVS:
            raise ValueError("provider %s auth_env must be one of %s" % (name, AUTH_ENVS))
        key_file = expand(item["key_file"]) if item.get("key_file") else None
        if key_file and not key_file.is_absolute():
            key_file = home / key_file
        models = item.get("models", {})
        if not isinstance(models, dict) or any(type(n) is not int or n <= 0 for n in models.values()):
            raise ValueError("provider %s models must map model IDs to positive context windows" % name)
        providers[name] = Provider(name, base_url, key_file, item.get("small_model", ""), item.get("ensure", ""),
                                   auth_env, item.get("key_env", ""), models)
    profiles = {}
    for name, item in raw["profiles"].items():
        item = dict(item)
        if item["provider"] not in providers:
            raise ValueError("profile %s uses an undeclared provider" % name)
        item.setdefault("context_window", providers[item["provider"]].models.get(item["model"].replace("[1m]", ""), 0))
        if type(item["context_window"]) is not int or item["context_window"] < 0:
            raise ValueError("profile %s context_window must be a non-negative integer" % name)
        if any(key in item.get("env", {}) for key in (*AUTH_ENVS, "ANTHROPIC_BASE_URL")):
            raise ValueError("put endpoint and credential settings in providers, not profile.env")
        item["add_dirs"] = tuple(os.path.expanduser(p) for p in item.get("add_dirs", ()))
        item["mcp"] = tuple(item.get("mcp", ("router",)))
        profiles[name] = Profile(name=name, **item)
    for name in (raw["default_profile"], raw["router"]["primary"], raw["router"]["backup"]):
        if name not in profiles:
            raise ValueError("default and Router Profiles must exist")
    if profiles[raw["router"]["primary"]].harness != profiles[raw["router"]["backup"]].harness:
        raise ValueError("Router primary and backup must use the same Harness")
    limits = {"max_workers": 10, "busy_interrupt_seconds": 10, "stall_minutes": 30, "worker_retries": 3}
    limits.update(raw.get("limits", {}))
    failover = {"silent_seconds": 20, "deep_probe_seconds": 120, "retries": 5, "probe_timeout": 5,
                "wake_grace_seconds": 90, "primary_check_seconds": 300}
    failover.update(raw.get("failover", {}))
    web = {"port": 8765, "user_name": "You", "user_avatar": "", "router_name": "Router"}
    web.update(raw.get("web", {}))
    if (not isinstance(web["router_name"], str) or not web["router_name"].strip() or
            len(web["router_name"]) > 80 or any(ord(c) < 32 for c in web["router_name"])):
        raise ValueError("web.router_name must be a nonempty name of at most 80 characters")
    if type(web["port"]) is not int or not 0 <= web["port"] <= 65535:
        raise ValueError("web.port must be between 0 and 65535")
    sources, rules = parse_routing(raw.get("sources", {}), raw.get("rules", []))
    return Config(home=home, web=web, sources=sources, rules=rules, profiles=profiles, providers=providers, harnesses=dict({"claude-code": {"binary": "claude"}}, **raw.get("harness", {})),
                  default_profile=raw["default_profile"], primary=raw["router"]["primary"],
                  backup=raw["router"]["backup"], limits=limits, failover=failover,
                  session_root=expand(raw.get("session_root", "~/.claude/projects")),
                  mcp_servers=raw.get("mcp", {}), source_repo=expand(raw["source_repo"]) if "source_repo" in raw else None)
