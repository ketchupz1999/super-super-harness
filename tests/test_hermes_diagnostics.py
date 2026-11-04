"""B36 fake-process regressions: pre-provisioned runtime, startup budgets and safe stderr."""
import asyncio
import base64
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from super_super_harness.adapters import hermes, hermes_acp, hermes_diagnostics, procs
from tests.test_hermes import configuration


class LiveDependencyTests(unittest.TestCase):
    def test_missing_mcp_is_not_mistaken_for_a_complete_acp_installation(self):
        from tests.hermes_live import runtime_dependencies
        payload = {"python": "3.14.7", "modules": {"acp": True, "mcp": False, "anthropic": True}}
        with patch("tests.hermes_live.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))) as run:
            result = runtime_dependencies("/fake/venv/bin/hermes", Path("/private/fixture"))
        self.assertTrue(result["modules"]["acp"])
        self.assertFalse(result["modules"]["mcp"])
        self.assertEqual(run.call_args.args[0][0:2], ["/fake/venv/bin/python", "-I"])
        self.assertNotIn("SSHARNESS_HERMES_KEY", run.call_args.kwargs["env"])

    def test_dependency_probe_resolves_the_same_console_symlink_as_launch(self):
        from tests.hermes_live import runtime_dependencies
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            executable = root / "venv/bin/hermes"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture")
            link = root / "hermes-link"
            link.symlink_to(executable)
            payload = {"python": "3.14.7", "modules": {"acp": True, "mcp": True, "anthropic": True}}
            with patch("tests.hermes_live.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))) as run:
                runtime_dependencies(link, root)
            self.assertEqual(run.call_args.args[0][0], str(executable.parent / "python"))

    def test_live_wait_budget_covers_the_handshake_and_fixture_execution(self):
        from tests.hermes_live import wait_until
        self.assertGreaterEqual(inspect.signature(wait_until).parameters["timeout"].default, hermes_acp.HANDSHAKE_TIMEOUT + 60)

    def test_dependency_probe_failure_does_not_expose_output_or_pass(self):
        from tests.hermes_live import runtime_dependencies
        with patch("tests.hermes_live.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="private-diagnostic")):
            result = runtime_dependencies("/fake/venv/bin/hermes", Path("/private/fixture"))
        self.assertFalse(any(result["modules"].values()))
        self.assertNotIn("private-diagnostic", json.dumps(result))


class HermesStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = configuration(self.root)
        self.profile = self.cfg.profile("work")
        self.events, self.exits, self.processes = [], [], []
        self.addAsyncCleanup(self.close_processes)

    async def close_processes(self):
        for process in self.processes:
            await asyncio.wait_for(process.close(wait=.1), 5)

    def process(self, env=None, mcp=None):
        async def event(message):
            self.events.append(message)
        async def exited(code):
            self.exits.append(code)
        argv = hermes.headless_argv(self.cfg, self.profile, "s", False, mcp, "fixture")
        process = hermes.Process(argv, hermes.build_env(self.cfg, self.profile) | (env or {}), self.root,
                                 event, exited, log_path=self.root / "worker.log")
        self.processes.append(process)
        return process

    async def test_runtime_contract_is_in_place_before_initialize(self):
        process = self.process({"SSHARNESS_FAKE_REQUIRE_RUNTIME": "1", "HERMES_DISABLE_LAZY_INSTALLS": "0",
                                "HERMES_RUNTIME_DIR": "/not-the-private-runtime"})
        await asyncio.wait_for(process.start(), 3)
        installs = Path(process.spec["home"]) / "installs"
        self.assertEqual(list(installs.glob("*/environments")), [])
        self.assertEqual(list(installs.glob("*/pm-runtime/generations")), [])
        self.assertEqual(hermes_acp.HANDSHAKE_TIMEOUT, 120)
        self.assertEqual(hermes.build_env(self.cfg, self.profile)["HERMES_RUNTIME_DIR"], str(self.root / "hermes/runtime"))
        self.assertEqual(hermes.build_env(self.cfg, self.profile)["HERMES_DISABLE_LAZY_INSTALLS"], "1")

    async def test_configured_handshake_timeout_overrides_default(self):
        self.cfg.harnesses["hermes"]["handshake_timeout"] = 1
        process = self.process({"SSHARNESS_FAKE_INITIALIZE_DELAY": ".1"})
        with patch.object(hermes_acp, "HANDSHAKE_TIMEOUT", .01):
            await asyncio.wait_for(process.start(), 3)
        self.assertEqual(process.spec["handshake_timeout"], 1)

    async def test_invalid_handshake_budgets_are_rejected(self):
        for value in (0, -1, True, "120", float("inf"), float("nan"), None):
            with self.subTest(value=value):
                self.cfg.harnesses["hermes"]["handshake_timeout"] = value
                with self.assertRaisesRegex(ValueError, "handshake_timeout"):
                    self.process()

    async def test_failed_start_keeps_bounded_redacted_stderr_not_provider_secrets(self):
        mcp = self.root / "mcp-input.json"
        mcp.write_text(json.dumps({"mcpServers": {"router": {"command": "unused", "args": [],
                                                           "env": {"SSHARNESS_TOKEN": "mcp-sensitive-value"}}}}))
        lines = ["old diagnostic %d" % i for i in range(100)]
        lines += ["fatal: dependency unavailable", "x-api-key: dummy-for-tests", "MCP failed: mcp-sensitive-value",
                  "Authorization: Bearer unrelated-sensitive-value", "api_key='quoted sensitive value'"]
        process = self.process({"SSHARNESS_FAKE_STDERR": "\n".join(lines) + "\n", "SSHARNESS_FAKE_STDERR_FLOOD": "1",
                                "SSHARNESS_FAKE_FAIL_METHOD": "initialize"}, mcp)
        with self.assertRaises(ValueError) as caught:
            await asyncio.wait_for(process.start(), 3)
        log = (self.root / "worker.log").read_text()
        combined = log + str(caught.exception)
        for value in ("dummy-for-tests", "mcp-sensitive-value", "unrelated-sensitive-value", "quoted sensitive value"):
            self.assertNotIn(value, combined)
        self.assertIn("fatal: dependency unavailable", log)
        self.assertIn("fatal: dependency unavailable", str(caught.exception))
        self.assertIn("old diagnostic 99", log)
        self.assertNotIn("old diagnostic 0\n", log)
        self.assertLessEqual(len(process.stderr.lines), hermes_diagnostics.LINES)
        self.assertLessEqual(len(process.stderr.text().encode()), hermes_diagnostics.BYTES)
        self.assertEqual((self.root / "worker.log").stat().st_mode & 0o777, 0o600)
        self.assertTrue(process.exited.is_set())
        self.assertEqual(len(self.exits), 1)

    async def test_stderr_is_separate_from_the_jsonrpc_stream_and_saved_on_close(self):
        process = self.process({"SSHARNESS_FAKE_STDERR": "diagnostic warning key=dummy-for-tests\n"})
        await process.start()
        process.send("hello")
        async def result():
            while not any(e["type"] == "result" for e in self.events):
                await asyncio.sleep(.01)
        await asyncio.wait_for(result(), 3)
        await process.close(wait=.1)
        self.assertIn("diagnostic warning", (self.root / "worker.log").read_text())
        self.assertNotIn("dummy-for-tests", (self.root / "worker.log").read_text())
        self.assertNotIn("diagnostic warning", json.dumps(self.events))
        self.assertEqual([e["result"] for e in self.events if e["type"] == "result"], ["fake answer"])

    async def test_large_lines_and_split_credentials_are_drained_without_leaking(self):
        tail = hermes_diagnostics.StderrTail({"API_KEY": "split-sensitive-value"}, self.root)
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100000 + b"\nnormal split-sensitive-")
        stream.feed_data(b"value\nAuthorization: Bearer extra-sensitive\n")
        stream.feed_eof()
        await tail.read(stream)
        self.assertIn("line omitted", tail.text())
        self.assertNotIn("split-sensitive", tail.text())
        self.assertNotIn("extra-sensitive", tail.text())
        self.assertLessEqual(len(tail.text().encode()), hermes_diagnostics.BYTES)

    async def test_authorization_schemes_are_redacted_as_complete_values(self):
        encoded = base64.b64encode(b"proxy-user:proxy-secret").decode()
        tail = hermes_diagnostics.StderrTail({"PASSWORD": "proxy-secret"}, self.root)
        tail.append(("Proxy-Authorization: Basic " + encoded).encode())
        tail.append(b'Authorization: Digest username="proxy-user", nonce="nonce-secret", response="digest-proof"')
        tail.append(b'headers={"authorization": "Negotiate ticket-secret"}')
        tail.append(b'proxy connection failed: https://other-user:other-password@example.invalid/path')
        for value in (encoded, "proxy-user", "nonce-secret", "digest-proof", "ticket-secret", "other-user", "other-password"):
            self.assertNotIn(value, tail.text())
        self.assertIn("Proxy-Authorization:", tail.text())

    async def test_detached_stderr_holder_cannot_block_exit_notification(self):
        process = self.process()
        child = None
        with patch.object(process, "_remember_children", return_value=None):
            await process.start()
            process.send("ORPHAN_STDERR")
            pid_file = Path(process.spec["home"]) / "child.pid"
            async def started():
                while not pid_file.exists():
                    await asyncio.sleep(.01)
            try:
                await asyncio.wait_for(started(), 3)
                child = int(pid_file.read_text())
                self.assertEqual(process._children, {}, "exercise a descendant missed by the snapshot")
                await asyncio.wait_for(process.close(wait=.1), 3)
                self.assertTrue(process.exited.is_set())
                self.assertEqual(len(self.exits), 1)
                self.assertTrue(process._stderr_transport.is_closing())
            finally:
                if child:
                    procs.kill([child])   # intentionally unobserved synthetic child belongs to this test

    async def test_close_aborts_buffered_stdin_even_after_close_has_started(self):
        process = self.process({"SSHARNESS_FAKE_INITIALIZE_DELAY": "60"})
        starting = asyncio.create_task(process.start())
        wire = Path(process.spec["home"]) / "wire.jsonl"
        async def entered():
            while not wire.exists() or "initialize" not in wire.read_text():
                await asyncio.sleep(.01)
        try:
            await asyncio.wait_for(entered(), 3)
            process.proc.stdin.write(b" " * (1024 * 1024))
            process.proc.stdin.close()
            self.assertTrue(process.proc.stdin.is_closing())
            self.assertGreater(process.proc.stdin.transport.get_write_buffer_size(), 0)
            await asyncio.wait_for(process.close(wait=.05), 3)
            with self.assertRaises(ValueError):
                await starting
            self.assertTrue(process.exited.is_set())
            self.assertEqual(len(self.exits), 1)
        finally:
            if process.proc and process.proc.returncode is None:
                procs.kill_tree(process.pid)
            if not starting.done():
                starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)

    async def test_log_symlink_is_not_followed(self):
        destination = self.root / "unchanged"
        destination.write_text("keep")
        link = self.root / "log-link"
        link.symlink_to(destination)
        tail = hermes_diagnostics.StderrTail({}, self.root)
        tail.append(b"diagnostic")
        with self.assertRaises(OSError):
            tail.save(link)
        self.assertEqual(destination.read_text(), "keep")
