"""Avatar precedence, durable waiting/rest and private hot-swapped assets."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from super_super_harness.ops import config
from super_super_harness.channels.avatar import Avatar, FLASH_SECONDS, LIMIT
from super_super_harness.daemon import Service
from super_super_harness.core.store import Store
from tests import test_web
from tests.support import ServiceCase, free_port, write_config


def tiny_png(red=80):
    def chunk(name, data):
        return struct.pack('!I',len(data)) + name + data + struct.pack('!I',zlib.crc32(name+data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('!2I5B',1,1,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(bytes([0,red,120,180]))) + chunk(b'IEND',b'')


def tiny_gif():
    # Generated one-pixel GIF, never a user's avatar.
    return (b'GIF89a' + struct.pack('<HHBBB',1,1,128,0,0) + bytes([60,100,160,255,255,255]) +
            b',' + struct.pack('<HHHHB',0,0,1,1,0) + bytes([2,2,68,1,0,59]))


class AvatarTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        self.store = Store(self.home / 'router.db')
        self.now = 2000000000.0
        self.router = SimpleNamespace(mode='running')
        self.cfg = SimpleNamespace(home=self.home,web={'router_name':'Test Router'})
        self.service = SimpleNamespace(store=self.store,router=self.router,cfg=self.cfg)
        self.avatar = Avatar(self.service,clock=lambda:self.now)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def state(self):
        return self.avatar.state()['state']

    def event(self, kind):
        with patch('super_super_harness.core.store.time.time',return_value=self.now):
            return self.store.event('system',kind)

    def test_seven_state_precedence_and_timed_return_to_running_worker(self):
        self.assertEqual(self.state(),'idle')
        turn = self.store.open_turn()
        self.assertEqual(self.state(),'thinking')
        task = self.store.add_task('ongoing','brief','work',None,'s','','')
        self.store.update_task(task,status='running')
        self.assertEqual(self.state(),'working')
        self.store.set('router_waiting','A question')
        self.assertEqual(self.state(),'waiting')
        self.event('avatar.done')
        token = self.avatar.state()['token']
        self.assertEqual(self.state(),'done')
        self.assertEqual(self.avatar.state()['until'],self.now+FLASH_SECONDS)
        self.event('avatar.error')
        self.assertEqual(self.state(),'error')
        self.now += FLASH_SECONDS
        self.assertEqual(self.state(),'waiting')
        self.store.unset('router_waiting')
        self.event('avatar.done')
        self.assertNotEqual(self.avatar.state()['token'],token)
        self.now += FLASH_SECONDS
        self.assertEqual(self.state(),'working')
        self.router.mode = 'paused'
        self.now += 10000
        self.assertEqual(self.state(),'error')
        self.router.mode = 'running'
        self.store.update_task(task,status='completed')
        self.assertEqual(self.state(),'thinking')
        self.store.run("UPDATE turns SET status='done' WHERE id=?",turn)
        self.assertEqual(self.state(),'rest')

    def test_rest_and_waiting_survive_store_reopen_and_new_activity(self):
        self.event('avatar.done')
        self.now += 4
        self.assertEqual(self.state(),'rest')
        self.store.close()
        self.store = Store(self.home / 'router.db')
        self.service.store = self.store
        self.avatar = Avatar(self.service,clock=lambda:self.now)
        self.assertEqual(self.state(),'rest')
        self.store.set('router_waiting','Question')
        self.assertEqual(self.state(),'waiting')
        self.store.close()
        self.store = Store(self.home / 'router.db')
        self.service.store = self.store
        self.avatar = Avatar(self.service,clock=lambda:self.now)
        self.assertEqual(self.state(),'waiting')
        self.store.unset('router_waiting')
        with patch('super_super_harness.core.store.time.time',return_value=self.now):
            self.store.add_message('new','user','new activity')
        self.assertEqual(self.state(),'idle')
        self.now += 1
        self.event('avatar.done')
        self.now += 4
        self.assertEqual(self.state(),'rest')
        with patch('super_super_harness.core.store.time.time',return_value=self.now):
            self.store.event('router','task.started')
        self.assertEqual(self.state(),'idle')

    def test_material_fallback_priority_and_hot_replacement_without_restart(self):
        self.assertIsNone(self.avatar.state()['asset'])
        root = self.home / 'avatar'
        root.mkdir()
        (root/'idle.png').write_bytes(tiny_png())
        self.avatar.scanned = float('-inf')
        idle = self.avatar.state()['asset']
        self.assertIn('/idle.png?',idle['url'])
        self.store.set('router_waiting','question')
        self.assertEqual(self.avatar.state()['asset'],idle)
        self.store.unset('router_waiting')
        self.event('avatar.done')
        self.assertIsNone(self.avatar.state()['asset'])  # Done never falls back to idle.
        self.now += 4
        self.assertEqual(self.avatar.state()['asset'],idle)  # Rest does.
        (root/'rest.gif').write_bytes(tiny_gif())
        self.avatar.scanned = float('-inf')
        self.assertTrue(self.avatar.state()['asset']['animated'])
        (root/'rest.png').write_bytes(tiny_png())
        self.avatar.scanned = float('-inf')
        before = self.avatar.state()['asset']['url']
        self.assertIn('/rest.png?',before)  # png wins over gif.
        (root/'rest.png').write_bytes(tiny_png(120))
        self.avatar.scanned = float('-inf')
        self.assertNotEqual(before,self.avatar.state()['asset']['url'])
        (root/'rest.png').unlink()
        (root/'rest.gif').unlink()
        self.avatar.scanned = float('-inf')
        self.assertEqual(self.avatar.state()['asset'],idle)

    def test_fixed_names_size_format_symlink_and_special_file_limits(self):
        root = self.home/'avatar'
        root.mkdir()
        for state,extension in [('../idle','png'),('idle','svg'),('unknown','gif')]:
            with self.assertRaises(ValueError):
                self.avatar.image(state,extension)
        (root/'idle.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'x'*LIMIT)
        with self.assertRaises(ValueError):
            self.avatar.image('idle','png')
        (root/'idle.png').write_text('<svg onload="bad()"/>')
        with self.assertRaises(ValueError):
            self.avatar.image('idle','png')
        (root/'idle.png').unlink()
        outside = self.home/'private.png'
        outside.write_bytes(tiny_png())
        (root/'idle.png').symlink_to(outside)
        with self.assertRaises(OSError):
            self.avatar.image('idle','png')
        (root/'idle.png').unlink()
        os.mkfifo(root/'idle.png')
        with self.assertRaises(ValueError):
            self.avatar.image('idle','png')
        (root/'idle.png').unlink()
        root.rmdir()
        target = self.home/'other'
        target.mkdir()
        (target/'idle.png').write_bytes(tiny_png())
        root.symlink_to(target,target_is_directory=True)
        with self.assertRaises(OSError):
            self.avatar.image('idle','png')

    def test_supported_file_extensions_and_gif_no_breathing_metadata(self):
        root = self.home/'avatar'
        root.mkdir()
        materials = {'png':tiny_png(),'gif':tiny_gif(),'jpg':bytes([255,216,255,224]),
                     'webp':b'RIFF'+struct.pack('<I',4)+b'WEBP'}
        for extension,body in materials.items():
            path = root/('thinking.'+extension)
            path.write_bytes(body)
            content,mime = self.avatar.image('thinking',extension)
            self.assertEqual(content,body)
            self.assertTrue(mime.startswith('image/'))
            path.unlink()

    def test_done_gif_is_single_play_without_modifying_original_file(self):
        root = self.home/'avatar'
        root.mkdir()
        gif = tiny_gif()
        looping = gif[:19] + b'\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00' + gif[19:]
        path = root/'done.gif'
        path.write_bytes(looping)
        content,mime = self.avatar.image('done','gif')
        self.assertEqual(mime,'image/gif')
        self.assertEqual(content,gif)
        self.assertEqual(path.read_bytes(),looping)
        path = root/'working.gif'
        path.write_bytes(looping)
        self.assertEqual(self.avatar.image('working','gif')[0],looping)

    def test_router_name_default_validation_and_no_schema_loss(self):
        write_config(self.home,1,2)
        cfg = config.load(self.home)
        self.assertEqual(cfg.web['router_name'],'Router')
        path = self.home/'profiles.toml'
        original = path.read_text()
        path.write_text(original.replace('port = 0','port = 0\nrouter_name = "Local Assistant"'))
        self.assertEqual(config.load(self.home).web['router_name'],'Local Assistant')
        for value in ('""','1','"'+ 'a'*81+'"'):
            path.write_text(original.replace('port = 0','port = 0\nrouter_name = '+value))
            with self.assertRaises(ValueError):
                config.load(self.home)


class AvatarConfigurationTests(unittest.TestCase):
    def test_documented_router_name_edit_preserves_init_generated_web_table(self):
        import re
        import subprocess
        import sys
        import tomllib
        documentation = (config.REPO_ROOT / 'docs/setup.md').read_text()
        example = re.findall(r'```toml\n(.*?)```', documentation, re.S)[0]
        self.assertTrue(example.startswith('["web"]\n'))
        self.assertIn('existing `["web"]` table', documentation)
        self.assertIn('Do not append', documentation)
        name = tomllib.loads(example)['web']['router_name']
        assignment = next(line for line in example.splitlines() if line.startswith('"router_name"'))
        with tempfile.TemporaryDirectory(prefix='ssharness-avatar-config-') as folder:
            home = Path(folder)
            initialized = subprocess.run([sys.executable, '-m', 'super_super_harness.cli', 'init',
                                          '--provider', 'deepseek', '--key-env', 'SSHARNESS_AVATAR_TEST_KEY'],
                                         cwd=config.REPO_ROOT, env=dict(os.environ, SSHARNESS_HOME=str(home)),
                                         stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                         timeout=5, start_new_session=True)
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            self.assertEqual(config.load(home).web['router_name'], 'Router')
            self.assertNotEqual(name, 'Router')
            path = home / 'profiles.toml'
            lines = path.read_text().splitlines()
            self.assertEqual(lines.count('["web"]'), 1)
            start = lines.index('["web"]') + 1
            end = next((i for i in range(start, len(lines)) if lines[i].startswith('[')), len(lines))
            setting = next((i for i in range(start, end)
                            if lines[i].split('=', 1)[0].strip().strip('"') == 'router_name'), None)
            if setting is None:
                lines.insert(start, assignment)
            else:
                lines[setting] = assignment
            path.write_text('\n'.join(lines) + '\n')
            self.assertEqual(path.read_text().splitlines().count('["web"]'), 1)
            self.assertEqual(tomllib.loads(path.read_text())['web']['router_name'], name)
            self.assertEqual(config.load(home).web['router_name'], name)
            self.assertFalse((home / 'keys').exists())
            self.assertFalse(config.load(home).db.exists())


class AvatarWebTests(ServiceCase):
    request = test_web.WebCase.request
    login = test_web.WebCase.login
    stream = test_web.WebCase.stream
    close_stream = test_web.WebCase.close_stream
    event = test_web.WebCase.event

    async def boot(self):
        self.cfg.web['port'] = free_port()
        await super().boot()
        self.port = self.cfg.web['port']
        self.cookie = ''
        await self.login()

    async def test_avatar_http_auth_fixed_names_hot_swap_and_csp(self):
        self.assertIsNone((await self.request())[2]['router']['avatar']['asset'])
        root = self.home/'avatar'
        root.mkdir()
        body = tiny_png()
        (root/'idle.png').write_bytes(body)
        self.service.web.router_avatar.scanned = float('-inf')
        state = (await self.request())[2]['router']['avatar']
        self.assertEqual(state['state'],'idle')
        path = state['asset']['url']
        self.assertEqual((await self.request(path=path,headers={'Cookie':''}))[0],401)
        status,headers,actual = await self.request(path=path)
        self.assertEqual(status,200)
        self.assertEqual(actual,body)
        self.assertEqual(headers['Content-Type'],'image/png')
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("img-src 'self' data:",headers['Content-Security-Policy'])
        for path in ['/api/router-avatar/../key','/api/router-avatar/idle.svg','/api/router-avatar/idle.png/extra',
                     '/api/router-avatar/%2e%2e/key','/api/router-avatar/not-a-state.png']:
            self.assertIn((await self.request(path=path))[0],(400,404))
        (root/'idle.png').write_bytes(tiny_png(130))
        reader,_ = await self.stream()
        await self.event(reader,'activity')
        original = state['asset']['url']
        await asyncio.sleep(1.1)
        updated = (await self.request())[2]['router']['avatar']['asset']['url']
        self.assertNotEqual(original,updated)
        self.assertEqual((await self.request(path=updated))[2],tiny_png(130))

    async def test_router_ask_user_durable_waiting_new_message_and_worker_completion(self):
        await self.tool('ask_user',question='Which neutral example should I use?')
        await self.outbox_idle()
        self.assertEqual((await self.request())[2]['router']['avatar']['state'],'waiting')
        self.assertEqual(self.store.one("SELECT kind FROM conversation WHERE role='router'")["kind"],'question')
        await self.halt()
        await self.boot()
        self.assertEqual((await self.request())[2]['router']['avatar']['state'],'waiting')
        self.say('Use the project notes example')
        await self.until(lambda:not self.store.get('router_waiting') and not self.store.current_turn())
        task = self.start_task('SLEEP 1')
        await self.wait_status(task,'completed')
        self.assertEqual((await self.request())[2]['router']['avatar']['state'],'done')
        await asyncio.sleep(FLASH_SECONDS+.2)
        await self.until(lambda:not self.store.current_turn())
        self.assertEqual((await self.request())[2]['router']['avatar']['state'],'rest')
        await self.halt()
        await self.boot()
        self.assertEqual((await self.request())[2]['router']['avatar']['state'],'rest')

    async def test_reset_clears_waiting(self):
        await self.tool('ask_user', question='Old question?')
        await self.service.dispatch(('owner', None), 'router_reset', {}, None)
        self.assertFalse(self.store.get('router_waiting'))
        self.assertNotEqual((await self.request())[2]['router']['avatar']['state'], 'waiting')

    async def test_attach_answer_handback_clears_waiting(self):
        await self.tool('ask_user', question='Answer interactively?')
        await self.service.router.detach()
        # The interactive Harness writes its answer into the session, not submit().
        await self.service.router.reattach()
        self.assertFalse(self.store.get('router_waiting'))
        self.assertNotEqual((await self.request())[2]['router']['avatar']['state'], 'waiting')

    async def begin_question_turn(self):
        self.say('SLEEP 0.7 ECHO first turn')
        await self.until(lambda: self.statuses() == ['confirmed'])

    async def test_ask_with_queued_user_does_not_enter_waiting(self):
        await self.begin_question_turn()
        router = self.service.router
        # Hold the transport, not the Router mode: this is an active turn.
        router.ready = False
        try:
            self.say('ECHO already answered')
            await self.until(lambda: self.statuses()[-1] == 'queued')
            await self.tool('ask_user', question='Which example?')
            self.assertFalse(self.store.get('router_waiting'))
        finally:
            router.ready = True
            await router.pump()
        await self.until(lambda: self.statuses() == ['done', 'done'])
        self.assertFalse(self.store.get('router_waiting'))

    async def test_ask_with_written_user_does_not_enter_waiting(self):
        await self.begin_question_turn()
        self.say('ECHO already answered')
        await self.until(lambda: self.statuses() == ['confirmed', 'written'])
        await self.tool('ask_user', question='Which example?')
        self.assertFalse(self.store.get('router_waiting'))
        await self.until(lambda: self.statuses() == ['done', 'done'])
        self.assertFalse(self.store.get('router_waiting'))
        self.assertNotEqual((await self.request())[2]['router']['avatar']['state'], 'waiting')

    async def test_confirmed_trigger_allows_normal_question(self):
        await self.begin_question_turn()
        await self.tool('ask_user', question='Normal question?')
        self.assertEqual(self.store.get('router_waiting'), 'Normal question?')
        await self.until(lambda: self.statuses() == ['done'])
        self.assertEqual(self.store.get('router_waiting'), 'Normal question?')

    async def test_answer_written_after_question_clears_waiting_after_all_turns(self):
        await self.begin_question_turn()
        await self.tool('ask_user', question='Normal question?')
        self.say('ECHO answer after question')
        await self.until(lambda: self.statuses() == ['confirmed', 'written'])
        await self.until(lambda: self.statuses() == ['done', 'done'])
        self.assertFalse(self.store.get('router_waiting'))

    async def test_submit_clears_waiting_before_late_replay_and_reasking_still_waits(self):
        await self.begin_question_turn()
        await self.tool('ask_user', question='Question awaiting an answer')
        self.assertEqual(self.store.get('router_waiting'), 'Question awaiting an answer')
        message = self.say('SLEEP 0.7 ECHO answer')
        await self.until(lambda: self.statuses() == ['confirmed', 'written'])
        self.assertFalse(self.store.get('router_waiting'))
        await self.until(lambda: self.statuses() == ['done', 'confirmed'])
        self.assertFalse(self.store.get('router_waiting'))
        await self.tool('ask_user', question='A new question after reading?')
        self.assertEqual(self.store.get('router_waiting'), 'A new question after reading?')
        row = self.store.one('SELECT uuid FROM messages WHERE id=?', message)
        await self.service.router._on_event({'type': 'user', 'isReplay': True, 'uuid': row['uuid']})
        self.assertEqual(self.store.get('router_waiting'), 'A new question after reading?')
        await self.until(lambda: self.statuses() == ['done', 'done'])
        self.assertEqual(self.store.get('router_waiting'), 'A new question after reading?')

    async def test_reminder_run_does_not_answer_or_suppress_question(self):
        await self.begin_question_turn()
        self.service.router.submit('user', 'ECHO scheduled', 'terminal', 'reminder.run')
        await self.until(lambda: self.statuses() == ['confirmed', 'written'])
        await self.tool('ask_user', question='Still need an answer?')
        self.assertEqual(self.store.get('router_waiting'), 'Still need an answer?')
        await self.until(lambda: self.statuses() == ['done', 'done'])
        self.assertEqual(self.store.get('router_waiting'), 'Still need an answer?')

    async def test_avatar_events_do_not_pollute_task_timeline(self):
        self.service.router.switching = True
        task_id = self.store.add_task('Example', 'brief', 'work', None, 'session', '', '')
        for kind, event, expected in [('worker.done', 'avatar.done', 'done'), ('worker.failed', 'avatar.error', 'error')]:
            await self.service.on_task_change(kind, task_id, 'result')
            row = self.store.one('SELECT * FROM events WHERE type=? ORDER BY id DESC LIMIT 1', event)
            self.assertIsNone(row['task_id'])
            self.assertEqual((await self.request())[2]['router']['avatar']['state'], expected)
        self.assertFalse(self.store.all("SELECT * FROM events WHERE task_id=? AND type LIKE 'avatar.%'", task_id))

    async def outbox_idle(self):
        await self.service.outbox.flush()
        await asyncio.sleep(0)
