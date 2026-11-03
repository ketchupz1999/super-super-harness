# Using Super Super Harness

This is the daily-use reference: how to talk to the Router, watch and steer Workers in the terminal and the web page, use items and reminders, answer questions, and what to do when something looks stuck. Installation is in [setup.md](setup.md); internals are in [design.md](design.md).

You talk to one **Router**. It starts background **Workers** for real work, each in its own workspace and git branch, and reports progress, questions and results back to you. Every entry point below talks to the same Router through the same background service.

## Contents

1. [Entry points](#entry-points)
2. [Terminal commands in detail](#terminal-commands-in-detail)
3. [The web page](#the-web-page)
4. [Workers and tasks](#workers-and-tasks)
5. [Items (事项) and dependencies](#items-事项-and-dependencies)
6. [Reminders](#reminders)
7. [Questions and confirmations](#questions-and-confirmations)
8. [Troubleshooting](#troubleshooting)
9. [Security boundaries](#security-boundaries)
10. [Known limits](#known-limits)
11. [Under construction](#under-construction)

## Entry points

All commands are `bin/supersuperharness <command>`. "Needs service" means the background service (`serve`) must be running.

| Command | Needs service | What it does |
|---|---|---|
| `serve` | n/a | Runs the service in the foreground (see [setup.md](setup.md#quick-start)). |
| `web [--open]` | yes | Prints a one-time local login link, or opens it in the browser. |
| `chat` | yes | Interactive terminal conversation with the Router. |
| `say TEXT` | yes | Sends one message and prints its message ID. |
| `history [--limit N]` | no | Prints recent terminal conversation (default 8 messages). |
| `status` | partly | Router mode and Profile, Worker slots, web listener, and the last 20 tasks. |
| `log TASK [--full] [--kinds K] [--raw] [-f]` | no | What a task did. |
| `attach` | yes | Takes the Router over in this terminal (interactive Claude Code), then hands it back. |
| `peek` | no | Opens a forked copy of the Router session; the background Router keeps running. |
| `open TASK` | yes | Takes one Worker over in this terminal, then hands it back. |
| `reminders` | no | Lists active reminders. |
| `events [--limit N]` | no | Lists recent bus events and where they were routed (default 20, at most 10,000). |
| `emit ...` | yes | Sends an event to the bus as the local owner; see [design.md](design.md#event-bus). |
| `stop` | yes | Stops the service. Running Workers pause and continue on the next start. |
| `router-reset` | yes | Gives the Router a fresh session. |
| `upgrade`, `rollback` | yes | Managed installs; see [setup.md](setup.md#upgrade-and-rollback-of-a-running-install). |
| `wait`, `stop-check` | yes | Used internally by `attach`; not for direct use. |

Commands marked "no" open the database read-only (`mode=ro`), work with the service stopped, and never migrate an older database.

## Terminal commands in detail

### `chat`

Connects, prints `已连接 Router · <model> · Worker <live>/<max>`, then the last 8 messages, and keeps listening for replies and notifications.

| Key / input | Effect |
|---|---|
| Enter | Send the line. |
| Ctrl-A / Ctrl-E, Home / End, ← / → | Move the cursor. |
| Ctrl-U, Ctrl-W, Backspace, Delete | Delete to start of line, previous word, previous character, next character. |
| Ctrl-C with text | Clears the line. |
| Ctrl-C on an empty line | First press warns `再按一次 Ctrl-C 退出`; a second press within 2 s exits. |
| Ctrl-D on an empty line, `/exit`, `/quit` | Exit. Lines already sent still finish sending. |
| Paste | Bracketed paste turns newlines into spaces, so a pasted block is one message. |

Leaving `chat` never affects the service. If the connection drops it prints `连接断开。` and exits 1; a failed send is reported and **not** retried automatically. When stdin is not a terminal (a pipe), each non-empty line is sent and `chat` exits after the last send completes. `NO_COLOR` or `TERM=dumb` disables color.

### `say` and `history`

`say "text"` submits one message on the terminal channel and prints its ID. Replies arrive on the terminal channel: watch them with `chat`, or read them later with `history`.

`history --limit N` prints timestamp, role, kind and text of the last N terminal messages. It reads the database read-only, works with the service stopped, and exits cleanly when piped into `head`. If the database cannot be read it says so without printing diagnostics.

### `status`

Prints the Router Profile and mode (`running`, `paused` or `attached`), running Workers against `max_workers`, and the web listener (`127.0.0.1:<port>`, `未启用` when disabled, or the port error). Then it lists the 20 newest tasks with ID, status, Profile, name and progress. If the service is down it prints `后台服务没有运行` and still lists tasks from the database.

### `log`

`TASK` is a task ID (`t12`) or a unique substring of its name; an ambiguous name lists up to six matches.

| Form | Shows |
|---|---|
| `log TASK` | Directory, branch, why this model was chosen, and the application event log. |
| `log TASK -f` | Same, then follows new events every second. |
| `log TASK --full` | The Harness transcript: `received`, `said`, `thinking`, `tool_call`, `tool_result`, each shortened to 300 characters. |
| `--kinds said,tool_call` | Only these kinds. |
| `--raw` | Do not shorten. `log TASK --full --raw` is the most complete history available; what is retained depends on the Harness. |

If the task's Profile or Harness can no longer be resolved, `--full` says so and shows application events instead.

### `attach`

Detaches the Router from the background service and opens it in this terminal as an interactive Claude Code session on the same conversation, with the Router's tools. While attached:

- The Router's mode is `attached`. The web shows `Router is attached in a terminal · Messages stay in the queue`.
- Messages from other channels and Worker results do **not** arrive by themselves. The Router is told to run `supersuperharness wait` in the background after starting Workers, and to call `inbox_read` when it returns.
- A Stop hook (`stop-check`) blocks ending a turn while unread messages are queued, or while Workers are running and nothing is waiting for them. It blocks at most three times in a row.
- Health probes and failover are suspended.

When you exit Claude Code the Router is handed back to the service (`Router 已交回后台。`). Requirements: the Router must already have a session (send it one message first), and **this terminal** must have the Provider's credential environment variable if you use one. See [Troubleshooting](#troubleshooting) if hand-back fails.

### `peek`

Opens a **fork** of the Router's session (`--fork-session`) in this terminal. The background Router keeps running and is not affected; nothing you do in the fork is seen by it. The fork has no Router tools. Works with the service stopped, as long as a Router session exists.

### `open TASK`

Holds the task (its process is stopped; a running or queued task becomes interrupted), opens its session interactively in the task's working directory, and releases it when you exit, printing `<id> 已交还，状态：<status>。要继续就告诉 Router。`. Nothing restarts a held task until you exit. The interactive session has no Router tools; to have the Worker continue afterwards, tell the Router (or use **More → Resume** in the web's [Inspect](#inspect)). Inspect's **Resume in terminal** copies this command for you.

### `stop` and `router-reset`

`stop` asks the service to shut down. It first tells you `后台服务要停了，N 个任务会暂停，恢复后接着跑。`; recorded task states are kept and the next `serve` resumes them. On the next start you also get a notice of how long the service was down and how many tasks continued.

`router-reset` kills the Router process, forgets its session and any pending Router question, and starts a fresh Router session. Tasks, items and history are kept.

## The web page

```sh
bin/supersuperharness web          # print a one-time login link
bin/supersuperharness web --open   # open it in the system browser
```

`web --open` prints `已在浏览器打开` when the browser accepts the request, never the (possibly consumed) link. If opening fails it explains how to get a new link without printing the old one. Without `--open` it prints a one-use URL.

| Property | Behavior |
|---|---|
| Listener | `127.0.0.1` only, on the configured port (default 8765). |
| Login link | One use, expires after 5 minutes. A new link invalidates an older unused link, not existing sessions. |
| Session | Signed cookie, 12 hours, `HttpOnly; SameSite=Strict; Path=/`. Login redirects to a token-free URL. Valid sessions survive a service restart. |
| Login expired | The page shows a prompt to run `supersuperharness web --open` on the service host (over an SSH tunnel: run `supersuperharness web` there and open the new link in the same browser). Returning to the tab reconnects automatically; no refresh needed. The page never fetches a login ticket itself. |
| Disabled / port busy | `web.port = 0` disables it. An occupied port leaves the web unavailable but the Router and Workers keep running; `status` shows the error. |

The interface text is English. What you, the Router and Workers write is shown as written. Pages are at most about 1,280 px wide, and the conversation column about 1,000 px.

### Layout

| Area | Contents |
|---|---|
| Left rail | **Chat** and **Workers** (with a count of Workers waiting for you), then **Soul & Memory** and **Models**; your name and avatar; light/dark theme toggle (remembered in the browser). At 1,080 px or less the rail shows icons only. |
| Header | The page title; connection indicator `This Mac · online / reconnecting / login required`; **N Needs you** (Workers waiting for you, plus one if the Router asked a question; jumps to the first waiting card); **N Running** (opens Workers on the Running filter); **View Workers**. |
| Banner | Shown when the Router is paused, attached in a terminal, or switching models. |

The connection indicator describes the browser's connection to the service, not Provider health. There is no Developer page (its useful part is now **Models**) and no Usage entry (see [Under construction](#under-construction)).

### Workers in the page

Every Worker looks the same wherever it appears: progress lines, question cards, Routing cards, the Workers table and Inspect.

- **Name.** The task number picks a name from a fixed list of 24: Nova, Pixel, Echo, Atlas, Juno, Orbit, Sage, Milo, Iris, Bolt, Luna, Rex, Kiko, Zed, Ivy, Taro, Mochi, Finn, Nori, Pip, Remy, Skye, Toto, Wren. `t1` is Nova, `t2` Pixel, and `t25` is Nova again, so a name is stable for life but not unique; the small line under it, `<task name> · <task id>`, is. Commands and the Router still use task IDs.
- **Model and Harness.** A model tag (the Profile's model, for example `opus 5.5`) and a Harness label (`Claude Code`) are always shown. The Harness is the one recorded for the task's session, otherwise its Profile's. If the private icon `harness-claude-code.png` exists, the Claude Code label carries it; otherwise the label is text only.
- **Avatar.** Chosen by model family: Harness `codex` → codex; Harness `muse` or a model name containing `muse` → muse; a model name containing `claude`, `opus`, `sonnet` or `haiku` → claude; containing `gpt`, `astra` or `sol` → chatgpt. The animated characters come from your private directory (see [setup.md](setup.md#worker-characters)); none ship with the project. Without a matching character, or if its image is missing or malformed, the Worker is a small built-in SVG robot whose flag takes the item's colour.
- **State.** Running plays the working animation (the robot blinks and types). Needs you plays waiting with a `?` badge. A Worker that completed in the last 8 seconds plays complete once with a `✓` badge, then rests on idle. Queued and Waiting on deps are idle at 45 % opacity. Failed shows a `!` badge. Everything else is idle. **Pause motion** (on the Router card) and the system "reduce motion" setting hold sprite characters on their first idle frame; "reduce motion" also stops the robot's blinking and typing.
- **Progress sentence.** The Worker's own latest progress report for its current run; otherwise its latest tool call in words: `Bash` → Running a command, `Read` → Reading `<file>`, `Edit` / `Write` / `MultiEdit` / `NotebookEdit` → Editing `<file>`, `Agent` / `Task` → Started a sub-agent, anything else → Using `<tool>`; `Starting` for a Worker whose process just launched. Raw tool input (JSON) is never shown.

Web status names: Running, Needs you, Waiting on deps, Queued, Interrupted, Done, Failed, Stopped (see [Workers and tasks](#workers-and-tasks)).

### Chat

The page is split: the conversation on the left, the **Routing panel** on the right.

- The Router conversation, newest at the bottom. **Earlier messages** pages back; **Back to latest** returns. A reader scrolled up keeps their position; a reader at the bottom follows new replies.
- Router messages are labelled **Router** with the Suber mark (Worker notices carry their own title). Your messages show your name.
- Composer: Enter sends, Shift+Enter inserts a newline. A failed send keeps your text and shows **Retry**; retrying the same request cannot create a duplicate Router message.
- Router replies render a small Markdown subset: headings, bold/italic, inline and fenced code, lists, blockquotes, tables and HTTP/HTTPS links. Raw HTML, images and other link schemes are not rendered. Your messages stay plain text.
- Below a Router reply, `N tool calls ▸` expands the Router's tool calls for that turn as plain sentences. Chips link to the Workers it touched and open Inspect.
- A Router question is a **Router · Needs you** card. Answer in the composer (**Answer below** focuses it) or click a suggested answer.
- A Worker question is a **Needs you** card: the question, the Worker's progress sentence, the Worker (avatar, name, `<task name> · <task id>`, item), **Inspect →**, suggested answers and a reply box. **Send answer** sends only to that Worker.
- Each running Worker has a progress line under the conversation: `[avatar] Pixel · opus 5.5 · Claude Code — Editing index.html`, the small `<task name> · <task id>` line, and **Inspect →**.

### Routing panel

The panel shows who the Router is talking to right now. It is always on the Chat page and is the first place to look.

| Part | Contents |
|---|---|
| Header | `Routing`, `N active` or `idle`, **Expand Routing** (opens the [Routing view](#routing-view)) and a collapse toggle. |
| Router card | Suber, the Router's mascot, in the Router's real state (idle, thinking, working, done, error, waiting or rest); `Router`; the Router's current model; a state label (Idle, Thinking, Working, Waiting for you, Got a result, Needs attention, Resting), or `Router idle` when no Worker is active; **Pause motion**. |
| Worker cards | One per active Worker (Running, Needs you, Waiting on deps, Queued), in list order, each joined to the Router by a line. A card shows avatar, name, status, the time since its latest Worker process started, model and Harness, the latest exchange with its time, and `<task name> · <task id>`. Click a card to open Inspect. |
| Finished today | `N done today · M failed or stopped`, collapsed: Workers that ended since local midnight, each opening Inspect. |

The latest exchange on a card is one of: `Needs you: <question>`; `Waiting on Nova, Echo` (open upstreams by name) or `Dependencies met, starting soon`; the progress sentence for a running Worker; otherwise `dispatched`, `message sent`, `asked you`, `reported back` or the latest progress text. Interrupted Workers have no card; find them on the Workers page.

Motion shows real traffic only. An envelope flies from the Router to a Worker when work is dispatched (a new Worker or a `task_send`). Marks fly back for progress (a dot), a question (`?`) and completion (`✓`); a completed Worker's card stays about 2 seconds so the `✓` can land. A `▶` travels a lane when a Worker is released by its dependencies. Suber briefly reacts on the Router card when a report arrives. Motion comes only from updates arriving on the live stream, never from a timer. Initial load, reconnects, history browsing and updates found only by background polling are silent. Motion stops while disconnected, while the tab is hidden and while the panel is collapsed. With reduced motion, the line and card are briefly highlighted instead of a moving mark.

The panel is 380 px wide (320 px at 1,080 px or less). Collapsed, only its header remains. At 760 px or less it sits above the conversation (at most 45 % of the height) and starts collapsed. Your last collapse choice is remembered in the browser.

### Routing view

**Expand Routing** opens the same picture in the full content area. It is the same live data, not a separate history. **← Back to Chat** returns; the rail keeps Chat highlighted.

- **You → Router → one lane per active Worker**, with the same cards. The Router card uses your configured `router_name`. A Worker waiting on another active Worker is joined to it by a dashed side line.
- The caption reads `N active · M finished today · lines move only when a new event arrives`, or `Reconnecting · Motion paused`, and says when the loaded data was truncated.
- Workers waiting for you are listed as `<name> · Answer in chat` buttons. The finished-today list follows.
- A legend explains the marks: envelope = work sent, dot = progress, `?` = question, `✓` = done, `▶` = released after its dependencies.
- **Item structure · dependencies by item** (collapsed) draws one graph per item: You, the Router, and the item's Workers arranged by dependency, with an arrow per `wait_for`, solid once met. Workers outside any item form an `Unassigned` group. An upstream from another item appears as `External upstream`; a member outside the loaded list shows `Not loaded` with its real ID. A cycle is reported, and its Workers are listed flat. Click a Worker to open Inspect.

### Workers

All Workers, grouped by item.

- Filters, with counts over all Workers: **Running** (running, queued, waiting on deps, interrupted), **Needs you** (waiting), **Done** (completed, failed, stopped). Running is the default.
- Search matches the Worker name, task name, task ID, progress, question, and item code or name.
- Groups follow the item list (open items first, newest first); `Unassigned` comes last. Each group header shows the item and its count.
- Each row shows avatar, name, `<task name> · <task id>`, the progress sentence, the question if it is waiting, model and Harness, status, last update, and **Inspect →** (plus **Answer**, which jumps to its card in Chat, when it needs you).
- Up to 1,000 Workers are loaded, unfinished first; a notice says when the list is truncated, and counts still include everything.

Below the table, **Reminders** lists active reminders with name, next local time and type, each with **Cancel** (asks for confirmation). It updates live.

### Inspect

Inspect is a Worker's session, read like a Claude Code conversation. It replaces the old detail drawer. It opens from any Worker: a Routing card, a progress line, a question card, a Workers row, a chip, a finished row, or an upstream or downstream link. It is a 480 px panel on the right; at 1,080 px or less it covers the page and keeps keyboard focus inside. Escape or **✕** closes it.

**Top bar**

| Control | Effect |
|---|---|
| Identity | Avatar, name, `<task name> · <task id>`, model, Harness, status, item and `This run · <time>` since the latest Worker process started. |
| **Session: `<id>`** | Copies the Harness session ID. Before the first launch it reads `Not started`, and both session buttons are disabled. |
| **Resume in terminal** | Copies `supersuperharness open <task id>` and says *Paste in a terminal to resume this Worker's Claude Code session. Exit to hand it back.* If the clipboard is unavailable it shows the text to copy. |
| **More** → Interrupt | Stops the current turn; progress is kept. Shown while running. |
| **More** → Resume | Continues an interrupted or failed Worker. |
| **More** → Switch model | Moves the Worker to another Profile with the same Harness, keeping its session. |
| **More** → View diff | Committed changes on the task branch since its fork point, shown below the conversation. **Uncommitted files are not shown.** Up to 256 KiB. Shown when the task has a branch. |
| **More** → Stop | Ends the Worker for good, after a confirmation; its workspace and branch are kept. Shown for running, queued, waiting, waiting-on-deps and interrupted Workers. |

**Resume in terminal** deliberately does not give `claude --resume <id>`. While the service still manages the session, resuming it directly would split the conversation; `open` first makes the service let go, then resumes it, and hands it back when you exit (see [`open TASK`](#open-task)).

**Overview**, above the conversation: the progress sentence; the pending question with its suggested answers and **Answer in chat**; the final report, rendered as Markdown with only its first paragraph shown until you open **Full report**; and **Context** (collapsed): branch, brief, **Waits on** and **Unblocks** (each a link to that Worker), and **View route**, which opens the Routing view.

**Conversation.** Entries in time order, each tagged `USER` (what the Worker received), `AGENT` (what it said), `THINK`, `TOOL`, `RESULT`, `EVENT` (application events, used when no transcript exists) or `ERROR`. Messages and replies render as Markdown. Tool calls, results, thinking and events are collapsed to one line; a tool call reads as its progress sentence, and expanding it shows only its command, file path, path, description, query or pattern. The header shows `<shown> / <total> entries`.

- **Live tail.** While Inspect is open the conversation refreshes every 3 seconds and keeps following the end if you are near the bottom.
- **Earlier entries** loads 100 older entries at a time from a pinned snapshot and pauses the live tail (`History · live paused`); **Latest** goes back to the live tail.
- Bounds: the transcript tail is read up to 2 MiB and the last 500 visible entries are kept; each entry shows at most 16,000 characters; truncation is marked (`Tail truncated`). Snapshots expire 5 minutes after creation, and at most four exist at once. Empty speech and known Harness control notices are hidden. For full history use `log TASK --full --raw`.

**Message box.** `Message <name>` sends a follow-up to this Worker (Enter or **Send**). Drafts are kept per Worker while you switch. Follow-ups are at-least-once: if the acknowledgement is lost and you resend, the Worker may receive the text twice. A failed follow-up keeps your draft.

### Soul & Memory

Two Router documents, edited in the page: **Soul**, who the Router is and how it works (`SSHARNESS_HOME/router/soul.md`, added to the Router's system prompt from its next start), and **Memory**, what it remembers about you (`SSHARNESS_HOME/router/memory.md`, imported by the Router's `CLAUDE.md` and updated by the Router through `memory_read` / `memory_write`).

Pick a tab, edit, **Save**; **Reload** fetches the latest file (it asks before discarding an unsaved draft). Each file is UTF-8, at most 16,384 bytes. A save carries the version you loaded: if the file changed since then, nothing is written, your draft is kept, and the page asks you to copy it, Reload, merge and Save again. Drafts survive switching pages; closing or reloading the tab with an unsaved draft asks first.

### Models

Read-only: which models the Router and Workers use.

- **Router**: Primary and Backup (model and Profile), In use (Primary, Backup or another Profile) and status (Running, Paused, Attached in a terminal or Switching).
- **Worker Profiles**: one row per Profile with name, Harness, model, Provider, whether it is the default Worker Profile, and Provider status (Reachable / Unreachable). The status is a shallow connectivity probe per Provider and Harness, cached 30 seconds, not a model request.
- The page refreshes every 3 seconds while open. It never shows keys, tokens, URLs, local paths, memory or outbox details. The running version is in the footer.

### Channels

Terminal and web talk to the same Router. An ordinary reply goes back to the channel the request came from; notifications (Worker results, reminders, alerts) go to all channels. A closed browser does not consume terminal notifications and does not build up a failed web backlog. Several tabs can be open at once; each reconciles against the stored conversation.

## Workers and tasks

The Router starts a Worker with a name, a brief, optionally a repository, a Profile and a reason for that Profile. Each task gets `SSHARNESS_HOME/workspaces/<id>-<name>/`. With a repository, the Worker works in a git worktree on branch `task/<id>`; without one, in an empty directory. A task keeps one identity and status across any number of Worker processes.

| Status | Web | Router label | Meaning |
|---|---|---|---|
| `queued` | Queued | 排队 | Waiting for a free slot (`max_workers`, default 10). |
| `running` | Running | 运行中 | A Worker process is working. |
| `waiting` | Needs you | 等你 | The Worker asked you a question. |
| `waiting_deps` | Waiting on deps | 等待依赖 | Waiting for upstream tasks; takes no slot. |
| `interrupted` | Interrupted | 已打断 | Interrupted or taken over; resume to continue. |
| `completed` | Done | 完成 | Finished with a report. Its session is kept for follow-ups (shown to the Router as 空闲, idle, under an item). |
| `failed` | Failed | 失败 | Launch failed, or retries were exhausted. Resume is possible. |
| `stopped` | Stopped | 已停止 | Ended for good. |

Automatic behavior you will notice:

- A model or network error, or a process exit without a result, is retried up to `worker_retries` (3) times with growing backoff before the task fails and the Router is told.
- A Worker with no activity for `stall_minutes` (30) is reported to the Router as stalled.
- After a restart, running and queued tasks are resumed. The previous process tree is killed first, and a resumed Worker is told the current `git status` so it checks before repeating work.
- When a Worker finishes, the Router receives its report (the first 600 characters, with a pointer to read the rest).

## Items (事项) and dependencies

An **item** is one matter you care about. The Router attaches Workers to items, so it sees a short table of matters rather than a long list of Workers, and a further request on the same matter goes to the Worker that already has the context. You manage items by talking to the Router ("start an item for the mic input work", "park the travel item").

| Kind | Example | Ends |
|---|---|---|
| `delivery` | a feature, a fix, an investigation | `delivered`, or closed when given up |
| `standing` | development, quant, travel | never delivered; only closed |

| Status | Meaning |
|---|---|
| `active` 进行中 | Being worked on. |
| `confirm` 待你确认 | Waiting for your confirmation. |
| `delivered` 已交付 | Done (delivery items only). |
| `parked` 搁置 | On hold. |
| `closed` 已关闭 | Closed; its Workers stop receiving the item's events. |

Each item has a short code (lowercase letters and digits joined by single `-`, at most 24 characters, starting with a letter; the system appends `-<n>`, so `mic-input` becomes `mic-input-3`), a display name (any language, up to 80 characters), a permission, progress, next step, an optional follow-up date, and notes.

- **Permission** `read` means "query and analyze only"; `act` means the Worker may carry out the work. It is written at the top of every attached Worker's brief. **It is an instruction, not an enforced restriction.**
- **Progress**: when an attached Worker completes, its conclusion becomes the item's progress as `<date> <task id> <name>：<conclusion>`.
- **Follow-up**: a `YYYY-MM-DD` date; an open item past that date is overdue and listed first to the Router, marked 已过期.
- **Notes** live in `SSHARNESS_HOME/items/<code>/notes.md`. Workers can read their own item and update its next step and notes, nothing else.

### Dependencies

The Router can start a task that waits for others: "when t5 and t6 are done, merge their results". Such a task is `waiting_deps`: not started and using no slot. When every upstream task has completed, it starts by itself, and its brief gets each upstream's conclusion, branch and commit. An upstream that completed once counts, even if it gets more work later.

If an upstream fails or is stopped first, nothing is cancelled: the Router is told the task is blocked and can resume the upstream, change what the task waits for, or stop it. A message sent to a waiting task is kept and delivered after its brief. Dependencies and attachments survive restarts. A dependency cycle is refused.

In the web page, the Workers table is grouped by item, and every Worker carries an item tag. A Worker waiting on dependencies shows as **Waiting on deps**, and its Routing card says `Waiting on <names>`. When both are active, a dashed line joins it to its upstream. The [Routing view](#routing-view) has **Item structure**, one graph per item with Workers arranged by dependency and an arrow per `wait_for`. [Inspect](#inspect) lists a Worker's upstream (**Waits on**) and downstream (**Unblocks**) under **Context**. The same data is in the API (`/api/items`, task `item`, `wait_for`, `upstream`, `downstream`).

## Reminders

Ask the Router in ordinary language: "Remind me tomorrow at 9 to review the project notes", or "Every weekday at 9, summarize the project status". It confirms the absolute date, time and local timezone, and asks one short question if a name or time is ambiguous.

| Type | At the scheduled time |
|---|---|
| `notify` | A distinct Reminder message to the terminal and web, plus a macOS system notification attempt. No model is needed, so it works even when the Router is paused or switching. |
| `run` | Queues a durable instruction marked with the reminder ID, name and original scheduled time. The Router handles it when available and may delegate the work to a Worker. |

Only the Router can add, list and cancel reminders; Workers cannot. **No reminder is permission to merge, push or deploy** beyond what you asked for.

### Inspect and cancel

- Web: the **Reminders** list on the Workers page, with **Cancel**.
- Terminal: `bin/supersuperharness reminders` prints `r<id> <type> <next local time> <name> [<cron>]`, with the service running or stopped.
- Tell the Router "cancel the project notes reminder"; it matches by name or content, so you do not need IDs.

Cancellation prevents future occurrences. An occurrence that was already queued, or work that already started, is kept.

### Time rules

Each reminder has exactly one schedule:

- **One time (`at`)**: an ISO date **and** time, such as `2027-01-12T09:00:00`. Without an offset it means local machine time; with an offset it is an absolute instant shown in local time. It must be in the future. Nonexistent (DST gap) and ambiguous (DST fold) local times are rejected; give an explicit offset.
- **Repeating (`cron`)**: five numeric fields, minute, hour, day of month, month, day of week. `*`, comma lists, inclusive ranges and `/step` are supported; Sunday is 0 or 7; for example `0 9 * * 1-5`. Names, seconds and macros are not accepted. When both day fields are restricted they are ORed (Vixie cron); a wildcard day field means AND. A schedule with no occurrence within eight years is rejected.
- Cron uses the machine's timezone (`TZ` if set, otherwise `/etc/localtime`), not UTC. IANA names, zone-file paths and POSIX strings such as `UTC0` or `CST-8` work. A repeated DST hour fires once, at its first occurrence. A time inside a skipped hour fires once at the first valid instant after the gap (02:30 becomes 03:00), and several matches inside one gap collapse into that one. The next occurrence is stored as an instant and later ones use the timezone current at the time; avoid changing the host timezone if you need fixed-zone behavior.

### Catch-up and delivery

- Checks start after the service (including the Router) has started, then run every second.
- After sleep or downtime a one-time reminder fires once; a cron reminder fires **only its latest missed occurrence**, then continues after now. There is never a backlog burst. Every delivered text includes the original planned time. Clock rollback cannot replay a recorded occurrence.
- The macOS notification is best effort and at most once: a crash at the wrong moment can lose the banner, and it is never replayed. Disabled notification permission, another platform or a failed call do not affect the message in the terminal and web. No browser or terminal needs to be open for the banner. Reminder text can appear on the lock screen depending on macOS settings; avoid sensitive content.
- A `run` reminder is processed like any Router input; arbitrary external side effects of the resulting work are not guaranteed exactly once.

## Questions and confirmations

- A Worker that needs a decision asks and ends its turn; its task becomes `waiting`. It appears as a **Needs you** card in Chat, in the header and rail counts, on its Routing card (`Needs you: <question>`, with a `?` badge) and at the top of its Inspect panel. Your answer goes only to that Worker, which then continues.
- The Router asks with a **Router · Needs you** card. The mascot switches to `waiting` until your next message arrives (from any channel). Worker events and scheduled `run` inputs do not count as answers.
- Questions can carry up to 8 short suggested answers (each one line, at most 80 characters). The page draws them as buttons on the question card and in Inspect; clicking one sends exactly that text as your answer. Free text still works. If the question changed meanwhile, the page refuses and asks you to check the current one.
- The Router also tells you about Worker results, failures, stalls and blocked dependencies. These are reports to you, not decisions it makes on your behalf.

## Troubleshooting

| Symptom | Cause and what to do |
|---|---|
| `后台服务没有运行` | Start `serve`. Read-only commands still work. |
| `Private configuration is missing; run supersuperharness init ...` | Run `init`, or `init --migrate-from` for an existing installation ([setup.md](setup.md#upgrade-migration)). |
| `Configuration is unavailable or invalid ...` | Check the private TOML (a duplicated table is a common cause). Diagnostics are deliberately suppressed. |
| Router **paused** (banner `Router paused`, mascot in error pose, an alert message) | The Router process crashed more than 3 times in a row, or both primary and backup failed a health check. Messages stay queued. The service re-probes every 15 s and resumes with the first healthy Profile among current, primary and backup, then says `Router 恢复了，用的是 ...`. Fix the Provider or credential; `router-reset` gives it a fresh session if the session itself is the problem. |
| `X 现在连不上，我先用 Y 顶着` | Automatic failover to the backup. Workers are unaffected. When primary recovers you are asked whether to switch back. |
| Banner `Router is attached in a terminal` but nobody is attached | An `attach` could not hand back (`交回后台失败`). Run `status`; once the service answers, run `attach` again and exit normally. |
| `Router 还没有会话` on `attach` | Send the Router one message first. |
| `接管启动失败` / `接管未成功（退出码 N）` | Interactive Claude Code did not start or exited with an error. Check that this terminal has the credential variable and that `claude` is on `PATH`. Interactive Claude Code may also stop at its own first-run or confirmation prompts (for example onboarding, folder trust, or confirming the bypass-permissions mode) for the Router or workspace directory: complete them once in that terminal, then retry `attach` / `open`. The background Router is restored either way. |
| `link expired or already used` | Run `web` again; links are single-use and last 5 minutes. |
| Web unreachable, `status` shows `Web unavailable on port ...` | The port is taken. Change `"port"` in the existing `["web"]` table and restart. |
| `Four timelines are being read` | Four Inspect conversations are pinned on earlier entries. Wait up to 5 minutes, or close other Inspect panels or press **Latest** there. |
| Workers show the built-in robot instead of characters | No valid character for that model family in `SSHARNESS_HOME/characters/`, or the atlas size does not match its manifest. See [setup.md](setup.md#worker-characters). |
| The Claude Code label has no icon | `SSHARNESS_HOME/characters/harness-claude-code.png` is missing or not a valid PNG. The label is shown as text. |
| View diff says fork point unavailable | An older task without a recorded fork point and no usable branch reflog. The diff is not guessed. |
| A Worker repeats a message | Follow-ups are at-least-once; a resend after a lost acknowledgement can deliver twice. |
| `emit failed: ... resend the same id` | The connection broke before an answer; resending the same `id` is safe. |
| macOS reminder banner missing | Check notification permission for the terminal or `osascript`. The message is still in the terminal and web. |

## Security boundaries

This is a single-user tool for a machine you trust.

- **Workers have full local permissions.** They run Claude Code with `--dangerously-skip-permissions`, as your user, with no sandbox. Only `sandbox = "none"` and `location = "local"` are accepted.
- **Accidental pushes are blocked, deliberate ones are not.** Workers get a git `pushInsteadOf` rewrite that breaks ordinary pushes. An explicit `pushurl`, or a longer matching `pushInsteadOf` prefix of your own, still pushes. This prevents mistakes; it is not a security boundary.
- **Worker tools are a convention, not isolation.** Workers cannot emit events through their tools, the service rejects their tokens for owner methods, and `emit` refuses to run when `SSHARNESS_TASK_ID` is set. A Worker that reads the owner token, or unsets the variable, can bypass this.
- **Item `read` permission and the Router's instructions are not enforced.** They tell the model what to do.
- **Event and report text is data.** The Router's input marks events as data, escapes external event text, and neutralizes `<event` / `</event` tags inside Worker reports so they cannot open or close an event block. This reduces prompt-injection risk; it does not remove it.
- **The web is local only.** It binds to `127.0.0.1`, checks `Host`, requires the exact same `Origin` for POST, has no CORS and is not meant for public deployment. **Cookies are not isolated by port:** other services on the same host can receive the cookie, so only run trusted same-host services. This is not a boundary against a hostile local service.
- **Secrets** stay in 0600 files or the environment. They are never CLI arguments or TOML values, and are never printed by `doctor`, logged, or put into the database. The HTTP handler never logs request targets, cookies, bodies or login tokens. Do not share login URLs or private files.

## Known limits

- One user, one machine. No remote execution, no cloud Workers, no Harness other than Claude Code.
- A session belongs to its Harness: switching a Worker or the Router to a Profile with a different Harness is refused, and Router primary/backup must share a Harness.
- Event subscriptions do not expire.
- `read` item permission is not enforced.
- Diffs show committed branch changes only; Inspect conversations are bounded snapshots, not an audit export.
- Worker names repeat every 24 tasks; the task ID is the identifier.
- The web cannot send images to the Router, has no language switch, and the Routing view shows the present only (no history replay).
- `doctor` and the presets are not end-to-end verified with real keys.
- The macOS notification is at most once; follow-ups to Workers are at least once; model-driven external side effects are not exactly once.
- `peek` and `open` sessions have no Router tools.

## Under construction

- **Usage** panel: hidden. Its page code and `GET /api/usage` remain, but there is no navigation entry; it returns with the roadmap.
