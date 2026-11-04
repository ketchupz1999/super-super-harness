"""Worker identity, private artwork and Inspect's read-only session contract."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests import test_web
from tests.support import PAGE, ServiceCase, free_port
from tests.test_avatar import tiny_png


class PrivateCharacters(ServiceCase):
    request = test_web.WebCase.request
    login = test_web.WebCase.login

    async def boot(self):
        self.cfg.web['port'] = free_port()
        await super().boot()
        self.port = self.cfg.web['port']
        self.cookie = ''
        await self.login()

    def artwork(self, atlas='claude-v1.png'):
        root = self.home / 'characters'
        root.mkdir(exist_ok=True)
        manifest = {'atlas': atlas, 'frameWidth': 128, 'frameHeight': 128,
                    'columns': 1, 'rows': 1, 'pixel': True,
                    'animations': {key: {'row': 0, 'frames': 1, 'frameDurationsMs': [200]}
                                   for key in ('idle', 'working', 'waiting', 'complete')}}
        (root / 'claude.json').write_text(json.dumps(manifest))
        self.service.web.characters_scanned = float('-inf')
        return root

    async def test_missing_artwork_is_optional_and_reads_require_login(self):
        state = (await self.request())[2]
        self.assertEqual(state['characters'], [])
        self.assertIsNone(state['harness_icon'])
        for path in ['/api/state', '/api/characters/claude.atlas', '/api/characters/harness-claude-code.png']:
            self.assertEqual((await self.request(path=path, headers={'Cookie': ''}))[0], 401)
        self.assertEqual((await self.request(path='/api/characters/claude.atlas'))[0], 404)
        self.assertEqual((await self.request(path='/api/characters/harness-claude-code.png'))[0], 404)

    async def test_private_atlas_manifest_icon_hot_swap_and_no_paths_or_keys(self):
        root = self.artwork('/d/characters/claude-v1.png')
        image = tiny_png()
        (root / 'claude-v1.png').write_bytes(image)
        (root / 'harness-claude-code.png').write_bytes(image)
        # service_pid is written after the Router and service startup writes finish.
        await self.until(lambda: self.store.get('service_pid') == str(os.getpid()), label='service startup completed')
        before = self.store.db.total_changes
        state = (await self.request())[2]
        art = state['characters'][0]
        self.assertEqual(art['id'], 'claude')
        self.assertEqual(art['animations']['working'], {'row': 0, 'frames': 1, 'durations': [200]})
        for url in [art['atlas'], state['harness_icon']]:
            status, headers, body = await self.request(path=url)
            self.assertEqual(status, 200)
            self.assertEqual(body, image)
            self.assertEqual(headers['Content-Type'], 'image/png')
            self.assertEqual(headers['Cache-Control'], 'no-store')
        public = json.dumps({'characters': state['characters'], 'harness_icon': state['harness_icon']})
        for secret in [str(self.home), '/d/characters', 'dummy-for-tests', 'key_file']:
            self.assertNotIn(secret, public)
        self.assertEqual(self.store.db.total_changes, before)
        (root / 'claude-v1.png').write_bytes(tiny_png(130))
        self.service.web.characters_scanned = float('-inf')
        self.assertNotEqual((await self.request())[2]['characters'][0]['atlas'], art['atlas'])
        (root / 'claude-v1.png').unlink()
        self.service.web.characters_scanned = float('-inf')
        self.assertEqual((await self.request())[2]['characters'], [])

    async def test_paths_symlinks_special_files_and_invalid_manifests_are_rejected(self):
        root = self.artwork()
        outside = self.home / 'outside.png'
        outside.write_bytes(tiny_png())
        image = root / 'claude-v1.png'
        image.symlink_to(outside)
        self.assertEqual((await self.request())[2]['characters'], [])
        for path in ['/api/characters/../outside.png', '/api/characters/%2e%2e/outside.png',
                     '/api/characters/claude.atlas/extra', '/api/characters/claude.json',
                     '/api/characters/CLAUDE.atlas', '/api/characters/unknown.atlas']:
            self.assertIn((await self.request(path=path))[0], (400, 404))
        image.unlink()
        os.mkfifo(image)
        self.service.web.characters_scanned = float('-inf')
        self.assertEqual((await self.request())[2]['characters'], [])
        image.unlink()
        image.write_bytes(b'not an image')
        self.service.web.characters_scanned = float('-inf')
        self.assertEqual((await self.request(path='/api/characters/claude.atlas'))[0], 404)
        icon = root / 'harness-claude-code.png'
        icon.symlink_to(outside)
        self.assertIsNone((await self.request())[2]['harness_icon'])
        self.assertEqual((await self.request(path='/api/characters/harness-claude-code.png'))[0], 404)
        manifest = root / 'claude.json'
        for value in [[], {'atlas': '../outside.png'}, {'atlas': 'file.svg'}, {'atlas': 'x.png', 'frameWidth': True}]:
            manifest.write_text(json.dumps(value))
            self.service.web.characters_scanned = float('-inf')
            self.assertEqual((await self.request())[2]['characters'], [])
        manifest.unlink()
        manifest.symlink_to(self.home / 'key')
        self.service.web.characters_scanned = float('-inf')
        self.assertEqual((await self.request())[2]['characters'], [])
        shutil.rmtree(root)
        root.symlink_to(self.home, target_is_directory=True)
        self.service.web.characters_scanned = float('-inf')
        self.assertEqual((await self.request())[2]['characters'], [])

    async def test_harness_and_session_identity_are_read_only_and_authenticated(self):
        task = self.start_task('ECHO done')
        await self.wait_status(task, 'completed')
        # The status flips before the run row is closed and the last events land; wait until the store is quiet.
        for _ in range(100):
            changes = self.store.db.total_changes
            await asyncio.sleep(0.05)
            ended = self.store.one('SELECT ended FROM worker_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', task)['ended']
            if ended and self.store.db.total_changes == changes:
                break
        self.store.set('task_harness:' + task, 'codex')
        before = self.store.db.total_changes
        state = (await self.request())[2]
        summary = next(t for t in state['tasks'] if t['id'] == task)
        self.assertEqual(summary['harness'], 'codex', 'Persisted session Harness wins over profile')
        run = self.store.one('SELECT started, ended FROM worker_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', task)
        self.assertEqual(summary['activity']['run_started'], run['started'])
        self.assertEqual(summary['activity']['run_ended'], run['ended'])
        path = '/api/workers/' + task + '?metadata=1'
        self.assertEqual((await self.request(path=path, headers={'Cookie': ''}))[0], 401)
        detail = (await self.request(path=path))[2]['task']
        self.assertEqual(detail['session_id'], self.store.task(task)['session_id'])
        self.assertEqual(detail['harness'], 'codex')
        self.assertNotIn('dummy-for-tests', json.dumps(detail))
        self.assertEqual(self.store.db.total_changes, before)

    async def test_new_run_does_not_reuse_old_reported_progress(self):
        task = self.start_task('ECHO first')
        await self.wait_status(task, 'completed')
        self.service.tasks.report_progress(task, 'Finished the previous assignment')
        self.service.tasks.send(task, 'TOOLSLEEP 900')
        await self.wait_status(task, 'running')
        await self.until(lambda: self.store.task(task)['progress'].startswith('Bash'))
        activity = next(t for t in (await self.request())[2]['tasks'] if t['id'] == task)['activity']
        self.assertEqual(activity['reported'], '')
        self.assertEqual(activity['tool']['name'], 'Bash')

    async def test_inspect_tail_and_older_pages_have_no_overlap_and_live_tail_refreshes(self):
        task = self.store.add_task('History', 'work', 'work', None, 'no-session', '', '')
        for i in range(251):
            self.store.event('worker', 'report.progress', task, text='Entry %d' % i)
        path = '/api/workers/' + task
        page = (await self.request(path=path + '?tail=1&limit=100'))[2]
        self.assertEqual(page['offset'], 151)
        self.assertEqual(page['previous_offset'], 51)
        self.assertIsNone(page['next_offset'])
        older = (await self.request(path=path + '?offset=51&limit=100&snapshot=' + page['snapshot']))[2]
        first = (await self.request(path=path + '?offset=0&limit=51&snapshot=' + page['snapshot']))[2]
        rows = first['timeline'] + older['timeline'] + page['timeline']
        self.assertEqual([json.loads(r['text'])['text'] for r in rows], ['Entry %d' % i for i in range(251)])
        self.store.event('worker', 'report.progress', task, text='Live update')
        latest = (await self.request(path=path + '?tail=1&limit=100&refresh=' + page['snapshot']))[2]
        self.assertEqual(json.loads(latest['timeline'][-1]['text'])['text'], 'Live update')
        self.assertEqual(latest['offset'], 152)


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for frontend regression tests')
class V2Frontend(unittest.TestCase):
    def check(self, name):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(['node', str(root / 'tests/web_regressions.js'),
                                 str(PAGE), name],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stable_names_and_model_family_not_task_order(self):
        self.check('v2-identity')

    def test_sprite_frames_complete_once_and_reduce_motion(self):
        self.check('v2-sprites')

    def test_inspect_chronology_plain_tools_session_and_safe_resume(self):
        self.check('v2-inspect')

    def test_completion_keeps_return_wire_until_flight_finishes(self):
        self.check('v2-completion')

    def test_four_navigation_entries_and_models_integration_contract(self):
        self.check('v2-navigation')
