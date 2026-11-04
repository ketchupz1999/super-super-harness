"""Declared-provider defaults, not a promise of live model availability. See docs/setup.md#providers-and-presets."""
PRESETS = {
    "anthropic": {
        "base_url": "https://api.anthropic.com", "auth_env": "ANTHROPIC_API_KEY",
        "key_env": "ANTHROPIC_API_KEY", "models": {"claude-opus-5-5": 1000000},
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic", "auth_env": "ANTHROPIC_AUTH_TOKEN",
        "key_env": "DEEPSEEK_API_KEY", "models": {"deepseek-v4-pro": 1048576, "deepseek-flash": 1048576},
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/anthropic", "auth_env": "ANTHROPIC_AUTH_TOKEN",
        "key_env": "GLM_API_KEY", "models": {"glm-5.3": 1000000, "glm-5.3-flash": 1000000},
    },
    "stepplan": {
        "base_url": "https://api.stepfun.com/step_plan", "auth_env": "ANTHROPIC_AUTH_TOKEN",
        "key_env": "STEP_API_KEY", "models": {"step-5-preview": 1000000, "step-3.7-flash": 256000},
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/anthropic", "auth_env": "ANTHROPIC_AUTH_TOKEN",
        "key_env": "MOONSHOT_API_KEY", "models": {"kimi-k3.1-pro": 1048576},
    },
}

# No Anthropic-compatible official endpoint. Do not turn this into a proxy preset.
PLANNED = {"gpt": "under construction / 建设中: waiting for a Codex Harness"}
AUTH_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def declared_providers(raw):
    """User declarations override defaults, including model windows; unknown names are allowed."""
    result = {name: dict(item, models=dict(item["models"])) for name, item in PRESETS.items()}
    for name, item in raw.items():
        if name in PLANNED:
            raise ValueError(PLANNED[name])
        base = result.get(name, {})
        # An explicit file must not accidentally inherit a built-in environment key.
        if "key_file" in item and "key_env" not in item:
            base["key_env"] = ""
        result[name] = dict(base, **item)
        result[name]["models"] = dict(base.get("models", {}), **item.get("models", {}))
    return result
