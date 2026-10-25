"""公告服务与 API 分发回归测试。"""
import unittest
from unittest.mock import patch

from module.api.announcement_service import AnnouncementService
from module.api.router import Router
from deploy.cloud_api import StaticResult


def cloud_result(data):
    notice = None if data is None else {'id': data['announcementId'], 'title': data['title'],
                                      'content': data['content'], 'url': data['url'] or None}
    return StaticResult(False, {'schema_version': 2, 'updated_at': '2026-10-26T10:00:00Z',
                                'announcement': notice}, 'notice-1')


class AnnouncementServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = AnnouncementService(ttl=2)

    def test_get_announcement_and_caching(self):
        mock_data = {
            'announcementId': 'test-id-1',
            'title': '测试公告',
            'content': '测试公告内容',
            'url': 'https://example.com',
        }
        with patch('module.base.api_client.ApiClient.fetch_announcement', return_value=cloud_result(mock_data)) as mock_get:
            res1 = self.service.get(force=False)
            self.assertEqual(res1, mock_data)
            self.assertEqual(mock_get.call_count, 1)

            # TTL 内再次获取，直接命中缓存，不应再次调用远端
            res2 = self.service.get(force=False)
            self.assertEqual(res2, mock_data)
            self.assertEqual(mock_get.call_count, 1)

            # force=True 时应忽略缓存强制请求
            self.service.get(force=True)
            self.assertEqual(mock_get.call_count, 2)

    def test_304_preserves_existing_cache(self):
        initial_data = {
            'announcementId': 'id-100',
            'title': '旧公告',
            'content': '旧内容',
            'url': '',
        }
        with patch('module.base.api_client.ApiClient.fetch_announcement', return_value=cloud_result(initial_data)):
            self.service.get(force=False)

        # 远端返回 None（例如 304 未修改）
        with patch('module.base.api_client.ApiClient.fetch_announcement', return_value=StaticResult(True, None, 'notice-1')):
            result = self.service.get(force=True)
            self.assertEqual(result, initial_data)

    def test_router_dispatch(self):
        router = Router(configs=None, runtime=None)
        mock_data = {
            'announcementId': 'router-test',
            'title': '路由测试',
            'content': '分发测试',
            'url': '',
        }
        with patch('module.base.api_client.ApiClient.fetch_announcement', return_value=cloud_result(mock_data)):
            res = router.dispatch('announcement.get', {'force': True})
            self.assertEqual(res, mock_data)

    def test_withdrawal_clears_previous_notice_and_caches_empty_result(self):
        data = {'announcementId': 'old', 'title': '旧公告', 'content': '旧内容', 'url': ''}
        with patch('module.base.api_client.ApiClient.fetch_announcement',
                   side_effect=[cloud_result(data), cloud_result(None)]) as fetch:
            self.assertEqual(self.service.get(), data)
            self.assertIsNone(self.service.get(force=True))
            self.assertIsNone(self.service.get())
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args.kwargs['etag'], 'notice-1')

    def test_network_failure_keeps_last_valid_notice(self):
        data = {'announcementId': 'old', 'title': '旧公告', 'content': '旧内容', 'url': ''}
        with patch('module.base.api_client.ApiClient.fetch_announcement',
                   side_effect=[cloud_result(data), TimeoutError('网络暂不可用')]):
            self.assertEqual(self.service.get(), data)
            self.assertEqual(self.service.get(force=True), data)


if __name__ == '__main__':
    unittest.main()
