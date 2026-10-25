"""真实本地 HTTP 报文回归，不访问云端、不读取用户设备身份。"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from deploy import device_id
from deploy.device_id import generate_device_id
from deploy.http import BrowserSession, get
from deploy.http_headers import BROWSER_USER_AGENT, browser_headers

ID = 'a' * 32
OTHER_ID = 'b' * 32


class LocalHTTP:
    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder or (lambda *_: (200, {}, b'{}'))
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                owner.requests.append((self.command, self.path, dict(self.headers), body))
                status, headers, content = owner.responder(self.command, self.path, self.headers, body)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BrowserHeadersTests(unittest.TestCase):
    def setUp(self):
        self.identity = self.enterContext(patch('deploy.http_headers.request_device_id', return_value=ID))

    def assert_headers(self, headers, identity=ID):
        headers = requests.structures.CaseInsensitiveDict(headers)
        self.assertEqual(headers['User-Agent'], BROWSER_USER_AGENT)
        self.assertEqual(headers['X-AzurPilot-Ture'], 'true')
        self.assertEqual(headers['X-AzurPilot-ID'], identity)
        self.assertEqual(headers['Cloudflare-Workers-Version-Key'], identity)
        self.assertNotIn('Origin', headers)
        self.assertNotIn('Referer', headers)

    def test_actual_request_cannot_override_protected_headers(self):
        with LocalHTTP() as server:
            result = get(server.url, headers={'user-agent': 'python', 'x-azurpilot-id': '',
                                              'X-AzurPilot-Ture': 'false', 'Origin': 'https://github.com',
                                              'Referer': 'https://example.com'})
            result.close()
        self.assert_headers(server.requests[0][2])
        self.assertEqual(server.requests[0][2]['Accept'], 'application/json')

    def test_cross_host_redirect_keeps_identity_and_next_request_refreshes_it(self):
        with LocalHTTP() as target:
            def redirect(*_):
                self.identity.return_value = OTHER_ID
                return 302, {'Location': target.url + '/asset'}, b''

            with LocalHTTP(redirect) as source, BrowserSession(binary=True, legacy_device=True) as session:
                session.get(source.url).close()
                session.get(target.url + '/next').close()
        self.assert_headers(source.requests[0][2], ID)
        self.assert_headers(target.requests[0][2], ID)
        self.assert_headers(target.requests[1][2], OTHER_ID)
        self.assertEqual(target.requests[0][2]['X-Device-Id'], ID)
        self.assertEqual(target.requests[0][2]['Accept-Encoding'], 'identity')

    def test_head_and_empty_identity_are_rejected_before_network(self):
        with BrowserSession() as session, self.assertRaises(ValueError):
            session.prepare_request(requests.Request('HEAD', 'http://127.0.0.1/'))
        self.identity.return_value = ''
        with self.assertRaises(ValueError):
            browser_headers()

    def test_json_identity_cannot_drift_between_payload_and_sending(self):
        with LocalHTTP() as server, BrowserSession() as session:
            self.identity.return_value = OTHER_ID
            with self.assertRaises(ValueError):
                session.post(server.url, json={'device_id': ID})
        self.assertEqual(server.requests, [])

    def test_transport_retry_preserves_prepared_identity(self):
        attempts = []

        def respond(*_):
            attempts.append(True)
            self.identity.return_value = OTHER_ID
            return (503 if len(attempts) == 1 else 200), {}, b'{}'

        with LocalHTTP(respond) as server, BrowserSession(legacy_device=True) as session:
            session.mount('http://', HTTPAdapter(max_retries=Retry(total=1, status_forcelist=(503,))))
            session.get(server.url).close()
        self.assertEqual(len(server.requests), 2)
        for _, _, headers, _ in server.requests:
            self.assert_headers(headers)
            self.assertEqual(headers['X-Device-Id'], ID)


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'log/device_id.json'
        self.generator = self.enterContext(patch('deploy.device_id.generate_device_id', return_value=ID))

    def write_identity(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({'device_id': value}), encoding='utf-8')

    def test_first_deployment_initializes_same_algorithm_identity(self):
        self.assertEqual(device_id.request_device_id(self.root), ID)
        self.assertEqual(json.loads(self.path.read_text())['device_id'], ID)
        self.assertEqual(device_id.request_device_id(self.root), ID)
        self.generator.assert_called_once_with()

    def test_valid_existing_identity_does_not_probe_hardware(self):
        self.write_identity(OTHER_ID)
        self.assertEqual(device_id.request_device_id(self.root), OTHER_ID)
        self.generator.assert_not_called()

    def test_fingerprint_order_and_hash_algorithm_are_preserved(self):
        fingerprint = 'baseboard.serialnumber=board|cpu.processorid=cpu|diskdrive.serialnumber=disk|platform=host-AMD64'
        with patch('deploy.device_id.platform.system', return_value='Windows'), \
                patch('deploy.device_id._wmic_query', side_effect=['board', 'cpu', 'None', 'disk']), \
                patch('deploy.device_id.platform.node', return_value='host'), \
                patch('deploy.device_id.platform.machine', return_value='AMD64'):
            self.assertEqual(device_id.collect_hardware_fingerprint(), fingerprint)
        with patch('deploy.device_id.collect_hardware_fingerprint', return_value=fingerprint):
            self.assertEqual(generate_device_id(), '164490d659278b0f3fcdc24b95d32cd0')

    def test_old_or_damaged_identity_is_not_overwritten_before_runtime_migration(self):
        for data in ('{"device_id":"' + 'c' * 64 + '"}', '{broken'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(data, encoding='utf-8')
            self.assertEqual(device_id.request_device_id(self.root), ID)
            self.assertEqual(self.path.read_text(), data)

    def test_runtime_migration_retains_old_identity_and_refreshes_headers(self):
        from module.base import device_id as runtime

        self.write_identity(ID)
        self.assertEqual(device_id.request_device_id(self.root), ID)
        with patch.object(runtime, 'generate_device_id', return_value=OTHER_ID), \
                patch.object(runtime, '_start_refresh_timer'), patch.object(runtime, '_old_device_id', None), \
                patch.object(device_id, '_runtime_id', None):
            self.assertEqual(runtime._init_device_id(self.root), OTHER_ID)
            self.assertEqual(runtime.get_old_device_id(), ID)
            self.assertEqual(browser_headers()['X-AzurPilot-ID'], OTHER_ID)
            self.assertEqual(json.loads(self.path.read_text())['device_id'], OTHER_ID)


if __name__ == '__main__':
    unittest.main()
