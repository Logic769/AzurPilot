"""预构建前端、ADB 和下载回退的本地报文及完整性验证。"""

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from deploy import frontend
from deploy.downloads import cache_download
from module.device.connection_attr import ConnectionAttr
from tests import test_cloud_http
from tests.test_cloud_http import ID, OTHER_ID, LocalHTTP


class DownloadWireTests(unittest.TestCase):
    assert_headers = test_cloud_http.BrowserHeadersTests.assert_headers

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.identity = self.enterContext(patch('deploy.http_headers.request_device_id', return_value=ID))

    def assert_binary_requests(self, *servers):
        for server in servers:
            for method, _, headers, _ in server.requests:
                self.assertEqual(method, 'GET')
                self.assert_headers(headers)
                self.assertEqual(headers['Accept'], '*/*')
                self.assertEqual(headers['Accept-Encoding'], 'identity')

    def test_atx_bad_archive_fallback_and_redirect_keep_headers_and_valid_archive(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo('atx-agent')
            info.size = 6
            archive.addfile(info, io.BytesIO(b'binary'))
        content = stream.getvalue()
        with LocalHTTP(lambda *_: (200, {}, content)) as target:
            def respond(_method, path, *_):
                if path == '/bad':
                    self.identity.return_value = OTHER_ID
                    return 200, {}, b'not-a-gzip'
                return 302, {'Location': target.url + '/archive'}, b''

            with LocalHTTP(respond) as mirror:
                path = self.root / 'atx-agent_linux.tar.gz'
                cache_download(mirror.url + '/bad', path, fallback_url=mirror.url + '/redirect')
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual([item[1] for item in mirror.requests], ['/bad', '/redirect'])
        self.assert_binary_requests(mirror, target)

    def test_frontend_probe_uses_get_and_download_keeps_archive_hash_validation(self):
        directory = self.root / 'frontend'
        directory.mkdir()
        (directory / 'package.json').write_text('{}', encoding='utf-8')
        (self.root / 'BUILD_MANIFEST').write_text(json.dumps({'azurpilot_commit': 'a' * 40}), encoding='utf-8')
        fingerprint = frontend.source_fingerprint(directory)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:xz') as archive:
            for name, content in (('dist/index.html', b'<html/>'),
                                  ('dist/.source-fingerprint', fingerprint.encode())):
                item = tarfile.TarInfo(name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
        content = stream.getvalue()
        digest = hashlib.sha256(content).hexdigest().encode()
        with LocalHTTP(lambda _method, path, *_: (200, {}, digest if path.endswith('.sha256') else content)) as server:
            with patch.dict(os.environ, {frontend.ANDROID_DIST_BASE_ENV: server.url}):
                self.assertTrue(frontend.android_dist_ready('a' * 40, self.root))
                frontend._android_fetch_dist(directory, self.root)
        self.assertEqual((directory / 'dist/index.html').read_bytes(), b'<html/>')
        self.assertEqual(len(server.requests), 3)
        self.assert_binary_requests(server)

    def test_adb_download_redirect_keeps_headers_and_extracts_platform_archive(self):
        content = io.BytesIO()
        executable = 'adb.exe' if os.name == 'nt' else 'adb'
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('platform-tools/' + executable, b'fixture-adb')
        with LocalHTTP(lambda *_: (200, {}, content.getvalue())) as target:
            with LocalHTTP(lambda *_: (302, {'Location': target.url + '/platform.zip'}, b'')) as source:
                with patch('module.device.connection_attr.platform_tools_url', return_value=source.url):
                    destination = self.root / 'tools' / executable
                    result = ConnectionAttr.download_adb_binary(object(), destination)
        self.assertEqual(Path(result).read_bytes(), b'fixture-adb')
        self.assert_binary_requests(source, target)

    def test_frontend_truncated_download_is_not_accepted(self):
        response = io.BytesIO(b'short')
        response.headers = {'Content-Length': '100'}
        with patch('deploy.frontend.urllib.request.urlopen', return_value=response):
            with self.assertRaises(RuntimeError):
                frontend._android_download('https://fixture.example/asset', self.root / 'bad.tar.xz')


if __name__ == '__main__':
    unittest.main()
