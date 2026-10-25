"""自建云 v2 的客户端；部署阶段也可以读取静态更新策略。"""

import json
import os
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from deploy import cloud_contract as contract
from deploy.http import get, post
from deploy.http_headers import browser_headers


def service_url(value):
    parsed = urlsplit(value)
    local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment \
            or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local)):
        raise ValueError('云服务基址必须为 HTTPS；回环 HTTP 仅用于隔离测试')
    return value.rstrip('/')


@dataclass(frozen=True)
class CloudEndpoints:
    static: str = 'https://alas-apiv2.nanoda.work'
    telemetry: str = 'https://alas-apiv2.nanoda.work'
    server: str = 'https://server-checker.nanoda.work'
    maintenance: str = 'https://api-blhx-maintain.nanoda.work'

    @classmethod
    def configured(cls):
        values = {key: service_url(os.environ.get('AZURPILOT_CLOUD_' + key.upper() + '_URL', default))
                  for key, default in vars(cls()).items()}
        return cls(**values)


@dataclass(frozen=True)
class StaticResult:
    not_modified: bool
    document: dict | None
    etag: str


class CloudHttpError(requests.HTTPError):
    """保留真实 HTTP 状态和协议错误码，不把失败当空结果。"""

    def __init__(self, response):
        self.status_code = response.status_code
        try:
            error = response.json().get('error', {})
            self.code = error.get('code', 'HTTP_ERROR') if isinstance(error, dict) else 'HTTP_ERROR'
            message = error.get('message', '') if isinstance(error, dict) else ''
        except (ValueError, AttributeError):
            self.code, message = 'HTTP_ERROR', ''
        super().__init__(f'云服务请求失败：HTTP {self.status_code} {self.code} {str(message)[:160]}', response=response)


def response_document(response):
    if response.status_code != 200:
        raise CloudHttpError(response)
    if len(response.content) > 256 * 1024:
        raise contract.CloudProtocolError('云 JSON 响应超过 256 KiB')
    return response.json()


class CloudClient:
    def __init__(self, endpoints=None):
        self._endpoints = endpoints
        self._policy_lock = threading.Lock()
        self._policy_etag = ''
        self._policy = None

    @property
    def endpoints(self):
        return self._endpoints or CloudEndpoints.configured()

    def _static(self, path, validator, *, etag='', timeout=5):
        headers = {'If-None-Match': etag} if etag else {}
        response = get(self.endpoints.static + path, headers=headers, timeout=timeout, allow_redirects=False)
        try:
            if response.status_code == 304:
                contract.require(bool(etag), '未发条件请求却收到 304')
                return StaticResult(True, None, etag)
            value = validator(response_document(response))
            return StaticResult(False, value, response.headers.get('ETag', ''))
        finally:
            response.close()

    def notice(self, *, etag='', timeout=5):
        return self._static(contract.NOTICE_PATH, contract.notice_document, etag=etag, timeout=timeout)

    def policy(self, timeout=5):
        with self._policy_lock:
            result = self._static(contract.UPDATE_PATH, contract.update_document,
                                  etag=self._policy_etag, timeout=timeout)
            if not result.not_modified:
                self._policy = result.document['force_update']
                self._policy_etag = result.etag
            contract.require(type(self._policy) is bool, '304 响应没有对应的更新策略缓存')
            return self._policy

    def maintenance(self, server, timeout=10):
        contract.require(server in contract.SERVERS, '维护请求区服无效')
        response = get(self.endpoints.maintenance + contract.MAINTENANCE_PATH,
                       params={'server': server}, timeout=timeout, allow_redirects=False)
        try:
            return contract.maintenance_document(response_document(response), server)['maintenance']
        finally:
            response.close()

    def submit_cl1(self, payload, timeout=10):
        headers = browser_headers()
        contract.cl1_request(payload, headers['X-AzurPilot-ID'])
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > contract.MAX_BODY_BYTES:
            raise contract.CloudProtocolError('CL1 请求超过 64 KiB')
        response = post(self.endpoints.telemetry + contract.CL1_PATH, json=payload, headers=headers,
                        timeout=timeout, allow_redirects=False)
        try:
            return contract.cl1_response(response_document(response), payload)
        finally:
            response.close()


cloud_client = CloudClient()
