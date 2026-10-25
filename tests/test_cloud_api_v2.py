"""云 v2 客户端的本地 HTTP 契约测试。"""

import copy
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from deploy.cloud_api import CloudClient, CloudEndpoints, CloudHttpError
from deploy import cloud_contract as contract
from tests.test_cloud_http import ID, LocalHTTP

NOW = '2026-10-26T10:00:00Z'
NOTICE = {'schema_version': 2, 'updated_at': NOW, 'announcement': {
    'id': 'notice-1', 'title': '测试公告', 'content': '内容', 'url': None}}
POLICY = {'schema_version': 2, 'updated_at': NOW, 'force_update': False}
PAYLOAD = {'request_id': '9b53d5a1-7bb6-4fc2-97c7-c602af093584', 'device_id': ID,
           'instance_id': 'b' * 16, 'month': '2026-10', 'collected_at': NOW,
           'battle_count': 1200, 'akashi_encounters': 60, 'akashi_ap': 4200}


def accepted(payload, duplicate=False):
    return {'schema_version': 2, 'request_id': payload['request_id'], 'accepted': True,
            'duplicate': duplicate, 'received_at': NOW,
            'snapshot': {**{key: value for key, value in payload.items() if key != 'request_id'},
                         **contract.cl1_metrics(payload)}}


def json_reply(document, status=200, headers=None):
    return status, {'Content-Type': 'application/json', **(headers or {})}, json.dumps(document).encode()


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('deploy.http_headers.request_device_id', return_value=ID))

    def client(self, server):
        return CloudClient(CloudEndpoints(server.url, server.url, server.url, server.url))

    def test_notice_paths_etag_and_withdrawal(self):
        document = copy.deepcopy(NOTICE)

        def respond(method, path, headers, _body):
            self.assertEqual((method, path), ('GET', contract.NOTICE_PATH))
            if headers.get('If-None-Match') == 'v1':
                return 304, {}, b''
            return json_reply(document, headers={'ETag': 'v1'})

        with LocalHTTP(respond) as server:
            client = self.client(server)
            first = client.notice()
            self.assertEqual(first.document, NOTICE)
            self.assertTrue(client.notice(etag=first.etag).not_modified)
            document['announcement'] = None
            self.assertIsNone(client.notice().document['announcement'])
        self.assertTrue(all(item[2]['Cloudflare-Workers-Version-Key'] == ID for item in server.requests))

    def test_policy_strict_boolean_and_http_failure_do_not_return_stale_success(self):
        reply = [json_reply(POLICY, headers={'ETag': 'policy-1'})]
        with LocalHTTP(lambda *_: reply[0]) as server:
            client = self.client(server)
            self.assertIs(client.policy(), False)
            reply[0] = (304, {}, b'')
            self.assertIs(client.policy(), False)
            reply[0] = json_reply({**POLICY, 'force_update': 'false'})
            with self.assertRaises(contract.CloudProtocolError):
                client.policy()
            reply[0] = json_reply({'error': {'code': 'UNAVAILABLE', 'message': '暂不可用'}}, 503)
            with self.assertRaises(CloudHttpError) as raised:
                client.policy()
            self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(server.requests[1][2]['If-None-Match'], 'policy-1')

    def test_maintenance_query_preserves_null_and_rejects_wrong_region_or_naive_time(self):
        data = {'schema_version': 2, 'updated_at': NOW, 'server': 'cn', 'maintenance': None}

        def respond(method, path, *_):
            parsed = urlsplit(path)
            self.assertEqual(parsed.path, contract.MAINTENANCE_PATH)
            self.assertEqual(parse_qs(parsed.query), {'server': ['cn']})
            return json_reply(data)

        with LocalHTTP(respond) as server:
            client = self.client(server)
            self.assertIsNone(client.maintenance('cn'))
            data['server'] = 'jp'
            with self.assertRaises(contract.CloudProtocolError):
                client.maintenance('cn')
            data['server'] = 'cn'
            data['maintenance'] = {'name': '国服', 'starts_at': '2026-10-29T09:00:00', 'ends_at': None}
            with self.assertRaises(contract.CloudProtocolError):
                client.maintenance('cn')

    def test_cl1_actual_post_contains_only_raw_snapshot_and_matching_identity(self):
        def respond(method, path, headers, body):
            self.assertEqual((method, path), ('POST', contract.CL1_PATH))
            payload = json.loads(body)
            self.assertEqual(headers['X-AzurPilot-ID'], payload['device_id'])
            self.assertEqual(headers['Cloudflare-Workers-Version-Key'], payload['device_id'])
            self.assertEqual(set(payload), contract.CL1_FIELDS)
            return json_reply(accepted(payload))

        with LocalHTTP(respond) as server:
            result = self.client(server).submit_cl1(PAYLOAD)
            self.assertTrue(result['accepted'])
            self.assertEqual(result['snapshot']['battle_rounds'], 600)

    def test_invalid_cl1_identity_is_rejected_before_network(self):
        with LocalHTTP() as server:
            with self.assertRaises(contract.CloudProtocolError):
                self.client(server).submit_cl1({**PAYLOAD, 'device_id': 'c' * 32})
        self.assertEqual(server.requests, [])

    def test_cl1_conflict_keeps_status_and_is_not_replayed(self):
        error = {'schema_version': 2, 'error': {'code': 'STALE_SNAPSHOT', 'message': '旧快照', 'details': {}}}
        with LocalHTTP(lambda *_: json_reply(error, 409)) as server:
            with self.assertRaises(CloudHttpError) as raised:
                self.client(server).submit_cl1(PAYLOAD)
        self.assertEqual(raised.exception.code, 'STALE_SNAPSHOT')
        self.assertEqual(len(server.requests), 1)

    def test_unknown_fields_boolean_counters_and_unconfirmed_receipt_are_rejected(self):
        for changes in ({'extra': 1}, {'battle_count': True}, {'month': '2026-13'}, {'akashi_ap': -1}):
            with self.subTest(changes=changes), self.assertRaises(contract.CloudProtocolError):
                contract.cl1_request({**PAYLOAD, **changes})
        for changes in ({'accepted': False}, {'request_id': 'wrong'}, {'duplicate': 'false'}):
            with self.subTest(changes=changes), self.assertRaises(contract.CloudProtocolError):
                contract.cl1_response({**accepted(PAYLOAD), **changes}, PAYLOAD)
        zero = {**PAYLOAD, 'akashi_encounters': 0, 'akashi_ap': 0}
        self.assertEqual(contract.cl1_metrics(zero)['average_stamina'], 0)


if __name__ == '__main__':
    unittest.main()
