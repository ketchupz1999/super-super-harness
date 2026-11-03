"""Independent in-memory v1 adapter. No Claude module, binary or stream-json parser."""
import asyncio
import uuid as uuidlib


class FakeHarness:
    def __init__(self):
        self.calls = []
        self.sessions = {}

    def build_env(self, cfg, profile, tmpdir=None):
        self.calls.append(("env", profile.name))
        return {}

    def headless_argv(self, cfg, profile, session_id, resume, mcp_config, system_prompt):
        self.calls.append(("headless", profile.name, resume))
        return [session_id]

    def interactive_argv(self, cfg, profile, session_id, mcp_config, system_prompt, fork=False, hooks=None):
        self.calls.append(("interactive", profile.name))
        return ["fake-interactive", session_id]

    def prepare_session(self, cfg, session_id):
        self.calls.append(("prepare", session_id))
        return session_id, session_id in self.sessions

    def session_has(self, cfg, session_id, marker):
        self.calls.append(("has", session_id))
        return any(marker in text for text in self.sessions.get(session_id, []))

    def session_file(self, cfg, session_id):
        self.calls.append(("file", session_id))
        return None

    def transcript(self, cfg, session_id, *, max_bytes=None, limit=None, text_limit=None):
        self.calls.append(("transcript", session_id))
        items = [{"time": "2026-11-02T00:00:00Z", "kind": "received", "text": text[:text_limit]}
                 for text in self.sessions.get(session_id, [])]
        return items[-limit:] if limit else items

    def ensure_provider(self, provider):
        self.calls.append(("ensure", provider.name))

    def shallow_probe(self, provider, timeout=5):
        self.calls.append(("shallow", provider.name))
        return True

    def deep_probe(self, provider, model, timeout=30):
        self.calls.append(("deep", provider.name, model))
        return True

    def available(self, cfg, profile):
        return True

    def Process(self, argv, env, cwd, on_event, on_exit, log_path=None):
        return FakeProcess(self, argv[0], on_event, on_exit)


class FakeProcess:
    pid = None
    closing = False

    def __init__(self, adapter, session_id, on_event, on_exit):
        self.adapter, self.session_id = adapter, session_id
        self.on_event, self.on_exit = on_event, on_exit
        self.exited = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.turns = set()

    async def start(self):
        return None

    def send(self, text, uuid=None):
        if self.closing:
            return None
        uuid = uuid or str(uuidlib.uuid4())
        turn = asyncio.create_task(self._turn(text, uuid))
        self.turns.add(turn)
        turn.add_done_callback(self.turns.discard)
        return uuid

    async def _turn(self, text, uuid):
        self.adapter.sessions.setdefault(self.session_id, []).append(text)
        await self.on_event({"type": "user", "isReplay": True, "uuid": uuid})
        if text.startswith("WAIT"):
            await self.interrupted.wait()
        if self.interrupted.is_set():
            self.interrupted.clear()
            await self.on_event({"type": "result", "subtype": "error_during_execution", "is_error": True})
            return
        await self.on_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "fake answer"}]}})
        await self.on_event({"type": "result", "subtype": "success", "is_error": False, "result": "fake answer"})

    def interrupt(self):
        if self.closing:
            return False
        self.interrupted.set()
        return True

    async def close(self, wait=5):
        if self.closing:
            await self.exited.wait()
            return
        self.closing = True
        self.interrupted.set()
        await asyncio.gather(*list(self.turns))
        self.exited.set()
        await self.on_exit(0)
