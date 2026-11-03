# Installation and configuration

This is the reference for installing Super Super Harness, writing its private configuration, choosing Providers and models, and upgrading. Daily use is in [guide.md](guide.md); architecture and protocols are in [design.md](design.md).

Everything here runs from a checkout through `bin/supersuperharness`. The launcher runs `python3 -P -m super_super_harness.cli`, so a service started from inside a repository never imports the working tree by accident.

## Contents

1. [Requirements](#requirements)
2. [Rename boundary](#rename-boundary)
3. [Quick start](#quick-start)
4. [`init`](#init)
5. [Credentials](#credentials)
6. [Files under `SSHARNESS_HOME`](#files-under-ssharness_home)
7. [Editing the private file and the `["web"]` table](#editing-the-private-file-and-the-web-table)
8. [Configuration reference](#configuration-reference)
9. [Providers and presets](#providers-and-presets)
10. [Profiles and model split](#profiles-and-model-split)
11. [Router primary and backup](#router-primary-and-backup)
12. [`doctor`](#doctor)
13. [Agent-assisted setup](#agent-assisted-setup)
14. [Upgrade migration](#upgrade-migration)
15. [Upgrade and rollback of a running install](#upgrade-and-rollback-of-a-running-install)
16. [Atomic writes and secret recovery](#atomic-writes-and-secret-recovery)

## Requirements

| Requirement | Why |
|---|---|
| macOS | The supported host. `serve` keeps the machine awake with `caffeinate`, and `notify` reminders use `osascript`. On other platforms both are skipped; nothing else is tested there. |
| Python 3.11+ | `tomllib` and the standard library only. No third-party packages, no npm build. |
| Git | Worker workspaces are git worktrees; `upgrade` uses `git archive`; the web diff view runs `git diff`. |
| [Claude Code](https://claude.com/claude-code) | The only implemented Harness. The executable must be on `PATH`, or set with `init --binary` / `[harness.claude-code].binary`. |
| A model credential | An API key for an Anthropic-compatible endpoint (see [Providers and presets](#providers-and-presets)). |

## Rename boundary

The default home is `~/.super-super-harness`, and only `SSHARNESS_*` application environment variables are recognized. There is no automatic import of the pre-rename home, credentials, database, workspaces or sessions, and no old-name command, package or environment alias.

- Switching an existing pre-rename local instance is separate work that needs explicit authorization and its own plan, including startup and rollback paths.
- The pre-rename upgrade launcher expects an executable that no longer ships. Do not use it to switch directly to this release.
- `init --migrate-from` copies configuration only. It does not translate environment names or move databases, sockets, tokens, workspaces or sessions.

→ **Existing installation that ran from a repository-local `profiles.toml`? Do the [upgrade migration](#upgrade-migration) before upgrading.** Public defaults deliberately do not contain your old Profiles.

## Quick start

```sh
# Python 3.11+ and Claude Code must already be installed.
# init and doctor do not start a service.
bin/supersuperharness init
bin/supersuperharness doctor          # makes one tiny, possibly billable model call
# Only after doctor succeeds:
bin/supersuperharness serve           # foreground; keep this terminal open
bin/supersuperharness web --open      # in another terminal
```

`serve` refuses to start without a private `SSHARNESS_HOME/profiles.toml`. It prints `run supersuperharness init` (or `init --migrate-from ...` when a database already exists). A second `serve` against the same home refuses with `already running as <pid>`; the lock is `SSHARNESS_HOME/service.pid`.

On macOS, `serve` runs `caffeinate -s -w <pid>`: the machine stays awake only on AC power, only while the service runs, and no system setting is changed. Set `SSHARNESS_NO_CAFFEINATE=1` to skip it (tests always do).

## `init`

`init` writes the private configuration. It never starts a service, reads the database or makes a network call.

### Interactive

```sh
bin/supersuperharness init
```

It asks for a Provider (`anthropic/deepseek/glm/stepplan/kimi/custom`, default `anthropic`) and a hidden API key. Known Providers fill in endpoint, auth mode, first model and context window. A custom Provider name asks for:

- Anthropic-compatible base URL, without `/v1/messages`
- authentication variable: `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`
- model ID
- context window in tokens

On a fresh home the first Profile is named `default` (or `--profile NAME`) and becomes the default Worker, Router primary and Router backup at once. When a private file already exists, interactive `init` asks for a **new** Profile name.

The key prompt uses `getpass`. If no terminal is available, `init` refuses rather than fall back to visible input, and prints how to use the non-interactive form. An empty key is refused and nothing is installed.

### Non-interactive flags

| Flag | Meaning |
|---|---|
| `--provider NAME` | Known preset or a new custom name (letters, digits, `_`, `-`). `gpt` is refused: under construction. |
| `--profile NAME` | Name of the new Profile. An existing Profile name is refused; Profiles are never overwritten. |
| `--base-url URL` | Custom Provider endpoint. |
| `--auth-env ANTHROPIC_API_KEY\|ANTHROPIC_AUTH_TOKEN` | Outgoing authentication mode. |
| `--key-env ENV_NAME` | Read the credential from this environment variable. Records the **name** only; no key prompt. |
| `--model ID` | Model ID; defaults to the Provider's first declared model. |
| `--context-window N` | Positive integer; defaults to the declared window of that model. |
| `--binary PATH` | Claude Code executable, stored as `[harness.claude-code].binary`. |
| `--migrate-from PATH` | Copy an old `profiles.toml` byte-for-byte. Ignores all other flags. See [Upgrade migration](#upgrade-migration). |

```sh
# Environment-only credentials, no prompt at all:
bin/supersuperharness init --provider deepseek --key-env DEEPSEEK_API_KEY

# Add a second Profile to an existing configuration:
bin/supersuperharness init --profile fast-worker --provider deepseek --model deepseek-flash --key-env DEEPSEEK_API_KEY
```

Keys are **never** CLI arguments, TOML values or diagnostic output.

### What `init` refuses or preserves

- `SSHARNESS_HOME`, or the keys directory it would write to, inside a git checkout, including symlinked homes or keys directories. This applies to environment-only credentials too.
- A private `profiles.toml` that is a symlink.
- A fresh configuration when `router.db` exists but `profiles.toml` does not: use `--migrate-from`.
- An existing Profile name.
- A Provider already used by existing Profiles: `init` reuses its credential, and refuses any change to its endpoint, auth mode or key source. Use a new Provider name, or edit the private file deliberately.
- A new Profile's window override is stored on the new Profile and never changes a shared model default inherited by older Profiles.

`init` may reformat the TOML (it writes quoted keys, see below) but keeps every existing setting and Profile. Init and migration serialize under a private 0600 lock file `SSHARNESS_HOME/.profiles.lock`, and new configuration is installed atomically without overwriting a concurrently created destination. Keep a private backup before manual edits.

## Credentials

There are two sources. Choose `SSHARNESS_HOME` outside any checkout for both.

| Source | How | Notes |
|---|---|---|
| Key file | Interactive `init` writes `SSHARNESS_HOME/keys/<provider>-<16 hex>` with mode 0600 and records `key_file`. | A new file per init, so an interrupted write can never rotate a live key. |
| Environment variable | `init --key-env NAME`, or `key_env = "NAME"` in the Provider. | Only the variable name is stored. |

Resolution, per Provider:

1. A non-empty `key_env` value wins.
2. Otherwise `key_file` is read. Relative paths resolve under `SSHARNESS_HOME`; absolute paths and `~` work.
3. A missing, empty or multi-line credential is an error that names the Provider, never the value.

An explicit `key_file` in your declaration does not accidentally inherit a preset's built-in `key_env` unless you also declare `key_env`.

Enter an environment credential without putting it into shell history:

```sh
set +x
DEEPSEEK_API_KEY="$(python3 -c 'import getpass; print(getpass.getpass("Provider key: "))')"
export DEEPSEEK_API_KEY
bin/supersuperharness doctor
```

Environment-only `init` can run before the variable is set; `doctor` fails its `credential` check until it is. The terminal running `serve` **and every terminal running `attach`, `peek` or `open`** must have the variable, because those commands build their own child environment. Having it only in the service is not enough for interactive takeover, and exporting it in another terminal does not update a service that is already running. No shell rc file or Claude settings file is modified.

`auth_env` is where the adapter puts the resolved secret, independent of the source variable's name:

| `auth_env` | Header Claude Code sends |
|---|---|
| `ANTHROPIC_API_KEY` | `x-api-key` |
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer ...` |

Only that one variable reaches the child. Unrelated inherited Anthropic credentials are not forwarded (see [design.md](design.md#claude-code-adapter)).

## Files under `SSHARNESS_HOME`

`SSHARNESS_HOME` defaults to `~/.super-super-harness`. It isolates configuration and all service state; tests must point it at a temporary directory, never the real home. The service creates it with mode 0700 and tightens it to 0700 if needed.

```text
SSHARNESS_HOME/
  profiles.toml          private, authoritative configuration (0600)
  keys/<provider>-<hex>  optional credential files (0600)
  owner-token            local owner authentication for the socket (0600)
  web-session-key        web cookie signing key (0600)
  router.db              SQLite: messages, tasks, events, items, reminders, ...
  router.sock            owner/MCP Unix socket (0600)
  service.pid            single-instance lock
  router/                the Router's working directory and session cwd; soul.md and memory.md
  workspaces/<task>/     one workspace per Worker task (brief.md, tmp/, repo/)
  items/<code>/notes.md  item notes (directory 0700, file 0600)
  run/                   per-launch MCP configs (0600) and sanitized logs
  avatar/                optional private Router artwork
  characters/            optional private Worker characters and Harness icon
  releases/<sha>/        installed versions, when using upgrade
  current -> releases/…  active version, when using upgrade
  service.log            output of a service started by upgrade/rollback
```

Precedence:

```text
Provider presets  <-  your [providers.*] declarations  <-  explicit Profile context_window
The repository profiles.toml is read ONLY when the private file does not exist and there is no router.db.
`serve` never runs on it.
```

| Rule | Detail |
|---|---|
| User configuration | Never merged with repository Profiles. Upgrades replace application code, not the private TOML or key files. |
| Missing private file with an existing DB | The loader and fresh `init` refuse to fall back to public defaults. Only file existence is checked; the database is not read. |
| Legacy custom Provider without `auth_env` | Means `ANTHROPIC_API_KEY`, preserving old file-based configurations. |
| Endpoint/auth in `profile.env` | Refused: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` belong in Provider declarations. |

## Editing the private file and the `["web"]` table

`init` writes TOML with quoted keys and table names, so a private file it created contains `["web"]`, `"port" = 8765` and so on, rather than `[web]`. Both spellings name the same table. **Edit the existing `["web"]` table. Do not append another `[web]` or `["web"]` table**: a duplicate table declaration makes the whole file invalid TOML. If a setting is absent, add it inside the existing table.

```toml
["web"]
"port" = 8766                      # default 8765; 0 disables the web channel
"router_name" = "Local Assistant"  # default "Router"; nonempty, at most 80 characters, no control characters
"user_name" = "You"                # label for your messages
"user_avatar" = ""                 # optional path to a png/jpg/jpeg/gif/webp, at most 2 MiB
```

| Key | Default | Effect |
|---|---|---|
| `port` | `8765` | The listener binds only to `127.0.0.1`. Integer 0–65535; `0` disables web access. If the port is occupied the service keeps running, the web is unavailable, and `status` reports the error. |
| `router_name` | `"Router"` | Name on the Router card in the web's expanded Routing view and in avatar state. The Chat page always says `Router`. |
| `user_name` | `"You"` | Your name in the page; its first character is the fallback avatar. |
| `user_avatar` | `""` | Image served to the page at `/api/avatar`. `~` is expanded. |

All four are read when the service starts; restart after editing. Check the listener with the configured port: `GET /api/state` returns 401 before login and 200 with a valid session.

### Router artwork

The built-in character **suber (苏宝)** is the default Router artwork. Optional private images override it per state and need no restart:

- Directory: `<SSHARNESS_HOME>/avatar/`
- File name: `<state>.<ext>`, state one of `idle`, `thinking`, `working`, `done`, `error`, `waiting`, `rest`; extension `png`, `webp`, `jpg` or `gif` (for example `idle.png`, `done.gif`). If several exist for one state, png → webp → jpg → gif.
- Each file at most 2 MiB, with a signature that matches its extension. The directory and files must be a real directory and regular files, not symlinks, FIFOs or devices.
- Additions, replacements and deletions are picked up within about one second through the live stream. Replace files atomically (write elsewhere, then rename) rather than writing a file in place while it is being served.
- `waiting` and `rest` fall back to your `idle` image before falling back to the built-in pose.

Nothing is uploaded and there is no upload or rename UI. Use original or properly licensed images and keep them out of git, documentation, screenshots and logs. How states are chosen and served is in [design.md](design.md#avatar-state-mechanism).

### Worker characters

Workers get animated characters chosen by model family (rules in [guide.md](guide.md#workers-in-the-page)). **No character artwork ships with the project**: some of it is official mascot material that a public repository cannot carry. Without these files every Worker is a built-in SVG robot and the page works the same.

- Directory: `<SSHARNESS_HOME>/characters/`, a real directory (not a symlink).
- Per character, a manifest `<id>.json` and the sprite atlas it names. The page uses the ids `claude`, `chatgpt`, `codex` and `muse`; other valid ids are accepted but never shown. At most 16 manifests are read, in file-name order.
- Optional Harness icon: `harness-claude-code.png`, shown next to the `Claude Code` label.

A setup with all four characters:

```text
SSHARNESS_HOME/characters/
  claude.json    claude-v1.png
  chatgpt.json   chatgpt-v1.png
  codex.json     codex-v4.webp
  muse.json      muse-v1.png
  harness-claude-code.png        optional
```

An atlas is a grid of equally sized frames, one animation per row. The manifest, for example `claude.json`:

```json
{
  "atlas": "claude-v1.png",
  "frameWidth": 256, "frameHeight": 256,
  "columns": 6, "rows": 4,
  "pixel": true,
  "animations": {
    "idle":     {"row": 0, "frames": 6, "frameDurationsMs": [1400, 240, 100, 80, 100, 1100]},
    "working":  {"row": 1, "frames": 6, "frameDurationsMs": [460, 320, 130, 90, 140, 460]},
    "waiting":  {"row": 2, "frames": 6, "frameDurationsMs": [500, 320, 220, 100, 220, 600]},
    "complete": {"row": 3, "frames": 6, "frameDurationsMs": [420, 250, 180, 100, 180, 600]}
  }
}
```

| Field | Rule |
|---|---|
| `<id>` (file name) | `[a-z][a-z0-9-]{0,31}`, followed by `.json`. The manifest file is at most 64 KiB. |
| `atlas` | The atlas file in the same directory. Only its last path component is used, so `/elsewhere/claude-v1.png` means `characters/claude-v1.png`. Name of 1–80 characters from `A-Za-z0-9._-`, ending in `.png` or `.webp`. |
| `frameWidth`, `frameHeight` | JSON integers, 8–1024. |
| `columns`, `rows` | JSON integers, 1–32. The atlas must be exactly `frameWidth × columns` by `frameHeight × rows` pixels, or the page falls back to the robot. |
| `animations` | All four of `idle`, `working`, `waiting` and `complete`, each with `row` (0 to `rows − 1`), `frames` (1 to `columns`, read left to right) and `frameDurationsMs` (exactly `frames` integers, each 20–10000). |
| `pixel` | Optional; `true` draws the frames without smoothing (pixel art). |

Any other fields (`id`, `version`, `loop`, `label`, `caption`, ...) are ignored. A manifest that breaks a rule, or whose atlas is missing, is skipped as a whole.

- Every file must be a regular, non-empty file, not a symlink, FIFO or device, of at most 8 MiB, and its first bytes must match its extension (PNG or WebP). The icon must be a PNG.
- The service rescans the directory at most once a second. Added, replaced and removed files take effect on the next page update without a restart; replace files atomically (write elsewhere, then rename).
- The images are served only to a logged-in page, through `/api/characters/<id>.atlas` and `/api/characters/harness-claude-code.png`; no file path is ever exposed (see [design.md](design.md#worker-identity-and-private-characters)).

Keep these files out of git, documentation, screenshots and logs unless you have the rights to publish them.

## Configuration reference

A complete private file, as you might write it by hand:

```toml
default_profile = "worker"        # Profile used when task_start names none
source_repo = "~/src/super-super-harness"   # default --repo for upgrade (optional)
session_root = "~/.claude/projects"         # where Claude Code stores session files (default)

[router]
primary = "router"
backup = "worker"                 # must use the same Harness as primary

[harness.claude-code]
binary = "claude"                 # any executable on PATH, or an explicit path

[providers.my-provider]
base_url = "https://your-provider.example/anthropic"
auth_env = "ANTHROPIC_AUTH_TOKEN"
key_env = "MY_PROVIDER_KEY"
# key_file = "keys/my-provider"   # alternative: 0600 file, never an inline key
# small_model = "your-small-model" # used as Claude Code's Haiku-class model
# ensure = "~/bin/start-gateway"   # trusted local preparation command

[providers.my-provider.models]
"your-router-model" = 1000000
"your-worker-model" = 256000

[profiles.router]
harness = "claude-code"
provider = "my-provider"
model = "your-router-model"

[profiles.worker]
harness = "claude-code"
provider = "my-provider"
model = "your-worker-model"
# context_window = 256000         # optional per-Profile override

[limits]
max_workers = 10

[failover]
silent_seconds = 20
```

Use your Provider's actual Anthropic-compatible base URL and do not append `/v1/messages`. Plain HTTP is allowed for explicitly configured local endpoints; prefer HTTPS for remote ones. URLs with credentials, a query string or a fragment are rejected.

### Top level

| Key | Default | Meaning |
|---|---|---|
| `default_profile` | required | Worker Profile when the Router does not choose one. |
| `[router] primary`, `backup` | required | Router Profiles. All three names must exist. |
| `session_root` | `~/.claude/projects` | Where the Claude Code adapter looks for session JSONL files. |
| `source_repo` | none | Default repository for `upgrade`. |
| `[mcp.<name>]` | none | Extra MCP server definitions (Claude Code `mcpServers` entries) that Profiles can list in `mcp`. |
| `[sources.<name>]`, `[[rules]]` | none | Event bus sources and routing rules; see [design.md](design.md#event-bus). |

### `[harness.<name>]`

| Key | Meaning |
|---|---|
| `binary` | Executable for the Claude Code adapter. `~` is expanded. Built-in default: `claude`. |
| `implementation` | Makes this Harness name an alias of a registered implementation, so it can carry separate settings, e.g. `[harness.claude-alt] implementation = "claude-code"` with its own `binary`. |

### `[providers.<name>]`

| Key | Default | Meaning |
|---|---|---|
| `base_url` | preset | Anthropic-compatible endpoint; trailing `/` is removed. |
| `auth_env` | preset, else `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`. |
| `key_env` | preset | Source environment variable name. |
| `key_file` | none | Source file; relative to `SSHARNESS_HOME`. |
| `models` | preset | Table of model ID → context window (positive integer). Yours are merged over the preset's. |
| `small_model` | the Profile's model | Passed to Claude Code as its Haiku-class default. |
| `ensure` | none | Trusted local command run before launches and probes. Split on spaces, not run through a shell; output suppressed; 30 s timeout; cached for 60 s. This is executable local configuration, not a remote import mechanism. If it fails at Router startup, `serve` exits naming only the field `[providers.<name>].ensure`. |

User declarations override presets field by field, including model windows. Unknown Provider names are allowed (custom Providers). `gpt` is refused.

### `[profiles.<name>]`

| Key | Default | Meaning |
|---|---|---|
| `harness` | required | `claude-code`, or an alias declared under `[harness]`. |
| `provider` | required | A declared or preset Provider. |
| `model` | required | Model ID passed with `--model` and as Claude Code's model defaults. |
| `context_window` | the Provider's window for `model` (a `[1m]` suffix is ignored for the lookup), else 0 | Non-negative integer. When non-zero, sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` so Claude Code does not compact early on models it does not know. |
| `settings` | `"project"` | Value for Claude Code `--setting-sources`. |
| `add_dirs` | `[]` | Extra directories passed with `--add-dir`; `~` expanded. |
| `mcp` | `["router"]` | MCP servers. `router` (the built-in tool server) is always included; other names refer to top-level `[mcp.<name>]`. |
| `tools` | `"all"` | Anything else is passed as `--tools`. |
| `env` | `{}` | Non-secret child environment overrides, applied last. Must not contain endpoint or credential variables. |
| `sandbox` | `"none"` | Only `"none"` is accepted; sandboxing is not implemented. |
| `location` | `"local"` | Only `"local"` is accepted; remote execution is not implemented. |

### `[limits]`

| Key | Default | Meaning |
|---|---|---|
| `max_workers` | `10` | Concurrent Worker processes. Further tasks wait as `queued`. |
| `busy_interrupt_seconds` | `10` | If a user message has been written to the Router for this long while it is still in a non-Router tool call, the Router's turn is interrupted so it can read the message. |
| `stall_minutes` | `30` | A live Worker with no event for this long produces a `worker.stalled` warning to the Router. |
| `worker_retries` | `3` | Retries after a model/network error or an exit without a result, with backoff `min(2^n, 30)` seconds; then the task fails. |

### `[failover]`

| Key | Default | Meaning |
|---|---|---|
| `silent_seconds` | `20` | During an open turn, silence or retries beyond this trigger a health probe, at most once per this interval. |
| `deep_probe_seconds` | `120` | Silence beyond this makes the probe a real tiny model call. |
| `retries` | `5` | `api_retry` events in a turn before probing deeply. |
| `probe_timeout` | `5` | Shallow probe timeout, also used by the Developer view. |
| `wake_grace_seconds` | `90` | After the host wakes from sleep (a gap over 30 s), no failover probing for this long. |
| `primary_check_seconds` | `300` | While running on backup, how often to check whether primary recovered. |

The failover policy itself is described in [design.md](design.md#router-health-and-failover).

## Providers and presets

**All runnable presets use the Claude Code Harness. None has been tested with a real API key.** Endpoint and auth settings were checked against official documentation on **2026-11-02**. This is a documentation check, not live compatibility certification. The machine-readable defaults are in [`catalog.py`](../super_super_harness/catalog.py).

| Provider ID | Anthropic-compatible base URL | Auth variable for Claude Code | Default source variable | Model IDs → context tokens |
|---|---|---|---|---|
| `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` → 1,000,000 |
| `deepseek` | `https://api.deepseek.com/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `DEEPSEEK_API_KEY` | `deepseek-v4-pro`, `deepseek-flash` → 1,048,576 each |
| `glm` | `https://open.bigmodel.cn/api/anthropic` | `ANTHROPIC_AUTH_TOKEN` | `GLM_API_KEY` | `glm-5.3`, `glm-5.3-flash` → 1,000,000 each |
| `stepplan` | `https://api.stepfun.com/step_plan` | `ANTHROPIC_AUTH_TOKEN` | `STEP_API_KEY` | `step-5-preview` → 1,000,000; `step-3.7-flash` → 256,000 |
| `kimi` | `https://api.moonshot.cn/anthropic` (official page verified in a browser) | `ANTHROPIC_AUTH_TOKEN` | `MOONSHOT_API_KEY` | `kimi-k3.1-pro` → 1,048,576 |
| `gpt` | **Under construction / 建设中** | n/a | n/a | Waits for a Codex Harness; no runnable preset. |

The model list follows the project brief; it does not claim every account or plan can use every model. Context windows were cross-checked in [models.dev's catalog](https://models.dev/api.json) under `anthropic`, `deepseek`, `zhipuai`, `stepfun` and `moonshotai-cn`; catalog data can change. Your own model and window declarations take precedence, and no network catalog fetch happens at runtime.

### Official authentication checks

| Provider | Official source | What was checked |
|---|---|---|
| Anthropic | [Claude Code environment variables](https://code.claude.com/docs/en/env-vars), [API overview](https://docs.anthropic.com/en/api/overview) | `ANTHROPIC_API_KEY` sends `X-Api-Key`; `ANTHROPIC_AUTH_TOKEN` sends `Authorization: Bearer`. The preset uses API-key mode. The API overview redirected to a region-unavailable page during a direct fetch; the API origin was corroborated by the official indexed overview, and header semantics were read from the live environment-variable page. |
| DeepSeek | [Integrate with Claude Code](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/), [Anthropic compatibility](https://api-docs.deepseek.com/guides/anthropic_api/) | Official Claude Code instructions export `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` and `ANTHROPIC_AUTH_TOKEN=<DeepSeek API Key>`. Direct Anthropic SDK examples may use API-key mode; the preset follows the **Claude Code** instructions. |
| GLM | [Claude Code setup](https://docs.bigmodel.cn/cn/coding-plan/tool/claude) | Official configuration sets `ANTHROPIC_AUTH_TOKEN` and `https://open.bigmodel.cn/api/anthropic`; its model mapping includes the GLM 5.3 family. |
| StepPlan | [Claude Code integration](https://platform.stepfun.com/docs/zh/guides/developer/integrations/claude-code) | Official JSON uses `ANTHROPIC_AUTH_TOKEN: YOUR_STEP_API_KEY`, `https://api.stepfun.com/step_plan` and `step-5-preview`. It warns that Claude Code appends `/v1/messages`, so do not add `/v1` to the base URL. |
| Kimi | [Use Kimi in Claude Code](https://platform.kimi.com/docs/guide/claude-code-kimi) (rendered in a browser), [quick start](https://platform.kimi.com/docs/get-api-key) | The old Moonshot documentation URL redirects to the Kimi platform. The rendered page declares **`https://api.moonshot.cn/anthropic`**, uses **`ANTHROPIC_AUTH_TOKEN`**, and has a Messages curl example with `Authorization: Bearer` and `model: kimi-k3.1-pro`. It warns against keeping a conflicting `ANTHROPIC_API_KEY`. This verifies the **`.cn`** endpoint only, not a previously reported third-party `.ai` URL. |

When checked, the Kimi guide's main mapping example still used `kimi-k3[1m]`, while its curl example and quick start used `kimi-k3.1-pro`. The preset keeps the brief's newer ID with a catalog window. No real request was made to resolve account-specific availability.

### GPT subscriptions are not an integration

If your GPT subscription includes Codex, use the official Codex product directly, following [OpenAI's Codex authentication documentation](https://developers.openai.com/codex/auth/). A subscription is not an Anthropic-compatible endpoint or a portable API key. This project does not route GPT subscriptions, export subscription credentials, provide a proxy, or implement Codex sessions; see the [Harness roadmap](design.md#roadmap-not-implemented).

### What remains unverified

- Real-key authentication, account permissions, balances, rate limits, model availability for an individual account, and billing.
- End-to-end Claude Code tool calls, model aliases, compaction, thinking modes, resume and interactive takeover with each preset.
- Official documentation can change; the dates and links above record what was checked, not a support commitment.
- Providers may require special thinking or output budgets even for a tiny request.

## Profiles and model split

A **Provider** is an endpoint plus authentication plus model defaults. A **Profile** picks a Harness, a Provider, a model and runtime settings. The Router chooses a Profile per Worker (`task_start(profile=...)`, with a stated reason) or falls back to `default_profile`. Adding a Profile does **not** change Router or Worker defaults; edit `default_profile` and `[router]` yourself.

Recommendation: give the Router a model that schedules reliably and explains clearly, for example the `claude-opus-5-5` preset, and let cost-sensitive or high-volume Workers use another Profile.

```sh
# 1. Router and default Worker on Anthropic (hidden key prompt)
bin/supersuperharness init --provider anthropic
# 2. A cheaper Worker Profile on DeepSeek (environment credential)
bin/supersuperharness init --profile fast-worker --provider deepseek --model deepseek-flash --key-env DEEPSEEK_API_KEY
# 3. A third Profile on another model of the same Provider
bin/supersuperharness init --profile pro-worker --provider deepseek --model deepseek-v4-pro
```

Step 3 reuses the shared `deepseek` credential (and refuses a different `--key-env`). Then edit the private file by hand, for example to make the cheap Profile the default Worker and the Router backup:

```text
"default_profile" = "fast-worker"

["router"]
"primary" = "default"
"backup" = "fast-worker"   # every Profile init creates uses claude-code, so the Harness matches
```

Edit the existing `"default_profile"` line and `["router"]` table in place; as with `["web"]`, never declare a table twice.

Other common splits:

| Goal | Router | `default_profile` | Other Profiles |
|---|---|---|---|
| One model everywhere | `default` | `default` | none |
| Strong Router, cheap Workers | Opus-class Profile | flash-class Profile | a strong Worker Profile the Router picks for hard tasks |
| Real Router failover | primary on Provider A | any | backup on Provider B, same Harness |

A running Worker can be moved to another Profile (`task_switch_model`, or the web **Switch model** control). Its session is kept, so the new Profile must use the **same Harness**.

## Router primary and backup

- `[router] primary` and `backup` must use the same Harness; the loader refuses otherwise. With only one Profile, both point to it and there is no real failover.
- To fail over for real, add a second Profile (ideally on a different Provider) and set `backup` to it.
- When the current Router model is unhealthy, the service probes the other one, switches if it answers, and tells you. If both are down, the Router pauses with an alert; messages stay queued and it resumes on its own when a model answers. While on backup it checks primary every `primary_check_seconds` and asks whether to switch back. See [design.md](design.md#router-health-and-failover).
- Neither cross-Harness session conversion nor a Codex Harness is implemented.

## `doctor`

```sh
bin/supersuperharness doctor                       # Router primary, backup and default Worker
bin/supersuperharness doctor --profile fast-worker --profile pro-worker
bin/supersuperharness doctor --timeout 15
```

| Check | What is actually verified |
|---|---|
| `harness` | The adapter's executable is available (`claude` found on `PATH`). Not a version or flag compatibility test. |
| `credential` | A non-empty credential resolves, without printing it. |
| `key_file_private` | A file credential is a regular, non-symlink 0600 file, unless an environment credential is in use. Existing external files are inspected, never silently chmod'ed. |
| `endpoint` | A bounded unauthenticated connectivity probe of `base_url`. An HTTP 401 still passes. |
| `model` | A tiny authenticated, **streamed** Messages request with the configured model and auth mode returns HTTP success and a first event that is not an error. It streams because a gateway in front of a thinking model (for example a proxy for a non-Anthropic model) ignores `max_tokens` and, unstreamed, answers only after the model has thought, which can outlast the 5 s default. **This can incur cost.** |
| `configuration` | Only appears, as `false`, when the Profile or its Provider could not be evaluated. |

`--profile` can be repeated; by default it checks the Router primary, backup and default Worker. `--timeout` bounds each probe (default 5 s, must be greater than 0 and at most 60 s). It prints a boolean per check as JSON and, for each failed one, a `reasons` entry such as `HTTP 401 authentication_error` or `no answer within 5 seconds`; reasons hold only status codes, error types and exception class names, never keys, URLs or upstream messages. It exits nonzero if any check fails. No daemon, database or socket is opened. Credential-bearing probes refuse redirects and bypass proxies. A Provider's `ensure` command has its own 30 s limit.

→ `doctor` is **not** end-to-end proof of Claude Code tool use, resumability or settings precedence. Tests use only fake local endpoints and fake credentials.

## Agent-assisted setup

Paste the following into an agent that has access to your local checkout. It must ask before writing private configuration or making real model calls.

```text
Help me configure super-super-harness from this checkout.

1. Read docs/setup.md and the "Harness protocol" section of docs/design.md.
   Do not modify README, change the project name, or invent Harness support.
2. Ask whether this is a new installation or an upgrade, which SSHARNESS_HOME to use,
   which Provider/model I want for Router and Workers, and whether credentials
   come from an environment variable or a private key file. Recommend a model
   that schedules reliably and explains clearly, e.g. the claude-opus-5-5 preset.
3. If this is a pre-rename installation, stop: switching its home, state and
   startup/rollback paths needs a separately approved plan; init --migrate-from
   only copies TOML and does not translate environment names or move state.
   For an upgrade within the current naming scheme, stop before replacing
   repository defaults. Locate the OLD active release's profiles.toml with my
   permission, preserve a private backup, and use the NEW checkout's
   `init --migrate-from` command. Never replace a different existing
   SSHARNESS_HOME/profiles.toml. Keep all old Profile names and external key
   references; do not guess missing settings.
4. Never ask me to paste a key into this chat or a visible command. Prefer an
   existing environment variable by NAME. Otherwise let me type the key locally
   at init's hidden prompt. Do not read key values, print environments, include
   secrets in logs, commit private files, or modify shell rc / Claude settings.
5. Prepare `bin/supersuperharness init` arguments for my approval. Known Providers
   supply defaults; custom Providers need an Anthropic-compatible base URL,
   auth_env (ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN), model ID and context
   window. User declarations override built-in defaults. Endpoint/auth fields
   belong in providers, not profile.env. All secrets must be private 0600 files
   outside a checkout or supplied through the service environment.
6. Before running `doctor`, explain that it makes a small potentially billable
   Messages API call and ask permission. Doctor does not start a service and
   does not read its database. Report boolean checks, never HTTP error bodies
   or secret values. All presets are documentation-checked, not real-key tested.
7. Router primary and backup must use the same Harness. Adding another Profile
   does not automatically change Router or Worker defaults; show me the exact
   private TOML changes. Do not overwrite an existing Profile with init.
   Edit the existing ["web"] table; never append a second one.
8. GPT/Codex, Pi, DeepSeek Harness and cloud execution are roadmap items, not
   implemented adapters. GPT subscriptions are only advice to use official
   Codex separately; never suggest subscription credential export or a proxy.
9. Do not start/stop/upgrade/rollback an existing service without explicit
   approval. Test any proposed migration using a temporary SSHARNESS_HOME first.
10. Finish with changed private file paths (not contents), Provider/model/auth
    mode, context window, checks run, costs/permissions authorized and anything
    not verified. Do not claim end-to-end success from a shallow probe.
```

An agent cannot safely configure a secret by remembering it. Keep secrets in the service's environment or local private files, not chat history. The runtime uses full-permission Workers; configuration help does not provide a sandbox.

## Upgrade migration

This is the **configuration-only** migration for releases that share the current command and environment naming. It is not a home-directory or project-rename migration (see [Rename boundary](#rename-boundary)).

Older releases could run directly from the repository's `profiles.toml`. Installing the clean public defaults cannot reconstruct those private settings once they are gone. The migration is explicit and offline: it does not contact, stop, start or upgrade the service. If a service database exists but the private `profiles.toml` does not, the loader and fresh `init` refuse to fall back to public defaults. This fails closed instead of silently losing old Profile routing, but you still need to migrate before upgrading to avoid an interrupted upgrade.

### Before the first upgrade

1. Find the `profiles.toml` used by the **currently running old release**. With an active-release symlink, take the old target's file, not the new checkout's defaults. Save a private backup. If a private `SSHARNESS_HOME/profiles.toml` already exists it is authoritative: do not replace it.
2. Leave external key files at their current paths. Convert legacy relative key paths to absolute or `~` paths if needed, because new relative paths resolve under `SSHARNESS_HOME`. No key contents go into TOML.
3. Run the **new checkout's** migration while the old file still exists:

   ```sh
   # Substitute your paths. For a rehearsal use a temporary SSHARNESS_HOME instead.
   export SSHARNESS_HOME="$HOME/.super-super-harness"
   NEW_REPO=/absolute/path/to/new-checkout
   OLD_CONFIG=/absolute/path/to/old-active-release/profiles.toml
   "$NEW_REPO/bin/supersuperharness" init --migrate-from "$OLD_CONFIG"
   cmp -s "$OLD_CONFIG" "$SSHARNESS_HOME/profiles.toml" && printf 'Configuration preserved\n'
   ```

4. Confirm privately that the default Profile, Router primary/backup, every old Profile name, harness binary, model IDs, directories and optional `source_repo` are present. The migration validates the file's structure, then copies it **byte-for-byte** with mode 0600. It does not read keys, the database, sockets or tokens. It refuses to overwrite a different private file; re-running the same migration is safe.
5. Run the new checkout's `doctor` only when ready to make authenticated, possibly billable requests. Get explicit approval for the actual upgrade or restart separately.

### After the upgrade

- Keep `SSHARNESS_HOME` unchanged. New and old releases both prefer its private TOML, and code upgrades and rollbacks leave it intact.
- Check that the old Profile names are still available before sending new work. Do **not** run a fresh `init` over an existing Profile to "restore defaults".
- Keep old external key files as long as the migrated TOML refers to them. Back up private configuration separately from git.
- If the repository file was already replaced **without** migration, recover it from a private backup or the old release/commit into a private temporary file, then migrate that file. Do not guess lost settings or start the service on the public placeholder Profile.

Automated coverage: `QuickConfiguration.test_legacy_migration_survives_install_upgrade_and_rollback_without_keys_or_db` rehearses the migration, two code installs and a rollback in a temporary `SSHARNESS_HOME`, keeping legacy Profile names and settings without reading even the fake external key files. No production migration or upgrade was performed.

### Database migrations

Schema changes are additive and happen when the service starts: new tables use `CREATE TABLE IF NOT EXISTS`, older databases gain new columns with `ALTER TABLE ... ADD COLUMN`, and missing historical state timestamps fall back to the last update time. Read-only commands (`history`, `reminders`, `events`, `status`, `log`) open SQLite with `mode=ro` and never migrate. Back up private state before any upgrade. Rollback caveats for specific features (tasks in `waiting_deps`, reminders, escaped events) are in [design.md](design.md#rollback-compatibility).

## Upgrade and rollback of a running install

Workers can change the application code, so a managed install only ever runs a tested copy of a commit. This applies to releases with the current naming; get explicit approval before upgrading or rolling back a live service.

```sh
bin/supersuperharness upgrade [--repo PATH] [--ref REF]   # REF defaults to main; PATH to source_repo
bin/supersuperharness rollback
```

`upgrade`:

1. Resolves `REF` to a short SHA in the repository and extracts it with `git archive` into `SSHARNESS_HOME/releases/<sha>` (through a `<sha>.partial` directory; an existing release is reused).
2. Runs the release test gate in that copy with a temporary `SSHARNESS_HOME` under `/tmp`, `SSHARNESS_NO_CAFFEINATE=1` and a 600 s limit: `tests.test_workers`, `test_router`, `test_web`, `test_harness`, `test_chat`, `test_reminders`, `test_avatar`, `test_bus`, `test_items`, `test_suber`, `test_c1`, `test_c2`, `test_c5`, `test_ui_v2`. On failure nothing changes.
3. Atomically repoints `SSHARNESS_HOME/current` to the new release.
4. Restarts: asks a running service to stop and waits up to 60 s for its socket to disappear, then starts `current/bin/supersuperharness serve` detached, logging to `SSHARNESS_HOME/service.log`.
5. Healthy means the socket answers and the Router reports `ready` within 30 s, and is still ready 5 s later. Readiness comes from the adapter-independent `router.ready` status, not a PID.
6. If unhealthy, it switches back to the previous release and restarts it; with no previous release it removes `current`.

It prints a JSON result (`ok`, `release`, `previous`, `rolled_back_to`, `error`) and exits nonzero unless `ok`.

`rollback` switches `current` to the most recently modified other installed release and restarts it, with the same health check but no test run. With no other release it reports `no earlier version`.

Private configuration, keys and the database are untouched by both commands.

## Atomic writes and secret recovery

- The owner token, web session key, `init` credentials and updates to private TOML are written to a same-directory 0600 temporary file, flushed and fsynced, then atomically renamed. An interrupted replacement leaves the previous valid file intact.
- First-time TOML installation and migration publish the completed temporary file with an atomic no-clobber link, so a racing writer is never overwritten.
- Empty owner-token or web-session-key files left by older writers are regenerated at startup; valid existing keys stay stable. Non-empty malformed key files and symlinks are refused, not overwritten.
- A hard-killed writer may leave a private `.name.*` temporary file; it is never used as configuration.
- A crash between `init`'s key write and TOML write can leave an unused private key file, but cannot rotate an existing Profile's credential.
- Login tickets for the web are only held in memory. An obsolete `web-token` file from older releases is deleted at startup.
