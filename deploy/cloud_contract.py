"""自建云 API v2 的轻量契约校验；部署时不依赖 Pydantic 或游戏模块。"""

import math
import re
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from deploy.device_id import valid_device_id

NOTICE_PATH = '/api/v2/get/notice/data.json'
UPDATE_PATH = '/api/v2/get/update/data.json'
SERVER_PATH = '/api/v2/get/server'
MAINTENANCE_PATH = '/api/v2/get/maintenance'
CL1_PATH = '/api/v2/post/cl1'
REGIONS = ('cn', 'cn_ios', 'cn_channel', 'en', 'jp', 'tw')
SERVERS = ('cn', 'en', 'jp', 'tw')
STATUSES = ('normal', 'full', 'reg_full', 'maintenance', 'unopened', 'unknown')
CL1_FIELDS = {'request_id', 'device_id', 'instance_id', 'month', 'collected_at',
              'battle_count', 'akashi_encounters', 'akashi_ap'}
MAX_BODY_BYTES = 64 * 1024


class CloudProtocolError(ValueError):
    """云协议不匹配，不能把无效响应当作成功或空业务结果。"""


def require(condition, message):
    if not condition:
        raise CloudProtocolError(message)


def object_value(value, label='文档'):
    require(isinstance(value, dict), f'{label}必须是 JSON 对象')
    return value


def text(value, label, *, empty=False):
    require(isinstance(value, str) and (empty or bool(value.strip())), f'{label}必须是字符串')
    return value


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f'{label}必须是大于等于 {minimum} 的整数')
    return value


def timestamp(value, label):
    text(value, label)
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value),
            f'{label}必须是带时区的 RFC 3339 时间')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CloudProtocolError(f'{label}不是有效时间') from exc
    require(parsed.tzinfo is not None, f'{label}必须包含时区')
    return parsed


def document(value):
    value = object_value(value)
    require(type(value.get('schema_version')) is int and value['schema_version'] == 2, '云协议版本必须为 2')
    timestamp(value.get('updated_at'), 'updated_at')
    return value


def notice_document(value):
    value = document(value)
    require('announcement' in value, '公告文档缺少 announcement')
    notice = value['announcement']
    if notice is not None:
        object_value(notice, 'announcement')
        text(notice.get('id'), 'announcement.id')
        text(notice.get('title'), 'announcement.title')
        text(notice.get('content'), 'announcement.content', empty=True)
        require('url' in notice, '公告缺少 url')
        url = notice['url']
        if url is not None:
            text(url, 'announcement.url')
            parsed = urlsplit(url)
            require(parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password,
                    '公告链接必须为 HTTPS 地址')
        require(bool(notice['content'].strip()) or url is not None, '公告内容和链接不能同时为空')
    return value


def update_document(value):
    value = document(value)
    require(type(value.get('force_update')) is bool, 'force_update 必须是 JSON 布尔值')
    return value


def server_document(value, region=None):
    value = document(value)
    require(value.get('region') in REGIONS, '服务器响应地区无效')
    if region is not None:
        require(value['region'] == region, '服务器响应地区与请求不一致')
    server = object_value(value.get('server'), 'server')
    integer(server.get('id'), 'server.id', 1)
    text(server.get('name'), 'server.name')
    require(server.get('status') in STATUSES, '服务器状态无效')
    return value


def maintenance_document(value, server=None):
    value = document(value)
    require(value.get('server') in SERVERS, '维护响应区服无效')
    if server is not None:
        require(value['server'] == server, '维护响应区服与请求不一致')
    require('maintenance' in value, '维护响应缺少 maintenance')
    item = value['maintenance']
    if item is not None:
        object_value(item, 'maintenance')
        text(item.get('name'), 'maintenance.name')
        start = timestamp(item.get('starts_at'), 'maintenance.starts_at')
        require('ends_at' in item, '维护响应缺少 ends_at')
        if item['ends_at'] is not None:
            require(timestamp(item['ends_at'], 'maintenance.ends_at') >= start, '维护结束时间不能早于开始时间')
    return value


def cl1_request(value, device_id=None):
    value = object_value(value, 'CL1 请求')
    require(set(value) == CL1_FIELDS, 'CL1 请求字段缺失或包含未知字段')
    try:
        UUID(text(value['request_id'], 'request_id'))
    except ValueError as exc:
        raise CloudProtocolError('request_id 必须是 UUID') from exc
    require(valid_device_id(value['device_id']), 'device_id 必须是 32 位小写十六进制字符串')
    if device_id is not None:
        require(value['device_id'] == device_id, '请求头与正文中的设备 ID 不一致')
    require(isinstance(value['instance_id'], str) and re.fullmatch(r'[0-9a-f]{16}', value['instance_id']),
            'instance_id 必须是 16 位小写十六进制字符串')
    require(isinstance(value['month'], str) and re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', value['month'])
            and int(value['month'][:4]) >= 1, 'month 必须是有效的 YYYY-MM')
    timestamp(value['collected_at'], 'collected_at')
    integer(value['battle_count'], 'battle_count', 1)
    integer(value['akashi_encounters'], 'akashi_encounters')
    integer(value['akashi_ap'], 'akashi_ap')
    return value


def cl1_metrics(value):
    """服务端契约与客户端验收共用当前项目的派生公式。"""
    rounds = value['battle_count'] // 2
    encounters = value['akashi_encounters']
    return {'battle_rounds': rounds, 'sortie_cost': rounds * 120,
            'akashi_probability': round(encounters / rounds, 4) if rounds else 0,
            'average_stamina': round(value['akashi_ap'] / encounters, 2) if encounters else 0,
            'net_stamina_gain': value['akashi_ap']}


def cl1_response(value, sent):
    value = object_value(value, 'CL1 响应')
    require(type(value.get('schema_version')) is int and value['schema_version'] == 2, 'CL1 响应版本无效')
    require(value.get('request_id') == sent['request_id'], 'CL1 响应请求 ID 不一致')
    require(value.get('accepted') is True and type(value.get('duplicate')) is bool, 'CL1 请求未被明确接受')
    timestamp(value.get('received_at'), 'received_at')
    snapshot = object_value(value.get('snapshot'), 'snapshot')
    for key in CL1_FIELDS - {'request_id'}:
        if key == 'collected_at':
            require(timestamp(snapshot.get(key), key) == timestamp(sent[key], key), 'CL1 快照时间不一致')
        else:
            require(type(snapshot.get(key)) is type(sent[key]) and snapshot[key] == sent[key], f'CL1 快照 {key} 不一致')
    for key, expected in cl1_metrics(sent).items():
        actual = snapshot.get(key)
        require(type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9), f'CL1 派生指标 {key} 不一致')
    return value
