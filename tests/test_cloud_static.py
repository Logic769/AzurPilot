"""静态资源发布的隔离验证，不运行 Wrangler 或访问 Cloudflare。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deploy.cloud_static import StaticPublisher, build_bundle, KINDS
from deploy.cloud_schema import openapi_document
from tests.test_cloud_api_v2 import NOTICE, POLICY


class StaticPublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.commands = []

    def runner(self, arguments):
        self.commands.append(arguments)
        if arguments[:2] == ['deployments', 'status']:
            return json.dumps({'versions': [{'version_id': 'old-version', 'percentage': 100}]})
        if arguments[:2] == ['versions', 'list']:
            tag = next(command[command.index('--tag') + 1] for command in self.commands if '--tag' in command)
            return json.dumps([{'id': 'new-version', 'annotations': {'workers/tag': tag}}])
        return ''

    def publisher(self, verifier):
        return StaticPublisher(self.root, runner=self.runner, verifier=verifier)

    def publish(self, publisher, document=NOTICE):
        return publisher.publish('notice', document, hostname='api.test.example', zone='test.example',
                                 preview_base='https://preview.test.example')

    def test_bundle_route_and_actual_file_match_full_path(self):
        for kind, document in (('notice', NOTICE), ('update', POLICY)):
            config = build_bundle(kind, document, self.root / kind)
            path = KINDS[kind][0]
            self.assertEqual(config['routes'][0]['pattern'], 'api.example.com' + path.rsplit('/', 1)[0] + '/*')
            self.assertEqual(json.loads((self.root / kind / 'public' / path.lstrip('/')).read_text()), document)
            self.assertNotIn('main', config)
            self.assertEqual(config['assets']['not_found_handling'], 'none')

    def test_preview_is_verified_before_production_and_state_is_written_after_confirmation(self):
        verified = []

        def verify(base, kind, doc):
            verified.append(base)
            if len(verified) == 1:
                self.assertFalse(any(command[:2] == ['versions', 'deploy'] for command in self.commands))
            self.assertFalse((self.root / 'notice/published.json').exists())

        self.assertEqual(self.publish(self.publisher(verify)), 'new-version')
        self.assertEqual(verified, ['https://preview.test.example', 'https://api.test.example'])
        self.assertTrue((self.root / 'notice/published.json').exists())

    def test_preview_failure_never_changes_production(self):
        with self.assertRaises(ValueError):
            self.publish(self.publisher(Mock(side_effect=ValueError('预览错误'))))
        self.assertFalse(any(command[:2] == ['versions', 'deploy'] for command in self.commands))
        self.assertFalse((self.root / 'notice/published.json').exists())

    def test_production_verification_failure_rolls_back_and_keeps_saved_document(self):
        build_bundle('notice', NOTICE, self.root / 'notice')
        new_document = copy.deepcopy(NOTICE)
        new_document['updated_at'] = '2026-10-26T11:00:00Z'
        verifier = Mock(side_effect=[None, ValueError('生产校验失败')])
        with self.assertRaises(ValueError):
            self.publish(self.publisher(verifier), new_document)
        deployment = [command for command in self.commands if command[:2] == ['versions', 'deploy']]
        self.assertEqual([command[2] for command in deployment], ['new-version@100', 'old-version@100'])
        saved = self.root / 'notice/public' / KINDS['notice'][0].lstrip('/')
        self.assertEqual(json.loads(saved.read_text()), NOTICE)

    def test_older_document_and_invalid_payload_fail_before_upload(self):
        build_bundle('notice', NOTICE, self.root / 'notice')
        (self.root / 'notice/published.json').write_text('{}')
        old = {**NOTICE, 'updated_at': '2026-10-25T00:00:00Z'}
        with self.assertRaises(ValueError):
            self.publish(self.publisher(Mock()), old)
        self.assertEqual(self.commands, [])
        with self.assertRaises(ValueError):
            build_bundle('update', {**POLICY, 'force_update': 'false'}, self.root / 'invalid')
        self.assertFalse((self.root / 'invalid').exists())

    def test_notice_content_change_requires_new_id_and_timestamp(self):
        build_bundle('notice', NOTICE, self.root / 'notice')
        (self.root / 'notice/published.json').write_text('{}')
        changed = copy.deepcopy(NOTICE)
        changed['announcement']['content'] = '另一份内容'
        with self.assertRaises(ValueError):
            self.publish(self.publisher(Mock()), changed)
        changed['updated_at'] = '2026-10-26T12:00:00Z'
        with self.assertRaises(ValueError):
            self.publish(self.publisher(Mock()), changed)
        self.assertEqual(self.commands, [])

    def test_local_record_failure_also_rolls_back_production_and_preserves_files(self):
        build_bundle('notice', NOTICE, self.root / 'notice')
        new_document = {**NOTICE, 'updated_at': '2026-10-26T12:00:00Z'}
        from deploy.cloud_static import atomic_write

        def failing_record(path, content):
            if path.endswith('published.json'):
                raise OSError('夹具模拟成功记录写入失败')
            return atomic_write(path, content)

        with patch('deploy.cloud_static.atomic_write', side_effect=failing_record), self.assertRaises(OSError):
            self.publish(self.publisher(Mock()), new_document)
        deployments = [command for command in self.commands if command[:2] == ['versions', 'deploy']]
        self.assertEqual([command[2] for command in deployments], ['new-version@100', 'old-version@100'])
        self.assertFalse((self.root / 'notice/published.json').exists())
        saved = self.root / 'notice/public' / KINDS['notice'][0].lstrip('/')
        self.assertEqual(json.loads(saved.read_text()), NOTICE)

    def test_machine_contract_only_contains_five_cloud_endpoints(self):
        contract = openapi_document()
        self.assertEqual(len(contract['paths']), 5)
        for path, methods in contract['paths'].items():
            self.assertNotIn('/ws', path)
            self.assertTrue(set(methods) <= {'get', 'post'})
            for operation in methods.values():
                headers = {item['name'] for item in operation['parameters'] if item['in'] == 'header'}
                self.assertTrue({'X-AzurPilot-Ture', 'X-AzurPilot-ID', 'Cloudflare-Workers-Version-Key'} <= headers)

    def test_generated_contract_is_current_and_examples_match_json_schema(self):
        from jsonschema import Draft202012Validator, FormatChecker
        from deploy.cloud_schema import NOTICE as notice_schema, POLICY as policy_schema, CL1_REQUEST, CL1_RECEIPT
        from tests.test_cloud_api_v2 import PAYLOAD, accepted

        path = Path(__file__).resolve().parents[1] / 'docs/modules/infra/cloud-api-v2.openapi.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), openapi_document())
        for schema, value in ((notice_schema, NOTICE), (policy_schema, POLICY),
                              (CL1_REQUEST, PAYLOAD), (CL1_RECEIPT, accepted(PAYLOAD))):
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


if __name__ == '__main__':
    unittest.main()
