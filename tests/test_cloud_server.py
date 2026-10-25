"""独立自建服务的请求验证、事务、去重及并发回归；只使用临时 SQLite。"""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from starlette.testclient import TestClient

from deploy.cloud_contract import CL1_PATH, MAINTENANCE_PATH, SERVER_PATH
from deploy.cloud_server import create_app
from deploy.cloud_store import SnapshotConflict, SnapshotStore
from tests.test_cloud_api_v2 import ID, NOW, PAYLOAD

HEADERS = {'X-AzurPilot-Ture': 'true', 'X-AzurPilot-ID': ID, 'Cloudflare-Workers-Version-Key': ID}


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        self.store = SnapshotStore(self.directory / 'cloud.sqlite', clock=lambda: self.now)

    def test_retry_returns_original_receipt_without_accumulating_snapshot(self):
        first = self.store.accept(PAYLOAD)
        self.now += timedelta(hours=1)
        retry = self.store.accept(dict(reversed(list(PAYLOAD.items()))))
        self.assertEqual(retry, {**first, 'duplicate': True})
        self.assertEqual(first['snapshot']['battle_rounds'], 600)
        self.assertEqual(first['snapshot']['sortie_cost'], 72000)
        self.assertEqual(first['snapshot']['akashi_probability'], 0.1)
        self.assertEqual(first['snapshot']['average_stamina'], 70)
        self.assertEqual(first['snapshot']['net_stamina_gain'], 4200)
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)

    def test_id_conflict_and_stale_snapshot_leave_latest_unchanged(self):
        self.store.accept(PAYLOAD)
        with self.assertRaises(SnapshotConflict) as raised:
            self.store.accept({**PAYLOAD, 'battle_count': 1400})
        self.assertEqual(raised.exception.code, 'REQUEST_CONFLICT')
        newer = {**PAYLOAD, 'request_id': str(uuid4()), 'collected_at': '2026-10-26T11:00:00Z', 'battle_count': 1400}
        self.store.accept(newer)
        with self.assertRaises(SnapshotConflict) as raised:
            self.store.accept({**PAYLOAD, 'request_id': str(uuid4())})
        self.assertEqual(raised.exception.code, 'STALE_SNAPSHOT')
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            latest = json.loads(connection.execute('SELECT snapshot FROM snapshots').fetchone()[0])
        self.assertEqual(latest['battle_count'], 1400)

    def test_id_deduplication_expires_after_24_hours_and_months_are_isolated(self):
        self.store.accept(PAYLOAD)
        self.now += timedelta(hours=24)
        renewed = self.store.accept(PAYLOAD)
        self.assertFalse(renewed['duplicate'])
        self.assertEqual(renewed['received_at'], '2026-10-27T10:00:00Z')
        for changes in ({'month': '2026-09'}, {'instance_id': 'c' * 16}, {'device_id': 'd' * 32}):
            self.store.accept({**PAYLOAD, **changes, 'request_id': str(uuid4())})
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM snapshots').fetchone()[0], 4)

    def test_parallel_duplicate_requests_only_write_one_receipt(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.store.accept(PAYLOAD), range(12)))
        self.assertEqual(sum(not result['duplicate'] for result in results), 1)
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM requests').fetchone()[0], 1)

    def test_receipt_write_failure_rolls_back_snapshot(self):
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            connection.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON requests "
                               "BEGIN SELECT RAISE(ABORT, '夹具模拟存储失败'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.accept(PAYLOAD)
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM snapshots').fetchone()[0], 0)

    def test_zero_encounters_and_single_battle_have_zero_division_results(self):
        result = self.store.accept({**PAYLOAD, 'battle_count': 1, 'akashi_encounters': 0, 'akashi_ap': 0})
        for field in ('battle_rounds', 'sortie_cost', 'akashi_probability', 'average_stamina', 'net_stamina_gain'):
            self.assertEqual(result['snapshot'][field], 0)


class DynamicApiTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        from unittest.mock import patch
        self.enterContext(patch('deploy.cloud_server.logger.exception'))
        self.store = SnapshotStore(directory / 'cloud.sqlite')
        self.sources = SimpleNamespace(
            server=Mock(return_value={'schema_version': 2, 'updated_at': NOW, 'region': 'cn',
                                      'server': {'id': 28, 'name': '长弓计划', 'status': 'normal'}}),
            maintenance=Mock(return_value={'schema_version': 2, 'updated_at': NOW, 'server': 'cn', 'maintenance': None}))
        self.client = self.enterContext(TestClient(create_app(self.store, self.sources), headers=HEADERS))

    def error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        value = response.json()
        self.assertEqual(value['schema_version'], 2)
        self.assertEqual(value['error']['code'], code)
        self.assertIsInstance(value['error']['details'], dict)

    def test_server_query_and_strict_unknown_duplicate_parameters(self):
        response = self.client.get(SERVER_PATH, params={'region': 'cn', 'server_id': 28})
        self.assertEqual(response.status_code, 200)
        self.sources.server.assert_called_once_with('cn', 28)
        for suffix in ('region=cn', 'region=cn&server_id=0', 'region=cn&server_id=true',
                       'region=cn&server_id=28&extra=1', 'region=cn&server_id=28&server_id=29'):
            self.error(self.client.get(SERVER_PATH + '?' + suffix), 400, 'VALIDATION_ERROR')
        self.sources.server.return_value = None
        self.error(self.client.get(SERVER_PATH + '?region=cn&server_id=28'), 404, 'SERVER_NOT_FOUND')
        self.sources.server.side_effect = OSError('数据源故障')
        self.error(self.client.get(SERVER_PATH + '?region=cn&server_id=28'), 503, 'SOURCE_UNAVAILABLE')

    def test_maintenance_null_is_success_and_failure_is_503(self):
        response = self.client.get(MAINTENANCE_PATH + '?server=cn')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['maintenance'])
        self.sources.maintenance.side_effect = OSError('故障')
        self.error(self.client.get(MAINTENANCE_PATH + '?server=cn'), 503, 'SOURCE_UNAVAILABLE')

    def test_cl1_accept_duplicate_conflict_and_stale_response(self):
        self.assertFalse(self.client.post(CL1_PATH, json=PAYLOAD).json()['duplicate'])
        self.assertTrue(self.client.post(CL1_PATH, json=PAYLOAD).json()['duplicate'])
        self.error(self.client.post(CL1_PATH, json={**PAYLOAD, 'battle_count': 1400}), 409, 'REQUEST_CONFLICT')
        old = {**PAYLOAD, 'request_id': str(uuid4()), 'collected_at': '2026-10-26T09:00:00Z'}
        self.error(self.client.post(CL1_PATH, json=old), 409, 'STALE_SNAPSHOT')

    def test_bad_json_unknown_fields_booleans_media_type_and_size(self):
        self.error(self.client.post(CL1_PATH, content=b'{bad', headers={'Content-Type': 'application/json'}),
                   400, 'INVALID_JSON')
        self.error(self.client.post(CL1_PATH, content=b'{"a":1,"a":2}', headers={'Content-Type': 'application/json'}),
                   400, 'INVALID_JSON')
        for changes in ({'extra': 1}, {'battle_count': True}, {'battle_count': 0}, {'akashi_ap': -1}):
            self.error(self.client.post(CL1_PATH, json={**PAYLOAD, **changes}), 400, 'VALIDATION_ERROR')
        self.error(self.client.post(CL1_PATH, content='{}'), 415, 'UNSUPPORTED_MEDIA_TYPE')
        self.error(self.client.post(CL1_PATH, content='x' * 65537, headers={'Content-Type': 'application/json'}),
                   413, 'PAYLOAD_TOO_LARGE')

    def test_methods_and_identity_fail_without_writing_snapshot(self):
        self.error(self.client.get(CL1_PATH), 405, 'METHOD_NOT_ALLOWED')
        self.error(self.client.post(CL1_PATH, json={**PAYLOAD, 'device_id': 'c' * 32}), 400, 'DEVICE_ID_MISMATCH')
        self.error(self.client.post(CL1_PATH, json=PAYLOAD, headers={'X-AzurPilot-ID': ''}), 400, 'INVALID_DEVICE_ID')
        self.error(self.client.get(MAINTENANCE_PATH + '?server=cn',
                                   headers={'Cloudflare-Workers-Version-Key': 'c' * 32}), 400, 'DEVICE_ID_MISMATCH')
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM snapshots').fetchone()[0], 0)

    def test_storage_failure_and_internal_error_never_return_accepted_or_stack(self):
        self.store.accept = Mock(side_effect=sqlite3.OperationalError('磁盘故障夹具'))
        response = self.client.post(CL1_PATH, json=PAYLOAD)
        self.error(response, 503, 'SERVICE_UNAVAILABLE')
        self.assertNotIn('磁盘故障夹具', response.text)
        self.sources.maintenance.side_effect = RuntimeError('内部堆栈夹具')
        response = self.client.get(MAINTENANCE_PATH + '?server=cn')
        self.error(response, 500, 'INTERNAL_ERROR')
        self.assertNotIn('内部堆栈夹具', response.text)


if __name__ == '__main__':
    unittest.main()
