"""Tools offered over MCP. The caller's token decides which ones it sees."""
import hashlib
import json


WORKER_CALLS = frozenset(("worker_hire", "worker_assign", "worker_say", "worker_interrupt", "worker_resume", "worker_stop",
                          "worker_switch_model", "worker_rename", "worker_read", "worker_detail", "worker_wait_for"))


def worker_call_key(name, args):
    """Correlate the Harness stream with the resolved identity at tool execution."""
    if name not in WORKER_CALLS:
        return None
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def chat_call_key(name, args):
    """Correlate a full tool input with its outcome without retaining its private arguments."""
    if name not in ("reminder_add", "worker_hire", "worker_assign", "worker_say", "item_create", "item_update"):
        return None
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tool(tool_name, description, required=(), /, **properties):
    return {"name": tool_name, "description": description, "inputSchema": {
        "type": "object", "properties": {key: kind if isinstance(kind, dict) else {"type": kind} for key, kind in properties.items()},
        "required": list(required)}}


STRINGS = {"type": "array", "items": {"type": "string"}}


ROUTER = [
    tool("observe_note", "Record once what you would do for a mode=observe event in this turn, without acting or notifying the user. "
         "event_id: the event's trigger ID; would_do: one or two sentences; route: a Worker code name, new Worker, tell user, or ignore.",
         ["event_id", "would_do", "route"], event_id="integer", would_do="string", route="string"),
    tool("memory_read", "Read current Router memory and its version before editing."),
    tool("memory_write", "Save merged Router memory using the version from memory_read. On conflict, read again and merge; do not use file tools.",
         ["content", "version"], content="string", version="string"),
    tool("reply", "Say something to the user.", ["text"], text="string"),
    tool("ask_user", "Ask the user a question and wait for their next message. End your turn after calling this. "
         "options: up to 8 short answers the page can offer; the user may still answer in free text.", ["question"],
         question="string", options=STRINGS),
    tool("worker_hire", "Hire a permanent Worker for an item and give it its first assignment. Returns immediately; results arrive as messages. "
         "item: required item code (legacy callers omitting it fall back to Miscellaneous). "
         "code_name: a memorable unique name, 1–24 letters/digits/hyphens starting with a letter. role: one-line responsibility. "
         "wait_for: Workers whose results it needs; names or internal ids, case-insensitive, names take precedence. "
         "It starts by itself when all upstream Workers complete. attachments: explicitly forward uploaded image ids; omitted means no images.",
         ["name", "brief", "item", "code_name", "role"], name="string", brief="string", profile="string", repo="string", reason="string", item="string", wait_for=STRINGS,
         code_name="string", role="string", attachments=STRINGS),
    tool("worker_wait_for", "Replace the upstream Workers a waiting Worker needs. An empty list starts it now. "
         "Worker parameters accept names or internal ids, case-insensitive, names first.", ["worker", "wait_for"],
         worker="string", wait_for=STRINGS),
    tool("item_create", "Create an item. code: short lowercase English like mic-input; a sequence number is added. "
         "kind: delivery (ends when delivered) or standing (a long-lived domain). permission: read (query and analyze only) or act.",
         ["code", "name", "kind", "permission"], code="string", name="string", kind="string", permission="string",
         next="string", follow_up="string"),
    tool("item_update", "Change an item's status (active, confirm, delivered, parked), progress, next, follow_up (YYYY-MM-DD), name or "
         "permission. To change notes pass notes with notes_version from item_read.", ["item"], item="string", status="string",
         progress="string", next="string", follow_up="string", name="string", permission="string", notes="string", notes_version="string"),
    tool("item_read", "Read an item: fields, attached Workers and notes with their version.", ["item"], item="string"),
    tool("item_list", "List items, newest first. Closed ones only with include_closed.", [], query="string", include_closed="boolean"),
    tool("item_close", "Close an item for good.", ["item"], item="string", reason="string"),
    tool("item_attach", "Attach an existing Worker to an open item: it subscribes to item:<code> and joins the item's row. "
         "A Worker on another item is not moved: you get where it is; tell the user, then call again with move=true. "
         "worker: code name or internal id, case-insensitive, code name first.",
         ["item", "worker"], item="string", worker="string", move="boolean"),
    tool("worker_assign", "Give an existing Worker new work, recording an assignment. Pass the status you saw as expected_status. "
         "attachments: explicitly forward image ids; omitted means no images. "
         "worker: code name or internal id, case-insensitive, code name first.", ["worker", "text"],
         worker="string", text="string", expected_status="string", attachments=STRINGS),
    tool("worker_say", "Insert a message into a Worker's conversation without creating an assignment, for example a clarification or pause request. "
         "attachments: explicitly forward image ids; omitted means no images. "
         "An idle Worker wakes to read it. worker: code name or internal id, case-insensitive, code name first.",
         ["worker", "text"], worker="string", text="string", attachments=STRINGS),
    tool("worker_interrupt", "Interrupt a Worker's current turn; progress is kept. worker: code name or internal id, case-insensitive, names first.",
         ["worker"], worker="string"),
    tool("worker_resume", "Continue an interrupted, failed or dormant Worker. worker: code name or internal id, case-insensitive, names first.",
         ["worker"], worker="string", text="string"),
    tool("worker_stop", "Stop a Worker for good. worker: code name or internal id, case-insensitive, names first.", ["worker"], worker="string"),
    tool("worker_switch_model", "Move a Worker to another Profile on the same Harness, keeping its session. "
         "worker: code name or internal id, case-insensitive, names first.", ["worker", "profile"], worker="string", profile="string"),
    tool("worker_read", "Read a Worker's report, status and recent event ids. worker: code name or internal id, case-insensitive, names first.",
         ["worker"], worker="string"),
    tool("worker_detail", "Read what a Worker really did: messages, thinking, tool calls and results. "
         "worker: code name or internal id, case-insensitive, names first.", ["worker"], worker="string", kinds="string", limit="integer"),
    tool("worker_search", "Find Workers by code name, assignment name, brief or report.", ["query"], query="string"),
    tool("worker_rename", "Change a Worker's code name without moving its workspace or session. "
         "worker: code name or internal id, case-insensitive, names first. code_name: unique, 1–24 letters/digits/hyphens starting with a letter.",
         ["worker", "code_name"], worker="string", code_name="string"),
    tool("event_read", "Read one event in full: event_id from the internal log, or trigger_id from an emitted event.", [],
         event_id="integer", trigger_id="integer"),
    tool("message_search", "Find past conversation with the user.", ["query"], query="string"),
    tool("router_switch_model", "Switch your own model, after the current turn. Only when the user asks.", ["profile"], profile="string"),
    tool("reminder_add", "Schedule a named notify (no model) or run (wake Router). Supply exactly one of at (ISO local time) or cron (numeric five-field local cron).", ["name", "text", "kind"],
         name="string", text="string", kind="string", at="string", cron="string"),
    tool("reminder_list", "List active reminders, current local time and timezone. Match cancellations by name; ask once if ambiguous."),
    tool("reminder_cancel", "Cancel future occurrences of a reminder. Already queued occurrences are kept.", ["reminder_id"], reminder_id="string"),
    tool("inbox_read", "While a person is attached: read messages waiting for you.", []),
]

WORKER = [
    tool("report_progress", "One short line about where you are.", ["text"], text="string"),
    tool("report_done", "The final report: conclusion, changes, outputs, what is left. Only the Worker itself, after acting on what "
         "its sub-agents returned; a sub-agent's call is refused.", ["text"], text="string"),
    tool("ask_user", "Ask the user for a decision, then end your turn. options: up to 8 short answers; free text still works.",
         ["question"], question="string", options=STRINGS),
    tool("subscribe", "Receive events on a thread, such as github:org/repo#12.", ["thread"], thread="string"),
    tool("unsubscribe", "Stop receiving events on a thread.", ["thread"], thread="string"),
    tool("hand_back", "An event you received is not yours to handle: return it to the Router with the reason. Subscriptions stay.",
         ["event_id", "reason"], event_id="integer", reason="string"),
    tool("request_help", "Ask the Router for another colleague: describe the work and optionally name an existing Worker in to. "
         "This wakes the Router with a worker.help event; it does not directly hire or contact a Worker. "
         "to accepts a code name or internal id, case-insensitive, code name first.", ["text"], text="string", to="string"),
    tool("notes_read", "Read your own durable working notes and their version. Use them for decisions, context and unfinished work that must survive session changes and dormancy."),
    tool("notes_write", "Save your own durable working notes using the version from notes_read. On conflict, read again and merge. "
         "These are separate from the item's shared notes; do not edit the notes file directly.",
         ["content", "version"], content="string", version="string"),
    tool("item_read", "Read your item: fields and notes with their version."),
    tool("item_update", "Update your item's next step, or its notes with notes_version from item_read.", [],
         next="string", notes="string", notes_version="string"),
]
