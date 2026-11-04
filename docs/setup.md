# Setup

Install, configure models, upgrade. Daily use is in [guide.md](guide.md); internals in [design.md](design.md).

## Requirements

| Need | Why |
|---|---|
| macOS | Supported host. Elsewhere, `caffeinate` and macOS notifications are skipped; nothing else is tested. |
| Python 3.11+ | Standard library only. No packages, no build step. |
| Git | Each Worker works in a git worktree. |
| [Claude Code](https://claude.com/claude-code) | Default Harness. `claude` on `PATH`, or `init --binary PATH`. An experimental Hermes Worker adapter is described [below](#hermes-experimental-workers). |
| A model key | Any Anthropic-compatible endpoint (see [Providers](#providers)). |

## Quick start

```sh
git clone https://github.com/ketchupz1999/super-super-harness.git && cd super-super-harness
bin/supersuperharness init            # pick a Provider and model, type the key (hidden)
bin/supersuperharness doctor          # checks setup; makes one tiny, possibly billable call
bin/supersuperharness serve           # foreground; keep this terminal open
bin/supersuperharness web --open      # in another terminal
```

`serve` refuses to run without a private config (run `init` first) and refuses a second copy on the same home. On macOS it keeps the machine awake on AC power while running; set `SSHARNESS_NO_CAFFEINATE=1` to skip.

## `init`

Writes the private config. It never starts the service or calls the network.

**Interactive:** asks for a Provider (`anthropic`, `deepseek`, `glm`, `stepplan`, `kimi` or a custom name) and the key. A custom Provider also asks for base URL, auth variable, model and context window. On a fresh home the first Profile (`default`) becomes the Router primary, Router backup and default Worker.

**Non-interactive:**

```sh
# key from an environment variable; only the variable NAME is stored
bin/supersuperharness init --provider deepseek --key-env DEEPSEEK_API_KEY

# add another Profile
bin/supersuperharness init --profile fast-worker --provider deepseek --model deepseek-flash --key-env DEEPSEEK_API_KEY

# custom Provider: give all five
bin/supersuperharness init --provider my-gw --base-url http://127.0.0.1:9000 \
  --auth-env ANTHROPIC_AUTH_TOKEN --key-env MY_KEY --model my-model --context-window 200000
```

| Flag | Meaning |
|---|---|
| `--provider` | Preset or new custom name |
| `--profile` | New Profile name (existing names are refused, never overwritten) |
| `--base-url`, `--auth-env`, `--model`, `--context-window` | For custom Providers; presets fill them in |
| `--key-env` | Environment variable holding the key |
| `--binary` | Claude Code executable |

`init` refuses a home inside a git checkout, a symlinked config, and changes to a Provider that existing Profiles already use.

## Keys

Keys are never CLI arguments, TOML values or log output.

| Source | How |
|---|---|
| Key file | Interactive `init` writes `SSHARNESS_HOME/keys/<provider>-<hex>` (0600). |
| Environment variable | `--key-env NAME`. The terminal running `serve`, and any terminal running `attach` / `open` / `peek`, must have it. |

If both are set, the environment variable wins. `auth_env` decides the header Claude Code sends: `ANTHROPIC_API_KEY` → `x-api-key`, `ANTHROPIC_AUTH_TOKEN` → `Authorization: Bearer`.

## Files

`SSHARNESS_HOME` defaults to `~/.super-super-harness` (mode 0700). Keep it outside any git checkout.

| Path | What |
|---|---|
| `profiles.toml` | Your config (0600). The only one the service reads. |
| `keys/` | Key files (0600) |
| `router.db` | SQLite: conversation, Workers, events, items, reminders |
| `router.sock`, `owner-token`, `web-session-key` | Local control socket and its secrets (0600) |
| `router/` | The Router's directory: `soul.md`, `memory.md` |
| `workspaces/<id>-<code_name>/` | One per Worker: `notes.md` (durable), `brief.md`, `tmp/`, `repo/` (git worktree) |
| `items/<code>/notes.md` | Item notes |
| `run/` | Per-launch MCP configs and sanitized logs |
| `avatar/`, `characters/` | Optional private artwork (below) |
| `releases/`, `current`, `service.log` | Managed installs (see [Upgrade](#upgrade-and-rollback)) |

## Config reference

`init` writes quoted TOML (`["web"]`, `"port" = 8765`). Edit the existing `["web"]` table in place. Do not append a second `[web]` or `["web"]` table: **a table declared twice makes the whole file invalid**. Add missing keys inside the existing table:

```toml
["web"]
"port" = 8765                      # 127.0.0.1 only; 0 disables the web
"router_name" = "Local Assistant"  # default "Router"
"user_name" = "You"
"user_avatar" = ""                 # optional png/jpg/gif/webp, ≤ 2 MiB
```

A complete file written by hand:

```toml
default_profile = "worker"        # Worker Profile when the Router picks none
source_repo = "~/src/super-super-harness"   # optional, default for `upgrade`

[router]
primary = "router"
backup = "worker"                 # same Harness as primary

[providers.my-provider]
base_url = "https://example.com/anthropic"   # without /v1/messages
auth_env = "ANTHROPIC_AUTH_TOKEN"
key_env = "MY_PROVIDER_KEY"                  # or key_file = "keys/..."
# small_model = "..."   used as Claude Code's Haiku-class model
# ensure = "~/bin/start-gateway"   local command run before launches (no shell, 30 s)

[providers.my-provider.models]
"router-model" = 1000000          # model → context window
"worker-model" = 256000

[profiles.router]
harness = "claude-code"
provider = "my-provider"
model = "router-model"

[profiles.worker]
harness = "claude-code"
provider = "my-provider"
model = "worker-model"
```

**Profile keys:** `harness`, `provider`, `model` (required); `context_window`, `settings` (Claude Code `--setting-sources`, default `project`), `add_dirs`, `mcp` (extra `[mcp.<name>]` servers), `tools`, `env` (non-secret only).

**Other tables:**

| Table | Keys (default) |
|---|---|
| `[limits]` | `max_workers` (10), `stall_minutes` (30), `worker_retries` (3 per assignment), `busy_interrupt_seconds` (10), `dormant_days` (7, positive integer) |
| `[failover]` | `silent_seconds` (20), `deep_probe_seconds` (120), `retries` (5), `probe_timeout` (5), `wake_grace_seconds` (90), `primary_check_seconds` (300) |
| `["web"]` | `port` (8765), `router_name` ("Router"), `user_name` ("You"), `user_avatar` ("") |
| `[harness.claude-code]` | `binary` ("claude") |
| `[sources.<name>]`, `[[rules]]` | Event sources and routing, see [design.md](design.md#event-bus) |

Restart the service after editing.

## Providers

Presets, all using the Claude Code Harness. Endpoints were checked against each Provider's docs (2026-11-02), **not tested with real keys**.

| Provider | Base URL | Auth | Key variable | Models (context) |
|---|---|---|---|---|
| `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` (1M) |
| `deepseek` | `https://api.deepseek.com/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `DEEPSEEK_API_KEY` | `deepseek-v4-pro`, `deepseek-flash` (1M) |
| `glm` | `https://open.bigmodel.cn/api/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `GLM_API_KEY` | `glm-5.3`, `glm-5.3-flash` (1M) |
| `stepplan` | `https://api.stepfun.com/step_plan` | `ANTHROPIC_AUTH_TOKEN` | `STEP_API_KEY` | `step-5-preview` (1M), `step-3.7-flash` (256K) |
| `kimi` | `https://api.moonshot.cn/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `MOONSHOT_API_KEY` | `kimi-k3.1-pro` (1M) |

Your own `[providers.*]` fields override a preset field by field.

## Splitting models

A Profile = Harness + Provider + model. A common split: a strong model for the Router, a cheaper one as `default_profile` for Workers, and a second Provider as the Router backup.

```sh
bin/supersuperharness init --provider anthropic                                  # Router
bin/supersuperharness init --profile fast-worker --provider deepseek --model deepseek-flash --key-env DEEPSEEK_API_KEY
```

Then set `default_profile = "fast-worker"` and `backup = "fast-worker"` by hand. Adding a Profile never changes defaults by itself.

**Failover:** if the Router's model stops answering, the service switches to `backup` on the same session and tells you; while on backup it checks primary every few minutes and asks you before switching back. Primary and backup must share a Harness.

## `doctor`

```sh
bin/supersuperharness doctor                         # Router primary, backup, default Worker
bin/supersuperharness doctor --profile fast-worker --timeout 15
```

Checks per Profile: `harness` (executable found), `credential` (a key resolves), `key_file_private` (0600, not a symlink), `endpoint` (reachable), `model` (one tiny streamed request; **may cost money**). Prints JSON; failures carry a safe reason, never a key or URL. It is not an end-to-end test of Claude Code tool use.

## Hermes (experimental Workers)

→ **Does not work with a real Hermes yet. Do not use it for real work.** A first live check (real Hermes, fake local Provider) failed: Hermes did not offer the `mcp__router__*` tools to the model, so a Worker cannot report back; a cold start in a fresh private home also took about 55 s, longer than the adapter's 30 s handshake limit. Keep the Router and Workers on Claude Code. Use a separate clean Hermes source-tree installation with ACP dependencies; do not reuse a bot installation containing `.env`. No Hermes code is imported into this service.

Add to the private `profiles.toml` (use your existing Provider and model):

```toml
[harness.hermes]
binary = "/path/to/clean/hermes-agent/.venv/bin/hermes"

[profiles.hermes-worker]
harness = "hermes"
provider = "my-provider"
model = "worker-model"
```

| Operation | Behavior |
|---|---|
| `doctor --profile hermes-worker` | Checks executable isolation and runs `hermes acp --check` with a disposable home, then normal Provider probes. The model probe may cost money. |
| Worker launch/resume | Creates a private home below `SSHARNESS_HOME/hermes/`; credentials and current MCP token are not command-line arguments. Every launch rewrites configuration. |
| `open` | Resumes the mapped Hermes ID; preserves existing MCP configuration. `peek` and Router `attach` are explicitly unsupported. |
| Model changes | Same saved Provider only; startup explicitly selects the model. Changing Provider identity, endpoint or auth mode requires a new session. |
| Restrictions | `tools=all`, default `settings=project`, no `add_dirs`. Only identifiable source-tree executables with no installation `.env` and no managed configuration are accepted; the adapter never repairs or removes external config. |
| Updates | Generated config disables passive source/plugin update checks and plugin auto-apply, not an explicit user-requested update. |
| Verification | Fake-only: `python3 -m unittest tests.test_hermes tests.test_hermes_state tests.test_hermes_integration`. Opt-in real-process checks are in `tests/hermes_live.py`; read the script before enabling `SSHARNESS_HERMES_LIVE=1`. |

The live script configures **real Hermes with a fake loopback Provider**, a private OS `HOME` and no real API key. It is not an OS network sandbox; use an isolated host if all external traffic must be forbidden. It deliberately kills only tracked test descendants to exercise unknown-execution recovery; its push probe targets a throwaway `file://` repository. It records actual auth headers as booleans, system-prompt presence, model selection, terminal `TMPDIR`, startup time and cleanup results in the retained temporary home's `verification.json`. External Provider compatibility still needs a separately authorized check.

Protocol mapping, delayed receipts, MCP credential bridging and remaining limitations: [Hermes protocol](protocol.md#hermes-experimental). The adapter never opens your bot's `~/.hermes`; its source-checked launch isolation and clean-install prerequisite still require the opt-in real-process verification.

## Private artwork (optional)

- **Router:** put `<state>.png|webp|jpg|gif` in `SSHARNESS_HOME/avatar/` (`idle`, `thinking`, `working`, `done`, `error`, `waiting`, `rest`; ≤ 2 MiB). Otherwise the built-in mascot is used.
- **Workers:** put `<id>.json` + an atlas in `SSHARNESS_HOME/characters/` for ids `claude`, `chatgpt`, `codex`, `muse`; optional `harness-claude-code.png`. None ship with the project; without them Workers are a built-in robot. Manifest:

```json
{ "atlas": "claude-v1.png", "frameWidth": 256, "frameHeight": 256, "columns": 6, "rows": 4, "pixel": true,
  "animations": { "idle":     {"row": 0, "frames": 6, "frameDurationsMs": [1400,240,100,80,100,1100]},
                  "working":  {"row": 1, "frames": 6, "frameDurationsMs": [460,320,130,90,140,460]},
                  "waiting":  {"row": 2, "frames": 6, "frameDurationsMs": [500,320,220,100,220,600]},
                  "complete": {"row": 3, "frames": 6, "frameDurationsMs": [420,250,180,100,180,600]} } }
```

The atlas must be exactly `frameWidth × columns` by `frameHeight × rows`; a bad file is skipped. Changes are picked up without restart.

## Upgrade and rollback

Workers can edit the code, so a managed install only runs a tested copy of a commit.

```sh
bin/supersuperharness upgrade [--repo PATH] [--ref REF]   # REF defaults to main, PATH to source_repo
bin/supersuperharness rollback
```

`upgrade` extracts the commit into `releases/<sha>`, runs the release tests there with a temporary home, points `current` at it, restarts the service, and rolls back if the Router is not ready within 30 s. `rollback` switches to the previous release. Neither touches your config, keys or database. Database changes happen when the service starts.

**Worker identity schema break:** databases from before `tasks.code_name` are rejected. Use a new `SSHARNESS_HOME`; there is no automatic migration or old-release database compatibility.
