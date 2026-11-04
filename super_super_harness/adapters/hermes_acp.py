"""Standard-library ACP client. No Hermes imports or execution outside start()."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid as uuidlib

from . import procs, hermes_state
from .hermes_diagnostics import StderrTail
from ..core.private_files import atomic_write

HANDSHAKE_TIMEOUT = 120


class RpcError(ValueError):
    """Fixed diagnostics only: upstream error messages can contain credentials."""


class Process:
    def __init__(self, argv, env, cwd, on_event, on_exit, log_path=None):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.on_event, self.on_exit, self.log_path = on_event, on_exit, log_path
        self.spec = json.loads(argv[0])
        self.proc, self.pid = None, None
        self.closing, self._broken, self._muted = False, False, True
        self.exited, self._spawned = asyncio.Event(), asyncio.Event()
        self._starting = False
        self._pending, self._children = {}, {}
        self._sequence = 0
        self._queue = asyncio.Queue()
        self._pump_task = self._reader = self._monitor = self._close_task = self._stderr_reader = None
        self.stderr = StderrTail(env, self.spec["home"])
        self._stdout = self._stderr = None
        self._stdout_transport = self._stderr_transport = None
        self._finish_task = None
        self._in_prompt = False
        self._message_id, self._kind, self._chunks = None, None, []
        self._turn_text = []
        self._completed_tools = set()

    async def _output_pipe(self):
        # Own the read transport so inherited descriptors cannot keep proc.wait()
        # pending after the ACP parent exits. No private asyncio transports needed.
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, "rb", buffering=0)
        reader = asyncio.StreamReader(limit=2 ** 24)
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), source)
        except BaseException:
            source.close()
            os.close(write_fd)
            raise
        return reader, transport, write_fd

    async def start(self):
        from .hermes import _check_install
        if self.closing or self._starting or self.proc:
            raise ValueError("Hermes process cannot be started twice or after close")
        self._starting = True
        try:
            env = dict(self.env, HERMES_HOME=self.spec["home"], HERMES_DISABLE_LAZY_INSTALLS="1",
                       HERMES_RUNTIME_DIR=self.spec["runtime_dir"])
            env.pop("HERMES_TUI_GATEWAY_URL", None)
            executable = _check_install(self.argv[1], env)
            stdout_fd = stderr_fd = None
            try:
                self._stdout, self._stdout_transport, stdout_fd = await self._output_pipe()
                self._stderr, self._stderr_transport, stderr_fd = await self._output_pipe()
                self.proc = await asyncio.create_subprocess_exec(
                    executable, *self.argv[2:], cwd=str(self.cwd), env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=stdout_fd, stderr=stderr_fd, start_new_session=True)
            finally:
                for fd in (stdout_fd, stderr_fd):
                    if fd is not None:
                        os.close(fd)
            self.pid = self.proc.pid
            self._stderr_reader = asyncio.create_task(self.stderr.read(self._stderr))
            self._reader = asyncio.create_task(self._read())
            self._monitor = asyncio.create_task(self._track_children())
            self._spawned.set()
            if self.closing:
                raise ValueError("Hermes launch was closed")
            await asyncio.wait_for(self._handshake(), self.spec.get("handshake_timeout", HANDSHAKE_TIMEOUT))
            if self.closing or self._broken:
                raise ValueError("Hermes launch was closed")
            await self._remember_children()
            if self.closing or self._broken or self.exited.is_set():
                raise ValueError("Hermes launch was closed")
            self._muted = False
            await self.on_event({"type": "system", "subtype": "init", "model": self.spec["model"], "tools": []})
            if self.closing or self._broken or self.exited.is_set():
                raise ValueError("Hermes launch was closed")
            self._pump_task = asyncio.create_task(self._pump())
            return self.pid
        except asyncio.CancelledError:
            self._spawned.set()
            await asyncio.shield(self.close(wait=.1))
            raise
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            self._spawned.set()
            await self.close(wait=.1)
            diagnostic = self.stderr.text()
            suffix = "\n" + diagnostic if diagnostic else "; check the isolated installation and configuration"
            raise ValueError("Hermes ACP startup failed (%s)%s" % (type(exc).__name__, suffix)) from None
        finally:
            self._spawned.set()

    async def _handshake(self):
        initialized = await self._rpc("initialize", protocolVersion=1, clientCapabilities={})
        if initialized.get("protocolVersion") != 1:
            raise RpcError("Hermes ACP protocol version is unsupported")
        params = {"cwd": str(Path(self.cwd).resolve()), "mcpServers": []}
        if self.spec["resume"] and self.spec.get("hermes_id"):
            await self._rpc("session/load", sessionId=self.spec["hermes_id"], **params)
            for admission in await asyncio.to_thread(self._unknown):
                await self._rpc("session/prompt", sessionId=self.spec["hermes_id"],
                                prompt=[{"type": "text", "text": "/discard " + admission}])
            if await asyncio.to_thread(self._unknown):
                raise RpcError("Hermes has unresolved execution state")
        else:
            result = await self._rpc("session/new", **params)
            hid = result.get("sessionId")
            if not isinstance(hid, str) or not hid:
                raise RpcError("Hermes did not return a session id")
            self.spec["hermes_id"] = hid
            atomic_write(self.spec["map"], json.dumps({"hermes_id": hid}))
        await self._rpc("session/set_model", sessionId=self.spec["hermes_id"], modelId="custom:ssh:" + self.spec["model"])
        await self._rpc("session/set_mode", sessionId=self.spec["hermes_id"], modeId="dont_ask")
        self._compacted = await asyncio.to_thread(hermes_state.compacted, self._state, self.spec["hermes_id"])

    @property
    def _state(self):
        return Path(self.spec["home"]) / "state.db"

    def _unknown(self):
        return hermes_state.unknown(self._state, self.spec["hermes_id"])

    def _delivered(self, marker):
        return hermes_state.has(self._state, self.spec["hermes_id"], marker)

    async def _compaction_boundary(self):
        count = await asyncio.to_thread(hermes_state.compacted, self._state, self.spec["hermes_id"])
        if count is None:
            return
        if self._compacted is not None and count > self._compacted:
            await self.on_event({"type": "system", "subtype": "compact_boundary"})
        self._compacted = count

    def _write(self, message):
        if not self.proc or self.proc.returncode is not None or self.proc.stdin.is_closing():
            return False
        try:
            self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode())
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    async def _rpc(self, method, **params):
        self._sequence += 1
        ident = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            if self._broken or not self._write({"id": ident, "method": method, "params": params}):
                raise RpcError("Hermes ACP connection closed")
            await self.proc.stdin.drain()
            return await future
        except (BrokenPipeError, ConnectionResetError):
            raise RpcError("Hermes ACP connection closed") from None
        finally:
            self._pending.pop(ident, None)
            if future.done() and not future.cancelled():
                future.exception()   # drain may fail before the reply future is awaited

    async def _read(self):
        try:
            while line := await self._stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RpcError("Invalid Hermes ACP frame")
                if "method" in message:
                    if "id" in message:
                        self._request(message)
                    elif message["method"] == "session/update" and not self._muted:
                        params = message.get("params", {})
                        if params.get("sessionId") == self.spec["hermes_id"]:
                            await self._update(params.get("update", {}))
                else:
                    future = self._pending.get(message.get("id"))
                    if future and not future.done():
                        if "error" in message or not isinstance(message.get("result"), dict):
                            future.set_exception(RpcError("Hermes ACP request failed"))
                        else:
                            future.set_result(message["result"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        finally:
            self._broken = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RpcError("Hermes ACP connection closed"))
            self._finish_task = asyncio.create_task(self._finish())
            await asyncio.shield(self._finish_task)

    def _abort_stdin(self):
        transport = self.proc.stdin.transport
        try:
            transport.abort()
        except AttributeError:
            # UnixWritePipeTransport.abort is not idempotent on Python 3.12:
            # a completed close has already cleared its loop. A closing pipe
            # with buffered input still has a loop and must actually be aborted.
            if not transport.is_closing():
                raise

    async def _finish(self):
        try:
            await self._cleanup_process()
        finally:
            self.exited.set()
            await self.on_exit(self.proc.returncode)

    async def _cleanup_process(self):
        try:
            if self._monitor:
                self._monitor.cancel()
                await asyncio.gather(self._monitor, return_exceptions=True)
            try:
                await self._remember_children()
                await asyncio.to_thread(self._kill_children)
            except (OSError, ValueError, subprocess.SubprocessError):
                self.stderr.append(b"[Hermes process inspection failed during cleanup]")
            self._abort_stdin()
            if self.proc.returncode is None:
                self.proc.kill()
            await self.proc.wait()
            if self._pump_task:
                if not self._in_prompt or self.closing:
                    self._pump_task.cancel()
                await asyncio.gather(self._pump_task, return_exceptions=True)
        finally:
            self._kill_group()
            self._abort_stdin()
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
            self._stdout_transport.close()
            await self._finish_stderr()

    async def _finish_stderr(self):
        try:
            if self._stderr_reader:
                try:
                    await asyncio.wait_for(asyncio.shield(self._stderr_reader), 1)
                except asyncio.TimeoutError:
                    # A detached descendant may retain fd 2 after the parent died.
                    self._stderr_transport.close()
                    await asyncio.wait_for(asyncio.gather(self._stderr_reader, return_exceptions=True), 1)
                except OSError:
                    pass
        finally:
            if self._stderr_transport:
                self._stderr_transport.close()
        try:
            await asyncio.to_thread(self.stderr.save, self.log_path)
        except (OSError, ValueError):
            pass   # a failed diagnostic write must not prevent process cleanup

    def _request(self, message):
        ident, method, params = message["id"], message["method"], message.get("params", {})
        if method == "session/request_permission":
            tool = params.get("toolCall", {})
            if str(tool.get("toolCallId", "")).startswith("clarify-"):
                outcome = {"outcome": "cancelled"}
            else:
                options = params.get("options", [])
                chosen = next((o for kind in ("allow_once", "allow_always", "reject_once", "reject_always")
                               for o in options if o.get("kind") == kind), None)
                if not chosen:
                    # No selectable response can settle this waiter. Fail rather than hang forever.
                    self._write({"id": ident, "error": {"code": -32602, "message": "No permission option"}})
                    raise RpcError("Hermes permission request has no selectable option")
                outcome = {"outcome": "selected", "optionId": chosen["optionId"]}
            self._write({"id": ident, "result": {"outcome": outcome}})
        elif method in ("clarify", "elicitation/create"):
            self._write({"id": ident, "result": {"outcome": {"outcome": "cancelled"}}})
        else:
            self._write({"id": ident, "error": {"code": -32601, "message": "Method not supported"}})

    async def _flush(self):
        if self._chunks:
            text = "".join(self._chunks)
            block = {"type": "text", "text": text} if self._kind == "agent_message_chunk" else {"type": "thinking", "thinking": text}
            if block["type"] == "text":
                self._turn_text.append(text)
            self._chunks = []
            await self.on_event({"type": "assistant", "message": {"content": [block]}, "parent_tool_use_id": None})

    async def _update(self, update):
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content", {})
            if content.get("type") != "text" or not isinstance(content.get("text"), str):
                return
            identity = update.get("messageId")
            if (identity, kind) != (self._message_id, self._kind):
                await self._flush()
            self._message_id, self._kind = identity, kind
            self._chunks.append(content["text"])
        elif kind == "tool_call":
            await self._flush()
            block = {"type": "tool_use", "id": update.get("toolCallId"),
                     "name": update.get("title", "").split(":", 1)[0].strip(), "input": update.get("rawInput") or {}}
            await self.on_event({"type": "assistant", "message": {"content": [block]}, "parent_tool_use_id": None})
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            ident = update.get("toolCallId")
            if ident in self._completed_tools:
                return
            self._completed_tools.add(ident)
            content = update.get("rawOutput")
            if content is None:
                content = "\n".join(c.get("content", {}).get("text", "") for c in update.get("content", [])
                                    if c.get("type") == "content" and c.get("content", {}).get("type") == "text")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            await self.on_event({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident,
                                 "is_error": update["status"] == "failed", "content": content}]}})

    def send(self, text, uuid=None):
        if self.closing or self._broken or self._muted or not self.proc or self.proc.returncode is not None:
            return None
        ident = uuid or str(uuidlib.uuid4())
        self._queue.put_nowait((ident, text))
        return ident

    async def _pump(self):
        while not self.closing and not self._broken:
            ident, text = await self._queue.get()
            self._turn_text, self._chunks, self._completed_tools = [], [], set()
            self._message_id, self._kind = None, None
            marker = "[ssharness-input:%s]" % uuidlib.uuid4()
            self._in_prompt = True
            try:
                await self.on_event({"type": "system", "subtype": "input_started", "uuid": ident, "marker": marker})
                response = await self._rpc("session/prompt", sessionId=self.spec["hermes_id"],
                                          prompt=[{"type": "text", "text": text + "\n\n" + marker}])
                await self._flush()
                stop = response.get("stopReason")
                durable = await asyncio.to_thread(self._delivered, marker)
                await self._compaction_boundary()
                if durable and stop in ("end_turn", "cancelled", "max_tokens", "max_turn_requests", "refusal"):
                    await self.on_event({"type": "user", "isReplay": True, "uuid": ident})
                cancelled = stop == "cancelled"
                success = stop == "end_turn" and durable
                await self.on_event({"type": "result", "subtype": "success" if success else
                                     "error_during_execution" if cancelled else "error",
                                     "is_error": not success, "result": "\n\n".join(self._turn_text) if success or cancelled
                                     else "Hermes turn did not finish with durable input"})
            except RpcError:
                self._broken = True
                await self._flush()
                await self.on_event({"type": "result", "subtype": "error", "is_error": True,
                                     "result": "Hermes ACP turn failed"})
                # Router recovery needs an exit, not a live process refusing all sends.
                await self._remember_children()
                await asyncio.to_thread(self._kill_children)
                return
            finally:
                self._in_prompt = False

    def interrupt(self):
        if self.closing or self._broken or self._muted:
            return False
        return self._write({"method": "session/cancel", "params": {"sessionId": self.spec["hermes_id"]}})

    async def _remember_children(self):
        if self.proc and self.proc.returncode is None:
            pids = await asyncio.to_thread(procs.descendants, self.pid)
            for pid in pids:
                if pid not in self._children:
                    started = await asyncio.to_thread(procs.start_time, pid)
                    if started:
                        self._children[pid] = started

    async def _track_children(self):
        while not self.closing and self.proc.returncode is None:
            await self._remember_children()
            await asyncio.sleep(.5)
        if not self.closing:
            # Drain final parent output, but do not wait forever on an inherited fd.
            try:
                await asyncio.to_thread(self._kill_children)
            except (OSError, ValueError, subprocess.SubprocessError):
                self.stderr.append(b"[Hermes process inspection failed after parent exit]")
            try:
                await asyncio.wait_for(self.exited.wait(), 1)
            except asyncio.TimeoutError:
                self._stdout_transport.close()

    def _kill_group(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _kill_children(self):
        try:
            for pid, started in list(self._children.items()):
                if procs.alive(pid, started):
                    procs.kill_tree(pid)
        finally:
            # Also catch children forked between snapshots, even if inspection fails.
            self._kill_group()

    async def close(self, wait=5):
        self.closing = True
        if self.exited.is_set():
            return
        if not self._close_task:
            self._close_task = asyncio.create_task(self._close(wait))
        await asyncio.shield(self._close_task)

    async def _close(self, wait):
        if self._starting and not self._spawned.is_set():
            await self._spawned.wait()
        if not self.proc:
            for transport in (self._stdout_transport, self._stderr_transport):
                if transport:
                    transport.close()
            self.exited.set()
            return
        if self._pump_task:
            self._pump_task.cancel()
        try:
            await self._remember_children()
        except (OSError, ValueError, subprocess.SubprocessError):
            self.stderr.append(b"[Hermes process inspection failed during close]")
        if not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.exited.wait(), wait)
        except asyncio.TimeoutError:
            try:
                await self._remember_children()
                await asyncio.to_thread(self._kill_children)
            except (OSError, ValueError, subprocess.SubprocessError):
                self.stderr.append(b"[Hermes process inspection failed during forced close]")
            finally:
                self._kill_group()
                self._abort_stdin()
                if self.proc.returncode is None:
                    self.proc.kill()
                self._stdout_transport.close()
            try:
                await asyncio.wait_for(self.exited.wait(), 2)
            except asyncio.TimeoutError:
                # A detached, unobserved descendant may still hold the stdout FD.
                self._reader.cancel()
                await asyncio.wait_for(self.exited.wait(), 2)
