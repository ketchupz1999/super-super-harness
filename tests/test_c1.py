"""C1 page responsibilities and cross-view interaction regressions."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend regression tests")
class PageSkeleton(unittest.TestCase):
    def run_render_test(self, name):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(["node", str(root / "tests/web_regressions.js"),
                                 str(root / "super_super_harness/web/index.html"), name],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_single_chat_character_and_separate_live_view(self):
        self.run_render_test("c1-skeleton")

    def test_worker_confirmations_escape_context_and_keep_task_identity(self):
        self.run_render_test("c1-confirmations")

    def test_router_questions_remain_conversation_messages_not_worker_replies(self):
        self.run_render_test("c1-router-question")

    def test_progress_links_only_use_actual_running_tasks(self):
        self.run_render_test("c1-progress")

    def test_tasks_filter_every_real_status_and_search_name_id_progress(self):
        self.run_render_test("c1-tasks")

    def test_hidden_chat_updates_preserve_scroll_and_selected_task(self):
        self.run_render_test("c1-navigation")

    def test_confirmation_and_worker_links_focus_the_correct_destination(self):
        self.run_render_test("c1-links")

    def test_worker_reply_uses_task_endpoint_and_keeps_failed_draft(self):
        self.run_render_test("c1-reply")

    def test_live_topology_retains_completed_context_and_highlight(self):
        self.run_render_test("c1-live")

    def test_confirmation_refresh_preserves_focus_selection_and_draft(self):
        self.run_render_test("c1-draft")

    def test_task_detail_keeps_all_existing_actions(self):
        self.run_render_test("c1-detail")

    def test_long_activity_region_does_not_hide_new_conversation_messages(self):
        self.run_render_test("c1-message-scroll")

    def test_router_pending_link_loads_latest_question_not_history(self):
        self.run_render_test("c1-latest-question")

    def test_live_confirmation_links_keep_focus_across_reconciliation(self):
        self.run_render_test("c1-live-focus")
