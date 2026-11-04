"""Built-in character assets and the shipped UI state/rendering contract."""
from pathlib import Path
import shutil
import struct
import subprocess
import unittest

from tests.support import PAGE, SUBER
ROOT = Path(__file__).resolve().parent.parent


class SuberAssets(unittest.TestCase):
    def test_web_assets_are_small_complete_square_canvases(self):
        folder = SUBER
        expected = {"mark", "idle", "idle-blink", "typing", "typing-clean",
                    "typing-blink", "waiting", "waiting-blink", "done"}
        self.assertEqual({file.stem for file in folder.iterdir()}, expected)
        total = 0
        for file in folder.iterdir():
            with self.subTest(file=file.name):
                content = file.read_bytes()
                self.assertEqual(content[:8], b"\x89PNG\r\n\x1a\n")
                size = 96 if file.stem == "mark" else 512
                self.assertEqual(struct.unpack(">II", content[16:24]), (size, size))
                self.assertEqual(content[25], 6, "Keep the original RGBA transparency")
                self.assertLess(len(content), 200_000)
                total += len(content)
        self.assertLess(total, 1_200_000)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI rendering tests")
class SuberFrontend(unittest.TestCase):
    def run_render_test(self, name):
        result = subprocess.run(["node", str(ROOT / "tests/web_regressions.js"),
                                 str(PAGE), name],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_state_mapping_and_pose_specific_blinks(self):
        self.run_render_test("suber-poses")

    def test_typing_layers_keep_body_keyboard_and_clip_ids_stable(self):
        self.run_render_test("suber-layers")

    def test_private_avatar_priority_failure_and_static_motion_fallback(self):
        self.run_render_test("suber-private")

    def test_builtin_layer_failures_retry_only_on_reconciliation(self):
        self.run_render_test("suber-recovery")

    def test_state_transitions_pause_and_reconciliation_preserve_pose(self):
        self.run_render_test("suber-transitions")

    def test_magpie_navigation_motion_and_theme_contract(self):
        self.run_render_test("suber-style")
