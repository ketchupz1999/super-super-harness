"""Real localhost HTTP/SSE against ServiceCase's deterministic subprocess Harness."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from super_super_harness.ops import config
from super_super_harness.core.store import Store
from super_super_harness.channels.web import BODY_LIMIT, DIFF_LIMIT, TRANSCRIPT_BYTES
from tests.support import PAGE, ServiceCase, free_port


class OfflineWebCommandTests(unittest.TestCase):
    def web_command(self, opening, accepted=True):
        from types import SimpleNamespace
        from super_super_harness.cli import cmd_web
        with patch('super_super_harness.cli.Client') as client, patch('webbrowser.open', return_value=accepted) as browser:
            client.return_value.call.return_value = {'url': 'http://127.0.0.1:22500/?token=fake-ticket'}
            with redirect_stdout(io.StringIO()) as output:
                cmd_web(None, SimpleNamespace(open=opening))
            client.return_value.reader.close.assert_called_once()
            client.return_value.sock.close.assert_called_once()
            if opening:
                browser.assert_called_once_with('http://127.0.0.1:22500/?token=fake-ticket')
            else:
                browser.assert_not_called()
            return output.getvalue()

    def test_web_open_hides_consumed_link_and_explains_browser_result(self):
        self.assertEqual(self.web_command(True), '已在浏览器打开\n')
        failed = self.web_command(True, accepted=False)
        self.assertNotIn('token=', failed)
        self.assertIn('supersuperharness web', failed)
        self.assertNotIn('已在浏览器打开', failed)

    def test_web_without_open_prints_login_link(self):
        self.assertEqual(self.web_command(False), 'http://127.0.0.1:22500/?token=fake-ticket\n')

    def test_web_without_service_reports_not_running_without_traceback(self):
        import sys
        from tests.support import write_config
        for stale_socket in (False, True):
            with self.subTest(stale_socket=stale_socket), tempfile.TemporaryDirectory(prefix="ssharness-offline-web-") as folder:
                home = Path(folder)
                write_config(home, 1, 2)
                # Cover a cleanly stopped service and a socket left after a crash.
                if stale_socket:
                    (home / "owner-token").write_text("fake-owner-token")
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(str(home / "router.sock"))
                result = subprocess.run([sys.executable, "-m", "super_super_harness.cli", "web"],
                                        capture_output=True, text=True, timeout=5,
                                        env=dict(os.environ, SSHARNESS_HOME=str(home)))
                self.assertEqual(result.returncode, 1)
                self.assertIn("后台服务没有运行", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertNotIn("fake-owner-token", result.stdout + result.stderr)


class WorkerCLIEntries(unittest.TestCase):
    def test_lookup_output_and_internal_rpc_targets(self):
        from types import SimpleNamespace
        from super_super_harness import cli
        from super_super_harness.ops.config import Paths
        with tempfile.TemporaryDirectory() as folder:
            cfg = Paths(Path(folder))
            store = Store(cfg.db)
            self.addCleanup(store.db.close)
            tid = store.add_task('Review pages', 'brief', 'work', None, 'session', '', '', code_name='Gate', role='Entries')
            store.update_task(tid, status='dormant', progress='Ready')
            for needle in ('Gate', 'gAtE', tid, 'pages'):
                self.assertEqual(cli.find_task(store.db, needle)['id'], tid)
            other = store.add_task('Review API', 'brief', 'work', None, 'other', '', '', code_name='Pixel')
            with self.assertRaises(SystemExit) as ambiguous:
                cli.find_task(store.db, 'Review')
            self.assertIn('Gate Review pages', str(ambiguous.exception))
            self.assertIn('Pixel Review API', str(ambiguous.exception))
            self.assertNotIn(tid + ' ', str(ambiguous.exception))
            with self.assertRaisesRegex(SystemExit, 'no Worker matches'):
                cli.find_task(store.db, 'missing')
            with patch.object(cli, 'Client', side_effect=OSError), redirect_stdout(io.StringIO()) as output:
                cli.cmd_status(cfg, SimpleNamespace())
            self.assertRegex(output.getvalue(), r'Gate\s+休眠\s+work\s+Review pages\s+Ready')
            with redirect_stdout(io.StringIO()) as output:
                cli.cmd_log(cfg, SimpleNamespace(task='gate', full=False, follow=False))
            self.assertTrue(output.getvalue().startswith('Gate Review pages'))
            cfg.profile = lambda name: None
            with patch.object(cli, 'Client') as client, patch.object(cli, 'exec_interactive', return_value=0) as interactive, redirect_stdout(io.StringIO()) as output:
                client.return_value.call.return_value = dict(store.task(tid), cwd=folder, system_prompt='')
                self.assertEqual(cli.cmd_open(cfg, SimpleNamespace(task='gate')), 0)
            self.assertEqual(client.return_value.call.call_args_list[0].kwargs, {'task_id': tid})
            self.assertEqual(client.return_value.call.call_args_list[1].kwargs, {'task_id': tid})
            self.assertIn('Gate 已交还', output.getvalue())
            interactive.assert_called_once()
            store.update_task(other, code_name=tid)
            self.assertEqual(cli.find_task(store.db, tid)['id'], other, 'An exact public code name has priority')

    def test_help_names_worker_argument(self):
        from super_super_harness import cli
        import sys
        for command in ('log', 'open'):
            with patch.object(sys, 'argv', ['supersuperharness', command, '--help']), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    cli.main()
            self.assertEqual(result.exception.code, 0)
            self.assertIn('WORKER', output.getvalue())
            self.assertNotIn('TASK', output.getvalue())


class WebCase(ServiceCase):
    async def boot(self):
        self.cfg.web["port"] = free_port()
        await super().boot()
        self.port = self.cfg.web["port"]
        self.cookie = ""
        await self.login()

    async def request(self, method="GET", path="/api/state", data=None, headers=None, raw=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        if raw is None:
            body = json.dumps(data).encode() if data is not None else b""
            fields = {"Host": "127.0.0.1:%d" % self.port, "Cookie": self.cookie,
                      "Content-Length": str(len(body))}
            if method == "POST":
                fields.update({"Origin": "http://127.0.0.1:%d" % self.port, "Content-Type": "application/json"})
            fields.update(headers or {})
            raw = (method + " " + path + " HTTP/1.1\r\n" + "".join(k + ": " + v + "\r\n" for k, v in fields.items()) + "\r\n").encode() + body
        writer.write(raw)
        await writer.drain()
        output = await asyncio.wait_for(reader.read(), 15)
        writer.close()
        await writer.wait_closed()
        head, body = output.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        fields = dict(line.split(": ", 1) for line in lines[1:])
        status = int(lines[0].split()[1])
        if fields.get("Content-Type", "").startswith("application/json"):
            body = json.loads(body) if body else None
        return status, fields, body

    async def login(self):
        url = self.service.web.issue_link()
        status, headers, _ = await self.request(path=urlsplit(url).path + "?" + urlsplit(url).query)
        self.assertEqual(status, 303)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        return url, headers

    async def stream(self, last=""):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\nLast-Event-ID: %s\r\n\r\n" %
                      (self.port, self.cookie, last)).encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        self.assertIn(b"200 OK", head)
        self.addAsyncCleanup(self.close_stream, writer)
        return reader, writer

    async def close_stream(self, writer):
        writer.close()
        await writer.wait_closed()

    async def event(self, reader, wanted):
        while True:
            block = (await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)).decode()
            if "event: " + wanted + "\n" in block:
                fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                return fields["id"], json.loads(fields["data"])

    async def test_suber_artwork_allowlist_and_unchanged_csp(self):
        names = ('mark', 'idle', 'idle-blink', 'typing', 'typing-clean',
                 'typing-blink', 'waiting', 'waiting-blink', 'done')
        page_headers = (await self.request(path='/'))[1]
        for name in names:
            with self.subTest(name=name):
                code, headers, body = await self.request(path='/assets/suber/' + name + '.png', headers={'Cookie': ''})
                self.assertEqual(code, 200)
                self.assertEqual(headers['Content-Type'], 'image/png')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(headers['Content-Security-Policy'], page_headers['Content-Security-Policy'])
                self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertLess(len(body), 200_000)
        for path in ('/assets/suber/unknown.png', '/assets/suber/preview.html',
                     '/assets/suber/idle.svg', '/assets/suber/idle.png/extra'):
            self.assertEqual((await self.request(path=path))[0], 404)
        for path in ('/assets/suber/../index.html', '/assets/suber/%2e%2e/index.html'):
            self.assertEqual((await self.request(path=path))[0], 400)
        self.assertEqual((await self.request('POST', '/assets/suber/idle.png', {}))[0], 404)
        self.assertEqual((await self.request(path='/api/router-avatar/idle.png', headers={'Cookie': ''}))[0], 401)
        self.assertIn("img-src 'self' data:", page_headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', page_headers['Content-Security-Policy'].split('script-src')[1].split(';')[0])

    async def test_router_files_require_login_and_same_origin(self):
        for name in ('soul', 'memory'):
            path = '/api/router-files/' + name
            self.assertEqual((await self.request(path=path, headers={'Cookie': ''}))[0], 401)
            self.assertEqual((await self.request('POST', path, {}, headers={'Cookie': ''}))[0], 401)
            self.assertEqual((await self.request('POST', path, {}, headers={'Origin': 'https://example.invalid'}))[0], 403)
            code, headers, body = await self.request(path=path)
            self.assertEqual(code, 200)
            self.assertIn('Content-Security-Policy', headers)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertIn('version', body)

    async def test_router_files_save_empty_unicode_and_private_atomic_files(self):
        for name in ('soul', 'memory'):
            path = '/api/router-files/' + name
            file = self.cfg.router_dir / (name + '.md')
            old = (await self.request(path=path))[2]
            self.assertIn('version', old)
            for content in ('# Neutral 测试\n<script>not executed</script>', ''):
                if file.exists():
                    file.chmod(0o644)
                    inode = file.stat().st_ino
                else:
                    inode = None
                code, _, saved = await self.request('POST', path, {'content': content, 'version': old['version']})
                self.assertEqual(code, 200)
                self.assertEqual(saved['content'], content)
                self.assertEqual(file.read_text(), content)
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                self.assertNotEqual(file.stat().st_ino, inode)
                self.assertEqual((await self.request(path=path))[2], saved)
                old = saved

    async def test_router_files_conflicts_between_tabs_and_router_tool(self):
        path = '/api/router-files/memory'
        first = (await self.request(path=path))[2]
        self.assertIn('version', first)
        saved = await self.tool('memory_write', content='Router update', version=first['version'])
        self.assertNotEqual(saved['version'], first['version'])
        self.assertEqual((await self.request('POST', path, {'content': 'stale tab', 'version': first['version']}))[0], 409)
        self.assertEqual((await self.request('POST', path, {'content': 'fresh tab', 'version': saved['version']}))[0], 200)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            await self.tool('memory_write', content='stale Router', version=saved['version'])
        self.assertEqual((await self.tool('memory_read'))['content'], 'fresh tab')

    async def test_router_files_reject_oversized_invalid_and_client_paths(self):
        path = '/api/router-files/soul'
        first = (await self.request(path=path))[2]
        self.assertIn('version', first)
        for data, expected in (({'content': 'x' * (16 * 1024 + 1), 'version': first['version']}, 413),
                               ({'content': '界' * 6000, 'version': first['version']}, 413),
                               ({'content': 'x'}, 400), ({'content': 5, 'version': first['version']}, 400),
                               ({'content': 'x', 'version': first['version'], 'path': str(self.home / 'elsewhere')}, 400)):
            self.assertEqual((await self.request('POST', path, data))[0], expected)
        for target in ('/api/router-files/other', '/api/router-files/memory.md', '/api/router-files/%2e%2e/key'):
            self.assertIn((await self.request(path=target))[0], (400, 404))
        self.assertEqual((await self.request(path=path + '?path=/tmp/elsewhere'))[0], 400)
        self.assertFalse((self.home / 'elsewhere').exists())
        self.assertEqual((await self.request(path=path))[2], first)
        self.assertFalse((self.cfg.router_dir / 'soul.md').exists())

    async def test_router_files_missing_soul_and_external_edit_conflict(self):
        path = '/api/router-files/soul'
        code, _, first = await self.request(path=path)
        self.assertEqual(code, 200)
        self.assertFalse(first['exists'])
        self.assertEqual(first['content'], '')
        file = self.cfg.router_dir / 'soul.md'
        file.write_text('external edit')
        self.assertEqual((await self.request('POST', path, {'content': 'stale', 'version': first['version']}))[0], 409)
        self.assertEqual(file.read_text(), 'external edit')

    async def test_router_files_refuse_symlinks_and_bound_existing_reads(self):
        path = '/api/router-files/soul'
        self.assertEqual((await self.request(path=path))[0], 200)
        file = self.cfg.router_dir / 'soul.md'
        target = self.home / 'unrelated'
        target.write_text('unchanged')
        file.symlink_to(target)
        self.assertEqual((await self.request(path=path))[0], 400)
        self.assertEqual((await self.request('POST', path, {'content': 'bad', 'version': 'missing'}))[0], 400)
        self.assertEqual(target.read_text(), 'unchanged')
        file.unlink()
        file.write_bytes(b'x' * (16 * 1024 + 1))
        self.assertEqual((await self.request(path=path))[0], 413)

    async def test_router_files_exact_limit_and_invalid_utf8(self):
        path = '/api/router-files/soul'
        first = (await self.request(path=path))[2]
        self.assertIn('version', first)
        code, _, saved = await self.request('POST', path, {'content': 'x' * (16 * 1024), 'version': first['version']})
        self.assertEqual(code, 200)
        self.assertEqual(len(saved['content']), 16 * 1024)
        self.assertEqual((await self.request(path=path))[2], saved)
        self.assertEqual((await self.request('POST', path, {'content': '\ud800', 'version': saved['version']}))[0], 400)
        file = self.cfg.router_dir / 'soul.md'
        file.write_bytes(b'\xff')
        self.assertEqual((await self.request(path=path))[0], 400)

    def usage_session(self, session, rows):
        root = self.cfg.session_root / 'usage-fixtures'
        root.mkdir(parents=True, exist_ok=True)
        path = root / (session + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def usage_row(self, mid='msg-1', stamp='2026-01-01T12:00:00Z', model='fixture-model', **counts):
        return {'type': 'assistant', 'uuid': 'row-' + mid, 'timestamp': stamp,
                'message': {'id': mid, 'model': model, 'content': [{'type': 'text', 'text': 'PRIVATE-BODY'}],
                            'usage': dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=3,
                                          cache_creation_input_tokens=2, **counts)}}

    def usage_task(self, rows, session='usage-worker'):
        task = self.store.add_task('PRIVATE-TITLE', 'PRIVATE-PROMPT', 'work', None, session, '', '')
        self.store.update_task(task, started_once=1, status='completed')
        self.store.run('INSERT INTO worker_runs(task_id,profile,session_id,started,ended) VALUES(?,?,?,?,?)',
                       task, 'work', session, 1767268800, 1767268801)
        self.usage_session(session, rows)
        return task

    async def usage_get(self, query='start=2026-01-01&days=2'):
        # Worker accounting tests exclude the fake Router's unmetered session.
        self.store.unset('router_session')
        getattr(self.service.web, 'usage_cache', {}).clear()
        status, _, body = await self.request(path='/api/usage?' + query)
        self.assertEqual(status, 200, body)
        return body

    async def test_usage_requires_login_and_retains_web_security(self):
        status, _, _ = await self.request(path='/api/usage', headers={'Cookie': ''})
        self.assertEqual(status, 401)
        status, _, _ = await self.request(path='/api/usage', headers={'Host': 'evil.invalid'})
        self.assertEqual(status, 403)
        status, headers, body = await self.request(path='/api/usage')
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual((await self.request('POST', '/api/usage', {}, {'Origin': 'http://evil.invalid'}))[0], 403)

    async def test_usage_deduplicates_messages_sessions_and_result_events(self):
        row = self.usage_row()
        task = self.usage_task([row, row, dict(row, uuid='another-block')])
        self.store.run('INSERT INTO worker_runs(task_id,profile,session_id,started) VALUES(?,?,?,?)',
                       task, 'other', 'usage-worker', 1767268800)
        for _ in range(2):
            self.store.event('worker', 'result', task, cost=99, usage={'input_tokens': 99999})
        body = await self.usage_get()
        self.assertEqual(body['summary']['known_tokens'], 20)
        self.assertEqual(body['models'][0]['model'], 'fixture-model')
        self.assertEqual(body['tasks'][0]['task_id'], task)
        self.assertEqual(body['tasks'][0]['tokens'], 20)
        self.assertEqual((await self.usage_get())['summary']['known_tokens'], 20)

    async def test_usage_unknown_is_not_zero_and_explicit_zero_is_known(self):
        task = self.usage_task([{'type': 'assistant', 'timestamp': '2026-01-01T12:00:00Z',
                                 'message': {'id': 'missing', 'model': 'fixture-model'}}])
        body = await self.usage_get()
        self.assertIsNone(body['tasks'][0]['tokens'])
        self.assertIsNone(body['tasks'][0]['known_tokens'])
        self.assertGreater(body['tasks'][0]['unknown_sessions'], 0)
        row = self.usage_row()
        row['message']['usage'] = dict.fromkeys(row['message']['usage'], 0)
        self.usage_session('usage-worker', [row])
        body = await self.usage_get()
        self.assertEqual(body['tasks'][0]['task_id'], task)
        self.assertEqual(body['tasks'][0]['tokens'], 0)

    async def test_usage_utc_days_are_half_open_and_model_is_historical(self):
        self.usage_task([self.usage_row('before', '2025-12-31T23:59:59.999Z'),
                         self.usage_row('a', '2026-01-01T00:00:00Z', 'old-model'),
                         self.usage_row('b', '2026-01-02T01:00:00+01:00', 'new-model'),
                         self.usage_row('after', '2026-01-03T00:00:00Z')])
        self.store.run('UPDATE worker_runs SET started=?,ended=?', 1767225600, 1767398400)
        body = await self.usage_get()
        self.assertEqual(body['timezone'], 'UTC')
        self.assertEqual([(x['day'], x['known_tokens']) for x in body['days']],
                         [('2026-01-01', 20), ('2026-01-02', 20)])
        self.assertEqual({x['model'] for x in body['models']}, {'old-model', 'new-model'})
        self.assertEqual(body['summary']['known_tokens'], 40)

    async def test_usage_bounds_tasks_sessions_bytes_and_date_range(self):
        path = self.usage_session('usage-worker', [])
        task = self.usage_task([])
        with path.open('wb') as file:
            file.write(b' ' * (2 * 1024 * 1024 + 1))
        for i in range(105):
            self.store.add_task('Task', 'PRIVATE-PROMPT', 'work', None, 'queued-' + str(i), '', '')
        body = await self.usage_get()
        self.assertLessEqual(len(body['tasks']), 100)
        self.assertTrue(body['truncated'])
        for query in ('days=32', 'days=0', 'days=bad', 'start=not-a-date', 'start=9999-12-31&days=31'):
            self.assertEqual((await self.request(path='/api/usage?' + query))[0], 400, query)
        self.store.run('DELETE FROM tasks WHERE id<>?', task)
        body = await self.usage_get()
        self.assertTrue(body['truncated'])
        self.assertIsNone(body['tasks'][0]['tokens'])

    async def test_usage_quota_unconfigured_and_worker_capacity(self):
        self.service.tasks.max_workers = 7
        self.store.add_task('Queued', 'brief', 'work', None, 'queued', '', '')
        body = await self.usage_get()
        self.assertEqual(body['workers'], {'running': len(self.service.tasks.live), 'queued': 1, 'limit': 7})
        self.assertTrue(body['quota'])
        self.assertTrue(all(row['status'] == 'unconfigured' for row in body['quota']))
        self.assertTrue(all(set(row) == {'provider', 'status'} for row in body['quota']))

    async def test_usage_response_contains_no_prompt_body_or_credentials(self):
        self.usage_task([self.usage_row()])
        self.store.event('worker', 'result', prompt='PRIVATE-PROMPT', text='PRIVATE-BODY', key='PRIVATE-KEY')
        with patch.object(config.Provider, 'key', side_effect=AssertionError('usage must not read credentials')):
            body = await self.usage_get()
        text = json.dumps(body)
        for private in ('PRIVATE-PROMPT', 'PRIVATE-BODY', 'PRIVATE-KEY', 'PRIVATE-TITLE', 'key_file', 'base_url', 'content'):
            self.assertNotIn(private, text)

    async def test_usage_stream_snapshots_take_maxima_not_sum(self):
        first = self.usage_row()
        last = self.usage_row()
        last['message']['usage']['output_tokens'] = 15
        self.usage_task([first, last, first])
        body = await self.usage_get()
        self.assertEqual(body['tasks'][0]['tokens'], 30)
        self.assertEqual(body['tasks'][0]['output_tokens'], 15)

    async def test_usage_resumed_run_without_metadata_stays_unknown(self):
        task = self.usage_task([self.usage_row()])
        self.store.run('INSERT INTO worker_runs(task_id,profile,session_id,started,ended) VALUES(?,?,?,?,?)',
                       task, 'other', 'usage-worker', 1767355200, 1767355300)
        body = await self.usage_get()
        self.assertEqual(body['tasks'][0]['known_tokens'], 20)
        self.assertIsNone(body['tasks'][0]['tokens'])
        self.assertEqual(body['tasks'][0]['unknown_runs'], 1)

    async def test_usage_current_router_and_historical_worker_sessions(self):
        task = self.usage_task([self.usage_row()], 'old-session')
        self.store.update_task(task, session_id='new-session', profile='other')
        self.usage_session('new-session', [self.usage_row('new', model='new-model')])
        self.store.set('router_session', 'usage-router')
        self.usage_session('usage-router', [self.usage_row('router', model='router-model')])
        status, _, body = await self.request(path='/api/usage?start=2026-01-01&days=2')
        self.assertEqual(status, 200, body)
        self.assertEqual({r['task_id']: r['known_tokens'] for r in body['tasks']}, {task: 40, 'router': 20})
        self.assertEqual(body['summary']['known_tokens'], 60)
        self.assertEqual({r['model'] for r in body['models']}, {'fixture-model', 'new-model', 'router-model'})

    async def test_usage_malformed_missing_identity_and_shared_session_fail_closed(self):
        task = self.usage_task([self.usage_row()])
        path = self.cfg.session_root / 'usage-fixtures' / 'usage-worker.jsonl'
        for bad in (None, True, -1, 1.5, '20'):
            row = self.usage_row()
            row['message']['usage']['input_tokens'] = bad
            self.usage_session('usage-worker', [row])
            body = await self.usage_get()
            self.assertIsNone(body['tasks'][0]['tokens'])
            self.assertIsNone(body['tasks'][0]['known_tokens'])
        row = self.usage_row()
        del row['message']['id']
        self.usage_session('usage-worker', [row])
        self.assertIsNone((await self.usage_get())['tasks'][0]['tokens'])
        self.usage_session('usage-worker', [self.usage_row()])
        with path.open('a') as file:
            file.write('{unfinished')
        body = await self.usage_get()
        self.assertEqual(body['tasks'][0]['known_tokens'], 20)
        self.assertIsNone(body['tasks'][0]['tokens'])
        self.usage_session('usage-worker', [self.usage_row()])
        other = self.usage_task([self.usage_row()], 'usage-worker')
        body = await self.usage_get()
        self.assertEqual({r['task_id'] for r in body['tasks']}, {task, other})
        self.assertIsNone(body['summary']['known_tokens'])
        self.assertTrue(all(r['tokens'] is None for r in body['tasks']))

    async def test_usage_read_caps_and_unsupported_harness_do_not_read_transcripts(self):
        from super_super_harness.channels import usage
        self.usage_task([self.usage_row(), self.usage_row('extra')])
        self.usage_task([self.usage_row('b')], 'usage-worker-b')
        for field, value in (('SESSION_LIMIT', 1), ('TOTAL_BYTES', 10), ('ROW_LIMIT', 1), ('DIRECTORY_LIMIT', 0)):
            with self.subTest(field=field), patch.object(usage, field, value):
                body = await self.usage_get()
                self.assertTrue(body['truncated'])
                self.assertIsNone(body['summary']['tokens'])
        self.store.set('task_harness:t1', 'missing-adapter')
        self.store.set('task_harness:t2', 'missing-adapter')
        with patch.object(usage, 'session_rows', side_effect=AssertionError('unsupported format must not be read')):
            body = await self.usage_get()
            self.assertTrue(all(r['tokens'] is None for r in body['tasks']))

    async def test_usage_incomplete_session_marks_existing_models_unknown(self):
        self.usage_task([self.usage_row()])
        path = self.cfg.session_root / 'usage-fixtures' / 'usage-worker.jsonl'
        with path.open('a') as file:
            file.write('{unfinished')
        body = await self.usage_get()
        model = next(r for r in body['models'] if r['model'] == 'fixture-model')
        self.assertEqual(model['known_tokens'], 20)
        self.assertIsNone(model['tokens'])
        self.assertGreater(model['unknown_sessions'], 0)

    async def test_usage_ended_run_at_window_start_does_not_overlap(self):
        task = self.usage_task([self.usage_row('before', '2025-12-31T23:59:59Z')], 'ended-session')
        self.store.run('UPDATE worker_runs SET started=?,ended=? WHERE task_id=?', 1767225500, 1767225600, task)
        self.usage_task([self.usage_row('inside')], 'inside-session')
        body = await self.usage_get()
        self.assertEqual(body['summary']['unknown_runs'], 0)
        self.assertEqual(body['summary']['tokens'], 20)

    async def test_usage_inaccessible_candidate_preserves_unknown_instead_of_500(self):
        from super_super_harness.channels import usage
        self.usage_task([self.usage_row()])
        (self.cfg.session_root / 'denied-project').mkdir()
        original = os.open
        def deny(path, *args, **kwargs):
            if os.fspath(path) == 'denied-project':
                raise PermissionError('synthetic inaccessible directory')
            return original(path, *args, **kwargs)
        with patch.object(usage.os, 'open', side_effect=deny):
            body = await self.usage_get()
        self.assertEqual(body['summary']['known_tokens'], 20)
        self.assertIsNone(body['summary']['tokens'])
        self.assertIsNone(body['tasks'][0]['tokens'])

    async def test_usage_shared_cache_uses_one_readonly_background_scan(self):
        from super_super_harness.channels import usage
        import threading
        self.usage_task([self.usage_row()])
        self.store.unset('router_session')
        calls = []
        original = usage.read_report
        main_thread = threading.get_ident()
        def scan(*args):
            calls.append(threading.get_ident())
            time.sleep(.05)
            return original(*args)
        with patch.object(usage, 'read_report', side_effect=scan), patch.object(usage.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
            replies = await asyncio.gather(*(self.request(path='/api/usage?start=2026-01-01&days=2') for _ in range(8)))
            self.assertTrue(all(code == 200 for code, _, _ in replies))
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0], main_thread)
            self.assertEqual(connect.call_count, 1)
            self.assertIn('mode=ro', connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs['uri'])
            self.assertEqual(replies[0][2]['summary']['known_tokens'], 20)

    async def test_router_pending_count_tracks_queue_not_stale_display_rows(self):
        self.service.router.mode = 'paused'
        self.assertEqual((await self.request())[2]['router']['pending'], 0)
        self.service.router.submit('user', 'fake queued message', 'web')
        self.assertEqual((await self.request())[2]['router']['pending'], 1)
        self.store.run("UPDATE messages SET status='done'")
        self.assertEqual((await self.request())[2]['router']['pending'], 0)

    async def test_worker_event_titles_use_code_names(self):
        self.service.router.mode = "paused"
        turn = self.store.open_turn()
        ids = [self.store.add_task("Neutral <task> %d" % i, "brief", "work", None, "s", "", "") for i in range(5)]
        for i, task_id in enumerate(ids + ids[:1]):
            mid = self.store.add_message(str(i), "event", "body has no task name", kind="worker.done", task_id=task_id)
            self.store.run("UPDATE messages SET turn_id=?,status='confirmed' WHERE id=?", turn, mid)
        self.service.router.reply("Done")
        _, _, body = await self.request(path="/api/conversation")
        row = body["items"][-1]
        self.assertIn("✓ Nova · Neutral <task> 0", row.get("title", ""))
        self.assertIn("Echo · Neutral <task> 2", row["title"])
        self.assertIn("5", row["title"])
        self.assertNotIn("Atlas · Neutral", row["title"])
        self.assertEqual(row["title"].count("Nova · Neutral"), 1)
        # The same metadata is returned on live reconciliation, not just history.
        _, _, live = await self.request(path="/api/conversation?after=0")
        self.assertEqual(live["items"][-1]["title"], row["title"])
        for kind, label in (("worker.failed", "告警"), ("worker.question", "提问")):
            self.store.run("UPDATE messages SET kind=?", kind)
            self.service.router.reply('Updated ' + kind)
            _, _, body = await self.request(path="/api/conversation")
            self.assertIn(label + " Nova · Neutral", body["items"][-1]["title"])
        self.store.run("DELETE FROM tasks")
        self.service.router.reply('Missing task names')
        status, _, body = await self.request(path="/api/conversation")
        self.assertEqual(status, 200)
        self.assertFalse(body["items"][-1].get("title"))

    async def test_worker_titles_respect_message_kind_and_group_latest_events(self):
        from tests.notification_helpers import CASES, populate
        for name, kind, events, expected_kind, title in CASES:
            with self.subTest(name=name):
                turn, ids = populate(self.store, events)
                self.service.router.reply(name, kind)
                live = (await self.request(path='/api/conversation?after=0'))[2]['items'][-1]
                self.assertEqual((live['kind'], live['title']), (expected_kind, title.format(*(self.worker_name(task_id) for task_id in ids))))
                self.store.run("UPDATE messages SET status='done' WHERE turn_id=?", turn)
                self.store.run("UPDATE turns SET status='done' WHERE id=?", turn)
                saved = (await self.request(path='/api/conversation'))[2]['items'][-1]
                self.assertEqual((saved['kind'], saved['title']), (live['kind'], live['title']))

    async def worker_rollover(self, terminal):
        from super_super_harness.channels.chat import Display, History
        from unittest.mock import Mock
        router = self.service.router
        self.say('SLEEP 0.8 ECHO initial')
        await self.until(lambda: self.statuses() == ['confirmed'])
        old_turn = self.store.current_turn()['id']
        read_task = self.store.add_task('Read task', 'brief', 'work', None, 's', '', '')
        unread_task = self.store.add_task('Unread task', 'brief', 'work', None, 's', '', '')
        history = History(self.cfg.db)
        self.addCleanup(history.close)
        with patch.object(router.process, 'send', Mock(return_value='delayed')):
            read = router.submit('event', 'read result', kind='worker.done', task_id=read_task)
            unread = router.submit('event', 'unread result', kind='worker.failed', task_id=unread_task)
            await self.until(lambda: self.statuses() == ['confirmed', 'written', 'written'])
        read_uuid = self.store.one('SELECT uuid FROM messages WHERE id=?', read)['uuid']
        await router._on_event({'type': 'user', 'isReplay': True, 'uuid': read_uuid})
        router.reply('First notification', 'notice')
        if terminal:
            live = history.incoming({'text': 'First notification', 'kind': 'notice'})
            before = (live.kind, live.title)
        else:
            live = (await self.request(path='/api/conversation?after=0'))[2]['items'][-1]
            before = (live['kind'], live['title'])
        await self.until(lambda: self.store.one('SELECT turn_id FROM messages WHERE id=?', unread)['turn_id'] != old_turn)
        self.assertEqual(before, ('worker.done', '✓ %s · Read task' % self.worker_name(read_task)))
        if terminal:
            saved = history.recent()[-1]
            self.assertEqual((saved.kind, saved.title), before)
            display = Display(io.StringIO(), False, False)
            display.message(live)
            rendered = display.output.getvalue()
            display.output = io.StringIO()
            display.message(saved)
            self.assertEqual(display.output.getvalue(), rendered)
        else:
            saved = (await self.request(path='/api/conversation'))[2]['items'][-1]
            self.assertEqual((saved['kind'], saved['title']), before)
        unread_uuid = self.store.one('SELECT uuid FROM messages WHERE id=?', unread)['uuid']
        await router._on_event({'type': 'user', 'isReplay': True, 'uuid': unread_uuid})
        router.reply('Second notification', 'notice')
        await router._on_result({'subtype': 'success', 'result': 'second'})
        if terminal:
            next_live = history.incoming({'text': 'Second notification', 'kind': 'notice'})
            self.assertEqual((next_live.kind, next_live.title),
                             ('worker.failed', '! 告警 %s · Unread task' % self.worker_name(unread_task)))
            self.assertEqual(history.recent()[-1].title, next_live.title)
        else:
            next_live = (await self.request(path='/api/conversation?after=0'))[2]['items'][-1]
            self.assertEqual((next_live['kind'], next_live['title']),
                             ('worker.failed', '! 告警 %s · Unread task' % self.worker_name(unread_task)))
            self.assertEqual((await self.request(path='/api/conversation'))[2]['items'][-1]['title'], next_live['title'])

    async def worker_late_confirmation(self, terminal, empty=False):
        from super_super_harness.channels.chat import History
        from tests.notification_helpers import populate
        self.service.router.mode = 'paused'
        turn, ids = populate(self.store, [(0, 'worker.done'), (1, 'worker.failed')])
        unread = self.store.one("SELECT id,uuid FROM messages WHERE task_id=?", ids[1])
        self.store.run("UPDATE messages SET status='written' WHERE id=?", unread['id'])
        if empty:
            self.store.run("UPDATE messages SET status='written' WHERE task_id=?", ids[0])
        history = History(self.cfg.db)
        self.addCleanup(history.close)
        self.service.router.reply('Before late replay', 'notice')
        if terminal:
            live = history.incoming({'text': 'Before late replay', 'kind': 'notice'})
            before = (live.kind, live.title)
        else:
            live = (await self.request(path='/api/conversation?after=0'))[2]['items'][-1]
            before = (live['kind'], live['title'])
        expected = ('notice', '') if empty else ('worker.done', '✓ %s · X' % self.worker_name(ids[0]))
        self.assertEqual(before, expected)
        after_title = '! 告警 %s · Y' % self.worker_name(ids[1]) + ('' if empty else '；✓ %s · X' % self.worker_name(ids[0]))
        await self.service.router._on_event({'type': 'user', 'isReplay': True, 'uuid': unread['uuid']})
        self.service.router.reply('After late replay', 'notice')
        if terminal:
            saved = next(m for m in history.recent() if m.text == 'Before late replay')
            self.assertEqual((saved.kind, saved.title), before)
            after = history.incoming({'text': 'After late replay', 'kind': 'notice'})
            self.assertEqual(after.title, after_title)
        else:
            rows = (await self.request(path='/api/conversation'))[2]['items']
            saved = next(m for m in rows if m['text'] == 'Before late replay')
            self.assertEqual((saved['kind'], saved['title']), before)
            self.assertEqual(rows[-1]['title'], after_title)
        self.assertEqual(self.store.current_turn()['id'], turn)

    async def test_terminal_worker_title_is_frozen_before_same_turn_late_replay(self):
        await self.worker_late_confirmation(terminal=True)

    async def test_web_worker_title_is_frozen_before_same_turn_late_replay(self):
        await self.worker_late_confirmation(terminal=False)

    async def test_terminal_empty_worker_title_is_frozen_before_late_replay(self):
        await self.worker_late_confirmation(terminal=True, empty=True)

    async def test_web_empty_worker_title_is_frozen_before_late_replay(self):
        await self.worker_late_confirmation(terminal=False, empty=True)

    async def test_terminal_worker_titles_ignore_unread_events_moved_to_next_turn(self):
        await self.worker_rollover(terminal=True)

    async def test_web_worker_titles_ignore_unread_events_moved_to_next_turn(self):
        await self.worker_rollover(terminal=False)

    async def test_cookie_one_time_link_and_loopback_binding(self):
        url, headers = await self.login()
        self.assertEqual(headers["Location"], "/")
        self.assertTrue(headers["Set-Cookie"].startswith("ssharness_web_session_%d=" % self.port))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        path = "/?" + urlsplit(url).query
        self.assertEqual((await self.request(path=path))[0], 401)
        self.assertFalse((self.cfg.home / "web-token").exists())
        self.assertEqual(self.service.web.server.sockets[0].getsockname()[0], "127.0.0.1")
        self.service.web.ticket = ("expired", time.monotonic() - 1)
        self.assertEqual((await self.request(path="/?token=expired"))[0], 401)
        self.assertEqual((await self.request(headers={"Cookie": ""}))[0], 401)
        self.assertEqual((await self.request(headers={"Cookie": "ssharness_web_session=wrong"}))[0], 401)
        self.assertEqual((await self.request(path="/api/stream", headers={"Cookie": ""}))[0], 401)
        self.assertEqual((await self.request(path="/"))[0], 200)
        self.assertEqual((await self.request(headers={"Host": "localhost:%d" % self.port}))[0], 200)

    async def test_non_ascii_login_token_returns_401_without_consuming_valid_ticket(self):
        url = self.service.web.issue_link()
        ticket = self.service.web.ticket
        for value in ("%E4%BD%A0%E5%A5%BD", "%C3%A9", "%FF", "wrong-ascii"):
            with self.subTest(value=value):
                status, headers, _ = await self.request(path="/?token=" + value, headers={"Cookie": ""})
                self.assertEqual(status, 401)
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(self.service.web.ticket, ticket)
        path = "/?" + urlsplit(url).query
        status, headers, _ = await self.request(path=path, headers={"Cookie": ""})
        self.assertEqual(status, 303)
        self.assertIn("Set-Cookie", headers)
        self.assertIsNone(self.service.web.ticket)
        self.assertEqual((await self.request(path=path, headers={"Cookie": ""}))[0], 401)

    async def test_unrelated_malformed_and_non_ascii_cookies_do_not_block_requests(self):
        for other in ["flag", "spaces=hello world", 'json={"theme": "dark"}',
                      "unicode=你好", "latin1=café"]:
            for cookie in [other + "; " + self.cookie, self.cookie + "; " + other]:
                with self.subTest(cookie=other):
                    self.assertEqual((await self.request(headers={"Cookie": cookie}))[0], 200)
                    self.assertEqual((await self.request(path="/", headers={"Cookie": cookie}))[0], 200)
        # Only the exact session name counts; invalid and duplicate sessions fail closed.
        for cookie in ["other=" + self.cookie, self.cookie + "; " + self.cookie,
                       self.service.web.cookie_name + "=invalid; flag", "unicode=你好"]:
            self.assertEqual((await self.request(headers={"Cookie": cookie}))[0], 401)
        raw = b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:" + str(self.port).encode()
        raw += b"\r\nCookie: bytes=\xff\x80; " + self.cookie.encode() + b"\r\n\r\n"
        self.assertEqual((await self.request(raw=raw))[0], 200)

    async def test_web_login_tickets_stay_in_memory_and_old_files_are_removed(self):
        token_file = self.home / "web-token"
        await self.login()
        self.assertFalse(token_file.exists())
        stale_url = self.service.web.issue_link()
        token_file.write_text("legacy-login-ticket")
        await self.halt()
        await self.boot()
        self.assertFalse(token_file.exists())
        self.assertEqual((await self.request(path="/?" + urlsplit(stale_url).query))[0], 401)
        # Disabled web instances also remove the obsolete file, without following symlinks.
        from super_super_harness.channels.web import Web
        self.cfg.web["port"] = 0
        target = self.home / "keep"
        target.write_text("do not change")
        token_file.symlink_to(target)
        other = Web(self.service)
        await other.start()
        self.assertFalse(token_file.is_symlink())
        self.assertEqual(target.read_text(), "do not change")

    def capture_loop_errors(self):
        errors = []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        return errors

    async def test_shutdown_closes_idle_and_sse_connections_without_waiting(self):
        errors = self.capture_loop_errors()
        idle_reader, idle_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addAsyncCleanup(self.close_stream, idle_writer)
        sse_reader, _ = await self.stream()
        await self.event(sse_reader, "reset")
        await self.until(lambda: len(self.service.web.connections) == 2)
        await asyncio.wait_for(self.halt(), 3)
        self.assertEqual(await asyncio.wait_for(idle_reader.read(), 1), b"")
        await asyncio.wait_for(sse_reader.read(), 1)
        self.assertFalse(self.service.web.connections)
        self.assertFalse(self.service.web.handlers)
        self.assertIsNone(self.service.web.server)
        self.assertEqual(errors, [])

    async def test_shutdown_closes_accepted_connection_before_handler_registration(self):
        from super_super_harness.channels.web import Web
        from dataclasses import replace
        from types import SimpleNamespace
        cfg = replace(self.cfg, web=dict(self.cfg.web, port=free_port()))
        web = Web(SimpleNamespace(cfg=cfg, store=self.store))
        entered, release = asyncio.Event(), asyncio.Event()
        handle = web.handle
        async def delayed(reader, writer):
            entered.set()
            await release.wait()
            await handle(reader, writer)
        web.handle = delayed
        await web.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", web.port)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(web.connections)
            closing = asyncio.create_task(web.close())
            await asyncio.sleep(.03)
            release.set()
            await asyncio.wait_for(closing, 1)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            self.assertFalse(web.connections)
            self.assertFalse(web.handlers)
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()
            await web.close()

    async def test_cancelled_service_closes_idle_and_sse_connections(self):
        errors = self.capture_loop_errors()
        idle_reader, idle_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addAsyncCleanup(self.close_stream, idle_writer)
        sse_reader, _ = await self.stream()
        await self.event(sse_reader, "reset")
        await self.until(lambda: len(self.service.web.connections) == 2)
        self.running.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.running, 3)
            self.assertEqual(await asyncio.wait_for(idle_reader.read(), 1), b"")
            await asyncio.wait_for(sse_reader.read(), 1)
            self.assertFalse(self.service.web.handlers)
            self.assertFalse(self.service.web.connections)
            self.assertIsNone(self.service.web.server)
            self.assertEqual(errors, [])
        finally:
            # Crash simulation intentionally leaves Harness children for recovery.
            await self.service.router.shutdown()
            await self.service.tasks.shutdown()
            self.store.close()

    async def test_host_origin_cors_and_path_security(self):
        for host in ["evil.example:%d" % self.port, "127.0.0.1", "localhost:1", "127.0.0.1:%d.evil" % self.port]:
            self.assertEqual((await self.request(headers={"Host": host}))[0], 403)
        for origin in ["", "null", "https://evil.example", "http://localhost:%d" % self.port]:
            code, headers, _ = await self.request("POST", "/api/messages", {"text": "x", "request_id": "test-security"}, {"Origin": origin})
            self.assertEqual(code, 403)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual((await self.request("OPTIONS"))[0], 405)
        for path in ["/api/workers/../owner-token", "/api/workers/%2e%2e/diff", "/api/workers/t1%2fsend", "/api/workers/t1\\send"]:
            self.assertEqual((await self.request(path=path))[0], 400)
        self.assertEqual((await self.request(path="/api/workers/t999"))[0], 404)
        self.assertEqual((await self.request(path="/owner-token"))[0], 404)
        self.assertEqual((await self.request("POST", "/api/messages", [], {}))[0], 400)
        self.assertEqual((await self.request("POST", "/api/messages", {}, {"Content-Type": "text/plain"}))[0], 415)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages")["n"], 0)

    async def test_bounded_body_headers_and_malformed_requests(self):
        self.assertEqual((await self.request("POST", "/api/messages", {"text": "x" * BODY_LIMIT}))[0], 413)
        host = "Host: 127.0.0.1:%d\r\n" % self.port
        for extra in [host, "Transfer-Encoding: chunked\r\n", "Content-Length: -1\r\n", "bad header\r\n", "X: " + "x" * 20000 + "\r\n"]:
            code, _, _ = await self.request(raw=("GET / HTTP/1.1\r\n" + host + extra + "\r\n").encode())
            self.assertEqual(code, 400)
        self.assertEqual((await self.request(path="/api/conversation?limit=100000"))[0], 400)
        self.assertEqual((await self.request(path="/api/conversation?before=no"))[0], 400)

    async def test_message_reply_history_pagination_and_retry(self):
        payload = {"text": "ECHO hello <script>alert(1)</script>", "request_id": "test-message-1"}
        code, _, result = await self.request("POST", "/api/messages", payload)
        self.assertEqual(code, 200)
        again = (await self.request("POST", "/api/messages", payload))[2]
        self.assertEqual(result, again)
        self.assertEqual((await self.request("POST", "/api/messages", dict(payload, text="different")))[0], 409)
        await self.until(lambda: self.store.one("SELECT 1 FROM outbox WHERE channel='web'"))
        self.assertEqual(self.store.one("SELECT channel FROM messages WHERE id=?", result["id"])["channel"], "web")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM outbox WHERE channel='terminal'")["n"], 0)
        history = (await self.request(path="/api/conversation?limit=1"))[2]
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["role"], "router")
        older = (await self.request(path="/api/conversation?before=%s&limit=1" % history["next_before"]))[2]
        self.assertEqual(older["items"][0]["text"], payload["text"])
        self.assertTrue(history["has_more"])
        after = (await self.request(path="/api/conversation?after=%s" % older["next_after"]))[2]
        self.assertEqual(after["items"][0]["id"], history["items"][0]["id"])
        code, headers, html = await self.request(path="/")
        self.assertNotIn(b"Sample data", html)
        self.assertNotIn(b"const tasks = [", html)
        self.assertIn("script-src 'sha256-", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"].split("script-src")[1].split(";")[0])

    async def test_page_does_not_include_internal_design_notes(self):
        status, _, page = await self.request(path="/")
        self.assertEqual(status, 200)
        self.assertTrue(page.split(b"<style>", 1)[1].lstrip().startswith(b":root {"))

    async def test_history_turn_tools_and_task_links(self):
        self.service.router.submit("user", "SLEEP 1 ECHO x", "web")
        await self.until(lambda: self.store.current_turn())
        task = await self.tool("worker_hire", name="test", brief="SLEEP 3")
        await self.service.router._on_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "mcp__router__worker_assign", "input": {"worker": task["worker"], "text": "hello"}}]}})
        await self.tool("reply", text="task started")
        history = (await self.request(path="/api/conversation"))[2]["items"]
        reply = [m for m in history if m["role"] == "router"][0]
        self.assertIn(self.worker_id(task), reply["tasks"])
        self.assertTrue(any(s["name"] == "mcp__router__worker_assign" for s in reply["steps"]))

    async def test_web_delivery_does_not_consume_terminal_backlog(self):
        self.service.outbox.add("broadcast", "notice")
        await self.service.outbox.flush()
        rows = self.store.all("SELECT channel,status FROM outbox WHERE text='broadcast'")
        self.assertEqual({r["channel"]: r["status"] for r in rows}, {"web": "sent", "terminal": "pending"})
        self.assertEqual(len((await self.request(path="/api/conversation"))[2]["items"]), 1)
        class Writer:
            def __init__(self): self.data = []
            def write(self, data): self.data.append(json.loads(data))
            async def drain(self): pass
        writer = Writer()
        self.service.outbox.channels["terminal"].clients.add(writer)
        self.say("ECHO terminal only")
        await self.until(lambda: any("terminal only" in m["text"] for m in writer.data))
        self.assertEqual(sum(m["text"] == "broadcast" for m in writer.data), 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM outbox WHERE channel='web' AND text LIKE '%terminal only%'")["n"], 0)
        self.service.outbox.channels["terminal"].clients.discard(writer)

    async def test_sse_reconnect_reconciles_without_loss_or_duplicates(self):
        reader, writer = await self.stream()
        cursor, _ = await self.event(reader, "reset")
        await self.event(reader, "activity")
        await self.close_stream(writer)
        before = (await self.request(path="/api/conversation"))[2]["next_after"]
        for i in range(3):
            await self.request("POST", "/api/messages", {"text": "ECHO reconnect-%d" % i, "request_id": "reconnect-%d" % i})
            await self.until(lambda: self.store.one("SELECT COUNT(*) n FROM outbox")["n"] >= i + 1)
        reader2, _ = await self.stream(cursor)
        await self.event(reader2, "reset")
        _, event = await self.event(reader2, "message")
        history = (await self.request(path="/api/conversation?after=%d" % before))[2]["items"]
        self.assertEqual(len(history), 6)
        self.assertEqual(len({m["id"] for m in history}), 6)
        self.assertEqual(event["latest"], history[-1]["id"])
        self.assertEqual((await self.request(path="/api/conversation?after=%d" % history[-1]["id"]))[2]["items"], [])
        self.service.router.mode = "paused"
        _, activity = await self.event(reader2, "activity")
        # A queued initial activity can precede the new paused notification.
        if activity["mode"] != "paused":
            _, activity = await self.event(reader2, "activity")
        self.assertEqual(activity["mode"], "paused")

    async def test_sse_task_changes_and_task_operations(self):
        reader, _ = await self.stream()
        await self.event(reader, "state")
        task = self.start_task("SLEEP 30", name='<img src=x onerror="alert(1)">')
        await self.worker_ready(task)
        await self.event(reader, "state")
        state = (await self.request())[2]
        self.assertEqual(state["counts"]["running"], 1)
        self.assertEqual(state["tasks"][0]["id"], task)
        self.assertEqual((await self.request("POST", "/api/workers/%s/interrupt" % task, {}))[0], 200)
        await self.wait_status(task, "interrupted")
        await self.until(lambda: task not in self.service.tasks.live)
        self.assertEqual((await self.request("POST", "/api/workers/%s/resume" % task, {"text": "SLEEP 10"}))[0], 200)
        await self.worker_ready(task)
        self.assertEqual((await self.request("POST", "/api/workers/%s/model" % task, {"profile": "other"}))[0], 200)
        self.assertEqual(self.store.task(task)["profile"], "other")
        self.assertEqual((await self.request("POST", "/api/workers/%s/model" % task, {"profile": "missing"}))[0], 400)
        self.assertEqual((await self.request("POST", "/api/workers/%s/send" % task, {"text": "hello", "expected_status": "waiting"}))[0], 409)
        await self.until(lambda: task in self.service.tasks.live and self.service.tasks.live[task].process)
        self.assertEqual((await self.request("POST", "/api/workers/%s/send" % task, {"text": "hello"}))[0], 200)
        self.assertEqual((await self.request("POST", "/api/workers/%s/stop" % task, {}))[0], 200)
        self.assertEqual(self.status(task), "stopped")
        self.assertEqual((await self.request("POST", "/api/workers/%s/send" % task, {"text": "no"}))[0], 409)

    async def test_waiting_clock_and_finished_duration(self):
        task = self.start_task("SLEEP 10")
        await self.worker_ready(task)
        self.service.tasks.ask_user(task, "Continue?")
        waiting = self.store.task(task)["state_since"]
        self.service.tasks.report_progress(task, "still waiting")
        self.assertEqual(self.store.task(task)["state_since"], waiting)
        info = (await self.request(path="/api/workers/%s" % task))[2]
        self.assertEqual(info["task"]["state_since"], waiting)
        self.assertGreaterEqual(waiting, info["task"]["started"])
        await self.service.tasks.stop(task)
        ended = self.store.task(task)["ended"]
        self.assertGreaterEqual(ended, info["task"]["started"])

    async def test_transcript_pagination_limits_and_event_fallback(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        detail = (await self.request(path="/api/workers/%s" % task))[2]
        self.assertEqual(detail["source"], "events")
        path = self.session_path(self.store.task(task)["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as file:
            file.write('x' * (TRANSCRIPT_BYTES + 1) + '\n')
            for i in range(600):
                file.write(json.dumps({"type": "assistant", "timestamp": "2026-11-01T00:00:00Z", "message": {"content": [
                    {"type": "thinking", "thinking": "step %d" % i},
                    {"type": "text", "text": '<script>alert("x")</script>'}]}}) + '\n')
        detail = (await self.request(path="/api/workers/%s?limit=20" % task))[2]
        self.assertEqual(detail["source"], "transcript")
        self.assertTrue(detail["truncated"])
        self.assertEqual(len(detail["timeline"]), 20)
        self.assertEqual(detail["next_offset"], 20)
        page = (await self.request(path="/api/workers/%s?offset=20&limit=20&snapshot=%s" % (task, detail["snapshot"])))[2]
        self.assertNotEqual(detail["timeline"][0], page["timeline"][0])
        self.assertLessEqual(page["total"], 500)

    async def test_timeline_hides_empty_speech_and_harness_prompts_before_paging(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        path = self.session_path(self.store.task(task)["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        notice = "[Your previous response had no visible output.\nPlease continue the task.]"
        rows = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": ""},
                {"type": "text", "text": " \n "}, {"type": "thinking", "thinking": "plan"}]}},
            {"type": "user", "message": {"content": notice}},
            {"type": "assistant", "message": {"content": notice}},
            {"type": "user", "message": {"content": "[Request interrupted by user for tool use]"}},
            {"type": "user", "message": {"content": "[Request interrupted by user]"}},
            {"type": "user", "message": {"content": "real request"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "true"}},
                {"type": "text", "text": "I saw " + notice}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": notice},
                {"type": "tool_result", "content": ""}]}},
            {"type": "assistant", "message": {"content": "done"}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows))
        first = (await self.request(path="/api/workers/%s?limit=2" % task))[2]
        self.assertEqual(first["source"], "transcript")
        self.assertEqual(first["total"], 7)
        self.assertEqual([s["kind"] for s in first["timeline"]], ["thinking", "received"])
        self.assertEqual(first["timeline"][1]["text"], "real request")
        page = (await self.request(path="/api/workers/%s?offset=2&limit=20&snapshot=%s" % (task, first["snapshot"])))[2]
        self.assertEqual([s["kind"] for s in page["timeline"]], ["tool_call", "said", "tool_result", "tool_result", "said"])
        self.assertEqual(page["timeline"][1]["text"], "I saw " + notice)
        self.assertTrue(page["timeline"][2]["error"])
        self.assertEqual(page["timeline"][2]["text"], notice)
        self.assertIsNone(page["next_offset"])
        # Filtering is a web presentation policy, not a rewrite of Harness logs.
        self.assertIn(notice, [r["message"]["content"] for r in map(json.loads, path.read_text().splitlines())
                               if isinstance(r["message"]["content"], str)])

    async def test_control_prompt_prefix_does_not_hide_real_followup(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        path = self.session_path(self.store.task(task)["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "[Your previous response had no visible output. Please continue.]\nPlease run [pytest -q]"
        path.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        detail = (await self.request(path="/api/workers/%s" % task))[2]
        self.assertEqual([s["text"] for s in detail["timeline"]], [text])

    async def test_hidden_transcript_entries_do_not_displace_visible_tail(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        path = self.session_path(self.store.task(task)["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        texts = ["keep this response"] + [" ", "[Your previous response had no visible output. Please continue.]"] * 300
        path.write_text("\n".join(json.dumps({"type": "assistant", "message": {"content": text}}) for text in texts))
        detail = (await self.request(path="/api/workers/%s" % task))[2]
        self.assertEqual([s["text"] for s in detail["timeline"]], ["keep this response"])
        self.assertEqual(detail["total"], 1)
        self.assertFalse(detail["truncated"])
        self.assertIsNone(detail["next_offset"])

    async def test_hidden_event_entries_do_not_displace_visible_tail(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        self.store.event("worker", "text", task, text="keep this response")
        for _ in range(600):
            self.store.event("worker", "text", task, text=" ")
        detail = (await self.request(path="/api/workers/%s" % task))[2]
        speech = [json.loads(s["text"])["text"] for s in detail["timeline"] if s["tool"] == "text"]
        self.assertEqual(speech, ["keep this response"])
        self.assertFalse(detail["truncated"])
        self.assertIsNone(detail["next_offset"])

    async def test_hidden_transcript_does_not_fall_back_to_noisy_events(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        path = self.session_path(self.store.task(task)["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "assistant", "message": {"content": " "}}) + "\n")
        detail = (await self.request(path="/api/workers/%s" % task))[2]
        self.assertEqual(detail["source"], "transcript")
        self.assertEqual(detail["timeline"], [])
        self.assertEqual(detail["total"], 0)
        self.assertIsNone(detail["next_offset"])

    async def test_event_fallback_hides_harness_speech_but_keeps_diagnostics(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        for text in ("", "  ", "[Your previous response had no visible output. Please continue.]", "visible"):
            self.store.event("worker", "text", task, text=text)
        self.store.event("worker", "tool", task, tool="Bash", input="")
        detail = (await self.request(path="/api/workers/%s" % task))[2]
        self.assertEqual(detail["source"], "events")
        speech = [s for s in detail["timeline"] if s["tool"] == "text"]
        self.assertEqual([json.loads(s["text"])["text"] for s in speech], ["visible"])
        self.assertTrue(any(s["tool"] == "tool" for s in detail["timeline"]))

    async def test_timeline_snapshot_stays_stable_while_worker_appends(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        path = self.session_path(self.store.task(task)["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        def append(start, end):
            with path.open("a") as file:
                for i in range(start, end):
                    file.write(json.dumps({"type": "assistant", "message": {"content": "step-%d" % i}}) + "\n")
        append(0, 600)
        first = (await self.request(path="/api/workers/%s?limit=100" % task))[2]
        self.assertEqual(first["timeline"][-1]["text"], "step-199")
        append(600, 620)
        next_page = (await self.request(path="/api/workers/%s?offset=100&snapshot=%s" % (task, first["snapshot"])))[2]
        self.assertEqual(next_page["timeline"][0]["text"], "step-200")
        self.store.update_task(task, status="completed", report="fresh report", ended=time.time())
        meta = (await self.request(path="/api/workers/%s?metadata=1" % task))[2]
        self.assertEqual(meta["task"]["status"], "completed")
        self.assertEqual(meta["task"]["report"], "fresh report")
        self.assertNotIn("timeline", meta)
        self.service.web.snapshots[first["snapshot"]]["expires"] = 0
        self.assertEqual((await self.request(path="/api/workers/%s?offset=100&snapshot=%s" % (task, first["snapshot"])))[0], 409)
        self.assertEqual((await self.request(path="/api/workers/%s?offset=100" % task))[0], 400)

    async def test_cookie_and_history_survive_daemon_restart(self):
        cookie = self.cookie
        await self.request("POST", "/api/messages", {"text": "ECHO persisted", "request_id": "restart-cookie"})
        await self.until(lambda: self.store.one("SELECT 1 FROM outbox WHERE channel='web'"))
        latest = (await self.request(path="/api/conversation"))[2]["next_after"]
        await self.halt()
        await ServiceCase.boot(self)
        self.assertEqual(self.cookie, cookie)
        self.assertEqual((await self.request())[0], 200)
        reader, _ = await self.stream("old-epoch:1")
        await self.event(reader, "reset")
        again = (await self.request("POST", "/api/messages", {"text": "ECHO persisted", "request_id": "restart-cookie"}))[2]
        self.assertEqual(again["id"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE role='user'")["n"], 1)
        self.assertGreaterEqual((await self.request(path="/api/conversation"))[2]["next_after"], latest)
        self.assertEqual((self.home / "web-session-key").stat().st_mode & 0o777, 0o600)
        invalid = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
        self.assertEqual((await self.request(headers={"Cookie": invalid}))[0], 401)

    async def test_removed_historical_profile_does_not_break_chat(self):
        self.service.tasks.closed = True
        task = self.start_task("brief", profile="other")
        self.store.update_task(task, status="completed")
        del self.cfg.profiles["other"]
        code, _, state = await self.request()
        self.assertEqual(code, 200)
        self.assertIn("profile removed", state["tasks"][0]["model"])
        self.assertEqual((await self.request(path="/api/workers/%s" % task))[0], 200)

    async def test_atomic_message_idempotency_and_notification_history(self):
        original = self.store.run
        def fail_request(sql, *args):
            if sql.startswith("INSERT INTO web_requests"):
                raise sqlite3.OperationalError("injected write failure")
            return original(sql, *args)
        with patch.object(self.store, "run", side_effect=fail_request):
            with self.assertRaises(sqlite3.OperationalError):
                await self.service.web.dispatch("POST", "/api/messages", {}, {"text": "not accepted", "request_id": "atomic-message"})
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages")["n"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM conversation")["n"], 0)
        def fail_history(sql, *args):
            if sql.startswith("INSERT INTO conversation"):
                raise sqlite3.OperationalError("injected history failure")
            return original(sql, *args)
        with patch.object(self.store, "run", side_effect=fail_history):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.outbox.add("broadcast")
            with self.assertRaises(sqlite3.OperationalError):
                self.service.router.submit("user", "rollback", "terminal")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM outbox")["n"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages")["n"], 0)
        await self.request("POST", "/api/messages", {"text": "ECHO accepted once", "request_id": "atomic-message"})
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM messages WHERE role='user'")["n"], 1)

    async def test_developer_shallow_probe_cache_and_no_secrets(self):
        with patch("super_super_harness.adapters.claude_code.shallow_probe", return_value=True) as probe:
            first = (await self.request(path="/api/developer"))[2]
            await self.request(path="/api/developer")
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(len(first["providers"]), 2)
            self.assertIn("swap", first)
            self.assertNotIn("dummy-for-tests", json.dumps(first))
            self.service.web.probed_at -= 31
            await self.request(path="/api/developer")
            self.assertEqual(probe.call_count, 4)

    async def test_port_conflict_does_not_stop_service_and_cli_link(self):
        from super_super_harness.channels.web import Web
        other = Web(self.service)
        with self.assertLogs("super_super_harness.channels.web", level="WARNING"):
            await other.start()
        self.assertFalse(other.status()["listening"])
        self.assertIn("unavailable", other.status()["error"])
        self.assertIsNotNone(self.service.router.process)
        from super_super_harness.cli import cmd_web
        from types import SimpleNamespace
        with redirect_stdout(io.StringIO()) as output:
            with patch("webbrowser.open") as opened:
                await asyncio.to_thread(cmd_web, self.cfg, SimpleNamespace(open=True))
                opened.assert_called_once()
        self.assertEqual(output.getvalue(), "已在浏览器打开\n")
        self.assertNotIn("token=", output.getvalue())
        status = await self.service.dispatch(("owner", None), "status", {}, None)
        self.assertTrue(status["web"]["listening"])

    async def test_worker_code_routes_resolve_all_actions_to_internal_ids(self):
        from unittest.mock import AsyncMock
        tid = self.store.add_task('Entry work', 'brief', 'work', None, 'session', '', '', code_name='Gate-2', role='Entries')
        other = self.store.add_task('Other work', 'brief', 'work', None, 'other', '', '', code_name=tid)
        for worker, selector, expected in [('Gate-2', '', tid), ('gAtE-2', '', tid), (tid, '', other),
                                           (tid, 'by=id', tid), (other, '', other)]:
            status, _, response = await self.request(path='/api/workers/' + worker + '?' + selector + '&metadata=1')
            self.assertEqual(status, 200)
            self.assertEqual(response['task']['id'], expected)
            self.assertEqual(response['task']['code_name'], self.store.task(expected)['code_name'])
            for action, method, body in [('send', 'send', {'text': 'Hello'}), ('interrupt', 'interrupt', {}),
                                         ('resume', 'resume', {}), ('stop', 'stop', {}), ('model', 'switch_model', {'profile': 'work'})]:
                mock_type = AsyncMock if action in ('stop', 'model') else None
                with patch.object(self.service.tasks, method, **({'new_callable': mock_type} if mock_type else {}), return_value={'ok': True}) as call:
                    self.assertEqual((await self.request('POST', '/api/workers/' + worker + '/' + action + '?' + selector, body))[0], 200)
                    self.assertEqual(call.call_args.args[0], expected)
            with patch.object(self.service.web, 'diff', new_callable=AsyncMock, return_value={'text': 'diff'}) as diff:
                self.assertEqual((await self.request(path='/api/workers/' + worker + '/diff?' + selector))[0], 200)
                diff.assert_awaited_once_with(expected)
        self.service.tasks.rename(tid, 'Arch')
        self.assertEqual((await self.request(path='/api/workers/Gate-2'))[0], 404)
        self.assertEqual((await self.request(path='/api/workers/arch?metadata=1'))[2]['task']['id'], tid)
        self.assertEqual((await self.request(path='/api/workers/' + tid + '?by=id&metadata=1'))[2]['task']['id'], tid)
        self.assertEqual((await self.request(path='/api/workers/Arch?by=typo'))[0], 400)
        for path in ['/api/tasks/' + tid, '/api/workers/missing', '/api/workers/1invalid', '/api/workers/' + 'A' * 25]:
            self.assertEqual((await self.request(path=path))[0], 404)

    async def test_worker_summaries_latest_assignment_and_new_router_tools(self):
        with patch.object(self.service.tasks, 'schedule'):
            tid = self.start_task('Initial work', code_name='Gate', role='Entry points')
            self.service.tasks.send(tid, 'Latest ' + '界' * 250)
        for task in [(await self.request())[2]['tasks'][0],
                     (await self.request(path='/api/workers/gate?metadata=1'))[2]['task']]:
            self.assertEqual(task['code_name'], task['worker_name'])
            self.assertEqual(task['role'], 'Entry points')
            self.assertEqual(task['assignment'], ('Latest ' + '界' * 250)[:200])
        self.store.open_turn()
        for index, tool in enumerate(('worker_hire', 'worker_assign', 'worker_say', 'worker_interrupt')):
            key = 'entry-call-' + str(index)
            self.store.event('router', 'tool', tool='mcp__router__' + tool,
                             input=json.dumps({'worker': 'gAtE', 'text': 'hello'}), call_key=key)
            self.store.event('router', 'tool.completed', call_key=key, ok=True)
        self.store.event('router', 'task.started', task_id=tid)
        self.store.event('router', 'task.message', task_id=tid, text='hello')
        self.service.router.reply('Assigned')
        message = (await self.request(path='/api/conversation'))[2]['items'][-1]
        self.assertEqual(message['tasks'], [tid])
        calls = {step['name']: step for step in message['steps']}
        for tool in ('worker_hire', 'worker_assign', 'worker_say'):
            self.assertTrue(calls['mcp__router__' + tool]['hidden'])
        self.assertFalse(calls['mcp__router__worker_interrupt']['hidden'])
        self.assertEqual(message['workers'][0]['code_name'], 'Gate')
        # A tool-only reference must link the stored Worker, not the public name as a graph key.
        self.store.run("DELETE FROM events WHERE type IN ('task.started','task.message')")
        message = (await self.request(path='/api/conversation'))[2]['items'][-1]
        self.assertEqual(message['tasks'], [tid])

    async def test_tool_targets_survive_renames_and_ignore_conflicting_input_names(self):
        tid = self.store.add_task('Entry work', 'brief', 'work', None, 'session', '', '', code_name='Gate')
        other = self.store.add_task('Other work', 'brief', 'work', None, 'other', '', '', code_name=tid)
        self.store.open_turn()
        # 1b persists the resolved id in event data, separate from the mutable tool input.
        event = self.store.event('router', 'tool', tool='mcp__router__worker_interrupt')
        self.store.run('UPDATE events SET data=? WHERE id=?', json.dumps({
            'tool': 'mcp__router__worker_interrupt', 'task_id': tid,
            'input': json.dumps({'worker': 'Gate'})}), event)
        self.service.router.reply('Interrupted')
        self.service.tasks.rename(tid, 'Arch')
        self.store.run("DELETE FROM events WHERE type='task.renamed'")
        message = (await self.request(path='/api/conversation'))[2]['items'][-1]
        self.assertEqual(message['tasks'], [tid])
        self.assertEqual(message['workers'][0]['code_name'], 'Arch')
        self.assertEqual(message['steps'][0]['task_id'], tid)
        # Even when a code name resembles an id, the durable field is never resolved as a name.
        self.store.run('UPDATE events SET data=? WHERE id=?', json.dumps({
            'tool': 'mcp__router__worker_interrupt', 'task_id': tid,
            'input': json.dumps({'worker': self.store.task(other)['code_name']})}), event)
        self.assertEqual((await self.request(path='/api/conversation'))[2]['items'][-1]['tasks'], [tid])

    async def test_usage_returns_current_code_names_without_changing_aggregation_keys(self):
        from super_super_harness.channels import usage
        tid = self.store.add_task('Entry work', 'brief', 'work', None, 'session', '', '', code_name='Gate')
        start, end = usage.window({})
        before = usage.read_report(self.cfg, {'running': 0, 'limit': 2}, start, end)
        self.assertEqual(before['tasks'][0]['task_id'], tid)
        self.assertEqual(before['tasks'][0]['code_name'], 'Gate')
        self.service.tasks.rename(tid, 'Arch')
        after = usage.read_report(self.cfg, {'running': 0, 'limit': 2}, start, end)
        self.assertEqual(after['tasks'][0]['code_name'], 'Arch')
        self.assertEqual(before['summary'], after['summary'])
        self.assertTrue(all(row['code_name'] == 'Router' for row in after['tasks'] if row['task_id'] == 'router'))

    async def test_diff_fork_point_and_size_limit(self):
        repo = self.home / "source"
        repo.mkdir()
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "test@example.test")
        git("config", "user.name", "Test")
        (repo / "a").write_text("base\n")
        git("add", ".")
        git("commit", "-qm", "base")
        task = self.start_task("SLEEP 20", repo=str(repo))
        await self.worker_ready(task)
        t = self.store.task(task)
        cwd = Path(t["cwd"])
        (cwd / "a").write_text("changed\n")
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qam", "changed"], cwd=cwd, check=True, capture_output=True)
        # Advance source independently: the stored fork must not move.
        (repo / "source-only").write_text("unrelated\n")
        git("add", ".")
        git("commit", "-qm", "advance source")
        self.assertTrue(t['branch'].startswith('w/'))
        self.service.tasks.rename(task, 'Renamed')
        self.assertEqual(self.store.task(task)['branch'], t['branch'])
        result = (await self.request(path="/api/workers/Renamed/diff"))[2]
        self.assertIn("+changed", result["text"])
        self.assertNotIn("source-only", result["text"])
        self.assertEqual(result["base"], t["base_commit"])
        (cwd / "big").write_text("line contents\n" * 50000)
        subprocess.run(["git", "add", "."], cwd=cwd, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "big"], cwd=cwd, check=True, capture_output=True)
        result = (await self.request(path="/api/workers/%s/diff" % task))[2]
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode()), DIFF_LIMIT)

    async def test_shutdown_waits_for_inflight_router_spawn(self):
        from super_super_harness.adapters.claude_code import Process
        router = self.service.router
        await router._kill()
        entered, release = asyncio.Event(), asyncio.Event()
        original = Process.start
        async def delayed(process):
            entered.set()
            await release.wait()
            return await original(process)
        with patch.object(Process, "start", delayed):
            spawning = asyncio.create_task(router.spawn())
            await entered.wait()
            stopping = asyncio.create_task(self.halt())
            await asyncio.sleep(.05)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(spawning, stopping), 5)
        self.cfg.limits["busy_interrupt_seconds"] = 0
        await router._busy_guard(1)  # must not touch the now-closed database
        self.assertFalse(router.spawning)

    async def test_router_shutdown_does_not_wait_for_socket_client_lifetime(self):
        from super_super_harness.adapters.claude_code import Process
        entered, release = asyncio.Event(), asyncio.Event()
        original = Process.start
        async def delayed(process):
            entered.set()
            await release.wait()
            return await original(process)
        reader, writer = await asyncio.open_unix_connection(str(self.cfg.sock))
        try:
            with patch.object(Process, "start", delayed):
                writer.write((json.dumps({"token": self.service.owner_token, "method": "router_reset"}) + "\n").encode())
                await writer.drain()
                await entered.wait()
                closing = asyncio.create_task(self.service.router.shutdown())
                await asyncio.sleep(.05)
                release.set()
                await asyncio.wait_for(closing, 5)
                self.assertIn("result", json.loads(await reader.readline()))
                # The client is deliberately still connected and awaiting more RPCs.
                self.assertFalse(writer.is_closing())
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_live_snapshot_refresh_does_not_evict_paged_tab(self):
        self.service.tasks.closed = True
        task = self.start_task("brief")
        first = (await self.request(path="/api/workers/%s" % task))[2]
        await self.request(path="/api/workers/%s?snapshot=%s&offset=0" % (task, first["snapshot"]))
        second = (await self.request(path="/api/workers/%s" % task))[2]
        for _ in range(8):
            second = (await self.request(path="/api/workers/%s?refresh=%s" % (task, second["snapshot"])))[2]
        self.assertEqual(len(self.service.web.snapshots), 2)
        self.assertEqual((await self.request(path="/api/workers/%s?snapshot=%s&offset=0" % (task, first["snapshot"])))[0], 200)

    async def test_closing_paged_timelines_releases_snapshot_slots(self):
        self.service.tasks.closed = True
        for i in range(6):
            task = self.start_task("brief", name="task %d" % i)
            page = (await self.request(path="/api/workers/%s" % task))[2]
            await self.request(path="/api/workers/%s?snapshot=%s&offset=0" % (task, page["snapshot"]))
            self.assertTrue(self.service.web.snapshots[page["snapshot"]]["pinned"])
            result = await self.request("POST", "/api/timeline/release", {"snapshot": page["snapshot"]})
            self.assertEqual(result[0], 200)
            self.assertEqual(len(self.service.web.snapshots), 0)

    async def test_parallel_instance_cookies_are_port_scoped(self):
        from super_super_harness.channels.web import Web
        from types import SimpleNamespace
        from dataclasses import replace
        other_home = self.home / "other-instance"
        other_home.mkdir()
        cfg = replace(self.cfg, home=other_home, web=dict(self.cfg.web, port=free_port()))
        other = Web(SimpleNamespace(cfg=cfg, store=self.store))
        cookie2 = other.cookie_name + "=" + other.session_cookie()
        both = self.cookie + "; " + cookie2
        self.assertNotEqual(other.cookie_name, self.service.web.cookie_name)
        self.assertTrue(other.authorized({"cookie": both}))
        self.assertTrue(self.service.web.authorized({"cookie": both}))
        self.assertFalse(other.authorized({"cookie": self.cookie}))

    async def test_avatar_is_only_configured_local_raster(self):
        avatar = self.home / "avatar.png"
        avatar.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.cfg.web["user_avatar"] = str(avatar)
        self.assertTrue((await self.request())[2]["user"]["avatar"])
        code, headers, body = await self.request(path="/api/avatar")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.cfg.web["user_avatar"] = str(self.home / "owner-token")
        self.assertEqual((await self.request(path="/api/avatar"))[0], 404)


class FrontendAvailability(unittest.TestCase):
    def test_missing_node_skips_frontend_regressions(self):
        import sys
        with tempfile.TemporaryDirectory() as empty_path:
            script = ("import unittest,sys; "
                      "suite=unittest.defaultTestLoader.loadTestsFromName('tests.test_web.FrontendRegressions'); "
                      "result=unittest.TextTestRunner(verbosity=2).run(suite); "
                      "sys.exit(not result.wasSuccessful())")
            result = subprocess.run([sys.executable, '-c', script],
                                    env=dict(os.environ, PATH=empty_path),
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('skipped=46', result.stderr)
        self.assertNotIn('FileNotFoundError', result.stderr)


@unittest.skipUnless(shutil.which('node'), 'Node.js is only needed for frontend regression tests')
class FrontendRegressions(unittest.TestCase):
    def run_render_test(self, name):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(["node", str(root / "tests/web_regressions.js"),
                                 str(PAGE), name], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_chat_fallback_busy_pending_idle_and_unchanged_render(self):
        self.run_render_test('fallback')

    def test_chat_hung_reconciliation_aborts_and_releases_gate(self):
        self.run_render_test('fallback-timeout')

    def test_chat_metadata_update_retains_message_nodes(self):
        self.run_render_test('fallback-metadata')

    def test_chat_empty_fallback_does_not_repaint(self):
        self.run_render_test('fallback-empty')

    def test_chat_focused_chip_keeps_task_identity_when_chips_reorder(self):
        self.run_render_test('chip-identity')

    def test_markdown_unclosed_link_scanning_is_bounded(self):
        self.run_render_test('markdown-bounds')

    def test_markdown_underscore_boundaries_preserve_identifiers_and_urls(self):
        self.run_render_test('markdown-word-boundaries')

    def test_markdown_ordered_start_continuations_and_blank_lines(self):
        self.run_render_test('markdown-ordered-continuations')

    def test_markdown_line_separator_failure_paths_are_bounded(self):
        self.run_render_test('markdown-unicode-bounds')

    def test_markdown_whitespace_64k_heading_is_bounded(self):
        self.run_render_test('markdown-space-heading-65536')

    def test_markdown_whitespace_256k_heading_is_bounded(self):
        self.run_render_test('markdown-space-heading-262144')

    def test_markdown_whitespace_64k_delimiter_is_bounded(self):
        self.run_render_test('markdown-space-delimiter-65536')

    def test_markdown_whitespace_256k_delimiter_is_bounded(self):
        self.run_render_test('markdown-space-delimiter-262144')

    def test_markdown_table_preserves_code_pipes_escaped_pipes_and_extra_cells(self):
        self.run_render_test('markdown-table-cells')

    def test_markdown_wide_data_rows_have_linear_output(self):
        self.run_render_test('markdown-wide-row')

    def test_markdown_wide_headers_have_linear_output(self):
        self.run_render_test('markdown-wide-header')

    def test_markdown_table_grid_limit_preserves_source_and_boundary(self):
        self.run_render_test('markdown-table-grid-limit')

    def test_markdown_two_space_sublist_ordinals_match_source(self):
        self.run_render_test('markdown-ordered-two-space-restart')

    def test_markdown_three_space_sublist_ordinals_match_source(self):
        self.run_render_test('markdown-ordered-three-space-restart')

    def test_markdown_tab_sublist_ordinals_match_source(self):
        self.run_render_test('markdown-ordered-tab-restart')

    def test_markdown_two_digit_sublist_ordinals_match_source(self):
        self.run_render_test('markdown-ordered-two-digit-restart')

    def test_markdown_backtick_info_does_not_consume_inline_code(self):
        self.run_render_test('markdown-fence-inline-prefix')

    def test_markdown_indented_ordered_items_end_current_list(self):
        self.run_render_test('markdown-ordered-sublist-boundary')

    def test_markdown_fence_accepts_info_strings_without_changing_closers(self):
        self.run_render_test('markdown-fence-info')

    def test_router_markdown_and_user_plain_text(self):
        self.run_render_test('markdown')

    def test_router_markdown_escapes_scripts_unsafe_links_and_attributes(self):
        self.run_render_test('markdown-xss')

    def test_tool_calls_only_on_router_collapsed_and_escaped(self):
        self.run_render_test('tool-calls')

    def test_401_explains_login_and_recovers_stream_without_reload(self):
        self.run_render_test('auth-recovery')

    def test_router_file_editor_escapes_and_preserves_drafts(self):
        self.run_render_test('files-draft')

    def test_router_file_editor_conflict_retains_version_and_draft(self):
        self.run_render_test('files-conflict')

    def test_tasks_scroll_survives_reminder_repaint(self):
        self.run_render_test("scroll")

    def test_cancel_confirmation_names_reminder(self):
        self.run_render_test("cancel")

    def test_scheduled_run_is_addressed_to_router(self):
        self.run_render_test("run")

    def test_worker_title_is_rendered_and_escaped(self):
        self.run_render_test("worker")

    def test_usage_unknown_quota_bounds_and_escaping(self):
        self.run_render_test("usage")

    def run_communication_test(self, body):
        page = PAGE
        script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map(), listeners = {};
function element() {
  return {dataset:{}, textContent:'', innerHTML:'', open:true, children:[],
    classList:{toggle(){}}, setAttribute(k,v){ this[k]=v; }, getAttribute(k){return this[k] || '';},
    contains(){return false;}, querySelectorAll(){return [];}, querySelector(){return element();},
    append(...nodes){this.children.push(...nodes);}, remove(){this.removed=true;},
    getBoundingClientRect(){return {top:0,bottom:208};},
    animate(frames,options){
      this.frames=frames; this.options=options;
      return this.animation={finished:new Promise(()=>{}),cancel(){this.cancelled=true;}};
    }};
}
const document = {hidden:false, activeElement:null,
  getElementById(id){if(!elements.has(id)) elements.set(id,element()); return elements.get(id);},
  querySelector(){return element();}, querySelectorAll(){return [];},
  createElementNS(){return element();}, addEventListener(name,fn){listeners[name]=fn;}};
const context = vm.createContext({document, console, element, listeners, assert, Map, Set,
  Date, JSON, Math, Promise, encodeURIComponent, EventSource:{CLOSED:2}, setTimeout(){return 1;}, clearTimeout(){}});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1].split("$('send').onclick")[0],context);
vm.runInContext(`const calls=[], reducedMotion={matches:false}; connected=true; state.view='live';
  const actualFlow=flow; flow=(edge,reverse=false)=>calls.push([edge,reverse]);`, context);
(async()=>{ await vm.runInContext('(async()=>{' + process.argv[2] + '})()',context); })()
  .catch(e=>{console.error(e); process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(page), body], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_communication_verifies_durable_rows_and_does_not_replay(self):
        self.run_communication_test(r"""
const rows=[{id:1,role:'user'}];
reconcileTraffic(rows,new Set(['message']),0);
assert.equal(calls.length,0,'initial history is a baseline, not live traffic');
rows.push({id:2,role:'user'},{id:3,role:'router'},
  {id:4,role:'user',kind:'reminder.run'},{id:5,role:'router',kind:'reminder'});
reconcileTraffic(rows,new Set(),0);
assert.equal(calls.length,0,'POST responses and history refreshes cannot trigger dots');
traffic.streamLatest=100; // A newer invalidation may arrive during reconciliation.
reconcileTraffic(rows,new Set(['message']),0);
assert.deepEqual(calls,[['user',false],['user',true]]);
assert.equal(traffic.latest,5,'an invalidation cursor must not skip unverified live rows');
reconcileTraffic(rows,new Set(['message']),0);
assert.equal(calls.length,2,'duplicate invalidations are not new traffic');
reconcileTraffic([...rows,{id:6,role:'router'}],new Set(['message']),0);
assert.equal(calls.length,3);
""")

    def test_communication_worker_updates_do_not_invent_dispatch(self):
        self.run_communication_test(r"""
data.tasks=[{id:'t1',status:'queued',progress:'',question:''}];
reconcileTraffic([],new Set(['state']),0);
for(const status of ['running','interrupted','running','stopped']) {
  data.tasks[0].status=status; reconcileTraffic([],new Set(['state']),0);
}
assert.equal(calls.length,0,'start/resume/interrupt/stop are not Worker messages');
data.tasks[0].status='running'; data.tasks[0].progress='Verified progress';
reconcileTraffic([],new Set(),0); assert.equal(calls.length,0);
reconcileTraffic([],new Set(['state']),0);
data.tasks[0].status='waiting'; data.tasks[0].question='Continue?';
reconcileTraffic([],new Set(['state']),0);
reconcileTraffic([],new Set(['state']),0);
data.tasks[0].status='completed'; reconcileTraffic([],new Set(['state']),0);
assert.deepEqual(calls,[['t1',true],['t1',true],['t1',true]]);
""")

    def test_communication_reset_stale_sync_and_history_are_quiet(self):
        self.run_communication_test(r"""
reconcileTraffic([{id:1,role:'user'}],new Set(['message']),0);
clearTraffic(true);
reconcileTraffic([{id:2,role:'router'}],new Set(['message']),0);
assert.equal(traffic.ready,false,'a pre-disconnect fetch cannot rearm motion');
reconcileTraffic([{id:2,role:'router'}],new Set(['message']),traffic.epoch);
assert.equal(calls.length,0,'reconnect reconciles without replaying missed traffic');
connected=false;
reconcileTraffic([{id:3,role:'router'}],new Set(['message']),traffic.epoch);
assert.equal(calls.length,0);
connected=true; state.historical=true; traffic.streamLatest=8;
reconcileTraffic([{id:1,role:'user'}],new Set(['message']),traffic.epoch);
state.historical=false;
reconcileTraffic([{id:8,role:'router'}],new Set(['message']),traffic.epoch);
assert.equal(calls.length,0,'returning from history never plays old traffic');
""")

    def test_communication_map_escapes_labels_and_shares_avatar_state(self):
        self.run_communication_test(r"""
data.tasks=[{id:'t1',name:'<img onerror="bad">',status:'running'}];
const nodes=[element(),element(),element()]; nodes[0].dataset.role='user'; nodes[1].dataset.role='router'; nodes[2].dataset.status='running'; nodes[2].dataset.task='t1';
const current=element();current.dataset.current='t1';
[...nodes,current].forEach(n=>{n.classList.toggle=(key,value)=>{n[key]=value;};});
$('commMap').querySelectorAll=selector=>selector==='.comm-node'?nodes:selector==='[data-current]'?[current]:selector==='.router-card'?[nodes[1]]:[];
$('avatarState').textContent='thinking'; renderCommunication();
assert.match($('commMap').innerHTML,/&lt;img/);
assert.doesNotMatch($('commMap').innerHTML,/<img/);
assert.equal(nodes[1].dataset.status,'thinking');
assert.equal(nodes[1]['router-busy'],true); assert.equal(current.on,true);
$('avatarState').textContent='waiting'; renderCommunication();
assert.equal(nodes[1]['router-busy'],false);
connected=false; renderCommunication(); assert.equal(current.on,false);
assert.match($('commCaption').textContent,/Motion paused/);
""")

    def test_communication_particles_follow_edges_and_cancel_without_timers(self):
        self.run_communication_test(r"""
flow=actualFlow;
const svg=element(), path=element(); path.dataset.commEdge='user'; path.ownerSVGElement=svg;
path.getTotalLength=()=>100; path.getPointAtLength=x=>({x,y:28});
$('commMap').querySelectorAll=selector=>selector==='[data-comm-edge]'?[path]:[];
flow('user'); flow('user',true);
const envelopes=svg.children.filter(n=>n.dataset.kind);
assert.equal(envelopes[0].frames[0].transform,'translate(0px,28px)');
assert.equal(envelopes[1].frames[0].transform,'translate(100px,28px)');
assert.equal(envelopes[0].dataset.direction,'to-router');
assert.equal(envelopes[1].dataset.direction,'to-you');
assert.equal(envelopes[0].options.duration,1150);
assert.equal(envelopes[0].options.iterations,undefined,'traffic is one-shot');
assert.ok(envelopes[0].children.some(n=>n.class==='comm-envelope'),'Chat messages are envelopes too');
clearTraffic(true);
assert.equal(traffic.particles.size,0);
assert.ok(svg.children.every(n=>n.removed && n.animation.cancelled));
const count=svg.children.length;
path.getClientRects=()=>[];
flow('user'); assert.equal(svg.children.length,count,'A hidden edge must not consume animation slots');
path.getClientRects=()=>[{}];
$('view-live').getBoundingClientRect=()=>({top:300,bottom:500});
flow('user'); assert.equal(svg.children.length,count,'An offscreen group must not animate');
$('view-live').getBoundingClientRect=()=>({top:0,bottom:208});
reducedMotion.matches=true; flow('user'); assert.equal(svg.children.length,count);
reducedMotion.matches=false;
connected=false; flow('user'); connected=true;
document.hidden=true; flow('user'); document.hidden=false;
state.view='tasks'; flow('user'); state.view='chat'; flow('user'); state.view='live';
$('communication').open=false; flow('user'); $('communication').open=true;
assert.equal(svg.children.length,count);
clearTraffic();
for(let i=0;i<50;i++) { path.dataset.commEdge='t'+i;flow('t'+i); }
assert.equal(traffic.particles.size,8,'bursts have bounded DOM/animation work');
assert.equal(traffic.queue.length,16,'excess events have a bounded queue');
clearTraffic();assert.equal(traffic.queue.length,0);
""")

    def test_communication_sync_retains_event_gate_across_fetch_failure(self):
        self.run_communication_test(r"""
renderHeader=()=>{}; renderActivity=()=>{}; renderReminders=()=>{}; renderTasks=()=>{}; renderThread=()=>{};
source={readyState:1}; const EventSource={CLOSED:2};
data.time=Date.now()/1000;
reconcileTraffic([{id:1,role:'user'}],new Set(['message']),0);
messages.set(1,{id:1,role:'user'}); latest=1;
traffic.events.add('message');
api=async()=>{throw Error('temporary test failure');};
await sync(); assert.equal(calls.length,0); assert.equal(traffic.events.has('message'),true);
api=async path=>path==='state'?data:{items:[{id:2,role:'router'}],next_after:2,has_more:false};
await sync(); assert.deepEqual(calls,[['user',true]]);
await sync(); assert.equal(calls.length,1);
""")

    def test_communication_first_live_message_in_an_empty_conversation(self):
        self.run_communication_test(r"""
renderHeader=()=>{}; renderActivity=()=>{}; renderReminders=()=>{}; renderTasks=()=>{}; renderThread=()=>{};
source={readyState:1}; data.time=Date.now()/1000;
reconcileTraffic([],new Set(['message']),0); // empty initial history has finished loading
traffic.events.add('message');
api=async path=>path==='state'?data:{items:[{id:1,role:'user'}],next_after:1,has_more:false};
await sync();
assert.deepEqual(calls,[['user',false]],'automatic initial-page loading is not user history navigation');
""")

    def test_communication_initial_sync_does_not_skip_later_invalidation(self):
        self.run_communication_test(r"""
renderHeader=()=>{}; renderActivity=()=>{}; renderReminders=()=>{}; renderTasks=()=>{}; renderThread=()=>{};
source={readyState:1}; data.time=Date.now()/1000;
messages.set(1,{id:1,role:'user'}); latest=1;
let release, reached, recent=0;
const waiting=new Promise(resolve=>{reached=resolve;});
api=async path=>{
  if(path==='state') return data;
  if(path==='conversation?limit=100' && ++recent===1) {
    reached(); return new Promise(resolve=>{release=resolve;});
  }
  return {items:[{id:2,role:'router'}],next_after:2,has_more:false};
};
// The first incremental read must not see the later message either.
const originalApi=api;
api=async path=>path.startsWith('conversation?after=') && !recent?
  {items:[],next_after:1,has_more:false}:originalApi(path);
const syncing=sync(); await waiting;
streamEvent('message',{data:'{"latest":2}'});
release({items:[{id:1,role:'user'}],next_after:1,has_more:false});
await syncing;
assert.deepEqual(calls,[['user',true]],'the later row must survive baseline reconciliation');
assert.equal(traffic.latest,2);
""")

    def test_communication_history_exit_with_inflight_sync_is_a_silent_baseline(self):
        self.run_communication_test(r"""
renderHeader=()=>{}; renderActivity=()=>{}; renderReminders=()=>{}; renderTasks=()=>{}; renderThread=()=>{};
source={readyState:1}; data.time=Date.now()/1000;
reconcileTraffic([{id:1,role:'user'}],new Set(['message']),0);
messages.set(1,{id:1,role:'user'}); latest=1; state.historical=true;
let release, reached, stateReads=0, newest=2;
const waiting=new Promise(resolve=>{reached=resolve;});
api=async path=>{
  if(path==='state') {
    if(!stateReads++) {reached(); return new Promise(resolve=>{release=resolve;});}
    return data;
  }
  return {items:[{id:newest,role:'router'}],next_after:newest,has_more:false};
};
const syncing=sync(); await waiting;
await history();
streamEvent('message',{data:'{"latest":2}'}); // delayed until after Back to latest
release(data); await syncing;
assert.equal(calls.length,0,'already displayed history must not replay');
newest=3; traffic.events.add('message'); await sync();
assert.deepEqual(calls,[['user',true]],'later real traffic still animates');
""")

    def test_communication_status_update_preserves_particles_and_node_identity(self):
        self.run_communication_test(r"""
data.tasks=[{id:'t1',name:'Worker',status:'running'}];
const nodes=[element(),element(),element()]; nodes[0].dataset.role='user'; nodes[1].dataset.role='router'; nodes[2].dataset.task='t1';
$('commMap').querySelectorAll=selector=>selector==='.comm-node'?nodes:[];
renderCommunication();
let cancelled=false, removed=false;
traffic.particles.add({animation:{cancel(){cancelled=true;}},node:{remove(){removed=true;}}});
document.activeElement=nodes[2];
const markup=$('commMap').innerHTML;
data.tasks[0].status='waiting'; renderCommunication();
assert.equal($('commMap').innerHTML,markup,'status alone must not rebuild the SVG');
assert.equal(document.activeElement,nodes[2]);
assert.equal(nodes[2].dataset.status,'waiting');
assert.equal(cancelled,false); assert.equal(removed,false); assert.equal(traffic.particles.size,1);
""")

    def test_communication_svg_button_space_key_activates_without_scrolling(self):
        self.run_communication_test(r"""
let opened=[], prevented=0;
openTask=id=>opened.push(id);
const event={key:' ',target:{dataset:{task:'t1'},matches:s=>s==='.comm-node[data-task]'},
  preventDefault(){prevented++;}};
listeners.keydown(event);
listeners.keydown({...event,repeat:true});
assert.deepEqual(opened,['t1']); assert.equal(prevented,2);
""")


class UsageMetadata(unittest.TestCase):
    def test_quota_hook_is_optional_validated_and_allowlisted(self):
        from super_super_harness.channels.usage import quota_snapshot
        self.assertEqual(quota_snapshot('fixture'), {'provider': 'fixture', 'status': 'unconfigured'})
        reader = lambda: {'limit': 1000, 'remaining': 400, 'key': 'PRIVATE-KEY', 'text': 'PRIVATE-BODY'}
        self.assertEqual(quota_snapshot('fixture', {'fixture': reader}),
                         {'provider': 'fixture', 'status': 'available', 'unit': 'tokens', 'limit': 1000, 'remaining': 400})
        for bad in (None, {'limit': True, 'remaining': 1}, {'limit': 10, 'remaining': -1},
                    {'limit': 2, 'remaining': 3}):
            with self.subTest(bad=bad):
                self.assertEqual(quota_snapshot('fixture', {'fixture': lambda: bad}),
                                 {'provider': 'fixture', 'status': 'unknown'})
        def broken():
            raise ValueError('PRIVATE-KEY')
        self.assertNotIn('PRIVATE-KEY', json.dumps(quota_snapshot('fixture', {'fixture': broken})))

    def test_usage_session_rejects_symlink_and_conflicting_model_identity(self):
        from super_super_harness.channels.usage import FILE_BYTES, session_rows
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'fixture.jsonl'
            link = Path(folder) / 'link.jsonl'
            target.write_text('PRIVATE-BODY')
            link.symlink_to(target)
            self.assertEqual(session_rows(link, FILE_BYTES)[:2], ([], True))
            row = {'type': 'assistant', 'timestamp': '2026-01-01T12:00:00Z',
                   'message': {'id': 'same', 'model': 'first-model',
                               'usage': dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)}}
            other = json.loads(json.dumps(row))
            other['message']['model'] = 'other-model'
            target.write_text(''.join(json.dumps(r) + '\n' for r in (row, other, row)))
            rows, incomplete, *_ = session_rows(target, FILE_BYTES)
            self.assertTrue(incomplete)
            self.assertTrue(all(counts is None for _, _, counts in rows))

    def test_usage_descriptor_open_blocks_replacement_symlink_and_fifo(self):
        from super_super_harness.channels import usage
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'fixture.jsonl'
            other = Path(folder) / 'other.jsonl'
            row = {'type': 'assistant', 'timestamp': '2026-01-01T12:00:00Z', 'message': {
                'id': 'fixture', 'model': 'fixture-model',
                'usage': dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=3, cache_creation_input_tokens=2)}}
            target.write_text(json.dumps(row) + '\n')
            other.write_text(json.dumps(row) + '\n')
            original = os.open
            opened = []
            def swap(path, flags, *args, **kwargs):
                opened.append(flags)
                if os.fspath(path) == target.name:
                    target.unlink()
                    target.symlink_to(other)
                return original(path, flags, *args, **kwargs)
            with patch.object(usage.os, 'open', side_effect=swap):
                rows, incomplete, *_ = usage.session_rows(target, usage.FILE_BYTES)
            self.assertEqual(rows, [])
            self.assertTrue(incomplete)
            self.assertTrue(opened)
            self.assertTrue(all(flags & os.O_NOFOLLOW for flags in opened))
            self.assertTrue(all(flags & os.O_NONBLOCK for flags in opened))
            target.unlink()
            os.mkfifo(target)
            # Child deadline keeps a regression from hanging the whole test suite.
            import sys
            script = ('from pathlib import Path; from super_super_harness.channels.usage import session_rows,FILE_BYTES; '
                      'import sys; rows,incomplete,*_=session_rows(Path(sys.argv[1]),FILE_BYTES); '
                      'assert rows==[] and incomplete')
            result = subprocess.run([sys.executable, '-c', script, str(target)], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_usage_report_does_not_migrate_or_write_existing_database(self):
        from super_super_harness.channels import usage
        from tests.support import write_config
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write_config(home, 1, 2)
            cfg = config.load(home)
            store = Store(cfg.db)
            try:
                schema = [tuple(r) for r in store.all('SELECT * FROM sqlite_master ORDER BY name')]
                changes = store.db.total_changes
                start, end = usage.window({'start': ['2026-01-01'], 'days': ['2']})
                usage.report(cfg, usage.plan(cfg, store.db, {'running': 0, 'limit': 2}), start, end)
                self.assertEqual(store.db.total_changes, changes)
                self.assertEqual([tuple(r) for r in store.all('SELECT * FROM sqlite_master ORDER BY name')], schema)
                # Existing read-only clients still see the baseline schema/queries.
                old = sqlite3.connect('file:' + str(cfg.db) + '?mode=ro', uri=True)
                try:
                    self.assertEqual(old.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
                    self.assertEqual(old.execute('SELECT COUNT(*) FROM worker_runs').fetchone()[0], 0)
                finally:
                    old.close()
            finally:
                store.close()


class Migration(unittest.TestCase):
    def test_messages_turn_index_is_used_for_new_and_existing_databases(self):
        from super_super_harness.core.store import SCHEMA
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "router.db"
                if legacy:
                    db = sqlite3.connect(path)
                    db.executescript(SCHEMA.replace("CREATE INDEX IF NOT EXISTS messages_turn ON messages(turn_id);", ""))
                    db.execute("INSERT INTO kv VALUES('web_history_migrated','1')")
                    db.execute("INSERT INTO messages(uuid,role,text,turn_id,task_id,created) VALUES('u','event','hello',7,'t1',1)")
                    db.commit()
                    db.close()
                store = Store(path)
                try:
                    plan = store.all("EXPLAIN QUERY PLAN SELECT task_id FROM messages WHERE turn_id=? AND task_id IS NOT NULL", 7)
                    self.assertTrue(any("USING INDEX messages_turn (turn_id=?)" in row["detail"] for row in plan),
                                    [dict(row) for row in plan])
                    self.assertEqual([r["task_id"] for r in store.all("SELECT task_id FROM messages WHERE turn_id=?", 7)],
                                     ["t1"] if legacy else [])
                finally:
                    store.close()
                store = Store(path)
                self.assertEqual(sum(r["name"] == "messages_turn" for r in store.all("PRAGMA index_list(messages)")), 1)
                store.close()

    def test_existing_database_columns_and_history_are_preserved(self):
        from super_super_harness.core.store import SCHEMA
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.db"
            db = sqlite3.connect(path)
            db.executescript(SCHEMA)
            db.execute("INSERT INTO tasks(id,code_name,name,profile,status,session_id,brief,created,updated,last_active) VALUES('t1','Nova','legacy','work','waiting','s','b',1,2,2)")
            db.execute("INSERT INTO messages(uuid,role,text,channel,created) VALUES('u','user','hello','terminal',1)")
            db.execute("INSERT INTO outbox(channel,kind,text,created) VALUES('terminal','answer','hi',2)")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.task("t1")["state_since"], 2)
            self.assertEqual(store.one("SELECT COUNT(*) n FROM conversation")["n"], 2)
            store.close()
            store = Store(path)
            self.assertEqual(store.one("SELECT COUNT(*) n FROM conversation")["n"], 2)
            store.close()

    def test_default_and_disabled_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(tmp)
            self.assertEqual(cfg.web["port"], 8765)
            self.assertEqual(cfg.web["user_avatar"], "")
