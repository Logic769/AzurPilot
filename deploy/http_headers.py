"""云请求与资源下载共用的浏览器请求头，仅依赖标准库。"""

from deploy.device_id import request_device_id, valid_device_id

BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
DEVICE_ID_HEADER = 'X-AzurPilot-ID'
VERSION_KEY_HEADER = 'Cloudflare-Workers-Version-Key'
_PROTECTED = {'user-agent', 'accept-language', 'x-azurpilot-ture', 'x-azurpilot-id',
              'cloudflare-workers-version-key', 'origin', 'referer'}


def browser_headers(headers=None, *, binary=False, legacy_device=False, device_id=None) -> dict[str, str]:
    """调用方可补充条件请求等字段，不能覆盖身份或加入固定跨站来源头。"""
    device_id = request_device_id() if device_id is None else device_id
    if not valid_device_id(device_id):
        raise ValueError('请求不能发送空设备 ID 或非法设备 ID')
    original = dict(headers or {})
    result = {key: value for key, value in original.items() if key.lower() not in _PROTECTED
              and key.lower() != 'x-device-id'}
    for key in list(result):
        if key.lower() == 'accept' or (binary and key.lower() == 'accept-encoding'):
            del result[key]
    result.update({'User-Agent': BROWSER_USER_AGENT,
                   'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                   'Accept': '*/*' if binary else 'application/json',
                   'X-AzurPilot-Ture': 'true', DEVICE_ID_HEADER: device_id,
                   VERSION_KEY_HEADER: device_id})
    if binary:
        result['Accept-Encoding'] = 'identity'
    if legacy_device or any(key.lower() == 'x-device-id' for key in original):
        result['X-Device-Id'] = device_id
    return result
