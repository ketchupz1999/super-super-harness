"""Schema-32 fixtures; no dependency on an installed Hermes."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from super_super_harness.adapters import hermes, hermes_state
from tests.test_hermes import configuration, state


class HermesStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = configuration(self.root)
        argv = hermes.headless_argv(self.cfg, self.cfg.profile("work"), "s", False, None, "fixture")
        self.spec = json.loads(argv[0])
        self.home = Path(self.spec["home"])
        self.path = self.home / "state.db"
        Path(self.spec["map"]).write_text(json.dumps({"hermes_id": "hid"}))
        self.db = state(self.home)
        self.addCleanup(self.db.close)

    def add(self, role, value, **extra):
        fields = {"session_id": "hid", "role": role, "content": value, "timestamp": 1793700000, **extra}
        self.db.execute("INSERT INTO messages(%s) VALUES(%s)" % (",".join(fields), ",".join("?" for _ in fields)), list(fields.values()))
        self.db.commit()

    def test_real_user_markers_decode_sentinel_and_include_compacted_rows(self):
        self.add("user", '\x00json:' + json.dumps([{"type": "text", "text": 'id="m1" 中文'}]), active=0, compacted=1)
        self.add("tool", "tool-only-marker")
        self.add("assistant", "assistant-only-marker")
        self.add("user", "other-session-marker", session_id="other")
        self.add("user", '"literal\\nnot-a-newline"')
        self.assertTrue(hermes.session_has(self.cfg, "s", 'id="m1" 中文'))
        self.assertTrue(hermes.session_has(self.cfg, "s", '"literal\\nnot-a-newline"'))
        for marker in ("tool-only-marker", "assistant-only-marker", "other-session-marker", "literal\nnot-a-newline"):
            self.assertFalse(hermes.session_has(self.cfg, "s", marker))
        self.assertEqual(hermes.prepare_session(self.cfg, "s"), ("s", True))

    def test_transcript_roles_order_hidden_and_removed_rows(self):
        self.add("user", "received", active=0, compacted=1)
        self.add("assistant", "said", reasoning_content="thought", tool_calls=json.dumps([
            {"id": "tc", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}]))
        self.add("tool", "result")
        self.add("user", "hidden", display_kind="hidden")
        self.add("assistant", "model-only", display_kind="model_only")
        self.add("assistant", "rewound", active=0, compacted=0)
        items = hermes.transcript(self.cfg, "s")
        self.assertEqual([i["kind"] for i in items], ["received", "thinking", "said", "tool_call", "tool_result"])
        self.assertEqual(items[3]["tool"], "terminal")
        self.assertEqual(items[3]["text"], '{"command":"pwd"}')
        self.assertTrue(all(i["time"].endswith("+00:00") for i in items))
        self.assertEqual(hermes.transcript(self.cfg, "s", limit=2), items[-2:])

    def test_payload_byte_item_and_text_bounds_apply_before_large_cells_reach_python(self):
        for i in range(20):
            self.add("user", str(i))
        self.add("assistant", "中" * 1000000)
        original = hermes_state._items
        observed = []
        def inspect(row):
            observed.extend(len(row[key] or b"") for key in hermes_state.PAYLOAD)
            return original(row)
        with patch.object(hermes_state, "_items", side_effect=inspect):
            items = hermes.transcript(self.cfg, "s", max_bytes=128, limit=3, text_limit=12)
        self.assertLessEqual(max(observed), 128)
        self.assertLessEqual(len(items), 3)
        self.assertLessEqual(sum(len(i["text"].encode()) for i in items), 128)
        self.assertTrue(items[-1]["truncated"])
        self.assertIn("truncated", items[-1]["text"])
        self.add("assistant", "", tool_calls=json.dumps([{"function": {"name": "large", "arguments": "x" * 1000000}}]))
        tool = hermes.transcript(self.cfg, "s", max_bytes=128, limit=1)
        self.assertEqual(tool[0]["kind"], "tool_call")
        self.assertTrue(tool[0]["truncated"])
        self.assertLessEqual(len(tool[0]["text"].encode()), 128)
        self.assertEqual(hermes.transcript(self.cfg, "s", max_bytes=0), [])
        self.assertEqual(hermes.transcript(self.cfg, "s", limit=0), [])

    def test_bounded_structured_content_does_not_disappear_at_nul_sentinel(self):
        self.add("user", '\x00json:' + json.dumps([{"type": "text", "text": "structured"}]))
        self.assertEqual(hermes.transcript(self.cfg, "s", max_bytes=1024)[0]["text"], "structured")

    def test_empty_and_image_only_tail_rows_do_not_hide_recent_items(self):
        self.add("user", "last real text")
        self.add("assistant", "")
        self.add("user", '\x00json:' + json.dumps([{"type": "image", "data": "image-only"}]))
        self.add("tool", "")
        self.assertEqual(hermes.transcript(self.cfg, "s", limit=1)[0]["text"], "last real text")

    def test_truncated_structured_images_are_not_rendered_as_raw_json(self):
        self.add("user", '\x00json:' + json.dumps([{"type": "image", "data": "private-image-data" * 1000}]))
        self.assertEqual(hermes.transcript(self.cfg, "s"), [])
        self.assertEqual(hermes.transcript(self.cfg, "s", max_bytes=128), [])

    def test_message_query_error_cannot_replace_a_saved_session(self):
        with patch.object(hermes_state, "_open") as opened:
            opened.return_value.__enter__.return_value.execute.side_effect = sqlite3.DatabaseError("bad data page")
            with self.assertRaisesRegex(ValueError, "refusing to discard"):
                hermes.prepare_session(self.cfg, "s")
        self.assertEqual(json.loads(Path(self.spec["map"]).read_text()), {"hermes_id": "hid"})

    def test_reads_are_read_only_and_unknown_schema_is_not_silently_resumed(self):
        self.add("user", "marker")
        before = self.path.read_bytes()
        hermes.transcript(self.cfg, "s")
        hermes.session_has(self.cfg, "s", "marker")
        with hermes_state._open(self.path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM messages")
        self.assertEqual(self.path.read_bytes(), before)
        self.db.execute("UPDATE schema_version SET version=99")
        self.db.commit()
        self.assertEqual(hermes.transcript(self.cfg, "s"), [])
        self.assertFalse(hermes.session_has(self.cfg, "s", "marker"))
        self.assertIsNone(hermes_state.compacted(self.path, "hid"))
        with self.assertRaisesRegex(ValueError, "unreadable or unsupported"):
            hermes.prepare_session(self.cfg, "s")
        self.db.execute("UPDATE schema_version SET version=32")
        self.db.execute("ALTER TABLE messages RENAME COLUMN display_kind TO unrecognized")
        self.db.commit()
        self.assertEqual(hermes.transcript(self.cfg, "s"), [])

    def test_missing_corrupt_and_malformed_cells_are_safe(self):
        self.assertEqual(hermes.transcript(self.cfg, "missing"), [])
        self.assertFalse((self.root / "hermes" / "homes" / "missing").exists())
        self.add("assistant", "fine", tool_calls="not-json")
        self.add("assistant", "also fine", tool_calls=json.dumps([None, {"function": None}]))
        self.assertEqual([i["text"] for i in hermes.transcript(self.cfg, "s")], ["fine", "also fine"])
        self.db.close()
        self.path.write_bytes(b"not a database")
        self.assertEqual(hermes.transcript(self.cfg, "s"), [])
        self.assertFalse(hermes.session_has(self.cfg, "s", "fine"))


class HermesCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_emits_only_when_count_grows_and_before_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg = configuration(root)
            profile = cfg.profile("work")
            events = []
            async def event(message):
                events.append(message)
            async def exited(code):
                pass
            process = hermes.Process(hermes.headless_argv(cfg, profile, "s", False, None, "fixture"),
                                     hermes.build_env(cfg, profile), root, event, exited)
            try:
                await process.start()
                with patch.object(hermes_state, "compacted", side_effect=[None, 1, 1]):
                    await process._compaction_boundary()
                    self.assertEqual(process._compacted, 0)
                    await process._compaction_boundary()
                    await process._compaction_boundary()
                self.assertEqual(len([e for e in events if e.get("subtype") == "compact_boundary"]), 1)
                events.clear()
                process._compacted = 0
                for text in ("COMPACT", "normal"):
                    prior = len([e for e in events if e["type"] == "result"])
                    process.send(text)
                    async def completed():
                        while len([e for e in events if e["type"] == "result"]) <= prior:
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(completed(), 3)
                boundaries = [i for i, e in enumerate(events) if e.get("subtype") == "compact_boundary"]
                self.assertEqual(len(boundaries), 1)
                self.assertLess(boundaries[0], next(i for i, e in enumerate(events) if e["type"] == "result"))
                self.assertTrue(hermes.session_has(cfg, "s", "COMPACT"))
            finally:
                await process.close(wait=.1)
