"""Tools offered over MCP. The caller's token decides which ones it sees."""
import hashlib
import json


def chat_call_key(name, args):
    """Correlate a full tool input with its outcome without retaining its private arguments."""
    if name not in ("reminder_add", "task_start", "task_send", "item_create", "item_update"):
        return None
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tool(tool_name, description, required=(), /, **properties):
    return {"name": tool_name, "description": description, "inputSchema": {
        "type": "object", "properties": {key: kind if isinstance(kind, dict) else {"type": kind} for key, kind in properties.items()},
        "required": list(required)}}


STRINGS = {"type": "array", "items": {"type": "string"}}


ROUTER = [
    tool("memory_read", "Read current Router memory and its version before editing."),
    tool("memory_write", "Save merged Router memory using the version from memory_read. On conflict, read again and merge; do not use file tools.",
         ["content", "version"], content="string", version="string"),
    tool("reply", "Say something to the user.", ["text"], text="string"),
    tool("ask_user", "Ask the user a question and wait for their next message. End your turn after calling this. "
         "options: up to 8 short answers the page can offer; the user may still answer in free text.", ["question"],
         question="string", options=STRINGS),
    tool("task_start", "Start a background Worker. Returns immediately; the result arrives as a new message. "
         "item: attach it to an item (it subscribes to item:<code>). wait_for: task ids it waits for; it starts by itself when all complete.",
         ["name", "brief"], name="string", brief="string", profile="string", repo="string", reason="string", item="string", wait_for=STRINGS),
    tool("task_wait_for", "Replace what a waiting task waits for. An empty list starts it now.", ["task_id", "wait_for"],
         task_id="string", wait_for=STRINGS),
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
    tool("item_attach", "Attach an existing task to an open item: it subscribes to item:<code> and joins the item's row. "
         "A task on another item is not moved: you get where it is; tell the user, then call again with move=true.",
         ["item", "task_id"], item="string", task_id="string", move="boolean"),
    tool("task_send", "Send a follow-up to a task. Pass the status you saw as expected_status.", ["task_id", "text"],
         task_id="string", text="string", expected_status="string"),
    tool("task_interrupt", "Stop what a Worker is doing now. Progress is kept.", ["task_id"], task_id="string"),
    tool("task_resume", "Continue an interrupted or failed task.", ["task_id"], task_id="string", text="string"),
    tool("task_stop", "End a task for good.", ["task_id"], task_id="string"),
    tool("task_switch_model", "Move a task to another Profile, keeping its session.", ["task_id", "profile"], task_id="string", profile="string"),
    tool("task_read", "Read a task: report, status, recent event ids.", ["task_id"], task_id="string"),
    tool("task_detail", "Read what a Worker really did: messages, thinking, tool calls and results.", ["task_id"],
         task_id="string", kinds="string", limit="integer"),
    tool("task_search", "Find tasks by name, brief or report.", ["query"], query="string"),
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
    tool("item_read", "Read your item: fields and notes with their version."),
    tool("item_update", "Update your item's next step, or its notes with notes_version from item_read.", [],
         next="string", notes="string", notes_version="string"),
]
