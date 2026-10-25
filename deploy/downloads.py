"""uiautomator2 资源下载适配：统一请求头、完整性检查与 ATX 回退。"""

import os
import tarfile
import tempfile
import zlib
from pathlib import Path

import requests

from deploy.atomic import atomic_replace
from deploy.http import get as download_get
from deploy.http_headers import browser_headers


def valid_atx_archive(path):
    try:
        with tarfile.open(path, 'r:gz') as archive:
            member = archive.getmember('atx-agent')
            if not member.isfile() or member.size <= 0:
                return False
            with archive.extractfile(member) as binary:
                while binary.read(64 * 1024):
                    pass
        return True
    except (OSError, EOFError, tarfile.TarError, KeyError, zlib.error):
        return False


def cache_download(url, storepath, *, timeout=None, fallback_url=None, logger=None):
    """保留缓存键和资源内容，只替换网络请求与下载完整性处理。"""
    path = Path(storepath)
    atx = 'atx-agent_' in path.name or 'atx-agent_' in url
    if path.is_file() and path.stat().st_size > 0 and (not atx or valid_atx_archive(path)):
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = browser_headers(binary=True)
    last_error = None
    for source in dict.fromkeys(item for item in (url, fallback_url) if item):
        partial = None
        try:
            if logger:
                logger.info(f'[资源下载] {source}')
            with download_get(source, binary=True, stream=True, headers=headers,
                              _operation_identity=headers['X-AzurPilot-ID'],
                              timeout=timeout if timeout is not None else (5, 30)) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as target:
                    partial = target.name
                    for chunk in response.iter_content(64 * 1024):
                        if chunk:
                            target.write(chunk)
                expected = response.headers.get('Content-Length')
                if expected is not None and os.path.getsize(partial) != int(expected):
                    raise ValueError('资源下载长度不完整')
            if os.path.getsize(partial) == 0 or (atx and not valid_atx_archive(partial)):
                raise ValueError('资源下载内容无效')
            atomic_replace(partial, str(path))
            partial = None
            return str(path)
        except (requests.RequestException, ValueError, OSError) as exc:
            last_error = exc
            if logger:
                logger.warning(f'[资源下载] 下载失败：{exc}')
        finally:
            if partial is not None:
                Path(partial).unlink(missing_ok=True)
    raise last_error or ValueError('没有可用的资源下载地址')
