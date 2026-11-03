"""A second implementation runs through unchanged application entry points."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from super_super_harness import cli, config
from super_super_harness.adapters import Harness, HarnessProcess, get_harness, register
from super_super_harness.daemon import Service
from tests.fake_harness import FakeHarness
from tests.support import write_config


class PushGuardTests(unittest.TestCase):
    def setUp(self):
        from super_super_harness.adapters.claude_code import PUSH_BLOCK
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.ssh_log = self.root / "ssh-called"
        ssh = self.root / "fake-ssh"
        ssh.write_text("#!/bin/sh\nprintf '%s\\n' called >> " + shlex.quote(str(self.ssh_log)) + "\nexit 93\n")
        ssh.chmod(0o755)
        # No user git/SSH config or real network, even when testing the unguarded baseline.
        self.env = {"PATH": os.defpath, "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ALLOW_PROTOCOL": "file:ssh", "GIT_SSH_COMMAND": shlex.quote(str(ssh)),
                    "GIT_SSH_VARIANT": "ssh"}
        self.blocked_env = dict(self.env, GIT_CONFIG_PARAMETERS=PUSH_BLOCK)
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("-c", "user.email=test@example.invalid", "-c", "user.name=test",
                 "commit", "-qm", "seed", "--allow-empty")
        self.git("init", "-q", "--bare", str(self.remote))
        self.git("remote", "add", "origin", "../remote.git")
        self.git("push", "-q", "origin", "main")

    def git(self, *args, blocked=False, check=True):
        return subprocess.run(["git", *args], cwd=self.repo,
                              env=self.blocked_env if blocked else self.env,
                              capture_output=True, text=True, check=check, timeout=10)

    def assert_push_blocked(self, url):
        self.git("remote", "set-url", "origin", url)
        rewritten = self.git("remote", "get-url", "--push", "origin", blocked=True).stdout.strip()
        pushed = self.git("push", "origin", "HEAD:refs/heads/guard-check", blocked=True, check=False)
        self.assertNotEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertTrue(rewritten.startswith("ssharness-push-blocked://x/"), rewritten)
        self.assertIn("ssharness-push-blocked", pushed.stderr)
        self.assertFalse(self.ssh_log.exists(), "push must be blocked before invoking SSH")
        absent = self.git("--git-dir=" + str(self.remote), "show-ref", "--verify", "--quiet", "refs/heads/guard-check", check=False)
        self.assertEqual(absent.returncode, 1, absent.stdout + absent.stderr)

    def test_relative_path_push_is_blocked(self):
        self.assert_push_blocked("../remote.git")

    def test_ssh_alias_push_is_blocked(self):
        self.assert_push_blocked("github-work:org/x.git")

    def test_deploy_user_push_is_blocked(self):
        self.assert_push_blocked("deploy@host:x.git")

    def test_git_protocol_push_is_rewritten_without_network(self):
        self.assert_push_blocked("git://host/x.git")

    def test_existing_url_prefixes_remain_blocked(self):
        for url in ("https://host/x.git", "http://host/x.git", "git@host:x.git",
                    "ssh://host/x.git", str(self.remote), self.remote.as_uri()):
            with self.subTest(url=url):
                self.assert_push_blocked(url)

    def test_fetch_and_pull_still_update_local_refs_and_worktree(self):
        self.git("branch", "--set-upstream-to=origin/main", "main")
        upstream = self.root / "upstream"
        self.git("clone", "-q", "--branch", "main", str(self.remote), str(upstream))
        (upstream / "fetched.txt").write_text("upstream change\n")
        for command in (["add", "fetched.txt"], ["-c", "user.email=test@example.invalid", "-c", "user.name=test",
                         "commit", "-qm", "upstream change"], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", *command], cwd=upstream, env=self.env, check=True, capture_output=True, timeout=10)
        expected = self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/main").stdout
        self.assertNotEqual(self.git("rev-parse", "HEAD").stdout, expected)
        fetched = self.git("fetch", "origin", blocked=True, check=False)
        self.assertEqual(fetched.returncode, 0, fetched.stdout + fetched.stderr)
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main").stdout, expected)
        pulled = self.git("pull", "--ff-only", "origin", "main", blocked=True, check=False)
        self.assertEqual(pulled.returncode, 0, pulled.stdout + pulled.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, expected)
        self.assertEqual((self.repo / "fetched.txt").read_text(), "upstream change\n")

    def test_explicit_pushurl_is_a_documented_limit_not_a_security_boundary(self):
        self.git("remote", "set-url", "--push", "origin", str(self.remote))
        pushed = self.git("push", "origin", "HEAD:refs/heads/explicit-pushurl", blocked=True, check=False)
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/explicit-pushurl").stdout,
                         self.git("rev-parse", "HEAD").stdout)


class HomePermissionsTests(unittest.TestCase):
    def make_service(self, home):
        write_config(home, 1, 2)
        service = Service(config.load(home))
        self.addCleanup(service.store.close)
        return service

    def test_existing_home_is_private_before_private_state_is_opened(self):
        from super_super_harness.store import Store
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            home.chmod(0o755)
            def open_store(path):
                self.assertEqual(home.stat().st_mode & 0o7777, 0o700)
                return Store(path)
            with patch("super_super_harness.daemon.Store", side_effect=open_store):
                self.make_service(home)
            self.assertEqual(home.stat().st_mode & 0o7777, 0o700)

    def test_existing_private_home_is_not_chmoded(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            home.chmod(0o700)
            with patch.object(Path, "chmod", autospec=True) as chmod:
                self.make_service(home)
                chmod.assert_not_called()
            self.assertEqual(home.stat().st_mode & 0o7777, 0o700)

    def test_new_service_home_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "new-home"
            cfg = config.load(home)
            old_umask = os.umask(0o022)
            try:
                service = Service(cfg)
                self.addCleanup(service.store.close)
            finally:
                os.umask(old_umask)
            self.assertEqual(home.stat().st_mode & 0o7777, 0o700)


class ProfileBoundaryTests(unittest.TestCase):
    def test_nondefault_sandbox_is_rejected(self):
        for sandbox in ("workspace", "docker", "", None):
            with self.subTest(sandbox=sandbox), self.assertRaisesRegex(ValueError, "sandbox.*none"):
                config.Profile("test", "claude-code", "custom", "test-model", sandbox=sandbox)

    def test_nonlocal_location_is_rejected(self):
        for location in ("remote", "ssh", "", None):
            with self.subTest(location=location), self.assertRaisesRegex(ValueError, "location.*local"):
                config.Profile("test", "claude-code", "custom", "test-model", location=location)

    def test_private_config_rejects_unsupported_profile_boundaries(self):
        from super_super_harness import setup
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            raw = setup.read_raw(home / "profiles.toml")
            for field, value in (("sandbox", "workspace"), ("location", "remote")):
                with self.subTest(field=field):
                    raw["profiles"]["work"][field] = value
                    (home / "profiles.toml").write_text(setup.toml_text(raw))
                    with self.assertRaisesRegex(ValueError, field):
                        config.load(home)
                    del raw["profiles"]["work"][field]

    def test_defaults_and_explicit_local_unsandboxed_profiles_are_accepted(self):
        default = config.Profile("test", "claude-code", "custom", "test-model")
        self.assertEqual((default.sandbox, default.location), ("none", "local"))
        self.assertEqual(default, replace(default, sandbox="none", location="local"))


class ServeConfigurationTests(unittest.TestCase):
    def check_missing_configuration(self, existing):
        from contextlib import redirect_stderr
        from super_super_harness.daemon import serve
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'private'
            if existing:
                home.mkdir()
                (home / 'router.db').write_bytes(b'existing installation marker')
            with patch('super_super_harness.daemon.config.home_path', return_value=home), \
                    patch('super_super_harness.daemon.config.load') as load, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(serve(), 1)
                load.assert_not_called()
            self.assertFalse((home / 'service.pid').exists())
            return errors.getvalue()

    def test_existing_installation_missing_config_requests_migration(self):
        error = self.check_missing_configuration(existing=True)
        self.assertIn('supersuperharness init --migrate-from', error)

    def test_new_installation_missing_config_requests_init(self):
        error = self.check_missing_configuration(existing=False)
        self.assertIn('supersuperharness init before serve', error)
        self.assertNotIn('--migrate-from', error)


class PublicConfigurationCommandTests(unittest.TestCase):
    def command(self, home, *args):
        import sys
        return subprocess.run([sys.executable, "-m", "super_super_harness.cli", *args],
                              cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=5,
                              env=dict(os.environ, SSHARNESS_HOME=str(home), SSHARNESS_NO_CAFFEINATE="1"))

    def test_profile_boundary_errors_are_actionable_in_every_cli_path_without_secrets(self):
        from super_super_harness import setup
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            original = setup.read_raw(home / "profiles.toml")
            original["providers"]["one"]["ensure"] = "/usr/bin/false fake-private-command-secret"
            for field, value in (("sandbox", "docker"), ("location", "remote")):
                original["profiles"]["main"][field] = value
                (home / "profiles.toml").write_text(setup.toml_text(original))
                for args in (("init", "--profile", "new", "--provider", "deepseek", "--key-env", "SSHARNESS_TEST_KEY"),
                             ("doctor",), ("serve",), ("chat",)):
                    with self.subTest(field=field, command=args[0]):
                        result = self.command(home, *args)
                        self.assertEqual(result.returncode, 1)
                        self.assertIn(field, result.stderr)
                        self.assertNotIn("fake-private-command-secret", result.stdout + result.stderr)
                        self.assertNotIn("dummy-for-tests", result.stdout + result.stderr)
                        self.assertNotIn("--migrate-from", result.stderr)
                        self.assertNotIn("Traceback", result.stdout + result.stderr)
                del original["profiles"]["main"][field]

    def test_init_checkout_refusal_explains_reason_for_environment_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder)
            (checkout / ".git").write_text("gitdir: fake-worktree\n")
            result = self.command(checkout / "private", "init", "--provider", "deepseek", "--key-env", "SSHARNESS_TEST_KEY")
            self.assertEqual(result.returncode, 1)
            self.assertIn("outside a git checkout", result.stderr)
            self.assertNotIn("for credential storage", result.stderr)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertFalse((checkout / "private" / "profiles.toml").exists())

    def test_unexpected_configuration_errors_still_hide_private_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "profiles.toml").write_text('private-secret = = = "do-not-print"')
            for args in (("init", "--provider", "deepseek", "--key-env", "SSHARNESS_TEST_KEY"),
                         ("doctor",), ("serve",), ("chat",)):
                with self.subTest(command=args[0]):
                    result = self.command(home, *args)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotIn("private-secret", result.stdout + result.stderr)
                    self.assertNotIn("do-not-print", result.stdout + result.stderr)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)


class AttachCommandTests(unittest.TestCase):
    def attach(self, code=0, handback_error=None, started=True, interactive_error=None):
        from contextlib import redirect_stderr
        from unittest.mock import Mock
        client = Mock()
        def call(method):
            if method == "router_detach":
                return {"started": started, "profile": "main", "session": "session"}
            if method == "router_mcp":
                return {"path": "fake-mcp"}
            if method == "router_attach" and handback_error:
                raise handback_error
            return {"ok": True}
        client.call.side_effect = call
        output, errors = io.StringIO(), io.StringIO()
        with patch("super_super_harness.cli.Client", return_value=client), \
                patch("super_super_harness.cli.exec_interactive", return_value=code, side_effect=interactive_error), \
                redirect_stdout(output), redirect_stderr(errors):
            result = cli.cmd_attach(SimpleNamespace(router_dir=Path("unused")), SimpleNamespace())
        self.assertEqual([c.args[0] for c in client.call.call_args_list].count("router_attach"), 1)
        client.reader.close.assert_called_once_with()
        client.sock.close.assert_called_once_with()
        return result, output.getvalue() + errors.getvalue()

    def test_success_reports_handback(self):
        result, output = self.attach()
        self.assertEqual(result, 0)
        self.assertIn("Router 已交回后台。", output)

    def test_interactive_failure_preserves_exit_code_and_gives_next_step(self):
        result, output = self.attach(code=7)
        self.assertEqual(result, 7)
        self.assertNotIn("Router 已交回后台。", output)
        self.assertIn("supersuperharness attach", output)
        self.assertIn("首次引导和确认选项", output)
        self.assertNotIn("凭据环境变量", output)

    def test_handback_failure_returns_nonzero_and_recovery_instructions(self):
        for error in (ConnectionRefusedError(), SystemExit("error: failed")):
            with self.subTest(error=type(error).__name__):
                result, output = self.attach(handback_error=error)
                self.assertEqual(result, 1)
                self.assertNotIn("Router 已交回后台。", output)
                self.assertIn("supersuperharness status", output)
                self.assertIn("supersuperharness attach", output)

    def test_interactive_launch_failure_is_actionable(self):
        result, output = self.attach(interactive_error=FileNotFoundError("fake-private-launch-diagnostic"))
        self.assertEqual(result, 1)
        self.assertNotIn("Router 已交回后台。", output)
        self.assertIn("supersuperharness attach", output)
        self.assertIn("当前终端的凭据环境变量", output)
        self.assertIn("claude 可执行文件", output)
        self.assertNotIn("首次引导", output)
        self.assertNotIn("fake-private-launch-diagnostic", output)

    def test_missing_session_does_not_report_success(self):
        result, output = self.attach(started=False)
        self.assertEqual(result, 1)
        self.assertIn("先发一条消息", output)
        self.assertNotIn("Router 已交回后台。", output)


class AttachFailureBoundaryTests(unittest.TestCase):
    def test_value_errors_at_each_attach_stage_are_safe_and_close_the_client(self):
        from contextlib import redirect_stderr
        from unittest.mock import Mock
        for stage in ("detach", "mcp", "interactive", "handback"):
            with self.subTest(stage=stage):
                client = Mock()
                def call(method):
                    if method == {"detach": "router_detach", "mcp": "router_mcp", "handback": "router_attach"}.get(stage):
                        raise json.JSONDecodeError("fake-private-diagnostic", "", 0)
                    return {"started": True, "profile": "main", "session": "session", "path": "fake-mcp", "ok": True}
                client.call.side_effect = call
                output, errors = io.StringIO(), io.StringIO()
                launch_error = ValueError("fake-private-diagnostic") if stage == "interactive" else None
                with patch("super_super_harness.cli.Client", return_value=client), \
                        patch("super_super_harness.cli.exec_interactive", return_value=7 if stage == "handback" else 0,
                              side_effect=launch_error), redirect_stdout(output), redirect_stderr(errors):
                    result = cli.cmd_attach(SimpleNamespace(router_dir=Path("unused")), SimpleNamespace())
                self.assertEqual(result, 7 if stage == "handback" else 1)
                client.reader.close.assert_called_once_with()
                client.sock.close.assert_called_once_with()
                text = output.getvalue() + errors.getvalue()
                self.assertNotIn("fake-private-diagnostic", text)
                self.assertNotIn("Router 已交回后台。", text)
                self.assertIn("supersuperharness attach", errors.getvalue())
                if stage == "handback":
                    self.assertIn("交回后台失败", errors.getvalue())
                    self.assertIn("supersuperharness status", errors.getvalue())

    def test_client_creation_value_error_is_safely_reported(self):
        from contextlib import redirect_stderr
        with patch("super_super_harness.cli.Client", side_effect=ValueError("fake-private-diagnostic")), \
                redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.cmd_attach(None, None), 1)
        self.assertIn("supersuperharness status", errors.getvalue())
        self.assertNotIn("fake-private-diagnostic", errors.getvalue())

    def test_missing_terminal_environment_key_has_no_traceback_or_diagnostic_leak(self):
        from contextlib import redirect_stderr
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            cfg = config.load(home)
            cfg.providers["one"] = replace(cfg.providers["one"], key_file=None, key_env="SSHARNESS_ATTACH_TEST_KEY")
            client = Mock()
            client.call.side_effect = [{"started": True, "profile": "main", "session": "session"},
                                       {"path": "fake-mcp"}, {"ok": True}]
            with patch.dict(os.environ, SSHARNESS_HOME=str(home), SSHARNESS_ATTACH_TEST_KEY=""), \
                    patch("super_super_harness.cli.Client", return_value=client), \
                    patch("super_super_harness.cli.subprocess.run", side_effect=AssertionError("must not launch without key")), \
                    redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.cmd_attach(cfg, SimpleNamespace()), 1)
            self.assertIn("supersuperharness attach", errors.getvalue())
            self.assertIn("当前终端的凭据环境变量", errors.getvalue())
            self.assertIn("claude 可执行文件", errors.getvalue())
            self.assertNotIn("首次引导", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertNotIn("credential is missing", errors.getvalue())
            self.assertEqual([call.args[0] for call in client.call.call_args_list],
                             ["router_detach", "router_mcp", "router_attach"])
            client.reader.close.assert_called_once_with()
            client.sock.close.assert_called_once_with()


class SayCommandTests(unittest.TestCase):
    def test_say_without_service_is_actionable_for_missing_and_stale_socket(self):
        import socket
        import sys
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory(prefix="ssharness-say-") as folder:
                home = Path(folder)
                write_config(home, 1, 2)
                if stale:
                    (home / "owner-token").write_text("fake-private-token")
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(str(home / "router.sock"))
                result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "say", "never delivered"],
                                        capture_output=True, text=True, timeout=5,
                                        env=dict(os.environ, SSHARNESS_HOME=str(home)))
                self.assertEqual(result.returncode, 1)
                self.assertIn("后台服务没有运行", result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertNotIn("fake-private-token", result.stdout + result.stderr)

    def test_say_success_prints_id_and_closes_client(self):
        from unittest.mock import Mock
        client = Mock()
        client.call.return_value = {"id": 17}
        with patch("super_super_harness.cli.Client", return_value=client), redirect_stdout(io.StringIO()) as output:
            cli.cmd_say(None, SimpleNamespace(text="hello"))
        self.assertEqual(output.getvalue(), "17\n")
        client.call.assert_called_once_with("say", text="hello")
        client.reader.close.assert_called_once_with()
        client.sock.close.assert_called_once_with()


class ServePreparationTests(unittest.TestCase):
    def test_failed_ensure_is_one_safe_error_and_leaves_no_pid_or_listener(self):
        import sys
        from super_super_harness import setup
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            raw = setup.read_raw(home / "profiles.toml")
            # An argument that looks like a secret must never reach diagnostic output.
            raw["providers"]["one"]["ensure"] = "/usr/bin/false fake-ensure-secret"
            (home / "profiles.toml").write_text(setup.toml_text(raw))
            result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "serve"],
                                    cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=10,
                                    env=dict(os.environ, SSHARNESS_HOME=str(home), SSHARNESS_NO_CAFFEINATE="1"))
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertEqual(len(result.stderr.strip().splitlines()), 1)
            self.assertIn("Provider", result.stderr)
            self.assertIn("one", result.stderr)
            self.assertIn("providers", result.stderr)
            self.assertIn("ensure", result.stderr)
            self.assertNotIn("fake-ensure-secret", result.stdout + result.stderr)
            self.assertNotIn("dummy-for-tests", result.stdout + result.stderr)
            self.assertFalse((home / "service.pid").exists())
            self.assertFalse((home / "router.sock").exists())
            # Startup must not claim that a failed primary Router is running.
            import sqlite3
            with sqlite3.connect((home / "router.db").as_uri() + "?mode=ro", uri=True) as db:
                self.assertIsNone(db.execute("SELECT value FROM kv WHERE key='service_pid'").fetchone())
                self.assertIsNone(db.execute("SELECT value FROM kv WHERE key='router_pid'").fetchone())


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_adapter_needs_only_registration_and_profile_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            cfg = config.load(home)
            cfg.profiles = {name: replace(p, harness="test-memory") for name, p in cfg.profiles.items()}
            cfg.harnesses["test-memory"] = {}
            fake = FakeHarness()
            register("test-memory", fake)
            self.assertIsInstance(fake, Harness)
            self.assertIs(get_harness(cfg, cfg.profile("work")), fake)
            service = Service(cfg)
            try:
                await service.router.spawn()
                self.assertIsInstance(service.router.process, HarnessProcess)
                service.router.submit("user", "hello", "terminal")
                for _ in range(100):
                    if service.store.one("SELECT id FROM outbox"):
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(service.store.one("SELECT text FROM outbox")["text"], "fake answer")
                # Durable input with no replay acknowledgement must not be delivered twice.
                message_id = service.store.add_message("unconfirmed", "user", "already seen", "terminal")
                row = service.store.one("SELECT * FROM messages WHERE id=?", message_id)
                fake.sessions[service.router.session_id].append(service.router._format(row))
                service.store.run("UPDATE messages SET status='written' WHERE id=?", message_id)
                await service.router._recover_inbox()
                self.assertEqual(service.store.one("SELECT status FROM messages WHERE id=?", message_id)["status"], "confirmed")
                service.store.run("UPDATE messages SET status='done' WHERE id=?", message_id)
                # A local PID is optional; readiness must be enough for upgrade health checks.
                from super_super_harness import releases
                status = await service.dispatch(("owner", None), "status", {}, None)
                self.assertTrue(status["router"]["ready"])
                self.assertIsNone(status["router"]["pid"])
                self.assertTrue(releases.healthy(lambda: status, SimpleNamespace(poll=lambda: None), seconds=.1, settle=0))
                # Reject a cross-Harness failover before terminating the healthy current process.
                process = service.router.process
                spare = cfg.profile("spare")
                cfg.profiles["spare"] = replace(spare, harness="incompatible")
                with self.assertRaisesRegex(ValueError, "switching Harness"):
                    await service.router.failover()
                self.assertIs(service.router.process, process)
                with self.assertRaises(ValueError):
                    await service.router_tool("router_switch_model", {"profile": "spare"})
                with patch.object(service.router, "_probe", return_value=False) as probe:
                    await service.router._try_unpause()
                    self.assertEqual(probe.call_count, 1)
                cfg.profiles["spare"] = spare
                task_id = service.tasks.start("memory task", "the brief")["id"]
                for _ in range(100):
                    if service.store.task(task_id)["status"] == "completed" and task_id not in service.tasks.live:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(service.store.task(task_id)["status"], "completed")
                waiting = service.tasks.start("interrupt me", "WAIT")["id"]
                for _ in range(100):
                    run = service.tasks.live.get(waiting)
                    if run and run.process and service.store.task(waiting)["pending"] == "[]":
                        break
                    await asyncio.sleep(.01)
                original_session = service.store.task(waiting)["session_id"]
                self.assertTrue(service.tasks.interrupt(waiting)["ok"])
                for _ in range(100):
                    if service.store.task(waiting)["status"] == "interrupted" and waiting not in service.tasks.live:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(service.store.task(waiting)["status"], "interrupted")
                service.tasks.resume(waiting, "finish")
                for _ in range(100):
                    if service.store.task(waiting)["status"] == "completed" and waiting not in service.tasks.live:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(service.store.task(waiting)["status"], "completed")
                self.assertEqual(service.store.task(waiting)["session_id"], original_session)
                self.assertIn(("headless", "work", True), fake.calls)
                self.assertTrue(await service.router._probe("main", deep=True))
                # Reads retain the original Harness even after its Profile was removed.
                del cfg.profiles["work"]
                self.assertEqual(service.task_detail(task_id, None, 20)[0]["text"], "the brief")
                cfg.profiles["work"] = replace(cfg.profile("other"), name="work", harness="claude-code")
                self.assertEqual(service.task_detail(task_id, None, 20)[0]["text"], "the brief")
                detail = await service.web.detail(task_id, {})
                self.assertEqual(detail["source"], "transcript")
                self.assertEqual(detail["timeline"][0]["text"], "the brief")
                cfg.profiles["work"] = replace(cfg.profile("other"), name="work")
                developer = await service.web.developer()
                self.assertTrue(all(p["healthy"] for p in developer["providers"]))
                cfg.profiles["work"] = replace(cfg.profile("other"), name="work")
                with patch("super_super_harness.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                    self.assertEqual(cli.exec_interactive(cfg, "work", "s", home, None, None), 0)
                    self.assertEqual(run.call_args.args[0], ["fake-interactive", "s"])
                with redirect_stdout(io.StringIO()) as output:
                    cli.cmd_log(cfg, SimpleNamespace(task=task_id, full=True, kinds=None, raw=True))
                self.assertIn("the brief", output.getvalue())
                detached = await service.router.detach()
                self.assertTrue(detached["started"])
                actions = {c[0] for c in fake.calls}
                self.assertTrue({"env", "headless", "interactive", "prepare", "has", "transcript", "file",
                                 "ensure", "shallow", "deep"} <= actions)
            finally:
                await service.router.shutdown()
                await service.tasks.shutdown()
                service.store.close()

    async def test_unknown_and_incomplete_adapter_are_explicit_errors(self):
        cfg = SimpleNamespace(harnesses={})
        with self.assertRaisesRegex(ValueError, "unknown Harness"):
            get_harness(cfg, SimpleNamespace(harness="not-registered"))
        with self.assertRaises(TypeError):
            register("incomplete", object())
