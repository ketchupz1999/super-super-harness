"""C5 system pages: read-only Models contract, Soul & Memory and the hidden Usage entry."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tests.support import PAGE
from super_super_harness.ops import config
from tests.test_items import WebItemCase


class ModelsContract(WebItemCase):
    async def test_models_requires_login_and_keeps_web_security(self):
        self.assertEqual((await self.request(path='/api/models', headers={'Cookie': ''}))[0], 401)
        self.assertEqual((await self.request(path='/api/models', headers={'Host': 'evil.invalid'}))[0], 403)
        self.assertEqual((await self.request('POST', '/api/models', {}, {'Origin': 'http://evil.invalid'}))[0], 403)
        status, headers, _ = await self.request(path='/api/models')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    async def test_models_lists_router_roles_and_every_profile_without_secrets(self):
        with patch.object(config.Provider, 'key', side_effect=AssertionError('models must not read credentials')), \
             patch('super_super_harness.adapters.claude_code.shallow_probe', side_effect=lambda p, t: p.name == 'one') as probe:
            status, _, body = await self.request(path='/api/models')
            await self.request(path='/api/models')
        self.assertEqual(status, 200, body)
        self.assertEqual(probe.call_count, 2, 'one shallow probe per Provider and Harness, cached across requests')
        router = body['router']
        self.assertEqual(router['primary'], {'profile': 'main', 'model': 'main-model'})
        self.assertEqual(router['backup'], {'profile': 'spare', 'model': 'spare-model'})
        self.assertEqual((router['active'], router['current']['model'], router['mode']), ('primary', 'main-model', 'running'))
        rows = {p['name']: p for p in body['profiles']}
        self.assertEqual(set(rows), {'main', 'spare', 'work', 'other'})
        self.assertEqual(rows['work'], {'name': 'work', 'harness': 'claude-code', 'model': 'work-model',
                                        'provider': 'two', 'default': True, 'healthy': False})
        self.assertTrue(rows['main']['healthy'])
        self.assertEqual([p['name'] for p in body['profiles'] if p['default']], ['work'])
        text = json.dumps(body)
        for secret in ('dummy-for-tests', 'http://', '127.0.0.1', str(self.home), str(self.cfg.home), 'key_file',
                       'base_url', 'swap', 'outbox', 'SSHARNESS_FAKE_ROOT'):
            self.assertNotIn(secret, text)


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for frontend regression tests')
class SystemPages(unittest.TestCase):
    def run_render_test(self, name):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(['node', str(root / 'tests/web_regressions.js'),
                                 str(PAGE), name],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_soul_memory_keeps_editor_without_explanations(self):
        self.run_render_test('c5-files')

    def test_models_page_is_read_only_escaped_and_hides_internals(self):
        self.run_render_test('c5-models')

    def test_usage_entry_is_hidden_and_routes_back_to_chat(self):
        self.run_render_test('c5-navigation')
