# Guide

Daily use. Install first with [setup.md](setup.md); internals are in [design.md](design.md).

You talk to one **Router**. For real work it starts **Workers**: each is its own Claude Code session, in its own workspace and git branch. Progress, questions and results come back to you. Terminal, web and CLI all talk to the same Router.

## Commands

All are `bin/supersuperharness <command>`. Commands marked *offline* read the database read-only and work with the service stopped.

| Command | What it does |
|---|---|
| `serve` | Run the service (foreground) |
| `web [--open]` | Print or open a one-time login link for the web page |
| `chat` | Talk to the Router in the terminal |
| `say "TEXT"` | Send one message; read replies with `chat` or `history` |
| `history [--limit N]` | Recent terminal conversation *(offline)* |
| `status` | Router state, Worker slots, web, latest Workers *(offline if the service is down)* |
| `watch [WORKER ...] [--item CODE] [--until done\|change] [--timeout SECONDS] [--json] [--router]` | Wait for key Worker changes via server push; optionally include Router questions |
| `log WORKER [-f] [--full] [--raw]` | What a Worker did; `--full` reads its session transcript *(offline)* |
| `open WORKER` | Take a Worker over in this terminal; hand it back on exit |
| `attach` | Take the Router over in this terminal; hand it back on exit |
| `peek` | Open a forked copy of the Router session; the real one keeps running |
| `reminders` | Active reminders *(offline)* |
| `events [--limit N]` | Recent events and where they went *(offline)* |
| `observations [--since 1d] [--source github] [--limit N]` | Hypothetical actions, routing and unobserved surplus events *(offline)* |
| `emit ...` | Send an event to the bus ([design.md](design.md#event-bus)) |
| `stop` | Stop the service; Workers pause and resume on next start |
| `router-reset` | Give the Router a fresh session (Workers, items and history kept) |
| `upgrade`, `rollback` | Managed installs ([setup.md](setup.md#upgrade-and-rollback)) |

`WORKER` is a code name such as `Pixel` (case-insensitive), an internal id, or a unique part of its name. Code names have 1–24 letters, digits or hyphens and start with a letter. An exact code name takes priority over an internal id or a title match. `status` shows code name, status, Profile, name and progress; **Resume in terminal** copies `supersuperharness open Pixel`.

**Watching from another program:** `supersuperharness watch Pixel --router --timeout 600 --json` prints an initial snapshot, then one JSON object per change (plain text without `--json`). Changes include status, questions with options, final reports (first 500 characters), session handoffs and deferred completion; ordinary progress is excluded.

→ `watch` accepts exact code names or internal ids, case-insensitively, not title fragments. Without names it selects currently running, queued, dependency-waiting or awaiting-you Workers. Selection is fixed at connection time; `--item` intersects it with that item. Unknown references are errors; an empty selection returns immediately unless `--router` is set.

→ Default `--until done` exits **0** once every selected Worker is outside running/queued/dependency-waiting (including failed, stopped or dormant—not necessarily successful). A report alone does not mean completion. `--until change` exits on the first change, not the snapshot. An outstanding or new watched question exits **2** in either mode. `--router` adds Router questions regardless of item; it waits for a question when no Workers match, but does not delay completed Workers.

→ A positive, finite `--timeout` (at most 31,536,000 seconds / 365 days) is an overall deadline in seconds; expiry exits **124**. Missing service, disconnected/overflowed subscription or other connection errors exit **1** with diagnostics on stderr. There is no reconnect or history replay. A snapshot larger than the 1 MiB buffer limit is rejected; select fewer Workers. JSON stdout stays JSON-lines; watching never consumes messages destined for `chat`.

**`chat` keys:** Enter sends; a paste is one message; Ctrl-C twice or `/exit` quits. Leaving `chat` does not affect the service.

**Taking over (`open`, `attach`):** the service lets go of that session, opens it as interactive Claude Code here, and takes it back when you exit. The terminal needs the Provider's key variable if you use one. While the Router is attached, messages from other channels wait in the queue.

## Web page

`web --open` logs you in (link: one use, 5 minutes; session: 12 hours). It listens on `127.0.0.1` only.

| Page | What |
|---|---|
| **Chat** | Conversation with the Router, and the **Routing panel** beside it: who the Router is talking to right now. Questions appear as **Needs you** cards with answer buttons. |
| **Routing** (Expand) | The same picture full screen: You → Router → each active Worker, dependencies as dashed lines, one graph per item. Lines move only when a real event arrives. |
| **Workers** | All Workers grouped by item; filters Running / Needs you / Done; search. Reminders list with Cancel. |
| **Inspect** | Open any Worker: its session like a Claude Code conversation (live), report, branch, what it waits on; message it; Interrupt / Resume / Switch model / View diff / Stop; **Resume in terminal** copies the `open` command. |
| **Soul & Memory** | Edit the Router's `soul.md` (who it is) and `memory.md` (what it remembers about you). |
| **Models** | Read-only: Router primary/backup, every Profile, Provider reachability. |

Replies go back to the channel you asked from; notifications (results, reminders, alerts) go to all channels.

## Workers

| Status | Meaning |
|---|---|
| Queued | Waiting for a free slot (`max_workers`, default 10) |
| Running | Working |
| Needs you | Asked you a question |
| Waiting on deps | Waiting for other Workers to finish; uses no slot |
| Interrupted | Stopped mid-turn or taken over; can resume |
| Done | Finished with a report. Its session is kept, so follow-ups continue it |
| Dormant | Idle for 7 days (configurable): checkout and scratch files removed, branch and notes kept; send a message to wake it |
| Failed | Launch failed or retries ran out; can resume |
| Stopped | Ended for good; workspace and branch are kept |

Automatic behavior:

- Model or network errors are retried (3 times by default), then the Router is told.
- No activity for 30 minutes → the Router is told the Worker is stalled.
- Background work blocks completion while the latest task list is nonempty. Once it is explicitly empty and the root turn succeeds, missing notification results get a bounded wait: `[limits] background_grace_seconds` (positive integer, default 20), measured from the later of those two events. A new active turn or undelivered input blocks this fallback; ordinary host replies can finish after the same grace. At the next health check after the grace expires, remaining notifications are treated as absorbed inline. This prevents a permanent Running state when the Harness hides notification attachments, but an invisible notification delayed beyond the grace can be lost; increase the setting if needed. If an idle Worker eligible to finish remains Running beyond `stall_minutes`, `worker.completion_stuck` is logged as an error and warns the Router.
- After a restart, running Workers resume and are told the current `git status`.
- Workers are shown by their stored code names, not rotating names derived from task numbers. The Router can choose or rename a code name; old names are never reused, and the Worker's internal identity stays unchanged.
- Each Worker belongs to an item (default: “杂事”). Its own versioned `notes.md` and the item's notes are included on every launch.
- After `[limits] dormant_days` idle days (default 7), an idle Worker becomes Dormant. Dirty git worktrees are kept rather than forcibly deleted.

## Items (事项)

An item is one matter you care about. The Router hangs Workers on items, so it keeps a short table of matters, and a follow-up goes to the Worker that already has the context. Manage items by talking ("start an item for the mic input", "park the travel item").

| Field | Values |
|---|---|
| Kind | `delivery` (a feature, a fix; ends delivered) or `standing` (an ongoing area; only closed) |
| Status | active, confirm (waiting for you), delivered, parked, closed |
| Permission | `read` (analyze only) or `act` (may do the work). An instruction, **not enforced** |
| Progress, next, follow-up date, notes | Progress is updated when a Worker finishes; notes live in `items/<code>/notes.md` |

**Dependencies:** "when these two are done, merge their results" starts a Worker that waits. It starts by itself once every upstream is done, with their reports, branches and commits in its brief. If an upstream fails, nothing is cancelled: the Router is told.

## Reminders

Ask in plain words: "remind me at 9 tomorrow…", "every weekday at 9, summarize…". The Router confirms the exact time.

| Type | At the time |
|---|---|
| `notify` | A reminder message in terminal and web, plus a macOS notification. No model needed. |
| `run` | An instruction to the Router, which may hand the work to a Worker. |

One-time reminders take an ISO date and time (local unless an offset is given); repeating ones take a 5-field cron in the machine's timezone. After sleep or downtime only the latest missed occurrence fires. List with `reminders` or the Workers page; cancel by telling the Router or with **Cancel**.

## Observations

[Enable GitHub notifications](setup.md#github-notifications-optional), then review without being notified:

```sh
bin/supersuperharness observations --since 7d --source github
```

The offline list shows time, event, browser link, `would_do` and `route`; `--since` accepts `m`, `h`, `d` (default 1 day; default limit 200). Web **System → Models → Observations** shows the last 24 hours, refreshing with the page. `超额未观察` (web: “Not observed (hourly limit)”) means the hourly quota was exhausted; “No note yet” may mean queued, failed or a turn that ended without a note. Neither is silently presented as reviewed.

→ This is a rehearsal: service MCP writes and user replies are blocked only in observation-only turns. Built-in tools rely on the prompt, **not a security boundary**. `used_builtin_tools` lists every reported tool name except this service's known Router MCP tools (including read-only tools and `observe_note`, bare or `mcp__router__`-prefixed). Native reads, NotebookEdit, Agent/Task, Skill, other MCP services, Hermes tools and unknown names are included. `mixed_turn: true` marks shared user/Worker input, where tool use is not automatically a violation. See [enforcement details](design.md#event-bus). In the first few days, compare proposed routes with your own choices, check missing notes, quota overflow and built-in-tool flags before considering broader permissions.

## Troubleshooting

| You see | Do |
|---|---|
| `后台服务没有运行` | Start `serve`. Offline commands still work. |
| `Private configuration is missing` | Run `init`. |
| `Configuration is unavailable or invalid` | Check `profiles.toml`; a table declared twice is the usual cause. |
| Router **paused** banner | Both models failed, or the Router crashed repeatedly. Messages stay queued; it retries every 15 s. Fix the Provider or key; `router-reset` if the session itself is broken. |
| `X 现在连不上，我先用 Y 顶着` | Automatic failover to backup. You will be asked before switching back. |
| `attach` / `open` fails to start | The terminal lacks the key variable or `claude` is not on `PATH`; or Claude Code is waiting on a first-run prompt — finish it once, then retry. |
| `link expired or already used` | Run `web` again. |
| Web unreachable | The port is taken. Change `"port"` in `["web"]` and restart. |
| A Worker got a message twice | Follow-ups are at-least-once. |

## Security

A single-user tool for a machine you trust.

- **Workers have full local permissions** (Claude Code with `--dangerously-skip-permissions`, as your user, no sandbox).
- **Ordinary `git push` from a Worker is blocked** to prevent accidents; a deliberate push can still get through.
- **Item permissions and Router instructions are not enforced**; they tell the model what to do.
- **Event and report text is marked as data** before a model sees it. This reduces prompt injection; it does not remove it.
- **The web is local only** (`127.0.0.1`, same-origin, no CORS). Cookies are not isolated by port, so run only trusted services on the same host.
- **Keys** stay in 0600 files or environment variables; they are never printed, logged or stored in the database.

## Known limits

- One user, one machine. Claude Code is the only Harness; no cloud Workers.
- A session belongs to its Harness: switching a Worker or the Router to a different Harness is refused.
- Diffs show committed changes only.
- The web cannot send images to the Router; the UI is English only; Routing shows the present, not history.
- Presets and `doctor` are not end-to-end tested with real keys.
