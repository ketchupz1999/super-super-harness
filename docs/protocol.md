# Harness protocol (v1)

How to plug an agent product (Codex, Pi, …) in as a Worker or Router. The contract is code: [`adapters/__init__.py`](../super_super_harness/adapters/__init__.py). Claude Code ([`adapters/claude_code.py`](../super_super_harness/adapters/claude_code.py)) and the experimental [Hermes adapter](../super_super_harness/adapters/hermes.py) implement it; `tests/fake_harness.py` is an in-memory reference.

## What an agent must be able to do

| Need | Why |
|---|---|
| Run headless and stream its output | The service drives it without a terminal |
| Accept more input on a running session | Follow-ups, answers, events |
| Accept local images via `image_input(cfg, profile, text, images)` | Optional adapter `IMAGE_INPUT` instructions turn validated image metadata (id, filename, MIME, size, hash, absolute path) into durable text. Claude Code uses `Read`; Hermes uses `vision_analyze(image_url=path, question=...)`. The optional `image_input_available(profile)` hook checks required tools (Claude Code requires `Read`). Unsupported adapters/Profiles reject images, not silently drop them. |
| Call MCP tools | Workers report back and the Router acts only through our tools |
| Resume a session by id | Follow-ups after a turn ends, restarts, model switches |
| Interrupt a turn | Interrupt and stop |

Everything else — translating its output, reading its session files, probing its endpoint — is the adapter's job.

## How it fits

```text
Profile.harness ──> get_harness() ──> your adapter
                                        ├─ setup, probes, session reads   (run in a thread)
                                        ├─ Process(...)  ──> events       (callbacks on the asyncio loop)
                                        └─ interactive_argv() ──> terminal takeover (open / attach)
```

| Router tool | Calls on the adapter |
|---|---|
| `worker_hire` | `prepare_session` → `build_env` + `headless_argv` → `Process(...)` → `start()` → `send(brief)` |
| `worker_assign` / `worker_say` | `send(text)` if running; otherwise launch resuming the same session |
| `worker_interrupt` | `interrupt()` |
| `worker_stop` | `close()` |
| `worker_switch_model` | `close()`, then launch the same session with the new Profile (same Harness only) |
| `open` / `attach` | `interactive_argv` |

## The `Harness` methods

| Method | Must |
|---|---|
| `build_env(cfg, profile, tmpdir=None)` | Return a clean child environment with the credential resolved. Never print secrets. |
| `headless_argv(cfg, profile, session_id, resume, mcp_config, system_prompt)` | Launch arguments for your `Process`. `mcp_config` is a private JSON file with `mcpServers`. |
| `interactive_argv(cfg, profile, session_id, mcp_config, system_prompt, fork=False, hooks=None)` | A terminal command for takeover. Raise `ValueError` for anything unsupported. |
| `prepare_session(cfg, session_id)` | Return `(session_id, resume)`. If a first launch was cut off before a real turn, return a new id. |
| `session_has(cfg, session_id, marker)` | Whether a real user turn contains `marker` (decoded text). The Router uses it to avoid replaying messages after a crash. |
| `session_file(cfg, session_id)` | Local transcript path, or `None` |
| `transcript(cfg, session_id, *, max_bytes, limit, text_limit)` | Ordered items `{time, kind, text}`; kinds `received`, `said`, `thinking`, `tool_call`, `tool_result`. Missing session → `[]`. Honor the bounds. |
| `ensure_provider(provider)` | Optional local preparation; else no-op |
| `shallow_probe(provider, timeout)` | Endpoint reachable? (An auth error still counts as reachable.) |
| `deep_probe(provider, model, timeout)` | One tiny real model call; boolean |
| `deep_check(provider, model, timeout)` | Optional `(ok, safe_reason)` for `doctor` |
| `available(cfg, profile)` | Is the binary or prerequisite present? |
| `Process(argv, env, cwd, on_event, on_exit, log_path=None)` | Create a process object; do not start it yet |

## The process object

| Member | Must |
|---|---|
| `await start()` | Start; return the PID (or `None` if not local) |
| `send(text, uuid=None)` | Queue input; return its UUID, or `None` if closed |
| `interrupt()` | Ask the current turn to stop |
| `await close(wait=5)` | Idempotent; end the process and its children; set `exited` |
| `on_event(dict)` | Called in order with v1 events (below) |
| `on_exit(code)` | Called exactly once |

## v1 events

Translate your native output into these. Unknown events are ignored.

| `type` | Meaning |
|---|---|
| `user` with `isReplay: true, uuid` | This input is now model-visible and durable. A Worker only completes after every sent input is acknowledged. |
| `assistant` | `{message: {content: [...]}, parent_tool_use_id}` with text blocks and tool calls `{type: "tool_use", id, name, input}`. `parent_tool_use_id` is `null` for the agent itself, set for its sub-agents. Our tools appear as `mcp__router__<name>`. |
| `system` / `init` | Optional `{model, tools}` |
| `system` / `input_started` | Optional `{uuid, marker}`: this queued input is starting its turn, **not** a durable receipt. Late-ACK adapters emit it before tools; `marker` is a unique string included in the persisted user content, allowing `session_has` to resolve uncertain assignment delivery on restart. Activates work without clearing a report again at ACK and distinguishes current input from unread input. Early-ACK adapters need not emit it. |
| `system` / `background_tasks_changed` | `{tasks: [...]}`; non-empty blocks completion |
| `system` / `api_retry` | Feeds Router health checks |
| `system` / `compact_boundary` | Context was compacted; optional `compact_metadata: {trigger, pre_tokens}`. The Router refreshes its Worker list. Workers record `worker.compacted` and a durable current-session count without waking the Router; more than `limits.compact_thrash` (default 3) in a single turn triggers a fresh-session handoff. Sub-agent boundaries do not count toward the Worker's context. |
| `result` | End of turn: `{subtype, is_error, result}` (errors may also carry `errors: [...]`). `success`; `error_during_execution` = interrupted. Worker context-overflow / automatic-compaction-failure / compaction-thrashing errors trigger a handoff, at most twice per assignment; other errors are retried. |

**`report_done` must come from the Worker itself, not a sub-agent.** Emit our tool calls as `assistant` tool_use events; Claude Code also passes the tool_use id in the MCP call's `_meta`, which the service matches. Without this, the Worker's final text becomes its report.

## Claude Code, for reference

| Area | How |
|---|---|
| Headless | `claude -p --input-format stream-json --output-format stream-json --verbose --replay-user-messages --model M --dangerously-skip-permissions --setting-sources S --settings '{"disableAllHooks":true}' --strict-mcp-config --mcp-config F --append-system-prompt P`, then `--resume ID` or `--session-id ID` |
| Takeover | Same arguments interactively, `--resume ID` (`--fork-session` for `peek`) |
| Environment | Allowlist only (`HOME`, `PATH`, `LANG`, …) plus endpoint, the one auth variable, model defaults, a push guard, `TMPDIR`; Profile `env` last |
| Push guard | `pushInsteadOf` rewrites that break ordinary pushes (accidents only) |
| Sessions | `session_root/*/<id>.jsonl`; sessions are stored per working directory, so a Worker's directory must not change |
| Probes | Shallow `GET base_url/`; deep `POST /v1/messages` with `max_tokens: 1`, no redirects, no proxy |

## Hermes (experimental)

**Headless Worker live check passed on 2026-11-04:** real Hermes `6b8fa991464d56680fc3ddd93755c8669412a44c` (a local build, not a published upstream commit), macOS / CPython 3.14.7, and a **fake loopback Provider**. All 32 checks passed, including actual MCP/reporting, both auth modes, interrupt, resume/model selection and crash recovery. This does not validate external Providers, interactive takeover or Router execution. Regular automated tests still use a fake ACP executable. [Setup and restrictions](setup.md#hermes-experimental-workers).

One private home owns one session's ACP process, gateway and MCP credentials:

```text
send(uuid) → local queue → input_started → session/prompt
                                            ├─ streamed text/tools → v1 events
                                            └─ response + persisted input marker
                                                 → compact_boundary? → isReplay → result
```

| Area | Hermes mapping / limit |
|---|---|
| Process | `hermes acp`, newline JSON-RPC v1; initialize → new/load → set_model (`custom:ssh:MODEL`) → dont_ask mode. Replay is muted. Handshake defaults to 120 s; `[harness.hermes].handshake_timeout` accepts finite positive seconds. |
| Runtime | Pre-provision one external Python 3.14 venv with `[acp,mcp,anthropic]`. Authoritative `HERMES_DISABLE_LAZY_INSTALLS=1` avoids bootstrap dependency installation; `HERMES_RUNTIME_DIR` names an adapter-owned tool store. Gateway eager warmup/environment probing are disabled; session/state directories remain independent. |
| Isolation | `SSHARNESS_HOME/hermes/homes/<our-id>/`; mapping in `hermes/sessions/<our-id>.json`. No inherited `HERMES_HOME` or gateway URL. Shared install `.env`, managed installs and unrecognized executable layouts are refused **before execution**. |
| Configuration | Private JSON-written `config.yaml` with `_config_version: 52`; ephemeral system prompt, in-place compaction, lazy installs and source/plugin update checks disabled. Key stays in the environment. Bearer custom endpoints use a fixed `key_cmd`, not a second auth header. |
| MCP | Configured from session creation; a stable per-home stdio launcher reads current `mcp.json`. The optional MCP SDK must be installed, and `tools.tool_search.enabled` is `off` so canonical `mcp__router__*` schemas are directly advertised. The live request had 31 tools, including 11 Router tools. ACP `mcpServers` remains empty: editor-injected servers get renamed and lose their transport on cold CLI resume. |
| Receipts | One prompt at a time. End/cancel responses alone do not prove admission: acknowledge only after finding the unique input marker in a persisted user row. Error responses receive no ACK. No claim of exactly-once tool effects after a crash. |
| Permissions | Automatically select allow_once/allow_always. `clarify-*` questions receive cancelled; an offered rejection is selected if no allow option exists. Unknown RPCs receive -32601. |
| Recovery | Load history is hidden. A crashed owner's `started` row becomes `unknown` only when the replacement owner initializes; inspect after load, resolve exact IDs with `/discard`, then recheck. The live fixture verifies that the same admission ends `terminal/interrupted`. No automatic replay inside the adapter; discard does not undo effects. |
| Reads | Read-only schema-32 SQLite, including compacted user rows for delivery checks. Transcript hides hidden/model-only and rewind-removed rows; SQL payload, scan-row, item and text bounds. Truncated structured payloads are skipped, not displayed as base64. Unknown history → empty fallback; unreadable saved state refuses resume rather than abandoning its ID. |
| Close | Snapshot/track descendant identities and kill their trees plus the ACP process group. Owned output-pipe transports prevent inherited stdout/stderr descriptors from blocking exit notification. Observed live processes were cleaned up; an unobserved detached child can still escape identity-based cleanup. |
| Diagnostics | Drain stderr separately from JSON-RPC; retain at most 40 lines / 16 KiB, omit overlong lines, redact configured secrets/authorization values/URL userinfo, and save the tail to the private Worker log. |
| Unsupported | Router execution, fork/peek, hooks/attach, nondefault tools/settings/add_dirs, saved-session Provider endpoint/identity/auth changes. Interactive `open` is not live-validated. No api_retry or background_tasks_changed events; agents must wait for background work themselves. |

`tests.test_hermes`, `tests.test_hermes_state`, `tests.test_hermes_diagnostics` and `tests.test_hermes_integration` cover fake protocol/state/lifecycle and application entry points. The separate, opt-in `tests/hermes_live.py` passed on the pinned build with fresh homes and a local fake Provider; its retained `verification.json` records cold/warm times and the actual observed assertions. It is not an OS network sandbox, an external-Provider certification, or a migration test for old per-home managed environments.

## Add an adapter

1. Implement the methods above in a trusted Python module. Don't reuse Claude Code's parser unless the wire format really matches.
2. `register("my-harness", impl)` at startup (or add a lazy registration in `get_harness`). Config never imports arbitrary modules.
3. Declare `[harness.my-harness]` and set `harness = "my-harness"` on a Profile.
4. Add tests for launch, replay acknowledgement, interrupt, resume, probes, transcript bounds and takeover. `python3 -m unittest tests.test_harness` runs the fake adapter through Router, Workers, CLI and web unchanged — copy it.

`init` still creates Claude Code Profiles. `doctor` uses adapter probes and an optional `installation_check(cfg, profile, timeout)` hook; a new Harness needs its own setup steps. Codex is tracked in [#2](https://github.com/ketchupz1999/super-super-harness/issues/2).
