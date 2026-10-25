"""全局公告服务模块。

负责从远端获取公告，支持缓存与并发保护。
"""

import threading
import time
from typing import Any, Dict, Optional

from module.base.api_client import ApiClient
from module.logger import logger


class AnnouncementService:
    """提供公告缓存与获取能力的服务类。

    Attributes:
        _ttl (int): 缓存过期时间（秒）。
        _lock (threading.Lock): 线程安全锁。
        _cached (Optional[Dict[str, Any]]): 缓存的公告数据。
        _last_fetch (float): 上次成功请求的时间戳。
    """

    def __init__(self, ttl: int = 90):
        """初始化公告服务。

        Args:
            ttl (int, optional): 缓存生存时间（秒）。默认为 90。
        """
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: Optional[Dict[str, Any]] = None
        self._last_fetch: float = 0.0
        self._etag = ''
        self._initialized = False

    def get(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """获取当前最新公告。

        Args:
            force (bool, optional): 为 True 时跳过缓存 TTL 强制重新拉取。默认为 False。

        Returns:
            Optional[Dict[str, Any]]: 公告数据字典，包含 announcementId, title, content, url 等；无公告时返回 None。
        """
        now = time.time()
        with self._lock:
            if not force and self._initialized and (now - self._last_fetch < self._ttl):
                return self._cached

            try:
                result = ApiClient.fetch_announcement(timeout=5, etag=self._etag)
                self._last_fetch = now
                if not result.not_modified:
                    notice = result.document['announcement']
                    self._cached = ApiClient.announcement_for_ui(notice) if notice is not None else None
                    self._etag = result.etag
                    self._initialized = True
            except Exception as e:
                logger.warning(f'[API] 拉取公告失败: {e}')
                self._last_fetch = now

            return self._cached


announcement_service = AnnouncementService()
