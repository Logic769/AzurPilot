"""只为指定云通信构造请求，不全局修改 requests。"""

import requests
from requests.structures import CaseInsensitiveDict

from deploy.http_headers import DEVICE_ID_HEADER, browser_headers


class BrowserSession(requests.Session):
    """每次发送刷新身份，重定向沿用该请求的身份和浏览器请求头。"""

    def __init__(self, *, binary=False, legacy_device=False, _operation_identity=None):
        super().__init__()
        self.binary = binary
        self.legacy_device = legacy_device
        self._operation_identity = _operation_identity

    def prepare_request(self, request):
        if request.method.upper() not in ('GET', 'POST'):
            raise ValueError('云通信只允许 GET 和 POST')
        merged = CaseInsensitiveDict(self.headers)
        merged.update(request.headers or {})
        binary = self.binary or (merged.get('Accept-Encoding') == 'identity' and merged.get('Accept') == '*/*')
        request.headers = browser_headers(merged, binary=binary, legacy_device=self.legacy_device,
                                          device_id=self._operation_identity)
        if request.json is not None:
            request.headers['Content-Type'] = 'application/json; charset=utf-8'
            if isinstance(request.json, dict) and 'device_id' in request.json \
                    and request.json['device_id'] != request.headers[DEVICE_ID_HEADER]:
                raise ValueError('设备身份已更新，取消旧身份的 JSON 请求')
        return super().prepare_request(request)

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        # 跳转可能发生在运行时身份迁移期间，同一请求仍必须保持原设备身份。
        prepared_request.headers = CaseInsensitiveDict(browser_headers(
            prepared_request.headers,
            binary=self.binary or prepared_request.headers.get('Accept-Encoding') == 'identity',
            legacy_device=self.legacy_device,
            device_id=prepared_request.headers[DEVICE_ID_HEADER],
        ))


def get(url, *, binary=False, legacy_device=False, _operation_identity=None, **kwargs):
    with BrowserSession(binary=binary, legacy_device=legacy_device, _operation_identity=_operation_identity) as session:
        return session.get(url, **kwargs)


def post(url, **kwargs):
    with BrowserSession() as session:
        return session.post(url, **kwargs)
