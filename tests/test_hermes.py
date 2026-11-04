"""Hermes contract tests: standard-library fixtures only, never a real Hermes binary."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from super_super_harness.adapters import Harness, get_harness
from super_super_harness.adapters import hermes
from super_super_harness.ops import config
from tests.support import write_config


def configuration(home):
    write_config(home, 1, 2)
    cfg = config.load(home)
    install = home / "fake-hermes-install"
    for file in ("hermes_cli/main.py", "acp_adapter/entry.py", ".venv/bin/hermes"):
        target = install / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((Path(__file__).with_name("fake_hermes_acp.py").read_text())
                          if file.endswith("bin/hermes") else "# fake installation marker\n")
    executable = install / ".venv/bin/hermes"
    executable.chmod(0o755)
    cfg.harnesses["hermes"] = {"binary": str(executable)}
    cfg.profiles = {name: replace(p, harness="hermes", env={}) for name, p in cfg.profiles.items()}
    return cfg


def state(home, hid="hermes-session"):
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER);
        INSERT INTO schema_version VALUES(32);
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, tool_calls TEXT, tool_call_id TEXT, reasoning TEXT,
            reasoning_content TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
            display_kind TEXT DEFAULT 'visible');
        CREATE TABLE IF NOT EXISTS session_admissions(
            seq INTEGER PRIMARY KEY, target_session_id TEXT, admission_id TEXT, status TEXT);
    """)
    return db


class HermesLiveGuardTests(unittest.TestCase):
    def test_manual_script_has_a_fail_closed_guard_before_launch_setup(self):
        # Static only: the live script is never imported or executed by this suite.
        import ast
        source = Path(__file__).with_name("hermes_live.py").read_text()
        tree = ast.parse(source)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        self.assertIsInstance(main.body[0], ast.If)
        self.assertEqual(ast.unparse(main.body[0].test), "os.environ.get('SSHARNESS_HERMES_LIVE') != '1'")
        self.assertTrue(any(isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value == 2
                            for node in main.body[0].body))
        imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        roots = {name.name.split('.')[0] for node in imports if isinstance(node, ast.Import) for name in node.names}
        roots |= {node.module.split('.')[0] for node in imports if isinstance(node, ast.ImportFrom)}
        self.assertTrue(roots <= sys.stdlib_module_names)


class HermesConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = configuration(self.root)
        self.profile = self.cfg.profile("work")
        self.mcp = self.root / "mcp-input.json"
        self.mcp.write_text(json.dumps({"mcpServers": {"router": {"command": sys.executable,
            "args": ["-c", "import os; print(os.environ['SSHARNESS_TOKEN'])"],
            "env": {"SSHARNESS_TOKEN": "first-token", "SSHARNESS_SOCK": "/fake/socket"}}}}))

    def launch_spec(self, profile=None, sid="test-session", prompt="worker instructions"):
        argv = hermes.headless_argv(self.cfg, profile or self.profile, sid, False, self.mcp, prompt)
        return argv, json.loads(argv[0])

    def test_lazy_registration_and_alias(self):
        self.assertIs(get_harness(self.cfg, self.profile), hermes)
        self.assertIsInstance(hermes, Harness)
        self.cfg.harnesses["alias"] = {"implementation": "hermes", "binary": "/fake/alias"}
        alias = replace(self.profile, harness="alias")
        self.assertIs(get_harness(self.cfg, alias), hermes)
        self.assertEqual(self.launch_spec(alias)[0][1:], ["/fake/alias", "acp"])
        self.assertTrue(hermes.available(self.cfg, self.profile))
        self.assertFalse(hermes.available(self.cfg, alias))

    def test_installation_check_uses_profile_path_not_the_parent_path(self):
        other = self.root / "other"
        other.mkdir()
        other_cfg = configuration(other)
        clean = Path(hermes.binary(self.cfg, self.profile))
        dirty = Path(hermes.binary(other_cfg, other_cfg.profile("work")))
        (other / "fake-hermes-install" / ".env").write_text("PRIVATE=fixture-only")
        with patch.dict(os.environ, PATH=str(clean.parent)):
            self.assertEqual(hermes._check_install("hermes", {"PATH": str(clean.parent)}), str(clean.resolve()))
            with self.assertRaisesRegex(ValueError, "shared .env"):
                hermes._check_install("hermes", {"PATH": str(dirty.parent)})

    def test_environment_is_clean_and_home_cannot_be_redirected(self):
        with patch.dict(os.environ, HERMES_HOME="/do-not-touch", HERMES_TUI_GATEWAY_URL="bad",
                        ANTHROPIC_API_KEY="ambient-secret", HTTPS_PROXY="bad", OTHER_SECRET="bad"):
            p = replace(self.profile, env={"CUSTOM": "ok", "HERMES_HOME": "/also-forbidden",
                                           "SSHARNESS_HERMES_KEY": "wrong-key",
                                           "HERMES_TUI_GATEWAY_URL": "also-bad"})
            env = hermes.build_env(self.cfg, p, self.root / "tmp")
        for name in ("HERMES_HOME", "HERMES_TUI_GATEWAY_URL", "ANTHROPIC_API_KEY", "HTTPS_PROXY", "OTHER_SECRET"):
            self.assertNotIn(name, env)
        self.assertEqual(env["SSHARNESS_HERMES_KEY"], "dummy-for-tests")
        self.assertEqual(env["HERMES_YOLO_MODE"], "1")
        self.assertEqual(env["TMPDIR"], str(self.root / "tmp"))
        self.assertEqual(env["CUSTOM"], "ok")
        self.assertIn("pushinsteadof", env["GIT_CONFIG_PARAMETERS"])

    def test_private_home_config_and_stable_mcp_bridge_have_no_key_in_argv(self):
        argv, spec = self.launch_spec(replace(self.profile, context_window=200000))
        home = Path(spec["home"])
        self.assertEqual(home, self.root / "hermes" / "homes" / "test-session")
        settings = json.loads((home / "config.yaml").read_text())
        self.assertEqual(settings["providers"]["ssh"]["models"], {"work-model": {"context_length": 200000}})
        self.assertEqual(settings["agent"]["system_prompt"], "worker instructions")
        self.assertEqual(settings["compression"], {"in_place": True})
        self.assertFalse(settings["updates"]["check"])
        self.assertFalse(settings["plugins"]["auto_apply"])
        self.assertEqual(settings["plugins"]["auto_update_check_hours"], 0)
        self.assertNotIn("dummy-for-tests", json.dumps(argv) + (home / "config.yaml").read_text())
        self.assertNotIn("first-token", json.dumps(argv) + (home / "config.yaml").read_text())
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        for name in ("config.yaml", "mcp.json", "ssharness_mcp.py"):
            self.assertEqual((home / name).stat().st_mode & 0o777, 0o600)
        server = settings["mcp_servers"]["router"]
        command = [server["command"], *server["args"]]
        self.assertEqual(subprocess.check_output(command, text=True).strip(), "first-token")
        self.mcp.write_text(self.mcp.read_text().replace("first-token", "new-token"))
        self.launch_spec(prompt="new instructions")
        self.assertEqual(json.loads((home / "config.yaml").read_text())["mcp_servers"], settings["mcp_servers"])
        self.assertEqual(subprocess.check_output(command, text=True).strip(), "new-token")

    def test_unsupported_profiles_are_explicit_and_bearer_uses_key_command(self):
        for field, value in (("tools", "Read"), ("add_dirs", ("/tmp",)), ("settings", "user")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Hermes requires"):
                self.launch_spec(replace(self.profile, **{field: value}))
        self.cfg.providers[self.profile.provider] = replace(self.cfg.providers[self.profile.provider],
                                                            auth_env="ANTHROPIC_AUTH_TOKEN")
        _, spec = self.launch_spec()
        declaration = json.loads((Path(spec["home"]) / "config.yaml").read_text())["providers"]["ssh"]
        self.assertNotIn("key_env", declaration)
        self.assertEqual(subprocess.check_output(declaration["key_cmd"], shell=True, text=True,
                                                env={"SSHARNESS_HERMES_KEY": "test-token"}), "test-token")

    def test_paths_cannot_escape_home(self):
        for sid in ("../x", "/tmp/x", "", "a/b"):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                self.launch_spec(sid=sid)
        (self.root / "hermes").symlink_to(self.root / "elsewhere")
        with self.assertRaisesRegex(ValueError, "SSHARNESS_HOME"):
            self.launch_spec()

    def test_prepare_absent_empty_and_real_user_turn(self):
        self.assertEqual(hermes.prepare_session(self.cfg, "s"), ("s", False))
        _, spec = self.launch_spec(sid="s")
        Path(spec["map"]).write_text(json.dumps({"hermes_id": "hid"}))
        new, resume = hermes.prepare_session(self.cfg, "s")
        self.assertNotEqual(new, "s")
        self.assertFalse(resume)
        with state(Path(spec["home"])) as db:
            db.execute("INSERT INTO messages(session_id,role,content,active,compacted) VALUES(?,?,?,?,?)",
                       ("hid", "user", '\x00json:' + json.dumps([{"type": "text", "text": 'seen id="m1"'}]), 0, 1))
        db.close()
        self.assertEqual(hermes.prepare_session(self.cfg, "s"), ("s", True))
        self.assertTrue(hermes.session_has(self.cfg, "s", 'id="m1"'))
        self.assertFalse(hermes.session_has(self.cfg, "s", "absent"))
        self.assertEqual(hermes.session_file(self.cfg, "s"), Path(spec["home"]) / "state.db")

    def test_interactive_open_requires_mapping_rejects_fork_and_hooks(self):
        for kwargs in ({"fork": True}, {"hooks": {}}, {"hooks": {"Stop": []}}):
            with self.assertRaisesRegex(ValueError, "fork/peek"):
                hermes.interactive_argv(self.cfg, self.profile, "s", self.mcp, "prompt", **kwargs)
        with self.assertRaisesRegex(ValueError, "not been created"):
            hermes.interactive_argv(self.cfg, self.profile, "s", self.mcp, "prompt")
        _, spec = self.launch_spec(sid="s")
        Path(spec["map"]).write_text(json.dumps({"hermes_id": "hid"}))
        self.assertEqual(hermes.interactive_argv(self.cfg, self.profile, "s", None, None),
                         ["/usr/bin/env", "HERMES_HOME=" + spec["home"], hermes.binary(self.cfg, self.profile), "--resume", "hid"])
        home = Path(spec["home"])
        self.assertIn("router", json.loads((home / "mcp.json").read_text())["mcpServers"])
        self.assertEqual(json.loads((home / "config.yaml").read_text())["agent"]["system_prompt"], "worker instructions")

    def test_doctor_checks_acp_dependencies_in_a_disposable_private_home(self):
        from super_super_harness.ops import setup
        self.assertEqual(hermes.installation_check(self.cfg, self.profile), (True, ""))
        self.assertEqual(list((self.root / "hermes").glob("check-*")), [])
        self.cfg.providers[self.profile.provider].key_file.chmod(0o600)
        with patch.object(hermes, "shallow_probe", return_value=True), patch.object(hermes, "deep_check", return_value=(True, "")):
            result = setup.doctor(self.cfg, ["work"])[0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["checks"]["harness_setup"])
        managed = self.root / "managed-profile"
        managed.mkdir()
        profile = replace(self.profile, env={"HERMES_MANAGED_DIR": str(managed)})
        with patch.object(hermes.subprocess, "run") as run:
            self.assertFalse(hermes.installation_check(self.cfg, profile)[0])
            run.assert_not_called()
        (self.root / "fake-hermes-install" / ".env").write_text("PRIVATE=do-not-read")
        with patch.object(hermes.subprocess, "run") as run:
            ok, reason = hermes.installation_check(self.cfg, self.profile)
            self.assertFalse(ok)
            self.assertIn("clean", reason)
            run.assert_not_called()
            self.assertNotIn("do-not-read", reason)

    def test_shared_dotenv_and_managed_installs_are_refused_without_reading_or_executing(self):
        executable = hermes.binary(self.cfg, self.profile)
        managed = self.root / "managed"
        env = {"HERMES_MANAGED_DIR": str(managed)}
        hermes._check_install(executable, env)
        dotenv = self.root / "fake-hermes-install" / ".env"
        dotenv.write_text("PRIVATE_DO_NOT_LOAD=secret\n")
        with patch.object(Path, "read_text", side_effect=AssertionError("no dotenv reads")):
            with self.assertRaisesRegex(ValueError, "shared .env"):
                hermes._check_install(executable, env)
        dotenv.unlink()
        managed.mkdir()
        with self.assertRaisesRegex(ValueError, "managed"):
            hermes._check_install(executable, env)
        with self.assertRaisesRegex(ValueError, "source-tree"):
            hermes._check_install(sys.executable, {})

    def test_saved_session_cannot_send_new_provider_key_to_frozen_endpoint(self):
        _, spec = self.launch_spec(sid="s")
        Path(spec["map"]).write_text(json.dumps({"hermes_id": "hid"}))
        original = (Path(spec["home"]) / "config.yaml").read_bytes()
        self.cfg.providers[self.profile.provider] = replace(self.cfg.providers[self.profile.provider],
                                                            base_url="https://other.invalid")
        for interactive in (False, True):
            with self.subTest(interactive=interactive), self.assertRaisesRegex(ValueError, "Provider endpoint"):
                if interactive:
                    hermes.interactive_argv(self.cfg, self.profile, "s", None, None)
                else:
                    self.launch_spec(sid="s")
        self.assertEqual((Path(spec["home"]) / "config.yaml").read_bytes(), original)


class HermesProcessTests(unittest.IsolatedAsyncioTestCase):
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

    async def event(self, message):
        self.events.append(message)

    async def exited(self, code):
        self.exits.append(code)

    def process(self, resume=False, env=None):
        argv = hermes.headless_argv(self.cfg, self.profile, "s", resume, None, "fixture instructions")
        process = hermes.Process(argv, hermes.build_env(self.cfg, self.profile) | (env or {}),
                                 self.root, self.event, self.exited, log_path=self.root / "process.log")
        self.processes.append(process)
        return process

    async def until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(.01)
        await asyncio.wait_for(poll(), 5)

    def results(self):
        return [e for e in self.events if e["type"] == "result"]

    def wire(self, process):
        path = Path(process.spec["home"]) / "wire.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def test_spawn_pins_the_executable_validated_in_profile_path(self):
        executable = Path(hermes.binary(self.cfg, self.profile))
        named = executable.with_name("fixture-hermes-bin")
        named.write_bytes(executable.read_bytes())
        named.chmod(0o755)
        self.cfg.harnesses["hermes"]["binary"] = named.name
        self.profile = replace(self.profile, env={"PATH": str(named.parent) + os.pathsep + os.environ.get("PATH", os.defpath)})
        process = self.process()
        with patch.object(asyncio, "create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn:
            await process.start()
            self.assertEqual(spawn.call_args.args[0], str(named.resolve()))
        await process.close(wait=.1)

    async def test_handshake_queue_ack_after_prompt_and_cancel_mapping(self):
        from super_super_harness.adapters import HarnessProcess
        process = self.process()
        self.assertIsInstance(process, HarnessProcess)
        self.assertIsNone(process.send("too early"))
        self.assertEqual(await process.start(), process.pid)
        frames = self.wire(process)
        self.assertEqual([f["method"] for f in frames],
                         ["initialize", "session/new", "session/set_model", "session/set_mode"])
        self.assertEqual(frames[0]["params"]["clientCapabilities"], {})
        self.assertEqual(frames[1]["params"]["mcpServers"], [])
        self.assertEqual(frames[2]["params"]["modelId"], "custom:ssh:work-model")
        self.assertEqual(process.send("WAIT", "first"), "first")
        self.assertEqual(process.send("second", "second"), "second")
        await self.until(lambda: hermes.session_has(self.cfg, "s", "WAIT"))
        self.assertFalse(any(e.get("isReplay") for e in self.events))
        self.assertEqual(len([f for f in self.wire(process) if f.get("method") == "session/prompt"]), 1)
        self.assertTrue(process.interrupt())
        await self.until(lambda: len(self.results()) == 2)
        receipts = [e["uuid"] for e in self.events if e.get("isReplay")]
        self.assertEqual(receipts, ["first", "second"])
        self.assertEqual([r["subtype"] for r in self.results()], ["error_during_execution", "success"])
        for ident in receipts:
            ack = next(i for i, e in enumerate(self.events) if e.get("isReplay") and e["uuid"] == ident)
            self.assertEqual(self.events[ack + 1]["type"], "result")
        self.assertEqual(self.results()[-1]["result"], "fake answer")
        await asyncio.gather(process.close(wait=.1), process.close(wait=.1))
        await process.close()
        self.assertEqual(len(self.exits), 1)
        self.assertTrue(process.exited.is_set())
        self.assertIsNone(process.send("closed"))
        self.assertFalse(process.interrupt())

    async def test_stream_coalescing_tools_and_permission_requests(self):
        process = self.process()
        await process.start()
        process.send("TOOLS PERMISSIONS")
        await self.until(lambda: len(self.results()) == 1)
        self.assertFalse(self.results()[0]["is_error"])
        assistants = [e for e in self.events if e["type"] == "assistant"]
        blocks = [b for e in assistants for b in e["message"]["content"]]
        self.assertEqual([b["text"] for b in blocks if b["type"] == "text"], ["comment", "fake answer"])
        tools = [b for b in blocks if b["type"] == "tool_use"]
        self.assertEqual(tools[0], {"type": "tool_use", "id": "call-1", "name": "mcp__router__report_progress",
                                    "input": {"text": "working"}})
        self.assertTrue(all(e["parent_tool_use_id"] is None for e in assistants))
        outputs = [e["message"]["content"][0] for e in self.events if e["type"] == "user" and not e.get("isReplay")]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0]["content"], "ok")
        self.assertTrue(outputs[1]["is_error"])

    async def test_load_discards_history_and_resolves_unknown_before_mode(self):
        first = self.process()
        await first.start()
        first.send("first")
        await self.until(lambda: self.results())
        await first.close(wait=.1)
        home = Path(first.spec["home"])
        with sqlite3.connect(home / "state.db") as db:
            db.execute("INSERT INTO session_admissions(target_session_id,admission_id,status) VALUES(?,?,?)",
                       (first.spec["hermes_id"], "unknown-1", "unknown"))
        self.events.clear()
        resumed = self.process(resume=True)
        await resumed.start()
        self.assertEqual([e["type"] for e in self.events], ["system"])
        frames = self.wire(resumed)
        load_index = next(i for i, f in enumerate(frames) if f.get("method") == "session/load")
        self.assertEqual(frames[load_index + 1]["params"]["prompt"], [{"type": "text", "text": "/discard unknown-1"}])
        self.assertEqual(frames[load_index + 2]["method"], "session/set_model")
        resumed.send("after resume")
        await self.until(lambda: self.results())
        self.assertEqual(self.results()[0]["result"], "fake answer")
        self.assertEqual(hermes.prepare_session(self.cfg, "s"), ("s", True))

    async def test_error_response_and_disconnect_are_safe_unacknowledged_failures(self):
        for command in ("ERROR", "CRASH"):
            with self.subTest(command=command):
                self.events.clear()
                process = self.process(resume=bool(self.processes))
                await process.start()
                process.send(command)
                await self.until(lambda: self.results())
                self.assertTrue(self.results()[0]["is_error"])
                self.assertEqual(self.results()[0]["subtype"], "error")
                self.assertFalse(any(e.get("isReplay") for e in self.events))
                self.assertNotIn("private", json.dumps(self.events))
                await asyncio.wait_for(process.exited.wait(), 3)
                self.assertFalse(process.closing, "error exit must trigger normal Router recovery")
                self.assertIsNone(process.send("must restart first"))
                await process.close(wait=.1)

    async def test_response_without_persisted_input_never_acknowledges(self):
        for command, subtype in (("NO_STORE", "error"), ("CANCEL_BEFORE", "error_during_execution")):
            with self.subTest(command=command):
                self.events.clear()
                process = self.process()
                await process.start()
                process.send(command)
                await self.until(lambda: self.results())
                self.assertFalse(any(e.get("isReplay") for e in self.events))
                self.assertEqual(self.results()[0]["subtype"], subtype)
                await process.close(wait=.1)

    async def test_handshake_failure_timeout_and_concurrent_close_clean_up(self):
        from super_super_harness.adapters import hermes_acp
        for env in ({"SSHARNESS_FAKE_FAIL_METHOD": "session/set_mode"},
                    {"SSHARNESS_FAKE_HANG_METHOD": "initialize"}):
            with self.subTest(env=env), patch.object(hermes_acp, "HANDSHAKE_TIMEOUT", .15):
                process = self.process(env=env)
                with self.assertRaisesRegex(ValueError, "startup failed"):
                    await asyncio.wait_for(process.start(), 3)
                self.assertTrue(process.exited.is_set())
                self.assertIsNotNone(process.proc.returncode)
        process = self.process(env={"SSHARNESS_FAKE_HANG_METHOD": "initialize"})
        starting = asyncio.create_task(process.start())
        await self.until(lambda: process.pid is not None)
        await asyncio.wait_for(process.close(wait=.1), 3)
        with self.assertRaises(ValueError):
            await starting
        self.assertEqual(len(self.exits), 3)

    async def test_close_during_last_startup_await_does_not_publish_readiness(self):
        process = self.process()
        entered, release = asyncio.Event(), asyncio.Event()
        remember = process._remember_children
        starting = None
        async def blocked_remember():
            if asyncio.current_task() is starting:
                entered.set()
                await release.wait()
            else:
                await remember()
        with patch.object(process, "_remember_children", side_effect=blocked_remember):
            starting = asyncio.create_task(process.start())
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(process.close(wait=.1), 3)
            release.set()
            with self.assertRaises(ValueError):
                await starting
        self.assertFalse(any(e.get("subtype") == "init" for e in self.events))
        self.assertEqual(len(self.exits), 1)

    async def test_parent_exit_with_inherited_stdout_does_not_hang_or_leak_group(self):
        from super_super_harness.adapters import procs
        process = self.process()
        await process.start()
        process.send("ORPHAN_PIPE")
        pid_file = Path(process.spec["home"]) / "child.pid"
        await self.until(pid_file.exists)
        child = int(pid_file.read_text())
        try:
            await asyncio.wait_for(process.exited.wait(), 3)
            await asyncio.wait_for(process.close(wait=.1), 3)
            for _ in range(100):
                if not await asyncio.to_thread(procs.alive, child):
                    break
                await asyncio.sleep(.02)
            self.assertFalse(await asyncio.to_thread(procs.alive, child))
            self.assertEqual(len(self.exits), 1)
        finally:
            procs.kill([child])

    async def test_close_kills_child_with_its_own_process_group(self):
        from super_super_harness.adapters import procs
        process = self.process()
        await process.start()
        process.send("WAIT CHILD")
        pid_file = Path(process.spec["home"]) / "child.pid"
        await self.until(pid_file.exists)
        child = int(pid_file.read_text())
        self.assertTrue(await asyncio.to_thread(procs.alive, child))
        await asyncio.wait_for(process.close(wait=.1), 5)
        for _ in range(100):
            if not await asyncio.to_thread(procs.alive, child):
                break
            await asyncio.sleep(.02)
        self.assertFalse(await asyncio.to_thread(procs.alive, child))
        self.assertEqual(len(self.exits), 1)
