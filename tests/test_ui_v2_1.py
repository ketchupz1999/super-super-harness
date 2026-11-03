"""v2.1: shared Worker names and conversation reminders."""
from datetime import datetime, timedelta
import json
import shutil
import subprocess
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from super_super_harness.worker_names import WORKER_NAMES, worker_name
from tests.support import ServiceCase, free_port
from tests import test_web


class WorkerNames(unittest.TestCase):
    def test_backend_matches_frozen_v2_algorithm(self):
        legacy = ('Nova Pixel Echo Atlas Juno Orbit Sage Milo Iris Bolt Luna Rex '
                  'Kiko Zed Ivy Taro Mochi Finn Nori Pip Remy Skye Toto Wren').split()
        self.assertEqual(list(WORKER_NAMES), legacy)
        for seq in range(1, 2401):
            self.assertEqual(worker_name(seq), legacy[(seq - 1) % 24])
        page = (Path(__file__).resolve().parents[1] / 'super_super_harness/web/index.html').read_text()
        fallback = re.search(r'const WORKER_NAMES = (\[.*?\]);', page)[1]
        self.assertEqual(json.loads(fallback.replace("'", '"')), legacy)


class ConversationV21(ServiceCase):
    request = test_web.WebCase.request
    login = test_web.WebCase.login

    async def boot(self):
        self.cfg.web['port'] = free_port()
        await super().boot()
        self.port, self.cookie = self.cfg.web['port'], ''
        await self.login()

    async def log_call(self, name, args):
        await self.service.router._on_event({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'mcp__router__' + name, 'input': args}]}})

    async def test_worker_names_in_state_detail_items_and_router_table_are_escaped(self):
        code = self.service.items.create('names', 'Item <tag>', 'delivery', 'act')['code']
        ids = [self.store.add_task('Task <tag>', 'brief', 'work', None, 'no-session', '', '') for _ in range(3)]
        self.service.items.attach(code, ids[0])
        self.store.update_task(ids[2], status='completed', ended=10**12)
        state = (await self.request())[2]
        self.assertEqual({t['id']: t['worker_name'] for t in state['tasks']}, dict(zip(ids, ['Nova', 'Pixel', 'Echo'])))
        for task_id, expected in zip(ids, ['Nova', 'Pixel', 'Echo']):
            detail = (await self.request(path='/api/tasks/' + task_id + '?metadata=1'))[2]['task']
            self.assertEqual(detail['worker_name'], expected)
            self.assertEqual(self.service.tasks.read(task_id)['worker_name'], expected)
        self.assertEqual(self.service.items.read(code)['tasks'][0]['worker_name'], 'Nova')
        self.assertEqual((await self.request(path='/api/items/' + code))[2]['tasks'][0]['worker_name'], 'Nova')
        with patch('super_super_harness.worker_names.WORKER_NAMES', ('<Name>&\nnext',)):
            table = self.service.tasks.render()
        for task_id in ids:
            self.assertIn(task_id + ' &lt;Name&gt;&amp; next', table)
        self.assertIn('Task &lt;tag&gt;', table)
        self.assertIn('Item &lt;tag&gt;', table)
        self.assertNotIn('<Name>', table)
        self.assertNotIn('<tag>', table)
        self.assertEqual(table.count('<tasks>'), 1)
        self.assertEqual(table.count('</tasks>'), 1)

    async def test_reminder_cards_use_successful_effects_once_and_survive_cancellation(self):
        self.store.open_turn()
        self.service.router.reply('Before scheduling')
        args = dict(name='Review <tag>', text='原文 <script> & details\nSecond line', kind='notify',
                    at=(datetime.now().astimezone() + timedelta(hours=2)).isoformat())
        reminder = await self.tool('reminder_add', **args)
        self.assertEqual(await self.tool('reminder_add', **args), reminder, 'Retry must not create another card')
        await self.log_call('reminder_add', args)
        self.store.event('router', 'tool', tool='mcp__router__reply', input='{}')
        self.service.router.reply('Scheduled')
        self.service.router.reply('Later reply')
        rows = (await self.request(path='/api/conversation'))[2]['items']
        self.assertEqual([len(m['reminders']) for m in rows], [0, 1, 0])
        card = rows[1]['reminders'][0]
        self.assertEqual(card, {key: reminder[key] for key in ('id', 'next_time', 'text', 'kind', 'cron')})
        self.assertTrue(all(step['hidden'] for step in rows[1]['steps']))
        self.assertEqual((await self.request(path='/api/conversation', headers={'Cookie': ''}))[0], 401)
        status, _, _ = await self.request('POST', '/api/reminders/cancel', {'reminder_id': reminder['id']})
        self.assertEqual(status, 200)
        self.assertEqual((await self.request())[2]['reminders']['items'], [])
        again = (await self.request(path='/api/conversation'))[2]['items']
        self.assertEqual(again[1]['reminders'], [card], 'History retains the confirmed schedule, not the mutable next occurrence')
        self.assertNotIn('dummy-for-tests', json.dumps(again))

    async def test_failed_reminder_call_never_claims_success(self):
        self.store.open_turn()
        with self.assertRaises(ValueError):
            await self.tool('reminder_add', name='Bad', text='No schedule', kind='notify', at='invalid')
        self.store.event('router', 'tool', tool='mcp__router__reminder_add', input='{}')
        self.service.router.reply('Could not schedule it')
        message = (await self.request(path='/api/conversation'))[2]['items'][0]
        self.assertEqual(message['reminders'], [])
        self.assertFalse(message['steps'][0]['hidden'])
        self.assertNotIn('summary', message['steps'][0])

    async def test_successful_router_actions_have_named_summaries_and_history_workers(self):
        self.store.open_turn()
        with patch.object(self.service.tasks, 'schedule'):
            code = (await self.tool('item_create', code='demo', name='Item <tag>', kind='delivery', permission='act'))['code']
            task = await self.tool('task_start', name='Task <tag>', brief='ECHO done', item=code)
            await self.tool('task_send', task_id=task['task_id'], text='More context')
            await self.tool('item_update', item=code, progress='Checked')
        for name, args in [('item_create', dict(code='demo', name='Item <tag>', kind='delivery', permission='act')),
                           ('task_start', dict(name='Task <tag>', brief='ECHO done', item=code)),
                           ('task_send', dict(task_id=task['task_id'], text='More context')),
                           ('item_update', dict(item=code, progress='Checked'))]:
            await self.log_call(name, args)
        self.service.router.reply('Assigned')
        message = (await self.request(path='/api/conversation'))[2]['items'][0]
        self.assertEqual([s['summary'] for s in message['steps'] if not s['hidden']],
                         ['Created item Item <tag>', 'Started Nova', 'Sent a message to Nova', 'Updated item Item <tag>'])
        self.assertEqual(message['workers'][0]['worker_name'], 'Nova')
        self.assertEqual(message['workers'][0]['name'], 'Task <tag>')
        self.assertEqual(message['workers'][0]['id'], task['task_id'])

    async def test_notes_only_item_update_has_named_summary(self):
        item = self.service.items.create('notes', 'Item notes', 'delivery', 'act')
        self.store.open_turn()
        args = dict(item=item['code'], notes='Updated notes', notes_version=item['notes_version'])
        await self.log_call('item_update', args)
        await self.tool('item_update', **args)
        self.service.router.reply('Notes updated')
        row = (await self.request(path='/api/conversation'))[2]['items'][0]
        self.assertEqual([s['summary'] for s in row['steps'] if not s['hidden']], ['Updated item Item notes'])

    async def test_late_reminder_and_cancel_recreate_keep_both_creation_snapshots(self):
        self.store.open_turn()
        self.service.router.reply('Will schedule that')
        args = dict(name='Review', text='Check this', kind='notify',
                    at=(datetime.now().astimezone() + timedelta(hours=2)).isoformat())
        first = await self.tool('reminder_add', **args)
        rows = (await self.request(path='/api/conversation'))[2]['items']
        self.assertEqual([r['id'] for r in rows[0]['reminders']], [first['id']], 'A call after the last reply is not lost')
        self.service.router.reply('First scheduled')
        await self.tool('reminder_cancel', reminder_id=first['id'])
        second = await self.tool('reminder_add', **args)
        self.service.router.reply('Rescheduled')
        rows = (await self.request(path='/api/conversation'))[2]['items']
        self.assertEqual([r['id'] for m in rows for r in m['reminders']], [first['id'], second['id']])
        self.assertEqual(rows[1]['reminders'][0]['id'], first['id'])
        self.assertEqual(rows[2]['reminders'][0]['id'], second['id'])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM effects WHERE tool='reminder_add'")['n'], 1)

    async def test_mixed_success_and_failure_keeps_unconfirmed_actions(self):
        self.store.open_turn()
        ids = [self.store.add_task('Work', 'Brief', 'work', None, 'no-session', '', '') for _ in range(2)]
        self.store.update_task(ids[1], status='stopped')
        with patch.object(self.service.tasks, 'schedule'):
            for task_id in ids:
                args = dict(task_id=task_id, text='Continue')
                await self.log_call('task_send', args)
                await self.tool('task_send', **args)
        good = dict(name='Review', text='x' * 400, kind='notify',
                    at=(datetime.now().astimezone() + timedelta(hours=2)).isoformat())
        await self.log_call('reminder_add', good)
        await self.tool('reminder_add', **good)
        bad = good | {'at': 'invalid'}
        await self.log_call('reminder_add', bad)
        with self.assertRaises(ValueError):
            await self.tool('reminder_add', **bad)
        self.service.router.reply('One message and one reminder succeeded')
        row = (await self.request(path='/api/conversation'))[2]['items'][0]
        calls = [s for s in row['steps'] if s['name'].startswith('mcp__router__')]
        self.assertEqual([s['hidden'] for s in calls], [True, False, True, False])
        self.assertEqual(len(row['reminders']), 1)
        self.assertEqual(row['reminders'][0]['text'], good['text'], 'Cards use full text, not the 300-character stream preview')

    async def test_legacy_effect_still_renders_without_creation_event(self):
        self.store.open_turn()
        args = dict(name='Legacy', text='Kept', kind='run',
                    at=(datetime.now().astimezone() + timedelta(hours=2)).isoformat())
        reminder = await self.tool('reminder_add', **args)
        self.store.run("DELETE FROM events WHERE type='reminder.added'")
        self.service.router.reply('Scheduled')
        row = (await self.request(path='/api/conversation'))[2]['items'][0]
        self.assertEqual(row['reminders'][0]['id'], reminder['id'])

    async def test_task_start_returns_worker_name(self):
        result = await self.tool('task_start', name='Test work', brief='ECHO done')
        self.assertEqual(result['worker_name'], 'Nova')
        await self.wait_status(result['task_id'], 'completed')

    async def test_worker_name_lookup_includes_all_cycles_before_limit(self):
        ids = [self.store.add_task('Work', 'Brief', 'work', None, 'no-session', '', '') for _ in range(49)]
        rows = self.service.tasks.search('nova', limit=2)
        self.assertEqual([r['id'] for r in rows], [ids[48], ids[24]])
        self.assertEqual([r['worker_name'] for r in rows], ['Nova', 'Nova'])


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for frontend regression tests')
class V21Frontend(unittest.TestCase):
    def check(self, name):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', str(root / 'tests/web_regressions.js'),
                                 str(root / 'super_super_harness/web/index.html'), name],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_upcoming_order_limit_fold_cancel_and_shared_worker_rows(self):
        self.check('v21-upcoming')

    def test_reminder_cards_and_named_inspect_chips_escape_data(self):
        self.check('v21-cards-and-chips')

    def test_router_tool_summaries_hide_reply_and_escape_names(self):
        self.check('v21-tool-summaries')

    def test_backend_names_win_on_all_worker_surfaces(self):
        self.check('v21-name-priority')
