"""快照提交与维护时间的业务回归，全部使用离线夹具。"""

import threading
import unittest
from concurrent.futures import Future
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from module.handover.handover import OperationHandover
from module.statistics.cl1_data_submitter import Cl1DataSubmitter
from module.base.api_client import ApiClient
from tests.test_cloud_api_v2 import ID, PAYLOAD, accepted


class SubmissionTests(unittest.TestCase):
    def submitter(self, pending):
        item = object.__new__(Cl1DataSubmitter)
        item._pending = None
        item._submit_lock = threading.RLock()
        item._last_submit_time = 0
        item._submit_interval = 600
        item.collect_data = Mock(return_value={})
        item.calculate_metrics = Mock(return_value=PAYLOAD)
        item.submit_data = Mock(return_value=pending)
        return item

    def test_enqueued_future_does_not_mark_submission_successful(self):
        pending = Future()
        item = self.submitter(pending)
        self.assertTrue(item.auto_submit())
        self.assertEqual(item._last_submit_time, 0)
        self.assertFalse(item.auto_submit())
        pending.set_result(accepted(PAYLOAD))
        self.assertGreater(item._last_submit_time, 0)
        self.assertIsNone(item._pending)

    def test_failure_clears_pending_but_does_not_delay_next_submission(self):
        pending = Future()
        item = self.submitter(pending)
        item.auto_submit()
        pending.set_exception(TimeoutError('网络断开'))
        self.assertEqual(item._last_submit_time, 0)
        self.assertIsNone(item._pending)
        self.assertTrue(item.should_submit())

    def test_facade_only_queues_raw_fields_and_keeps_request_id_for_worker(self):
        data = {**PAYLOAD, 'net_stamina_gain': 4200, 'sortie_cost': 72000}
        with patch('module.base.api_client.get_device_id', return_value=ID), \
                patch('module.base.async_executor.async_executor.submit') as submit:
            self.assertIs(ApiClient.submit_cl1_data(data), submit.return_value)
        payload = submit.call_args.args[1]
        self.assertEqual(set(payload), set(PAYLOAD))
        self.assertEqual(payload['akashi_ap'], 4200)
        self.assertNotIn('sortie_cost', payload)


class MaintenanceTests(unittest.TestCase):
    def fake(self, payload):
        return SimpleNamespace(config=SimpleNamespace(OperationHandover_MaintainOverride=True, SERVER='cn'),
                               handover_maintain_query=lambda: (payload, None, '查询失败' if payload is None else ''))

    def test_null_maintenance_is_success_but_source_failure_is_not(self):
        self.assertTrue(OperationHandover.handover_maintain_state(self.fake({}))[2])
        self.assertFalse(OperationHandover.handover_maintain_state(self.fake(None))[2])

    def test_offset_time_is_converted_to_local_time_without_reinterpreting_region(self):
        payload = {'name': '日服', 'starts_at': '2026-10-26T09:00:00+09:00', 'ends_at': None}
        expected = datetime.fromisoformat(payload['starts_at']).astimezone().replace(tzinfo=None)
        with patch('module.handover.handover.current_time', return_value=expected):
            start, _, queried = OperationHandover.handover_maintain_state(self.fake(payload))
        self.assertEqual(start, expected)
        self.assertTrue(queried)

    def test_naive_time_is_not_accepted_as_local_time(self):
        payload = {'name': '国服', 'starts_at': '2026-10-26T09:00:00', 'ends_at': None}
        self.assertFalse(OperationHandover.handover_maintain_state(self.fake(payload))[2])


if __name__ == '__main__':
    unittest.main()
