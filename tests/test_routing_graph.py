"""Routing graph and compact Inspect regressions execute the shipped JavaScript."""
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.support import PAGE


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for frontend regression tests')
class RoutingGraph(unittest.TestCase):
    def check(self, scenario):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', str(root / 'tests/web_regressions.js'),
                                 str(PAGE), scenario],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shared_grouped_layout_single_worker_and_dependencies(self):
        self.check('routing-layout')

    def test_dependency_satisfaction_does_not_cancel_inflight_answer(self):
        self.check('routing-release-preserves-flight')

    def test_return_to_chat_remeasures_visible_graph_width(self):
        self.check('routing-resize')

    def test_item_rename_updates_both_group_titles_and_escapes_text(self):
        self.check('routing-renamed-item')

    def test_running_waiting_currents_and_background_pause(self):
        self.check('routing-signals')

    def test_envelope_dot_question_answer_and_dependency_sequence(self):
        self.check('routing-events')

    def test_bounded_queue_arrival_hold_fade_and_reduced_motion(self):
        self.check('routing-lifecycle')

    def test_compact_running_summary_keeps_escaped_confirmation(self):
        self.check('routing-summary')

    def test_inspect_tool_arguments_result_preview_and_unbroken_metadata(self):
        self.check('routing-inspect')

    def test_short_model_names_keep_escaped_full_id_in_title(self):
        self.check('routing-models')
