# FAQ

Short answers with links to the details. Something missing? [Open an issue](https://github.com/ketchupz1999/super-super-harness/issues).

- [What is Super Super Harness?](#what-is-super-super-harness)
- [How is this different from Claude Code sub-agents?](#how-is-this-different-from-claude-code-sub-agents)
- [How do I run multiple Claude Code sessions in parallel?](#how-do-i-run-multiple-claude-code-sessions-in-parallel)
- [Can I run Codex or other agents as Workers?](#can-i-run-codex-or-other-agents-as-workers)
- [Which models can I use?](#which-models-can-i-use)
- [Does it run in the cloud?](#does-it-run-in-the-cloud)
- [Does it work on Linux or Windows?](#does-it-work-on-linux-or-windows)
- [Is it safe?](#is-it-safe)
- [Will Workers push or merge my code?](#will-workers-push-or-merge-my-code)
- [What happens when the Router's model runs out of quota?](#what-happens-when-the-routers-model-runs-out-of-quota)
- [What happens if my Mac restarts?](#what-happens-if-my-mac-restarts)
- [How much does it cost?](#how-much-does-it-cost)
- [Is it a DeepSeek project?](#is-it-a-deepseek-project)

## What is Super Super Harness?

A local orchestrator for coding agents. You talk to one Router, a long-lived Claude Code session. It hands your requests to Workers, each a separate Claude Code session with its own code name, working in parallel. Progress, questions and results come back to you in the terminal or a local web page.

## How is this different from Claude Code sub-agents?

A sub-agent is a helper inside one session: it gets a task and returns its result to the session that started it. Here each Worker is its own Claude Code session, managed by a background service:

- **Its own branch.** Each Worker with a repository works in a git worktree on branch `w/<code_name>`, so parallel Workers don't edit the same files.
- **It lives on.** A finished Worker keeps its session and notes, so a follow-up goes back to the Worker that already has the context. Idle Workers go dormant after 7 days and wake when messaged. You can open any Worker's session in the web page or take it over in a terminal.
- **It talks to you.** Workers report progress and ask you questions with options; you answer them directly.
- **Dependencies are code.** "Start B when A is done" is handled by the scheduler, which passes A's report to B. The Router doesn't sit and wait.
- **It survives restarts.** Workers, events and messages are stored in SQLite.

See [Workers](guide.md#workers) and [Items](guide.md#items-事项).

## How do I run multiple Claude Code sessions in parallel?

You can do it by hand with git worktrees and several terminals. This project does that for you: tell the Router what you want, and it starts up to `max_workers` (default 10) Workers at once, each in its own worktree. Extra Workers wait in a queue, and Workers waiting on dependencies don't take a slot. You watch them all in the Routing view.

## Can I run Codex or other agents as Workers?

Not yet. Claude Code is the only Harness that works today; an [experimental Hermes adapter](setup.md#hermes-experimental-workers) does not work with a real Hermes yet. Any agent that implements the [Harness protocol](protocol.md) can become a Worker. Codex is tracked in [#2](https://github.com/ketchupz1999/super-super-harness/issues/2); Pi and DeepSeek Harnesses come later.

## Which models can I use?

Any model behind an Anthropic-compatible endpoint that Claude Code can use. `init` has presets for Anthropic, DeepSeek, GLM, StepPlan and Kimi, plus a custom option. The Router and Workers can use different Profiles, for example a strong model for the Router and a cheaper one for Workers. The presets were checked against each provider's docs, not tested with real keys. See [Providers](setup.md#providers).

## Does it run in the cloud?

No. It runs on your own machine for one user. The web page binds to `127.0.0.1` and is not meant for public deployment. Cloud agents are on the [roadmap](https://github.com/ketchupz1999/super-super-harness/issues/1) for later.

## Does it work on Linux or Windows?

macOS is the supported host. On other platforms, keeping the machine awake (`caffeinate`) and macOS notifications are skipped. Nothing else has been tested there. See [Requirements](setup.md#requirements).

## Is it safe?

Only run it on a machine you trust. Workers run Claude Code with `--dangerously-skip-permissions`, as your user, with no sandbox. Some guards prevent mistakes, but they are not security boundaries. See [Security](guide.md#security).

## Will Workers push or merge my code?

Workers commit on their own branch `w/<code_name>`. Ordinary `git push` from a Worker is blocked to prevent accidents. A deliberate push can still get through, so this is not a security boundary. Nothing reaches your main branch unless you, or an agent you ask, merges it.

## What happens when the Router's model runs out of quota?

If the Router's model stops responding, the service checks it and switches the Router to a backup Profile on the same session, then tells you. While on backup, it checks the primary model every few minutes and asks you once whether to switch back. See [Router health and failover](design.md#router-health-and-failover).

## What happens if my Mac restarts?

Start the service again. Running and queued Workers resume, and dependencies are kept. Each resumed Worker is told its current `git status`, so it checks before repeating work.

## How much does it cost?

The project is free (MIT). Every Worker is a full Claude Code session billed by your model provider, so ten Workers cost roughly ten sessions. Putting Workers on a cheaper model is the main lever.

## Is it a DeepSeek project?

No. It's an independent project inspired by DeepSeek's *Exploring Super-agent Design* and [`dsh-superagent-prototype`](https://github.com/deepseek-ai/dsh-superagent-prototype).
