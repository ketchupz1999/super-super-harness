"""GitHub connector/config contracts. Only a temporary, offline fake gh can run."""
import asyncio
from copy import deepcopy
from email.utils import formatdate
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit
import uuid

from super_super_harness.core.store import Store
from super_super_harness.events.bus import Bus, DATA_BYTES
from super_super_harness.events.github import GitHubNotifications, HEALTH_KEY, STATE_KEY, iso, notification_event, response, timestamp
from super_super_harness.ops import config


START = timestamp("2026-11-04T12:00:00Z")
MODIFIED = "Wed, 04 Nov 2026 12:00:10 GMT"
NEXT = '<https://api.github.com/notifications?page=2>; rel="next"'
SECRET = "synthetic-secret-do-not-log"


def notification(id="n1", seconds=10, repo="org/repo", reason="mention", path="pulls/42", **extra):
    row = {"id": id, "updated_at": iso(START + seconds), "reason": reason, "unread": True,
           "repository": {"full_name": repo},
           "subject": {"title": "A useful change", "type": "PullRequest",
                       "url": "https://api.github.com/repos/" + repo + "/" + path}}
    row.update(extra)
    return row


def raw_config(sources=None, rules=()):
    return {"default_profile": "p", "router": {"primary": "p", "backup": "p"},
            "providers": {"x": {"base_url": "http://127.0.0.1:1"}},
            "profiles": {"p": {"harness": "claude-code", "provider": "x", "model": "m"}},
            "sources": sources or {}, "rules": list(rules)}


class GitHubConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="ssharness-github-config-")
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        env = patch.dict(os.environ, {"SSHARNESS_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def load(self, sources=None, rules=()):
        return config.from_raw(raw_config(sources, rules), self.home)

    def test_absent_connector_preserves_permission_mapping(self):
        for sources in ({}, {"manual": {"permission": "notify"}}, {"github": {}},
                        {"github": {"permission": "observe"}}):
            with self.subTest(sources=sources):
                cfg = self.load(sources)
                self.assertIsNone(cfg.github)
                self.assertEqual(cfg.sources, {name: item.get("permission", "observe") for name, item in sources.items()})
                self.assertEqual(cfg.rules, ())

    def test_connector_defaults_and_options_are_separate_from_permissions(self):
        cfg = self.load({"github": {"kind": "github_notifications"}, "manual": {"permission": "notify"}})
        self.assertEqual(cfg.sources, {"github": "observe", "manual": "notify"})
        self.assertEqual(cfg.github, {"interval_seconds": 120, "repos": [], "env": {}, "max_observe_per_hour": 12})
        self.assertEqual(cfg.rules, ())  # The Bus, not the parser, supplies the default observe rule.
        cfg.github["repos"].append("org/repo")
        self.assertEqual(self.load({"github": {"kind": "github_notifications"}}).github["repos"], [])

    def test_load_private_toml_with_proxy_and_explicit_observe_rule(self):
        (self.home / "profiles.toml").write_text('''
default_profile = "p"
[router]
primary = "p"
backup = "p"
[providers.x]
base_url = "http://127.0.0.1:1"
[profiles.p]
harness = "claude-code"
provider = "x"
model = "m"
[sources.github]
kind = "github_notifications"
permission = "observe"
interval_seconds = 300
repos = ["Owner/Repo", "other/repo"]
max_observe_per_hour = 0
env = { https_proxy = "http://127.0.0.1:8890" }
[[rules]]
source = "github"
type_prefix = "github.review_requested"
action = "observe"
''')
        cfg = config.load()
        self.assertEqual(cfg.home, self.home)
        self.assertEqual(cfg.sources, {"github": "observe"})
        self.assertEqual(cfg.github, {"interval_seconds": 300, "repos": ["Owner/Repo", "other/repo"],
                                      "env": {"https_proxy": "http://127.0.0.1:8890"}, "max_observe_per_hour": 0})
        self.assertEqual(cfg.rules, ({"source": "github", "type_prefix": "github.review_requested", "action": "observe"},))

    def test_bad_connector_options_raise_public_errors_without_values(self):
        cases = {
            "interval_seconds": [True, False, 0, -1, 1.5, "120", None],
            "max_observe_per_hour": [True, False, -1, 1.5, "12", None],
            "repos": [None, "org/repo", {}, [1], ["repo"], ["org/repo/extra"], ["org/re po"], [SECRET]],
            "env": [None, [], {"https_proxy": 1}, {"bad-name": SECRET}, {"https_proxy": "bad\x00"},
                    {"GH_TOKEN": SECRET}, {"GITHUB_TOKEN": SECRET}, {"api_key": SECRET},
                    {"PASSWORD": SECRET}, {"SECRET": SECRET}, {"GH_DEBUG": SECRET}, {"DEBUG": SECRET}],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(config.PublicConfigurationError) as caught:
                    self.load({"github": {"kind": "github_notifications", field: value}})
                self.assertIn("[sources.github]", str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))

    def test_connector_kind_and_options_are_not_accepted_on_other_sources(self):
        for sources in ({"other": {"kind": "github_notifications"}},
                        {"github": {"kind": SECRET}},
                        {"github": {"interval_seconds": 120}},
                        {"github": {"kind": "github_notifications", "token": SECRET}},
                        {"github": {"kind": "github_notifications", "permission": SECRET}}):
            with self.subTest(sources=sources), self.assertRaises(config.PublicConfigurationError) as caught:
                self.load(sources)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_observe_rules_accept_only_observe_permissions(self):
        for permission in ("notify", "act"):
            for rule in ({"source": "github", "action": "observe"}, {"action": "observe"},
                         {"type_prefix": "github.", "action": "observe"}):
                with self.subTest(permission=permission, rule=rule), self.assertRaises(config.PublicConfigurationError) as caught:
                    self.load({"github": {"kind": "github_notifications", "permission": permission}}, [rule])
                self.assertIn("permission = observe", str(caught.exception))
            # notify/act themselves remain valid when no observe rule could match them.
            self.assertEqual(self.load({"github": {"kind": "github_notifications", "permission": permission}}).sources,
                             {"github": permission})
        for source in ("worker", "reminder", "undeclared"):
            with self.subTest(source=source), self.assertRaises(config.PublicConfigurationError):
                self.load({}, [{"source": source, "action": "observe"}])

    def test_broad_observe_rule_rejects_mixed_permissions_but_explicit_rule_works(self):
        sources = {"github": {"kind": "github_notifications"}, "manual": {"permission": "notify"}}
        with self.assertRaises(config.PublicConfigurationError):
            self.load(sources, [{"action": "observe"}])
        cfg = self.load(sources, [{"source": "github", "action": "observe"}])
        self.assertEqual(cfg.rules, ({"source": "github", "action": "observe"},))
        cfg = self.load({"github": {"kind": "github_notifications"}, "manual": {}}, [{"action": "observe"}])
        self.assertEqual(cfg.rules, ({"action": "observe"},))


class GitHubNormalizationTests(unittest.TestCase):
    def test_web_urls_threads_and_lowercase_reasons(self):
        for path, suffix, thread_suffix in (("pulls/42", "/pull/42", "#42"),
                                             ("issues/17", "/issues/17", "#17"),
                                             ("discussions/8", "/discussions/8", "#8"),
                                             ("commits/aB12cd", "/commit/aB12cd", ""),
                                             ("releases/123", "", "")):
            with self.subTest(path=path):
                row = notification(path=path, reason="REVIEW_REQUESTED")
                event = notification_event(row)
                self.assertEqual(event["source"], "github")
                self.assertEqual(event["type"], "github.review_requested")
                self.assertEqual(event["id"], "n1:" + row["updated_at"])
                self.assertEqual(event["thread"], "github:org/repo" + thread_suffix)
                self.assertEqual(event["url"], "https://github.com/org/repo" + suffix)
                self.assertEqual(event["severity"], "info")
                self.assertEqual(event["text"], "org/repo · PullRequest · A useful change · review_requested")
                self.assertEqual(event["data"]["subject"]["url"], row["subject"]["url"])
                self.assertTrue(event["data"]["unread"])

    def test_unknown_reasons_remain_lowercase_not_a_fixed_allowlist(self):
        for reason in ("mention", "ci_activity", "comment", "subscribed", "FUTURE_REASON"):
            with self.subTest(reason=reason):
                self.assertEqual(notification_event(notification(reason=reason))["type"], "github." + reason.lower())

    def test_missing_or_unrelated_subject_url_uses_repository_page(self):
        for url in (None, "", "https://api.github.com/repos/other/repo/issues/42", "https://example.invalid/x",
                    "javascript:alert(1)", "https://api.github.com/repos/org/repo/issues/not-a-number"):
            row = notification()
            row["subject"]["url"] = url
            with self.subTest(url=url):
                event = notification_event(row)
                self.assertEqual(event["thread"], "github:org/repo")
                self.assertEqual(event["url"], "https://github.com/org/repo")
        row["subject"].pop("url")
        self.assertEqual(notification_event(row)["url"], "https://github.com/org/repo")

    def test_useful_data_is_bounded_and_title_is_one_line_without_mutating_input(self):
        row = notification(unread="true", ignored=SECRET * 10000)
        row["repository"]["private_blob"] = SECRET * 10000
        row["subject"].update(title=("标题\n\t🙂 " * 10000), type="x" * 1000,
                              url="https://example.invalid/" + "x" * 10000, body=SECRET * 10000)
        original = deepcopy(row)
        event = notification_event(row)
        self.assertEqual(row, original)
        self.assertEqual(len(event["data"]["subject"]["title"]), 8000)
        self.assertEqual(len(event["data"]["subject"]["type"]), 100)
        self.assertEqual(len(event["data"]["subject"]["url"]), 2000)
        self.assertLessEqual(len(json.dumps(event["data"], ensure_ascii=False).encode()), DATA_BYTES)
        self.assertEqual(set(event["data"]), {"id", "updated_at", "reason", "repository", "subject", "unread"})
        self.assertEqual(event["data"]["repository"], {"full_name": "org/repo"})
        self.assertNotIn(SECRET, json.dumps(event))
        self.assertEqual(event["text"], " ".join(event["text"].split()))
        self.assertFalse(event["data"]["unread"])

    def test_response_headers_are_case_insensitive_and_body_is_not_split_again(self):
        status, headers, body = response('HTTP/2.0 200 OK\r\nX-PoLl-InTeRvAl: 90\r\nLast-Modified: ' + MODIFIED
                                         + '\r\nLink: ' + NEXT + '\r\n\r\n[\n\n]')
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"x-poll-interval": "90", "last-modified": MODIFIED, "link": NEXT})
        self.assertEqual(body, "[\n\n]")
        self.assertEqual(response("HTTP/1.1 304 Not Modified\n\n"), (304, {}, ""))

    def test_invalid_response_framing_is_rejected(self):
        for output in ("", "[]", "200 OK\n\n[]", "HTTP/2.0 nope\n\n[]", "HTTP/2.0 200\n[]"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                response(output)


class GitHubConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory(prefix="ssharness-github-")
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        shutil.copyfile(Path(__file__).with_name("fake_gh.py"), fake)
        fake.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.scenario = self.home / "responses.json"
        self.calls_file = self.home / "calls.jsonl"
        self.db_path = self.home / "router.db"
        # No inherited PATH, tokens, gh configuration or real HOME can reach the child.
        env = patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home),
                                     "SSHARNESS_HOME": str(self.home), "GH_CONFIG_DIR": str(self.home / "gh-config"),
                                     "FAKE_GH_SCENARIO": str(self.scenario), "FAKE_GH_CALLS": str(self.calls_file),
                                     "FAKE_GH_DB": str(self.db_path)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(self.db_path)
        self.addCleanup(lambda: self.store.close())
        self.router = SimpleNamespace(submit=Mock(side_effect=self.submit))
        self.options = config.github_options({})
        self.bus = Bus(self.store, self.router, None, {"github": "observe"}, github=self.options)
        self.source = GitHubNotifications(self.store, self.bus, self.options, clock=lambda: START)
        self.addAsyncCleanup(self.source.close)

    def submit(self, role, text, channel="", kind="", task_id=None):
        return self.store.add_message(str(uuid.uuid4()), role, text, channel, kind, task_id)

    def queue(self, *responses):
        self.scenario.write_text(json.dumps(responses))
        self.calls_file.unlink(missing_ok=True)

    def calls(self):
        rows = [json.loads(line) for line in self.calls_file.read_text().splitlines()] if self.calls_file.exists() else []
        self.assertTrue(all(row["valid"] for row in rows), "connector must use only gh api notifications GET")
        return rows

    def state(self):
        return json.loads(self.store.get(STATE_KEY))

    def rows(self, table):
        return self.store.all("SELECT * FROM " + table)

    def assert_counts(self, triggers=0, messages=0, observations=0):
        for table, count in (("triggers", triggers), ("messages", messages), ("observations", observations)):
            self.assertEqual(len(self.rows(table)), count, table)
        self.assertEqual(len(self.rows("outbox")), 0)
        self.assertEqual(len(self.rows("deliveries")), 0)

    def assert_warning(self, count=1):
        rows = self.store.all("SELECT * FROM events WHERE type='source.warn'")
        self.assertEqual(len(rows), count)
        for row in rows:
            self.assertEqual((row["source"], row["type"], row["severity"]), ("github", "source.warn", "warn"))
            message = json.loads(row["data"])["message"]
            self.assertIn("gh auth login", message)
            self.assertIn("Retrying", message)
            self.assertNotIn(SECRET, json.dumps(dict(row)))

    def assert_recovered(self, count=1):
        rows = self.store.all("SELECT * FROM events WHERE type='source.recovered'")
        self.assertEqual(len(rows), count)
        for row in rows:
            self.assertEqual((row["source"], row["severity"]), ("github", "info"))
            self.assertIn("已恢复", json.loads(row["data"])["message"])
            self.assertNotIn(SECRET, row["data"])

    def query(self, call):
        return parse_qs(urlsplit(call["args"][5]).query)

    def headers(self, call):
        args = call["args"]
        return [args[i + 1] for i, value in enumerate(args) if value == "--header"]

    async def test_first_start_barrier_is_persisted_before_request_and_filters_history(self):
        initial = {"started": START, "since": START, "modified": "", "poll": 60}
        self.assertEqual(self.state(), initial)
        self.assertEqual(self.calls(), [])
        self.queue({"body": [notification("old", -100), notification("boundary", 0), notification("new", 1)]})
        await self.source.tick()
        call, = self.calls()
        self.assertEqual(call["state"], initial)
        self.assertEqual(self.query(call), {"all": ["true"], "since": [iso(START - 1)], "per_page": ["100"], "page": ["1"]})
        self.assertIn("If-Modified-Since: " + formatdate(START - 1, usegmt=True), self.headers(call))
        self.assert_counts(2, 2, 2)
        self.assertEqual([row["event_id"] for row in self.rows("triggers")],
                         ["boundary:" + iso(START), "new:" + iso(START + 1)])
        self.assertEqual(self.state()["since"], START + 1)

    async def test_fractional_start_includes_startup_second_once_across_repoll_and_restart(self):
        self.store.unset(STATE_KEY)
        self.source = GitHubNotifications(self.store, self.bus, self.options, clock=lambda: START + 0.5)
        initial = {"started": START + 0.5, "since": START + 0.5, "modified": "", "poll": 60}
        self.assertEqual(self.state(), initial)
        rows = [notification("previous-second", -1), notification("startup-second", 0)]
        self.queue({"body": rows}, {"body": rows})
        await self.source.tick()
        self.assert_counts(1, 1, 1)
        call = self.calls()[0]
        self.assertEqual(call["state"], initial)
        self.assertEqual(self.query(call)["since"], [iso(START - 0.5)])
        self.assertIn("If-Modified-Since: " + formatdate(START - 1, usegmt=True), self.headers(call))
        await self.source.tick()
        self.assert_counts(1, 1, 1)
        self.assertEqual(self.state(), initial)
        self.assertIn("If-Modified-Since: " + formatdate(START - 1, usegmt=True), self.headers(self.calls()[1]))
        self.store.close()
        self.store = Store(self.db_path)
        bus = Bus(self.store, self.router, None, {"github": "observe"}, github=self.options)
        source = GitHubNotifications(self.store, bus, self.options, clock=lambda: START + 500)
        self.assertEqual(source.state, initial)
        self.queue({"body": rows + [notification("after-start", 10)]})
        await source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual([row["event_id"] for row in self.rows("triggers")],
                         ["startup-second:" + iso(START), "after-start:" + iso(START + 10)])
        self.assertEqual(self.router.submit.call_count, 2)
        self.assertEqual(source.state["started"], START + 0.5)
        self.assertEqual(source.state["since"], START + 10)
        self.assertIn("If-Modified-Since: " + formatdate(START - 1, usegmt=True), self.headers(self.calls()[0]))
        self.assertEqual(self.rows("events"), [])

    async def test_bus_deduplicates_repeated_notification_but_accepts_same_id_updated(self):
        row = notification()
        self.queue({"body": [row, row]}, {"body": [row]}, {"body": [notification(seconds=20)]})
        with patch.object(self.bus, "emit", wraps=self.bus.emit) as emit:
            await self.source.tick()
            await self.source.tick()
            self.assertEqual(emit.call_count, 3)  # Not an in-memory connector cache: the Bus dedupes.
            self.assert_counts(1, 1, 1)
            await self.source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual([row["event_id"] for row in self.rows("triggers")],
                         ["n1:" + iso(START + 10), "n1:" + iso(START + 20)])
        self.assertEqual(self.router.submit.call_count, 2)
        self.assertEqual(self.rows("events"), [])

    async def test_repos_filter_is_case_insensitive_and_filtered_rows_advance_cursor(self):
        self.options["repos"] = ["ORG/REPO"]
        self.queue({"body": [notification("keep", repo="org/repo"), notification("skip", 20, repo="elsewhere/repo")]})
        await self.source.tick()
        self.assert_counts(1, 1, 1)
        self.assertEqual(self.rows("triggers")[0]["event_id"], "keep:" + iso(START + 10))
        self.assertEqual(self.state()["since"], START + 20)

    async def test_empty_repos_accepts_all_repositories(self):
        self.queue({"body": [notification("a"), notification("b", repo="elsewhere/repo")]})
        await self.source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual({row["thread"] for row in self.rows("triggers")}, {"github:org/repo#42", "github:elsewhere/repo#42"})

    async def test_304_keeps_cursor_and_modified_and_accepts_nonzero_gh_exit(self):
        self.queue({"body": [notification()], "headers": {"Last-Modified": MODIFIED}},
                   {"status": 304, "body": "", "headers": {"X-Poll-Interval": "240", "Last-Modified": "ignored"},
                    "returncode": 1, "stderr": SECRET})
        await self.source.tick()
        saved = self.state()
        await self.source.tick()
        self.assertEqual(self.state(), dict(saved, poll=240))
        self.assertEqual(self.source.interval, 240)
        self.assert_counts(1, 1, 1)
        self.assertEqual(self.rows("events"), [])
        self.assertIn("If-Modified-Since: " + MODIFIED, self.headers(self.calls()[1]))

    async def test_missing_gh_warns_without_losing_startup_barrier_then_recovers(self):
        (self.bin / "gh").unlink()
        for _ in range(3):
            await self.source.tick()
        self.assertEqual(self.state()["started"], START)
        self.assertEqual(self.state()["since"], START)
        self.assert_counts()
        self.assert_warning()
        shutil.copyfile(Path(__file__).with_name("fake_gh.py"), self.bin / "gh")
        (self.bin / "gh").chmod(0o755)
        self.queue({"body": [notification()]})
        await self.source.tick()
        self.assert_counts(1, 1, 1)
        self.assert_warning()  # Recovery is a separate info event, not another warning.
        self.assert_recovered()

    async def test_errors_never_log_response_stderr_or_sensitive_headers(self):
        cases = [{"stdout": "", "stderr": SECRET, "returncode": 1},
                 {"status": 401, "body": {"message": SECRET}, "headers": {"Authorization": SECRET}, "returncode": 1},
                 {"status": 500, "body": SECRET},
                 {"status": 200, "body": [notification()], "stderr": SECRET, "returncode": 1},
                 {"stdout": "malformed " + SECRET, "stderr": SECRET}]
        self.queue(*cases)
        saved = self.state()
        with self.assertNoLogs(level="WARNING"):
            for count, case in enumerate(cases, 1):
                with self.subTest(case=case):
                    await self.source.tick()
                    self.assert_warning()
                    self.assertEqual(self.state(), saved)
                    self.assert_counts()
        self.assertEqual(len(self.calls()), len(cases))

    async def test_environment_overrides_disable_prompts_and_debug(self):
        self.options["env"] = {"https_proxy": "http://127.0.0.1:8890", "FAKE_GH_MARKER": "configured"}
        self.queue({"body": []})
        with patch.dict(os.environ, {"GH_DEBUG": "api", "DEBUG": "1", "GH_PROMPT_DISABLED": "0"}):
            await self.source.tick()
        self.assertEqual(self.calls()[0]["env"], {"GH_PROMPT_DISABLED": "1", "GH_DEBUG": None, "DEBUG": None,
                                                 "https_proxy": "http://127.0.0.1:8890", "FAKE_GH_MARKER": "configured"})
        self.assertEqual(self.rows("events"), [])

    async def test_poll_interval_respects_sixty_seconds_server_and_configuration(self):
        for configured, header, expected in ((1, "1", 60), (20, "59", 60), (120, "60", 120),
                                             (120, "300", 300), (600, "300", 600)):
            with self.subTest(configured=configured, header=header):
                self.options["interval_seconds"] = configured
                self.queue({"headers": {"X-PoLl-InTeRvAl": header}})
                await self.source.tick()
                self.assertEqual(self.source.interval, expected)
                self.assertEqual(self.state()["poll"], max(60, int(header)))
        for header in ("", "0", "-1", "NaN", "1.5"):
            with self.subTest(header=header):
                self.queue({"headers": {"X-Poll-Interval": header}})
                await self.source.tick()
                self.assertEqual(self.state()["poll"], 300)
        self.assertEqual(self.rows("events"), [])

    async def test_error_response_still_persists_server_retry_interval(self):
        self.queue({"status": 403, "headers": {"X-Poll-Interval": "900"}, "body": SECRET, "returncode": 1})
        await self.source.tick()
        self.assertEqual(self.source.interval, 900)
        self.assertEqual(self.state(), {"started": START, "since": START, "modified": "", "poll": 900})
        self.assert_warning()

    async def test_pagination_uses_first_page_modified_and_sorts_all_events(self):
        self.queue({"body": [notification("later", 30)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED, "X-Poll-Interval": "70"}},
                   {"body": [notification("earlier", 10)], "headers": {"Last-Modified": "wrong page", "X-Poll-Interval": "180"}})
        await self.source.tick()
        self.assert_counts(2, 2, 2)
        calls = self.calls()
        self.assertEqual([self.query(call)["page"] for call in calls], [["1"], ["2"]])
        self.assertEqual([self.query(call)["since"] for call in calls], [[iso(START - 1)]] * 2)
        self.assertFalse(any(header.startswith("If-Modified-Since:") for header in self.headers(calls[1])))
        saved = self.state()
        self.assertEqual({key: value for key, value in saved.items() if key != "snapshot"},
                         {"started": START, "since": START, "modified": "", "poll": 180})
        self.assertRegex(saved["snapshot"], r"^[0-9a-f]{64}$")
        self.assertEqual([row["event_id"].split(":")[0] for row in self.rows("triggers")], ["earlier", "later"])
        # The same complete scan confirms the offset pages; duplicates stay in the Bus.
        self.queue({"body": [notification("later", 30)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED}},
                   {"body": [notification("earlier", 10)], "headers": {"Last-Modified": "wrong page"}})
        await self.source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual(self.state(), {"started": START, "since": START + 30, "modified": MODIFIED, "poll": 180})
        self.assertEqual([self.query(call)["since"] for call in self.calls()], [[iso(START - 1)]] * 2)

    async def test_failed_later_page_commits_neither_events_nor_cursor_but_keeps_poll_header(self):
        self.queue({"body": [notification("baseline", 5)], "headers": {"Last-Modified": MODIFIED}})
        await self.source.tick()
        saved = self.state()
        first = {"body": [notification("page1", 20)], "headers": {"Link": NEXT, "Last-Modified": "not committed"}}
        for failure in ({"status": 502, "body": SECRET, "headers": {"X-Poll-Interval": "300"}},
                        {"status": 304, "body": ""}, {"body": "not JSON " + SECRET}, {"body": {"message": SECRET}}):
            with self.subTest(failure=failure):
                self.queue(first, failure)
                await self.source.tick()
                self.assert_counts(1, 1, 1)
                self.assertEqual(self.state(), dict(saved, poll=300))
                self.assert_warning()
                self.assertEqual(len(self.calls()), 2)
        self.queue(first, {"body": [notification("page2", 30)]})
        await self.source.tick()
        self.assert_counts(3, 3, 3)
        self.assertEqual(self.state()["since"], START + 5)
        self.assertEqual(self.state()["modified"], "")
        self.assertRegex(self.state()["snapshot"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.query(self.calls()[0])["since"], [iso(START + 4)])
        self.assertIn("If-Modified-Since: " + MODIFIED, self.headers(self.calls()[0]))
        self.queue(first, {"body": [notification("page2", 30)]})
        await self.source.tick()
        self.assert_counts(3, 3, 3)
        self.assertEqual(self.state()["since"], START + 30)
        self.assertNotIn("snapshot", self.state())

    async def test_emit_failure_rolls_back_prior_triggers_messages_and_progress(self):
        self.queue({"body": [notification("a", 10), notification("b", 20)], "headers": {"Last-Modified": MODIFIED}})
        saved = self.state()
        real_submit = self.submit
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError(SECRET)
            return real_submit(*args, **kwargs)

        self.router.submit.side_effect = fail_second
        await self.source.tick()
        self.assert_counts()
        self.assertEqual(self.state(), saved)
        self.assert_warning()
        self.router.submit.side_effect = real_submit
        self.queue({"body": [notification("a", 10), notification("b", 20)]})
        await self.source.tick()
        self.assert_counts(2, 2, 2)

    async def test_restart_resumes_persisted_cursor_and_bus_dedupe_not_new_start_time(self):
        self.queue({"body": [notification()], "headers": {"Last-Modified": MODIFIED, "X-Poll-Interval": "240"}})
        await self.source.tick()
        saved = self.state()
        self.store.close()
        self.store = Store(self.db_path)
        self.bus = Bus(self.store, self.router, None, {"github": "observe"}, github=self.options)
        clock = Mock(return_value=START + 100)
        source = GitHubNotifications(self.store, self.bus, self.options, clock=clock)
        clock.assert_not_called()  # Construction resumes stored progress, not a new startup barrier.
        self.assertEqual(source.state, saved)
        self.assertEqual(source.interval, 240)
        self.queue({"body": [notification("history", -1), notification(), notification("during-downtime", 20)]})
        await source.tick()
        self.assert_counts(2, 2, 2)
        call, = self.calls()
        self.assertEqual(self.query(call)["since"], [iso(START + 9)])
        self.assertIn("If-Modified-Since: " + MODIFIED, self.headers(call))
        self.assertEqual(source.state["started"], START)
        self.assertEqual(source.state["since"], START + 20)
        self.assertEqual(source.state["modified"], "")  # No stale validator when 200 omits Last-Modified.

    async def test_restart_after_failed_first_poll_keeps_original_history_barrier(self):
        self.queue({"stdout": "", "returncode": 1})
        await self.source.tick()
        self.store.close()
        self.store = Store(self.db_path)
        bus = Bus(self.store, self.router, None, {"github": "observe"}, github=self.options)
        source = GitHubNotifications(self.store, bus, self.options, clock=lambda: START + 100)
        self.queue({"body": [notification("old", -1), notification("while-offline", 10)]})
        await source.tick()
        self.assert_counts(1, 1, 1)
        self.assertEqual(source.state["started"], START)
        self.assertEqual(self.rows("triggers")[0]["event_id"], "while-offline:" + iso(START + 10))

    async def test_removal_from_101_to_100_notifications_does_not_lose_shifted_last_row(self):
        # GitHub sorts newest first. Between requests the newest notification disappears,
        # shifting the 101st row onto page 1: the first page-2 request returns nothing.
        rows = [notification("n%d" % number, number) for number in range(101, 0, -1)]
        self.source.bus = Bus(self.store, self.router, None, {"github": "observe"},
                              github=dict(self.options, max_observe_per_hour=200))
        self.source.state["modified"] = MODIFIED
        self.store.set(STATE_KEY, json.dumps(self.source.state))
        self.queue({"body": rows[:100], "headers": {"Link": NEXT, "Last-Modified": MODIFIED}},
                   {"body": []},
                   {"body": rows[1:], "headers": {"Last-Modified": MODIFIED}})
        await self.source.tick()
        self.assert_counts(100, 100, 100)
        self.assertNotIn("n1:" + iso(START + 1), {row["event_id"] for row in self.rows("triggers")})
        self.assertEqual(self.state()["since"], START)
        self.assertEqual(self.state()["modified"], "")
        self.assertIn("snapshot", self.state())
        await self.source.tick()
        self.assert_counts(101, 101, 101)
        self.assertEqual({row["event_id"] for row in self.rows("triggers")},
                         {row["id"] + ":" + row["updated_at"] for row in rows})
        self.assertEqual(self.state()["since"], START + 100)
        self.assertNotIn("snapshot", self.state())  # A complete single page has no offset race.
        calls = self.calls()
        self.assertEqual([self.query(call)["page"] for call in calls], [["1"], ["2"], ["1"]])
        self.assertEqual([self.query(call)["since"] for call in calls], [[iso(START - 1)]] * 3)
        self.assertIn("If-Modified-Since: " + formatdate(START - 1, usegmt=True), self.headers(calls[2]))
        self.assertEqual(self.rows("events"), [])

    async def test_changed_multi_page_snapshot_requires_another_matching_complete_scan(self):
        first = {"body": [notification("newest", 30)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED}}
        second = {"body": [notification("older", 10)]}
        changed = {"body": [notification("older", 11)]}
        self.queue(first, second, first, changed, first, changed)
        await self.source.tick()
        fingerprint = self.state()["snapshot"]
        self.assert_counts(2, 2, 2)
        await self.source.tick()
        self.assert_counts(3, 3, 3)
        self.assertNotEqual(self.state()["snapshot"], fingerprint)
        self.assertEqual(self.state()["since"], START)
        self.assertEqual(self.state()["modified"], "")
        await self.source.tick()
        self.assert_counts(3, 3, 3)
        self.assertEqual(self.state(), {"started": START, "since": START + 30, "modified": MODIFIED, "poll": 60})
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(self.rows("events"), [])

    async def test_restart_with_pending_snapshot_rescans_old_cursor_and_confirms_without_duplicates(self):
        first = {"body": [notification("newest", 30)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED}}
        second = {"body": [notification("older", 10)]}
        self.queue(first, second)
        await self.source.tick()
        saved = self.state()
        self.assertIn("snapshot", saved)
        self.assertEqual(saved["since"], START)
        self.store.close()
        self.store = Store(self.db_path)
        bus = Bus(self.store, self.router, None, {"github": "observe"}, github=self.options)
        source = GitHubNotifications(self.store, bus, self.options, clock=lambda: START + 500)
        self.assertEqual(source.state, saved)
        self.queue(first, {"status": 500, "body": SECRET}, first, second)
        await source.tick()
        self.assertEqual(self.state(), saved)  # An incomplete scan must not replace the saved fingerprint.
        self.assert_counts(2, 2, 2)
        self.assert_warning()
        await source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual(self.state(), {"started": START, "since": START + 30, "modified": MODIFIED, "poll": 60})
        calls = self.calls()
        self.assertEqual(calls[0]["state"], saved)
        self.assertEqual([self.query(call)["since"] for call in calls], [[iso(START - 1)]] * 4)
        for call in (calls[0], calls[2]):
            self.assertIn("If-Modified-Since: " + formatdate(START - 1, usegmt=True), self.headers(call))

    async def test_retry_after_seconds_persists_deadline_skips_early_ticks_and_resumes_at_deadline(self):
        clock = Mock(return_value=START)
        self.source.clock = clock
        self.queue({"status": 429, "headers": {"Retry-After": "600", "X-Poll-Interval": "180"},
                    "body": SECRET, "stderr": SECRET, "returncode": 1},
                   {"status": 304, "body": ""})
        await self.source.tick()
        self.assertEqual(self.state(), {"started": START, "since": START, "modified": "", "poll": 180, "retry_at": START + 600})
        self.assertEqual(self.source.interval, 600)
        clock.return_value = START + 50.25
        self.assertEqual(self.source.interval, 550)  # Round up, never resume before the deadline.
        await self.source.tick()
        clock.return_value = START + 599.99
        await self.source.tick()
        self.assertEqual(len(self.calls()), 1)
        self.assert_warning()
        self.assert_recovered(0)  # Waiting out a cooldown is not evidence that the source recovered.
        clock.return_value = START + 600
        await self.source.tick()
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.source.interval, 180)
        self.assert_counts()
        self.assert_warning()  # Skipped ticks and a successful retry produce no extra warning.

    async def test_retry_after_http_date_on_error_is_persisted_and_obeyed(self):
        clock = Mock(return_value=START)
        self.source.clock = clock
        self.queue({"status": 503, "headers": {"rEtRy-AfTeR": formatdate(START + 900, usegmt=True)},
                    "body": SECRET, "returncode": 1}, {"body": [notification()]})
        await self.source.tick()
        self.assertEqual(self.state()["retry_at"], START + 900)
        self.assertEqual(self.source.interval, 900)
        clock.return_value = START + 899
        await self.source.tick()
        self.assertEqual(len(self.calls()), 1)
        clock.return_value = START + 900
        await self.source.tick()
        self.assertEqual(len(self.calls()), 2)
        self.assert_counts(1, 1, 1)
        self.assert_warning()

    async def test_successful_304_also_persists_retry_after_without_parsing_body(self):
        self.queue({"status": 304, "headers": {"Retry-After": "300"}, "body": "not JSON", "returncode": 1})
        await self.source.tick()
        self.assertEqual(self.state()["retry_at"], START + 300)
        self.assertEqual(self.source.interval, 300)
        await self.source.tick()
        self.assertEqual(len(self.calls()), 1)
        self.assert_counts()
        self.assertEqual(self.rows("events"), [])

    async def test_rate_limit_reset_on_success_persists_deadline_and_defers_next_poll(self):
        clock = Mock(return_value=START)
        self.source.clock = clock
        self.queue({"body": [notification()], "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(START + 3600))}},
                   {"body": [notification(seconds=20)]})
        await self.source.tick()
        self.assert_counts(1, 1, 1)
        self.assertEqual(self.state()["retry_at"], START + 3600)
        self.assertEqual(self.state()["since"], START + 10)
        self.assertEqual(self.source.interval, 3600)
        clock.return_value = START + 3599
        await self.source.tick()
        self.assertEqual(len(self.calls()), 1)
        clock.return_value = START + 3600
        await self.source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.rows("events"), [])

    async def test_retry_interval_is_max_of_retry_after_reset_poll_header_and_configuration(self):
        self.options["interval_seconds"] = 1500
        self.queue({"status": 403, "headers": {"Retry-After": "600", "X-RateLimit-Remaining": "0",
                                                "X-RateLimit-Reset": str(int(START + 900)), "X-Poll-Interval": "1200"},
                    "body": SECRET, "returncode": 1})
        await self.source.tick()
        self.assertEqual(self.state()["retry_at"], START + 900)
        self.assertEqual(self.state()["poll"], 1200)
        self.assertEqual(self.source.interval, 1500)
        self.options["interval_seconds"] = 120
        self.assertEqual(self.source.interval, 1200)
        self.assert_warning()

    async def test_retry_after_later_than_rate_reset_wins(self):
        self.queue({"status": 429, "headers": {"Retry-After": "1200", "X-RateLimit-Remaining": "0",
                                                "X-RateLimit-Reset": str(int(START + 900))}, "returncode": 1})
        await self.source.tick()
        self.assertEqual(self.state()["retry_at"], START + 1200)
        self.assertEqual(self.source.interval, 1200)
        self.assert_warning()

    async def test_malformed_expired_or_nonexhausted_rate_headers_do_not_add_backoff(self):
        headers = [{"Retry-After": value} for value in ("", "invalid", "-1", "1.5", "0", formatdate(START - 1, usegmt=True))]
        headers += [{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": value}
                    for value in ("", "invalid", "-1", str(int(START - 1)))]
        headers += [{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": str(int(START + 900))},
                    {"X-RateLimit-Reset": str(int(START + 900))},
                    {"X-RateLimit-Remaining": "0"}]
        self.queue(*({"headers": item} for item in headers))
        for count, item in enumerate(headers, 1):
            with self.subTest(headers=item):
                await self.source.tick()
                self.assertNotIn("retry_at", self.state())
                self.assertEqual(self.source.interval, 120)
                self.assertEqual(len(self.calls()), count)
        self.assert_counts()
        self.assertEqual(self.rows("events"), [])

    async def test_restart_during_backoff_preserves_deadline_and_skips_gh_until_due(self):
        self.queue({"status": 429, "headers": {"Retry-After": "600"}, "returncode": 1})
        await self.source.tick()
        saved = self.state()
        self.store.close()
        self.store = Store(self.db_path)
        bus = Bus(self.store, self.router, None, {"github": "observe"}, github=self.options)
        clock = Mock(return_value=START + 400)
        source = GitHubNotifications(self.store, bus, self.options, clock=clock)
        self.assertEqual(source.state, saved)
        self.assertEqual(source.interval, 200)
        self.queue({"body": [notification()]})
        await source.tick()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state(), saved)
        clock.return_value = START + 600
        await source.tick()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["state"], saved)
        self.assert_counts(1, 1, 1)
        self.assert_warning()

    async def test_rate_exhausted_on_first_page_stops_pagination_without_committing_partial_scan(self):
        clock = Mock(return_value=START)
        self.source.clock = clock
        self.queue({"body": [notification("page1", 20)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED,
                    "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(START + 600))}},
                   {"body": [notification("page1", 20)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED}},
                   {"body": [notification("page2", 10)]})
        await self.source.tick()
        self.assertEqual(len(self.calls()), 1)
        self.assert_counts()
        self.assertEqual(self.state(), {"started": START, "since": START, "modified": "", "poll": 60, "retry_at": START + 600})
        self.assert_warning()
        await self.source.tick()
        self.assertEqual(len(self.calls()), 1)
        clock.return_value = START + 600
        await self.source.tick()
        self.assert_counts(2, 2, 2)
        self.assertEqual([self.query(call)["page"] for call in self.calls()], [["1"], ["1"], ["2"]])
        self.assertEqual(self.state()["since"], START)
        self.assertIn("snapshot", self.state())
        self.assert_warning()

    async def test_retry_after_on_second_page_stops_third_page_and_preserves_pending_snapshot(self):
        first = {"body": [notification("page1", 20)], "headers": {"Link": NEXT, "Last-Modified": MODIFIED}}
        self.queue(first, {"body": [notification("page2", 10)]})
        await self.source.tick()
        saved = self.state()
        self.assertIn("snapshot", saved)
        self.queue(first, {"body": [notification("new-page2", 30)], "headers": {"Link": NEXT, "Retry-After": "900"}})
        await self.source.tick()
        self.assertEqual(len(self.calls()), 2)  # No page 3, even though the server supplied a next link.
        self.assertEqual(self.state(), dict(saved, retry_at=START + 900))
        self.assert_counts(2, 2, 2)  # The newly encountered page-2 notification is not partially committed.
        self.assert_warning()
        await self.source.tick()
        self.assertEqual(len(self.calls()), 2)

    async def test_malformed_notifications_fail_whole_poll_without_progress(self):
        bad_rows = [None, [], {}, notification(updated_at="not-a-date"), notification(updated_at=None),
                    notification(repository=None), notification(repo="invalid repo"), notification(subject=None),
                    notification(reason=None), notification(reason="not a type"), notification(id="x" * 501)]
        for field in ("id", "updated_at", "reason", "subject", "repository"):
            row = notification()
            del row[field]
            bad_rows.append(row)
        row = notification()
        row["subject"]["url"] = 7
        bad_rows.append(row)
        saved = self.state()
        cases = [{"body": [notification("valid-first"), bad]} for bad in bad_rows]
        cases.extend({"body": body} for body in ("not JSON", "null", {}, {"message": SECRET}))
        self.queue(*cases)
        for count in range(1, len(cases) + 1):
            with self.subTest(case=count):
                await self.source.tick()
                self.assert_counts()
                self.assertEqual(self.state(), saved)
                self.assert_warning()

    async def test_observation_limit_records_excess_and_duplicates_do_not_spend_budget(self):
        self.bus = Bus(self.store, self.router, None, {"github": "observe"}, github=dict(self.options, max_observe_per_hour=2))
        self.source.bus = self.bus
        self.queue({"body": [notification("a", 1), notification("a", 1), notification("b", 2), notification("c", 3)]})
        with patch("super_super_harness.events.bus.time.time", return_value=START):
            await self.source.tick()
        self.assert_counts(3, 2, 3)
        self.assertEqual([row["status"] for row in self.rows("observations")], ["queued", "queued", "limited"])
        self.assertIsNone(self.rows("observations")[2]["message_id"])
        self.queue({"body": [notification("c", 3), notification("d", 4)]})
        with patch("super_super_harness.events.bus.time.time", return_value=START + 3601):
            await self.source.tick()
        self.assert_counts(4, 3, 4)
        self.assertEqual([row["status"] for row in self.rows("observations")], ["queued", "queued", "limited", "queued"])

    async def test_zero_observation_budget_records_without_router_submission(self):
        self.source.bus = Bus(self.store, self.router, None, {"github": "observe"}, github=dict(self.options, max_observe_per_hour=0))
        self.queue({"body": [notification()]})
        await self.source.tick()
        self.assert_counts(1, 0, 1)
        self.assertEqual(self.rows("observations")[0]["status"], "limited")
        self.router.submit.assert_not_called()

    async def test_default_observe_requires_connector_kind_and_observe_permission(self):
        cases = [({}, "record"), ({"github": {"permission": "observe"}}, "record"),
                 ({"github": {"kind": "github_notifications"}}, "observe"),
                 ({"github": {"kind": "github_notifications", "permission": "notify"}}, "record"),
                 ({"github": {"kind": "github_notifications", "permission": "act"}}, "record")]
        for sources, expected in cases:
            with self.subTest(sources=sources):
                cfg = config.from_raw(raw_config(sources), self.home)
                bus = Bus(self.store, self.router, None, cfg.sources, cfg.rules, github=cfg.github)
                self.assertEqual(bus.route(notification_event(notification()))[1], expected)
        cfg = config.from_raw(raw_config({"github": {"kind": "github_notifications"}},
                                        [{"source": "github", "action": "record"}]), self.home)
        bus = Bus(self.store, self.router, None, cfg.sources, cfg.rules, github=cfg.github)
        self.assertEqual(bus.route(notification_event(notification())), ("rule 1", "record"))

    async def test_unavailable_and_recovered_transitions_are_durable_across_restart(self):
        self.queue({"status": 401, "body": SECRET}, {"stdout": "", "returncode": 1},
                   {"status": 502, "body": SECRET}, {"body": []}, {"status": 304, "body": ""},
                   {"status": 401, "body": SECRET}, {"status": 502, "body": SECRET},
                   {"status": 304, "body": ""})
        await self.source.tick()
        self.assertEqual(self.store.get(HEALTH_KEY), "unavailable")
        source = GitHubNotifications(self.store, self.bus, self.options, clock=lambda: START)
        await source.tick()
        await source.tick()
        self.assert_warning()
        self.assert_recovered(0)
        await source.tick()  # Complete 200 recovers, even if there were no notifications.
        await source.tick()  # Healthy 304 does not emit a second recovery.
        self.assertEqual(self.store.get(HEALTH_KEY), "healthy")
        self.assert_recovered()
        await source.tick()
        self.assert_warning(2)
        source = GitHubNotifications(self.store, self.bus, self.options, clock=lambda: START)
        await source.tick()
        self.assert_warning(2)
        await source.tick()  # A successful 304 can recover too.
        self.assert_recovered(2)
        self.assertEqual([row["type"] for row in self.rows("events")],
                         ["source.warn", "source.recovered", "source.warn", "source.recovered"])

    async def drive_three_cycles(self):
        sleeping = asyncio.Event()
        delays = []

        async def next_cycle(delay):
            delays.append(delay)
            if len(delays) == 3:
                sleeping.set()
                await asyncio.Future()

        with patch("super_super_harness.events.github.asyncio.sleep", side_effect=next_cycle):
            self.source.start()
            task = self.source.task
            await asyncio.wait_for(sleeping.wait(), timeout=5)
            self.assertFalse(task.done())
            await self.source.close()
        self.assertTrue(task.cancelled())  # CancelledError must not become a retry/warning.
        self.assertEqual(delays, [120, 120, 120])

    async def test_unexpected_and_sqlite_poll_failures_do_not_kill_polling(self):
        with patch.object(self.source, "request", AsyncMock(side_effect=[
                RuntimeError(SECRET), sqlite3.OperationalError(SECRET), (200, {}, "[]")])) as request:
            await self.drive_three_cycles()
        self.assertEqual(request.await_count, 3)
        self.assert_warning()
        self.assert_recovered()
        self.assertEqual(self.store.get(HEALTH_KEY), "healthy")

    async def test_cancelled_poll_propagates_without_warning(self):
        with patch.object(self.source, "request", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.source.tick()
        self.assertEqual(self.rows("events"), [])
        self.assertIsNone(self.store.get(HEALTH_KEY))

    async def test_run_supervises_exceptions_escaping_tick(self):
        with patch.object(self.source, "tick", AsyncMock(side_effect=[
                RuntimeError(SECRET), sqlite3.DatabaseError(SECRET), None])) as tick:
            await self.drive_three_cycles()
        self.assertEqual(tick.await_count, 3)
        self.assert_warning()

    async def test_sqlite_emit_failure_rolls_back_then_success_recovers(self):
        self.queue({"body": [notification()]}, {"body": [notification()]})
        saved = self.state()
        with patch.object(self.bus, "emit", side_effect=sqlite3.OperationalError(SECRET)):
            await self.source.tick()
        self.assert_counts()
        self.assertEqual(self.state(), saved)
        self.assert_warning()
        await self.source.tick()
        self.assert_counts(1, 1, 1)
        self.assert_recovered()

    async def test_failed_warning_store_uses_one_sanitized_fallback_and_recovers(self):
        with patch.object(self.source, "request", AsyncMock(side_effect=sqlite3.DatabaseError(SECRET))), \
                patch.object(self.store, "event", side_effect=sqlite3.OperationalError(SECRET)), \
                self.assertLogs("super_super_harness.events.github", level="WARNING") as logs:
            await self.source.tick()
            await self.source.tick()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("service log", logs.output[0])
        self.assertNotIn(SECRET, logs.output[0])
        self.assertEqual(self.rows("events"), [])
        self.queue({"body": []})
        await self.source.tick()
        self.assert_recovered()
        self.assertEqual(self.store.get(HEALTH_KEY), "healthy")

    async def test_failed_recovery_status_is_retried_without_duplicate_logs(self):
        self.queue({"status": 503, "body": SECRET}, {"body": []}, {"body": []}, {"body": []}, {"body": []})
        await self.source.tick()
        self.assert_warning()
        with patch.object(self.store, "event", side_effect=sqlite3.OperationalError(SECRET)), \
                self.assertLogs("super_super_harness.events.github", level="WARNING") as logs:
            await self.source.tick()
            await self.source.tick()
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(SECRET, logs.output[0])
        self.assertTrue(self.source.health_pending)
        self.assertEqual(self.store.get(HEALTH_KEY), "unavailable")
        await self.source.tick()
        self.assertFalse(self.source.health_pending)
        self.assertEqual(self.store.get(HEALTH_KEY), "healthy")
        self.assert_recovered()
        await self.source.tick()
        self.assert_recovered()
        self.assert_warning()

    async def test_pending_unavailable_status_is_stored_once_when_database_recovers(self):
        self.queue(*({"status": 503, "body": SECRET} for _ in range(4)))
        with patch.object(self.store, "event", side_effect=sqlite3.OperationalError(SECRET)), \
                self.assertLogs("super_super_harness.events.github", level="WARNING") as logs:
            await self.source.tick()
            await self.source.tick()
        self.assertEqual(len(logs.output), 1)
        self.assertTrue(self.source.health_pending)
        await self.source.tick()
        self.assertFalse(self.source.health_pending)
        self.assertEqual(self.store.get(HEALTH_KEY), "unavailable")
        await self.source.tick()
        self.assert_warning()
        self.assert_recovered(0)

    async def test_warning_event_and_health_transition_commit_together(self):
        self.queue({"status": 500, "body": SECRET})
        save = self.store.set

        def fail_health(key, value):
            if key == HEALTH_KEY:
                raise sqlite3.OperationalError(SECRET)
            return save(key, value)

        with patch.object(self.store, "set", side_effect=fail_health), \
                self.assertLogs("super_super_harness.events.github", level="WARNING") as logs:
            await self.source.tick()
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(SECRET, logs.output[0])
        self.assertEqual(self.rows("events"), [])  # No committed warn with an uncommitted health state.
        self.assertIsNone(self.store.get(HEALTH_KEY))
        self.queue({"body": []})
        await self.source.tick()
        self.assert_recovered()

    async def test_polling_lifecycle_uses_negotiated_interval_and_close_cancels_sleep(self):
        self.queue({"headers": {"X-Poll-Interval": "240"}})
        sleeping = asyncio.Event()

        async def sleep_until_cancelled(delay):
            sleeping.set()
            await asyncio.Future()

        with patch("super_super_harness.events.github.asyncio.sleep", new=AsyncMock(side_effect=sleep_until_cancelled)) as sleep:
            self.source.start()
            task = self.source.task
            await asyncio.wait_for(sleeping.wait(), timeout=5)
            sleep.assert_awaited_once_with(240)
            await self.source.close()
        self.assertIsNone(self.source.task)
        self.assertTrue(task.cancelled())
        self.assertEqual(len(self.calls()), 1)
        await self.source.close()  # Idempotent cleanup.


if __name__ == "__main__":
    unittest.main()
