"""自建云 API 门面：公告条件请求与 CL1 快照后台提交。"""

from datetime import datetime, timezone
from uuid import uuid4

from deploy.cloud_api import cloud_client
from deploy.cloud_contract import CL1_PATH, NOTICE_PATH, cl1_request
from module.base.device_id import get_device_id
from module.logger import logger


class ApiClient:
    """保持现有业务入口，云协议与部署阶段共用轻量客户端。"""

    CL1_DATA_PATH = CL1_PATH
    ANNOUNCEMENT_PATH = NOTICE_PATH
    ANNOUNCEMENT_CHECK_INTERVAL = 90

    @classmethod
    def fetch_announcement(cls, *, timeout=5, etag=''):
        """显式区分 200 撤回、304 未修改和网络失败。"""
        get_device_id()
        return cloud_client.notice(timeout=timeout, etag=etag)

    @classmethod
    def get_announcement(cls, timeout=5, current_id=None):
        """兼容旧业务调用；缓存服务使用 fetch_announcement 保留撤回语义。"""
        try:
            notice = cls.fetch_announcement(timeout=timeout).document['announcement']
            if notice is None or notice['id'] == current_id:
                return None
            return cls.announcement_for_ui(notice)
        except Exception as exc:
            logger.warning(f'[基础-API] 获取公告失败：{exc}')
            return None

    @staticmethod
    def announcement_for_ui(notice):
        """仅适配既有页面字段，不修改 WebUI 接口契约。"""
        return {'announcementId': notice['id'], 'title': notice['title'],
                'content': notice['content'], 'url': notice['url'] or ''}

    @classmethod
    def submit_cl1_data(cls, data, timeout=10):
        """返回后台 Future；仅服务端明确确认后才能记为提交成功。"""
        if data.get('battle_count', 0) == 0:
            return None
        payload = {
            'request_id': str(uuid4()), 'device_id': get_device_id(),
            'instance_id': data['instance_id'], 'month': data['month'],
            'collected_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'battle_count': data['battle_count'], 'akashi_encounters': data['akashi_encounters'],
            'akashi_ap': data.get('akashi_ap', data.get('net_stamina_gain')),
        }
        cl1_request(payload)
        from module.base.async_executor import async_executor
        return async_executor.submit(cls._submit_cl1_data, payload, timeout)

    @staticmethod
    def _submit_cl1_data(payload, timeout):
        result = cloud_client.submit_cl1(payload, timeout=timeout)
        logger.info(f'[基础-API] CL1 月度快照已确认：{payload["month"]}')
        return result
