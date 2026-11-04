"""B26: private images through real local HTTP and deterministic Harness processes."""
import asyncio
import base64
import threading
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from super_super_harness.adapters import hermes, image_input
from super_super_harness.channels.chat import History
from super_super_harness.core.attachments import Attachments, MAX_BYTES, MAX_COUNT, image_type
from super_super_harness.core.store import Store
from super_super_harness.ops import config
from super_super_harness.router.router import Router
from tests import test_web, test_hermes_integration
from tests.support import PAGE, ServiceCase, write_config

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1kAAAAASUVORK5CYII=")
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


class ImageRouterDelivery(unittest.IsolatedAsyncioTestCase):
    """Admission and delivery may use different Profiles, including after failover."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssharness-image-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        write_config(self.home, 1, 2)
        self.cfg = config.load(self.home)
        self.cfg.profiles["spare"] = replace(self.cfg.profile("spare"), tools="Bash")
        self.store = Store(self.home / "router.db")
        self.addCleanup(self.store.close)
        self.router = Router(self.cfg, self.store, SimpleNamespace(render=lambda: ""), None, None)
        self.router.mode = "paused"
        self.router._busy_guard = AsyncMock()
        self.files = Attachments(self.cfg, self.store)
        self.image = self.files.save(PNG, "screenshot.png", "image/png")
        self.path = str(self.files.path(self.files.get(self.image["id"])))
        self.send = Mock(side_effect=lambda text, uuid: uuid)
        self.router.process = SimpleNamespace(send=self.send)
        self.router.ready = True

    async def queue_batch(self):
        image_id = self.router.submit("user", "inspect screenshot", "web", attachments=[self.image["id"]])
        text_id = self.router.submit("user", "ordinary follow-up", "web")
        await asyncio.sleep(0)   # Drain submit's scheduled pumps while paused.
        return image_id, text_id

    def states(self):
        return [row["status"] for row in self.store.all("SELECT status FROM messages ORDER BY id")]

    def without_read(self):
        self.router.check_profile("spare")
        self.store.set("router_profile", "spare")

    def assert_unavailable(self, text):
        for expected in ("inspect screenshot", self.image["id"], self.path, "Image viewing is unavailable",
                         "NOT been inspected", "Tell the user", "do not bypass", "explicit, authorized handoff"):
            self.assertIn(expected, text)
        self.assertNotIn("Use Read", text)
        self.assertNotIn("vision_analyze", text)

    async def test_profile_change_delivers_notice_and_does_not_block_following_text(self):
        await self.queue_batch()
        self.without_read()
        self.router.mode = "running"
        await self.router.pump()
        self.assertEqual(self.send.call_count, 2)
        self.assert_unavailable(self.send.call_args_list[0].args[0])
        self.assertIn("ordinary follow-up", self.send.call_args_list[1].args[0])
        self.assertEqual(self.states(), ["written", "written"], "Formatting alone is not a durable receipt")
        self.assertEqual(self.files.read(self.files.get(self.image["id"])), PNG)
        with self.assertRaisesRegex(ValueError, "enabled tools"):
            self.router.submit("user", "new image", "web", attachments=[self.image["id"]])
        self.assertEqual(len(self.store.all("SELECT id FROM messages")), 2)

    async def test_mixed_observation_batch_keeps_notice_and_normal_text_in_shared_input(self):
        await self.queue_batch()
        event_id = self.router.submit("event", "observe fixture", kind="fixture")
        self.store.run("INSERT INTO observations(trigger_id,message_id,status,created) VALUES(?,?,?,?)", 1, event_id, "queued", 0)
        await asyncio.sleep(0)
        self.without_read()
        self.router.mode = "running"
        await self.router.pump()
        self.assertEqual(self.send.call_count, 1)
        text = self.send.call_args.args[0]
        self.assert_unavailable(text)
        self.assertIn("ordinary follow-up", text)
        self.assertIn('mode="observe" event_id="1"', text)
        self.assertEqual(self.states(), ["written"] * 3)
        self.assertEqual(len({row["uuid"] for row in self.store.all("SELECT uuid FROM messages")}), 1)

    async def test_refused_send_keeps_input_queued_and_retry_uses_restored_capability(self):
        await self.queue_batch()
        self.without_read()
        self.router.mode = "running"
        self.send.side_effect = None
        self.send.return_value = None
        await self.router.pump()
        self.assertEqual(self.states(), ["queued", "queued"])
        self.store.set("router_profile", "main")
        self.send.reset_mock()
        self.send.side_effect = lambda text, uuid: uuid
        await self.router.pump()
        self.assertEqual(self.send.call_count, 2)
        text = self.send.call_args_list[0].args[0]
        self.assertIn(self.image["id"], text)
        self.assertIn("Use Read", text)
        self.assertNotIn("Image viewing is unavailable", text)
        self.assertEqual(self.states(), ["written", "written"])

    async def test_interactive_profile_change_returns_both_inputs_with_explicit_notice(self):
        await self.queue_batch()
        self.without_read()
        self.router.mode = "attached"
        delivered = self.router.read_inbox()
        self.assertEqual(len(delivered), 2)
        self.assert_unavailable(delivered[0])
        self.assertIn("ordinary follow-up", delivered[1])
        self.assertEqual(self.states(), ["done", "done"])
        self.assertEqual(self.router.read_inbox(), [])

    async def test_interactive_format_failure_leaves_entire_batch_unconsumed(self):
        _, text_id = await self.queue_batch()
        self.router.mode = "attached"
        self.router.stop_blocks = 2
        original = self.router._format
        def fail_second(row):
            if row["id"] == text_id:
                raise ValueError("fixture formatting failure")
            return original(row)
        with patch.object(self.router, "_format", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "fixture formatting"):
                self.router.read_inbox()
        self.assertEqual(self.states(), ["queued", "queued"])
        self.assertEqual(self.router.stop_blocks, 2)
        delivered = self.router.read_inbox()
        self.assertEqual(len(delivered), 2)
        self.assertIn(self.image["id"], delivered[0])
        self.assertIn("ordinary follow-up", delivered[1])
        self.assertEqual(self.states(), ["done", "done"])
        self.assertEqual(self.router.stop_blocks, 0)

    async def test_interactive_confirmation_failure_rolls_back_entire_batch(self):
        _, text_id = await self.queue_batch()
        self.router.mode = "attached"
        original = self.store.run
        def fail_second(sql, *args):
            if sql == "UPDATE messages SET status='done' WHERE id=?" and args == (text_id,):
                raise RuntimeError("fixture database failure")
            return original(sql, *args)
        with patch.object(self.store, "run", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "fixture database"):
                self.router.read_inbox()
        self.assertEqual(self.states(), ["queued", "queued"])
        self.assertEqual(len(self.router.read_inbox()), 2)
        self.assertEqual(self.states(), ["done", "done"])


class ImageStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssharness-images-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.cfg = SimpleNamespace(home=self.home, web={})
        self.store = Store(self.home / "test.db")
        self.addCleanup(self.store.close)
        self.files = Attachments(self.cfg, self.store)

    def test_headers_mime_extensions_and_private_storage(self):
        image = self.files.save(PNG, "截图.png", "image/png")
        row = self.files.get(image["id"])
        path = self.files.path(row)
        self.assertEqual(self.files.read(row), PNG)
        self.assertEqual(image["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertIsNone(row["source_message_id"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for directory in (self.files.root, path.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for body, filename, mime in ((b"<script>bad</script>", "x.png", "image/png"),
                                     (PNG, "x.jpg", "image/jpeg"), (PNG, "x.png", "text/html"),
                                     (PNG, "x.svg", "image/png"), (PNG, "x\n.png", "image/png"),
                                     (b"\x89PNG\r\n\x1a\n", "x.png", "image/png")):
            with self.subTest(filename=filename, mime=mime), self.assertRaises(ValueError):
                self.files.save(body, filename, mime)
        self.assertEqual(image_type(GIF), "image/gif")
        self.assertEqual(image_type(b"\xff\xd8\xff\xe0fixture"), "image/jpeg")
        self.assertEqual(image_type(b"RIFF\x0c\x00\x00\x00WEBPVP8L\x00\x00\x00\x00"), "image/webp")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attachments")["n"], 1)

    def test_limits_ids_linking_and_reopen(self):
        self.cfg.web = {"image_max_bytes": len(PNG), "image_max_count": 1}
        image = self.files.save(PNG, "x.png", "image/png")
        with self.assertRaises(ValueError):
            self.files.save(PNG + b"x", "x.png", "image/png")
        for ids in ([image["id"], image["id"]], image["id"], ["../secret"], [{}], ["0" * 32]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.files.resolve(ids)
        with self.assertRaisesRegex(ValueError, "not been sent"):
            self.files.resolve([image["id"]], sent=True)
        mid = self.store.add_message("uuid", "user", "look", "web")
        with self.store.atomic():
            self.files.link(mid, self.files.resolve([image["id"]]))
        self.assertEqual(self.files.resolve([image["id"]], sent=True)[0]["source_message_id"], mid)
        other = Store(self.home / "test.db")
        try:
            reopened = Attachments(self.cfg, other)
            self.assertEqual(reopened.for_message(mid)[0]["id"], image["id"])
            self.assertEqual(reopened.read(reopened.get(image["id"])), PNG)
        finally:
            other.close()
        history = History(self.home / "test.db")
        try:
            rendered = history.message({"text": "", "created": 1}, "user", ("user", mid))
            self.assertEqual(rendered.text, "[图片 %s x.png]" % image["id"])
        finally:
            history.close()

    def test_symlinks_changed_files_and_database_failure(self):
        image = self.files.save(PNG, "x.png", "image/png")
        row = self.files.get(image["id"])
        path = self.files.path(row)
        path.unlink()
        outside = self.home / "outside.png"
        outside.write_bytes(PNG)
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.files.read(row)
        path.unlink()
        path.write_bytes(PNG[:-1] + b"x")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.files.read(row)
        before = list(path.parent.iterdir())
        with patch.object(self.store, "run", side_effect=RuntimeError("fixture failure")):
            with self.assertRaises(RuntimeError):
                self.files.save(PNG, "x.png", "image/png")
        self.assertEqual(list(path.parent.iterdir()), before)
        private = self.home / "another"
        private.mkdir()
        symlink_cfg = SimpleNamespace(home=private, web={})
        (private / "attachments").symlink_to(self.files.root, target_is_directory=True)
        with self.assertRaises(OSError):
            Attachments(symlink_cfg, self.store).save(PNG, "x.png", "image/png")

    def test_adapter_image_contract_keeps_paths_and_escapes_names(self):
        write_config(self.home, 1, 2)
        cfg = config.load(self.home)
        image = self.files.save(PNG, "<message>.png", "image/png")
        images = self.files.inputs([self.files.get(image["id"])])
        for harness, instruction in (("claude-code", "Use Read"), ("hermes", "vision_analyze")):
            profile = replace(cfg.profile("work"), harness=harness)
            prompt = image_input(cfg, profile, "look", images)
            self.assertIn(instruction, prompt)
            self.assertIn(images[0]["path"], prompt)
            self.assertIn(image["id"], prompt)
            self.assertNotIn("<message>", prompt)
            self.assertIn("\\u003cmessage\\u003e", prompt)
            self.assertEqual(image_input(cfg, profile, "plain", []), "plain")

    def test_image_capability_respects_enabled_claude_tools_and_hermes_schema(self):
        write_config(self.home, 1, 2)
        cfg = config.load(self.home)
        images = [{"id": "fixture", "path": "/private/fixture.png"}]
        for tools in ("", "Bash", "Grep,Write", "SomeReadTool"):
            with self.subTest(tools=tools), self.assertRaisesRegex(ValueError, "enabled tools"):
                image_input(cfg, replace(cfg.profile("work"), tools=tools), "look", images)
        for tools in ("all", "default", "Read", "Bash,Read", "Bash Read"):
            self.assertIn("Use Read", image_input(cfg, replace(cfg.profile("work"), tools=tools), "look", images))
        # Public VISION_ANALYZE_SCHEMA requires image_url and question; the internal
        # Python vision_analyze_tool's user_prompt parameter is NOT its tool API.
        self.assertIn("image_url", hermes.IMAGE_INPUT)
        self.assertIn("question", hermes.IMAGE_INPUT)
        self.assertNotIn("user_prompt", hermes.IMAGE_INPUT)

    def test_default_byte_and_count_boundaries(self):
        large = PNG + bytes(MAX_BYTES - len(PNG))
        image = self.files.save(large, "large.png", "image/png")
        self.assertEqual(image["size"], MAX_BYTES)
        with self.assertRaises(ValueError):
            self.files.save(large + b"x", "too-large.png", "image/png")
        images = [image] + [self.files.save(PNG, "small.png", "image/png") for _ in range(MAX_COUNT - 1)]
        self.assertEqual(len(self.files.resolve([a["id"] for a in images])), MAX_COUNT)
        extra = self.files.save(PNG, "extra.png", "image/png")
        with self.assertRaises(ValueError):
            self.files.resolve([a["id"] for a in images] + [extra["id"]])

    def test_configuration_defaults_and_bounds(self):
        write_config(self.home, 1, 2)
        original = (self.home / "profiles.toml").read_text()
        cfg = config.load(self.home)
        self.assertEqual(cfg.web["image_max_bytes"], MAX_BYTES)
        self.assertEqual(cfg.web["image_max_count"], MAX_COUNT)
        for key, valid, invalid in (("image_max_count", "2", ["0", "7", "true"]),
                                    ("image_max_bytes", "100", ["0", str(MAX_BYTES + 1), '"100"'])):
            for value in [valid] + invalid:
                (self.home / "profiles.toml").write_text(original.replace("[web]", "[web]\n%s = %s" % (key, value)))
                if value == valid:
                    self.assertEqual(config.load(self.home).web[key], int(value))
                else:
                    with self.assertRaisesRegex(ValueError, key):
                        config.load(self.home)


class ImageHTTP(ServiceCase):
    # Share only HTTP helpers, not the hundreds of unrelated WebCase tests.
    request = test_web.WebCase.request
    login = test_web.WebCase.login

    async def boot(self):
        from tests.support import free_port
        self.cfg.web["port"] = free_port()
        await super().boot()
        self.port, self.cookie = self.cfg.web["port"], ""
        await self.login()

    async def upload(self, body=PNG, filename="screenshot.png", mime="image/png", headers=None):
        fields = {"Host": "127.0.0.1:%d" % self.port, "Cookie": self.cookie,
                  "Origin": "http://127.0.0.1:%d" % self.port, "Content-Type": mime, "Content-Length": str(len(body))}
        fields.update(headers or {})
        raw = ("POST /api/attachments?filename=" + quote(filename) + " HTTP/1.1\r\n" +
               "".join(k + ": " + v + "\r\n" for k, v in fields.items()) + "\r\n").encode() + body
        return await self.request(raw=raw)

    async def test_upload_read_history_and_router_input(self):
        status, _, image = await self.upload(filename="截图.png")
        self.assertEqual(status, 200, image)
        status, headers, body = await self.request(path="/api/attachments/" + image["id"])
        self.assertEqual((status, body), (200, PNG))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = {"text": "", "request_id": "image-only-request", "attachments": [image["id"]]}
        status, _, sent = await self.request("POST", "/api/messages", payload)
        self.assertEqual(status, 200, sent)
        mid = sent["id"]
        await self.until(lambda: self.store.one("SELECT status FROM messages WHERE id=?", mid)["status"] == "done")
        row = self.store.one("SELECT * FROM attachments WHERE id=?", image["id"])
        path = str(self.service.web.attachments.path(row))
        transcript = self.session_path(self.service.router.session_id).read_text()
        self.assertIn(image["id"], transcript)
        self.assertIn(path, transcript)
        self.assertIn("Use Read", transcript)
        self.assertEqual(row["source_message_id"], mid)
        _, _, conversation = await self.request(path="/api/conversation")
        self.assertEqual([m for m in conversation["items"] if m["role"] == "user"][0]["attachments"], [image])
        self.assertEqual((await self.request("POST", "/api/messages", payload))[2], sent)
        self.assertEqual((await self.request("POST", "/api/messages", dict(payload, text="different")))[0], 409)
        _, _, second = await self.upload()
        self.assertEqual((await self.request("POST", "/api/messages", dict(payload, attachments=[second["id"]])))[0], 409)
        self.assertEqual((await self.request())[2]["image_limits"], {"max_bytes": MAX_BYTES, "max_count": MAX_COUNT})

    async def test_restart_preserves_queued_router_assign_and_say_images(self):
        _, _, image = await self.upload()
        self.service.router.mode = "paused"
        payload = {"text": "inspect queued", "request_id": "restart-image-request", "attachments": [image["id"]]}
        status, _, saved = await self.request("POST", "/api/messages", payload)
        self.assertEqual(status, 200)
        self.store.open_turn()
        with patch.object(self.service.tasks, "schedule"):
            task_id = self.start_task("ECHO initial", code_name="QueuedImages")
            for ids in ([image["id"]], []):
                await self.tool("worker_assign", worker="QueuedImages", text="ECHO same text", attachments=ids)
            await self.tool("worker_say", worker="QueuedImages", text="ECHO clarification", attachments=[image["id"]])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"], 3)
        await self.halt()
        await self.boot()
        await self.worker_ready(task_id)
        path = self.session_path(self.store.task(task_id)["session_id"])
        await self.until(lambda: "clarification" in path.read_text())
        user_inputs = [json.loads(line)["message"]["content"] for line in path.read_text().splitlines()
                       if json.loads(line).get("type") == "user"]
        self.assertTrue(any(image["id"] in text and "same text" in text for text in user_inputs))
        self.assertTrue(any(image["id"] in text and "clarification" in text for text in user_inputs))
        self.assertEqual((await self.request("POST", "/api/messages", payload))[2], saved)
        self.assertEqual((await self.request(path="/api/attachments/" + image["id"]))[2], PNG)

    async def test_accepted_retry_ignores_new_admission_limits_and_tools(self):
        first = (await self.upload())[2]
        second = (await self.upload())[2]
        payload = {"text": "two images", "request_id": "accepted-retry-images", "attachments": [first["id"], second["id"]]}
        status, _, saved = await self.request("POST", "/api/messages", payload)
        self.assertEqual(status, 200)
        await self.until(lambda: self.store.one("SELECT status FROM messages WHERE id=?", saved["id"])["status"] == "done")
        self.cfg.web["image_max_count"] = 1
        self.cfg.profiles[self.cfg.primary] = replace(self.cfg.profile(self.cfg.primary), tools="")
        status, _, retry = await self.request("POST", "/api/messages", payload)
        self.assertEqual((status, retry), (200, saved))
        self.assertEqual((await self.request("POST", "/api/messages", dict(payload, request_id="new-two-images")))[0], 400)
        self.assertEqual((await self.request("POST", "/api/messages", dict(payload, request_id="new-no-read-image", attachments=[first["id"]])))[0], 400)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE role='user'")["n"], 1)
        task = self.start_task("ECHO initial")
        self.cfg.profiles["work"] = replace(self.cfg.profile("work"), tools="Bash")
        with self.assertRaisesRegex(ValueError, "enabled tools"):
            self.service.tasks.send(task, "look", attachments=[first["id"]])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task)["n"], 1)

    async def test_slow_image_disk_io_does_not_block_state_requests(self):
        files = self.service.web.attachments
        for operation in ("write", "read"):
            image = (await self.upload())[2] if operation == "read" else None
            entered, release = threading.Event(), threading.Event()
            original = getattr(files, operation)
            def slow(*args):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("image I/O blocked the service loop")
                return original(*args)
            with patch.object(files, operation, side_effect=slow):
                request = asyncio.create_task(self.upload() if operation == "write" else self.request(path="/api/attachments/" + image["id"]))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertEqual((await asyncio.wait_for(self.request(), .5))[0], 200)
                    if operation == "write":
                        self.assertEqual((await asyncio.wait_for(self.upload(), .5))[0], 429)
                finally:
                    release.set()
                    response = await request
                self.assertEqual(response[0], 200, response)

    async def test_cancelled_upload_finishes_file_and_metadata_before_releasing_slot(self):
        web = self.service.web
        entered, release = threading.Event(), threading.Event()
        original = web.attachments.write
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture write was not released")
            return original(*args)
        closing = None
        with patch.object(web.attachments, "write", side_effect=slow):
            request = asyncio.create_task(self.upload())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                closing = asyncio.create_task(web.close())
                await asyncio.sleep(0)
                self.assertTrue(web.upload_busy)
                self.assertFalse(closing.done())
            finally:
                release.set()
                if closing:
                    await closing
                await asyncio.gather(request, return_exceptions=True)
        self.assertFalse(web.upload_busy)
        rows = self.store.all("SELECT * FROM attachments")
        self.assertEqual(len(rows), 1)
        self.assertEqual(web.attachments.read(rows[0]), PNG)

    async def test_auth_origin_format_limits_and_unknown_ids(self):
        for headers, expected in (({"Cookie": ""}, 401), ({"Origin": "http://evil.invalid"}, 403),
                                  ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Host": "evil.invalid"}, 403)):
            self.assertEqual((await self.upload(headers=headers))[0], expected)
        self.assertEqual((await self.upload(body=b"<html>bad</html>"))[0], 400)
        self.assertEqual((await self.upload(filename="fake.jpg"))[0], 400)
        self.assertEqual((await self.upload(mime="image/svg+xml"))[0], 415)
        self.cfg.web["image_max_bytes"] = len(PNG)
        self.assertEqual((await self.upload(body=PNG + b"x"))[0], 413)
        _, _, image = await self.upload()
        url = "/api/attachments/" + image["id"]
        for headers, expected in (({"Cookie": ""}, 401), ({"Origin": "http://evil.invalid"}, 403),
                                  ({"Sec-Fetch-Site": "same-site"}, 403)):
            self.assertEqual((await self.request(path=url, headers=headers))[0], expected)
        self.assertEqual((await self.request(path="/api/attachments/" + "0" * 32))[0], 404)
        for ids in ([image["id"]] * 7, [image["id"]] * 2, ["0" * 32], "bad", [{}]):
            status, _, _ = await self.request("POST", "/api/messages", {"text": "x", "request_id": "bad-image-input", "attachments": ids})
            self.assertEqual(status, 400)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE role='user'")["n"], 0)

    async def test_worker_explicit_forwarding_and_durable_say(self):
        _, _, image = await self.upload()
        files = self.service.web.attachments
        with self.assertRaisesRegex(ValueError, "not been sent"):
            self.start_task("inspect", attachments=[image["id"]])
        self.service.router.submit("user", "look", "web", attachments=[image["id"]])
        await self.until(lambda: self.store.one("SELECT COUNT(*) n FROM messages WHERE status!='done'")["n"] == 0)
        task_id = self.start_task("WAIT", code_name="ImageReader")
        await self.worker_ready(task_id)
        self.assertNotIn(image["id"], self.session_path(self.store.task(task_id)["session_id"]).read_text())
        self.service.router.mode = "paused"
        self.store.open_turn()
        args = {"worker": "ImageReader", "text": "inspect forwarded", "attachments": [image["id"]]}
        result = await self.tool("worker_assign", **args)
        self.assertTrue(result["ok"])
        sid = self.store.task(task_id)["session_id"]
        await self.until(lambda: image["id"] in self.session_path(sid).read_text())
        self.assertIn(str(files.path(files.get(image["id"]))), self.session_path(sid).read_text())
        assigned = self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"]
        await self.tool("worker_assign", **args)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"], assigned)
        self.service.tasks.held.add(task_id)
        await self.service.tasks._end_process(task_id)
        await self.tool("worker_say", worker="ImageReader", text="clarification", attachments=[image["id"]])
        pending = self.service.tasks._pending_says(task_id)
        self.assertIn(image["id"], pending[0]["text"])
        self.assertIn("Use Read", pending[0]["text"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM assignments WHERE task_id=?", task_id)["n"], assigned)
        hired = await self.tool("worker_hire", name="Image worker", brief="inspect", item=self.service.items.of(task_id),
                                code_name="ImageHire", role="test images", attachments=[image["id"]])
        self.assertIn(image["id"], self.store.task(self.worker_id(hired))["brief"])


class ImageHermes(ServiceCase):
    boot = test_hermes_integration.HermesIntegrationTests.boot
    async def halt(self):
        await super().halt()
        await self.until(lambda: all(done.is_set() for group in self.cleanups.values() for done in group))
        self.assertEqual(self.cleanup_errors, [])

    async def test_router_and_worker_receive_local_image_paths_through_fake_acp(self):
        files = Attachments(self.cfg, self.store)
        image = files.save(PNG, "screenshot.png", "image/png")
        mid = self.service.router.submit("user", "inspect image", "web", attachments=[image["id"]])
        await self.until(lambda: self.store.one("SELECT status FROM messages WHERE id=?", mid)["status"] == "done")
        path = str(files.path(files.get(image["id"])))
        self.assertTrue(hermes.session_has(self.cfg, self.service.router.session_id, path))
        task_id = self.start_task("WAIT", code_name="ImageHermes")
        await self.until(lambda: hermes.session_has(self.cfg, self.store.task(task_id)["session_id"], "WAIT"))
        sid = self.store.task(task_id)["session_id"]
        self.assertFalse(hermes.session_has(self.cfg, sid, path))
        await self.tool("worker_assign", worker="ImageHermes", text="REPORT", attachments=[image["id"]])
        self.service.tasks.interrupt(task_id)
        await self.until(lambda: hermes.session_has(self.cfg, sid, path))
        frames = [json.loads(line) for line in (self.home / "hermes" / "homes" / sid / "wire.jsonl").read_text().splitlines()]
        prompts = [f["params"]["prompt"] for f in frames if f.get("method") == "session/prompt"]
        received = next(p[0]["text"] for p in prompts if path in p[0]["text"])
        self.assertIn("vision_analyze", received)
        self.assertIn("image_url", received)
        self.assertIn(image["id"], received)
        self.assertIn("[ssharness-input:", received)


class ImageFrontend(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for frontend regression fixture")
    def test_image_regressions(self):
        for case in ("b26-image-inputs", "b26-image-limits", "b26-image-upload-retry", "b26-image-send-retry",
                     "b26-image-races", "b26-image-rendering", "b26-image-auth-lifecycle"):
            result = subprocess.run(["node", str(Path(__file__).with_name("web_regressions.js")), str(PAGE), case],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
