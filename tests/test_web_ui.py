"""Isolated HTTP contract tests for the local Meeting Desk interface."""
import io
import socket
import json
import tempfile
import threading
import unittest
import types
from pathlib import Path
from unittest.mock import patch

from src.desktop.service import DesktopService
from src.web_ui.server import WebServer


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        repo = base / 'repo'
        repo.mkdir()
        self.root = base / 'meetings'
        config = base / 'config'
        data = base / 'data'
        config.mkdir()
        data.mkdir()
        env = patch.dict('os.environ', {'WHISPER_SCRIPT_CONFIG_DIR':str(config),
                                        'WHISPER_SCRIPT_DATA_DIR':str(data),
                                        'MEETING_RECORDS_DIR':str(self.root),
                                        'PROMPT_NOTES_DIR':str(base / 'notes')})
        env.start()
        self.addCleanup(env.stop)
        service_factory = lambda: DesktopService(repo, {'output_root':str(self.root), 'whisper_root':str(base / 'whisper')})
        self.server = WebServer.__new__(WebServer)
        self.server.server_address = ('127.0.0.1', 8765)
        self.server.server_port = 8765
        self.server.token = 'test-session-token'
        self.server.service_factory = service_factory
        self.server.process_lock = threading.Lock()
        self.server.processes = {}
        self.port = 8765
        self.host = '127.0.0.1:8765'

    def request(self, method, path, body=None, token=None, host=None, origin=None):
        from src.web_ui.server import RequestHandler
        headers = {'Host':host or self.host}
        if token is not None:
            headers['X-Meeting-Desk-Token'] = token
        if origin is not None:
            headers['Origin'] = origin
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
            headers['Content-Length'] = str(len(body))
        else:
            body = b''
        request = (f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in headers.items()) + '\r\n').encode() + body
        class MemoryConnection:
            def __init__(self, raw):
                self.input = io.BytesIO(raw)
                self.output = io.BytesIO()
            def makefile(self, mode, buffering=-1):
                return self.input if 'r' in mode else self.output
            def sendall(self, value):
                self.output.write(value)
            def close(self):
                pass
        connection = MemoryConnection(request)
        RequestHandler(connection, ('127.0.0.1', 12345), self.server)
        raw = connection.output.getvalue()
        status = int(raw.split(b' ',2)[1])
        return status, raw.split(b'\r\n\r\n',1)[1]

    def test_binds_only_ipv4_loopback(self):
        with patch('src.web_ui.server.ThreadingHTTPServer.__init__', return_value=None) as init:
            WebServer(8765, self.server.service_factory)
        self.assertEqual(init.call_args.args[0], ('127.0.0.1', 8765))
        self.assertEqual(WebServer.address_family, socket.AF_INET)

    def test_missing_and_wrong_tokens_rejected(self):
        for token in (None, 'wrong'):
            status, _ = self.request('POST','/api/action',{'action':'list'},token)
            self.assertIn(status, (401,403))

    def test_bad_host_and_origin_rejected(self):
        for host, origin in [(f'evil.example:{self.port}',None), (self.host,'https://evil.example'),
                             (self.host,f'http://localhost:{self.port}')]:
            status, _ = self.request('POST','/api/action',{'action':'list'},self.server.token,host,origin)
            self.assertEqual(status,403)

    def test_read_only_actions(self):
        for action, key in [('list','meetings'),('list_jobs','jobs'),('environment','settings')]:
            status, raw = self.request('POST','/api/action',{'action':action},self.server.token,
                                       origin='http://'+self.host)
            self.assertEqual(status,200)
            result = json.loads(raw)
            self.assertTrue(result['ok'])
            self.assertIn(key,result)

    def test_environment_reads_settings_again_for_each_request(self):
        settings_path = Path(self.temp.name) / 'config' / 'desktop_settings.json'
        settings_path.write_text(json.dumps({'model':'small'}), encoding='utf-8')
        status, raw = self.request('POST','/api/action',{'action':'environment'},self.server.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['settings']['model'], 'small')

        settings_path.write_text(json.dumps({'model':'large-v3'}), encoding='utf-8')
        status, raw = self.request('POST','/api/action',{'action':'environment'},self.server.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['settings']['model'], 'large-v3')

    def test_outside_meeting_folder_is_rejected_by_action(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        status, raw = self.request('POST','/api/action',{'action':'preview','folder':str(outside),'kind':'raw'},self.server.token)
        self.assertEqual(status,400)
        self.assertIn('目前會議資料夾',json.loads(raw)['error'])

    def test_static_homepage_loads(self):
        status, raw = self.request('GET','/')
        self.assertEqual(status,200)
        self.assertIn('Meeting Desk'.encode(),raw)

    def test_process_progress_and_cancellation(self):
        folder = self.root / 'sample'
        folder.mkdir(parents=True)
        started = threading.Event()
        process_service = self.server.service_factory()
        factory_calls = []
        dispatched_services = []
        self.server.service_factory = lambda: factory_calls.append(process_service) or process_service

        def fake_dispatch(service, request, cancellation=None, progress=None):
            dispatched_services.append(service)
            progress({'message':'測試進度'})
            started.set()
            for _ in range(200):
                if cancellation.is_cancelled():
                    break
                threading.Event().wait(.01)
            cancellation.throw_if_cancelled('process')
            return {'meeting':{'id':str(folder)}}

        with patch('src.web_ui.server.dispatch', side_effect=fake_dispatch):
            status, raw = self.request('POST','/api/process',
                                       {'action':'process','folder':str(folder)},self.server.token)
            self.assertEqual(status,202)
            job_id = json.loads(raw)['id']
            self.assertTrue(started.wait(1))
            self.assertEqual(factory_calls, [process_service])
            self.assertEqual(dispatched_services, [process_service])
            status, raw = self.request('GET','/api/process/'+job_id,token=self.server.token)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(raw)['events'][0]['message'],'測試進度')
            status, _ = self.request('POST','/api/process/'+job_id+'/cancel',{},self.server.token)
            self.assertEqual(status,200)
            for _ in range(100):
                _, raw = self.request('GET','/api/process/'+job_id,token=self.server.token)
                result = json.loads(raw)
                if result['done']:
                    break
                threading.Event().wait(.01)
            self.assertTrue(result['done'])
            self.assertTrue(result['result']['cancelled'])

    def test_no_shell_launch_or_shell_true_in_new_program(self):
        source = Path(__file__).resolve().parents[1] / 'src' / 'web_ui'
        for path in source.rglob('*'):
            if path.suffix not in {'.py','.js','.html','.css'}:
                continue
            value = path.read_text(encoding='utf-8')
            self.assertNotIn('shell=True',value)
            self.assertNotIn('bash',value.lower())
            self.assertNotIn('subprocess',value)

    def test_static_files_use_local_resources_and_page_dialogs(self):
        source = Path(__file__).resolve().parents[1] / 'src' / 'web_ui' / 'static'
        for path in source.iterdir():
            value = path.read_text(encoding='utf-8')
            for forbidden in ('prompt(', 'confirm(', 'alert(', 'http://', 'https://'):
                self.assertNotIn(forbidden, value, f'{path.name}: {forbidden}')
        html = (source / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<dialog id="confirmDialog"', html)
        self.assertIn('id="jobsToggle"', html)
        self.assertIn('id="meetingSearch"', html)

    def test_webview_is_optional_only(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('pywebview', (root / 'requirements-webview.txt').read_text())
        for path in [root / 'setup.py', *root.glob('requirements*.txt')]:
            if path.name == 'requirements-webview.txt':
                continue
            self.assertNotIn('pywebview', path.read_text(encoding='utf-8'))

    def test_app_window_uses_token_url_and_stops_server(self):
        from src.web_ui import __main__ as entry
        calls = []
        class FakeServer:
            url = 'http://127.0.0.1:7777/?token=temporary-session'
            def __init__(self, port=8765):
                calls.append(('server',port))
            def serve_forever(self):
                calls.append(('serve',))
            def shutdown(self):
                calls.append(('shutdown',))
            def server_close(self):
                calls.append(('close',))
        fake_webview = types.SimpleNamespace(
            create_window=lambda *args, **kwargs: calls.append(('window',args,kwargs)),
            start=lambda: calls.append(('start',)))
        with patch.dict('sys.modules', {'webview':fake_webview}), \
             patch.object(entry, 'WebServer', FakeServer), \
             patch.object(entry.webbrowser, 'open') as browser:
            self.assertEqual(entry.main(['--app']), 0)
        browser.assert_not_called()
        window = next(item for item in calls if item[0] == 'window')
        self.assertEqual(window[1][:2], ('Meeting Desk', FakeServer.url))
        self.assertIn(('shutdown',), calls)
        self.assertIn(('close',), calls)
        self.assertLess(calls.index(('start',)), calls.index(('shutdown',)))

    def test_missing_webview_reports_traditional_chinese_error(self):
        from src.web_ui import __main__ as entry
        with patch.dict('sys.modules', {'webview':None}), \
             patch.object(entry, 'WebServer') as server, \
             patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertNotEqual(entry.main(['--app']), 0)
        server.assert_not_called()
        self.assertIn('requirements-webview.txt', output.getvalue())
        self.assertIn('瀏覽器模式', output.getvalue())


if __name__ == '__main__':
    unittest.main()
