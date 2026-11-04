"""C2 read contracts, grouped dependency graphs, drawer navigation and question choices."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid

from tests.support import PAGE
from tests.test_items import WebItemCase


class ReadContracts(WebItemCase):
    async def test_router_question_requires_login_and_clears_on_answer(self):
        self.service.router.ask_user('继续 <检查>？', ['继续', '先暂停'])
        self.assertEqual((await self.request(headers={'Cookie': ''}))[0], 401)
        self.assertEqual((await self.request(path='/api/conversation', headers={'Cookie': ''}))[0], 401)
        question = (await self.request())[2]['router']['question']
        self.assertEqual(question['text'], '继续 <检查>？')
        self.assertEqual(question['options'], ['继续', '先暂停'])
        rows = (await self.request(path='/api/conversation'))[2]['items']
        self.assertEqual([r['id'] for r in rows if r['current_question']], [question['id']])
        response = await self.request('POST', '/api/messages', {'text': '继续', 'request_id': str(uuid.uuid4())})
        self.assertEqual(response[0], 200)
        self.assertIsNone((await self.request())[2]['router']['question'])
        self.assertFalse(any(r['current_question'] for r in (await self.request(path='/api/conversation'))[2]['items']))

    async def test_pending_question_identity_is_global_and_not_avatar_state(self):
        self.service.router.ask_user('Same question?', ['First'])
        first = (await self.request())[2]['router']['question']['id']
        self.service.router.ask_user('Same question?', ['Second'])
        current = (await self.request())[2]['router']['question']
        self.assertNotEqual(first, current['id'])
        self.assertEqual(current['options'], ['Second'])
        for i in range(60):
            self.service.router.reply('Later notice %d' % i, 'notice')
        self.store.event('system', 'avatar.done')
        state = (await self.request())[2]['router']
        self.assertEqual(state['avatar']['state'], 'done')
        self.assertEqual(state['question']['id'], current['id'])
        recent = (await self.request(path='/api/conversation?limit=1'))[2]['items']
        self.assertFalse(any(row['current_question'] for row in recent))
        old = (await self.request(path='/api/conversation?before=%d' % current['id']))[2]['items']
        self.assertFalse(any(row['current_question'] for row in old))
        self.service.router.submit('user', 'scheduled', 'web', 'reminder.run')
        self.assertEqual(self.service.web.router_question()['id'], current['id'])
        self.service.router.submit('user', 'answer', 'web')
        self.assertIsNone(self.service.web.router_question())
        self.service.router.ask_user('An ask while the answer is unread', ['A'])
        self.assertIsNone(self.service.web.router_question())

    async def test_worker_metadata_exposes_question_identity_and_complete_relations(self):
        code = await self.item(code='ui', name='UI <test>')
        upstream = await self.start('ECHO verified', name='Upstream <one>', item=code)
        await self.idle(upstream)
        worker = await self.start('SLEEP 2', name='Review', item=code, wait_for=[upstream])
        await self.until(lambda: self.store.task(worker)['started_once'])
        await self.wtool(worker, 'ask_user', question='Continue?', options=['Yes <safe>', 'No'])
        downstream = await self.start('ECHO summary', name='Downstream', item=code, wait_for=[worker])
        path = '/api/workers/%s?metadata=1' % worker
        self.assertEqual((await self.request(path=path, headers={'Cookie': ''}))[0], 401)
        task = (await self.request(path=path))[2]['task']
        self.assertIsInstance(task['question_id'], int)
        self.assertEqual(task['options'], ['Yes <safe>', 'No'])
        self.assertEqual(task['upstream'], [{'id': upstream, 'name': 'Upstream <one>', 'worker_name': 'Nova', 'code_name': 'Nova', 'role': '', 'status': 'completed', 'satisfied': True}])
        self.assertEqual(task['downstream'], [{'id': downstream, 'name': 'Downstream', 'worker_name': 'Echo', 'code_name': 'Echo', 'role': '', 'status': 'waiting_deps', 'satisfied': False}])
        first_question = task['question_id']
        await self.wtool(worker, 'ask_user', question='Continue again?', options=['Another'])
        task = (await self.request(path=path))[2]['task']
        self.assertNotEqual(task['question_id'], first_question)
        self.assertEqual(task['options'], ['Another'])
        self.service.tasks.send(upstream, 'TOOLSLEEP 900')
        await self.wait_status(upstream, 'running')
        self.assertTrue((await self.request(path=path))[2]['task']['upstream'][0]['satisfied'], 'Dependency satisfaction is sticky, not inferred from current upstream status')

    async def test_new_read_fields_do_not_mutate_database(self):
        self.service.router.ask_user('Read only?', ['Yes'])
        before = self.store.db.total_changes
        question = self.service.web.router_question()
        self.assertIsNotNone(question)
        self.service.web.router_state()
        self.service.web.conversation({})
        self.assertEqual(self.service.web.task_relations('t999'), {'upstream': [], 'downstream': []})
        self.assertEqual(self.store.db.total_changes, before)


class Activity(WebItemCase):
    async def test_activity_reports_run_start_own_words_tool_and_exchange(self):
        worker = await self.start('TOOLSLEEP 900', name='Busy')
        await self.until(lambda: self.store.task(worker)['progress'].startswith('Bash'), label='tool progress')
        await self.wtool(worker, 'report_progress', text='Checking <layout>')
        self.store.event('worker', 'tool', worker, tool='mcp__router__report_progress', input='{}')
        self.assertEqual((await self.request(headers={'Cookie': ''}))[0], 401)
        before = self.store.db.total_changes
        task = next(t for t in (await self.request())[2]['tasks'] if t['id'] == worker)
        activity = task['activity']
        self.assertEqual(activity['reported'], 'Checking <layout>')
        self.assertEqual(activity['tool']['name'], 'Bash', 'Router tools are messages, not work')
        self.assertEqual(activity['exchange']['type'], 'report.progress')
        self.assertEqual(activity['exchange']['text'], 'Checking <layout>')
        self.assertEqual(activity['run_started'], self.store.one('SELECT MAX(started) s FROM worker_runs WHERE task_id=?', worker)['s'])
        self.assertEqual(self.store.db.total_changes, before, 'Reading activity writes nothing')
        idle = self.store.add_task('Never started', 'b', 'work', None, 's-idle', '', '')
        task = next(t for t in (await self.request())[2]['tasks'] if t['id'] == idle)
        self.assertEqual(task['activity'], {'run_started': None, 'run_ended': None, 'reported': '', 'tool': None, 'exchange': None})


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for frontend regression tests')
class RunView(unittest.TestCase):
    def run_render_test(self, name):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(['node', str(root/'tests/web_regressions.js'),
                                 str(PAGE), name],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_item_groups_and_unassigned_tasks(self):
        self.run_render_test('c2-groups')

    def test_dependency_layout_and_sticky_satisfaction(self):
        self.run_render_test('c2-layout')

    def test_cross_item_unloaded_references_and_cycles_are_explicit(self):
        self.run_render_test('c2-references')

    def test_empty_and_truncated_graphs_do_not_invent_workers(self):
        self.run_render_test('c2-empty')

    def test_external_graph_and_drawer_text_is_escaped(self):
        self.run_render_test('c2-escaping')

    def test_drawer_reuses_actions_and_shows_worker_context(self):
        self.run_render_test('c2-drawer')

    def test_options_send_exact_text_and_keep_free_input(self):
        self.run_render_test('c2-options')

    def test_stale_options_and_double_clicks_are_rejected(self):
        self.run_render_test('c2-stale-options')

    def test_answered_router_questions_are_plain_messages(self):
        self.run_render_test('c2-answered')

    def test_progress_updates_retain_keyboard_focus(self):
        self.run_render_test('c2-progress-focus')

    def test_links_open_drawer_and_return_to_correct_card(self):
        self.run_render_test('c2-links')

    def test_graph_uses_fresh_snapshot_and_selected_unloaded_dependencies(self):
        self.run_render_test('c2-snapshot')

    def test_option_submission_survives_inflight_sync_and_restores_other_choices(self):
        self.run_render_test('c2-option-reconcile')

    def test_option_preflight_rejects_a_replaced_remote_question(self):
        self.run_render_test('c2-option-preflight')

    def test_loaded_placeholder_rebuilds_communication_and_timer(self):
        self.run_render_test('c2-placeholder')

    def test_drawer_controls_have_stable_focus_identity(self):
        self.run_render_test('c2-drawer-focus')

    def test_c3_dispatch_requires_unambiguous_durable_evidence(self):
        self.run_render_test('c3-dispatch')

    def test_c3_reports_and_dependency_release_map_once(self):
        self.run_render_test('c3-reports')

    def test_c3_history_and_reconnect_do_not_replay(self):
        self.run_render_test('c3-history')

    def test_c3_motion_budget_and_reduced_motion_feedback(self):
        self.run_render_test('c3-motion')

    def test_c3_packet_symbols_are_fixed_and_noninteractive(self):
        self.run_render_test('c3-symbols')

    def test_c3_suber_reaction_returns_to_actual_pose(self):
        self.run_render_test('c3-suber')

    def test_c4_progress_is_plain_language_never_raw_json(self):
        self.run_render_test('c4-progress')

    def test_c4_workers_three_filters_grouped_by_item(self):
        self.run_render_test('c4-groups')

    def test_c4_routing_lists_active_lanes_waits_and_folded_finished(self):
        self.run_render_test('c4-routing')

    def test_c4_flights_reuse_routing_wires_and_release_fallback(self):
        self.run_render_test('c4-flights')

    def test_c4_drawer_summary_report_lead_and_more_menu(self):
        self.run_render_test('c4-drawer')

    def test_c4_interface_copy_is_english_and_wider(self):
        self.run_render_test('c4-english')

    def test_c4_chat_routing_panel_live_collapsible_and_idle(self):
        self.run_render_test('c4-panel')

    def test_c4_worker_robots_states_and_progress_line(self):
        self.run_render_test('c4-robots')
