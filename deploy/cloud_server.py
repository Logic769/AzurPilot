"""自建云 v2 的独立 ASGI 服务；不注册进本地 WebUI。"""

import argparse
import json
import logging
import re
import sqlite3
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from deploy import cloud_contract as contract
from deploy.cloud_store import SnapshotConflict, SnapshotStore
from deploy.device_id import valid_device_id

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message = status, code, message
        self.details = details or {}


def error_response(status, code, message, details=None):
    return JSONResponse({'schema_version': 2, 'error': {
        'code': code, 'message': message, 'details': details or {}}}, status_code=status)


def identity(request):
    device = request.headers.get('X-AzurPilot-ID')
    affinity = request.headers.get('Cloudflare-Workers-Version-Key')
    if not valid_device_id(device) or not valid_device_id(affinity):
        raise ApiError(400, 'INVALID_DEVICE_ID', '请求头必须包含合法的 32 位设备 ID', {'field': 'device_id'})
    if device != affinity:
        raise ApiError(400, 'DEVICE_ID_MISMATCH', '设备 ID 与 CF 版本亲和键不一致', {'field': 'device_id'})
    if request.headers.get('X-AzurPilot-Ture') != 'true':
        raise ApiError(400, 'VALIDATION_ERROR', '缺少有效的项目请求标识', {'field': 'X-AzurPilot-Ture'})
    return device


def query(request, fields):
    pairs = request.query_params.multi_items()
    if set(request.query_params) != set(fields) or len(pairs) != len(fields):
        raise ApiError(400, 'VALIDATION_ERROR', '查询参数缺失、重复或包含未知字段')
    return dict(pairs)


def strict_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON 字段重复')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('JSON 不允许 NaN 或 Infinity')

    try:
        return json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ApiError(400, 'INVALID_JSON', '请求正文不是合法 JSON') from exc


class FileSources:
    """读取服务端采集器原子发布的有效文档；缺失维护文件表示数据源失败。"""

    def __init__(self, directory):
        self.directory = Path(directory)

    def server(self, region, server_id):
        path = self.directory / 'server' / region / f'{server_id}.json'
        # 已完成的元数据目录必须显式区分未知服务器与整份数据源缺失。
        if not path.parent.is_dir():
            raise OSError('服务器数据源尚未初始化')
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def maintenance(self, server):
        return json.loads((self.directory / 'maintenance' / f'{server}.json').read_text(encoding='utf-8'))


def create_app(store, sources):
    """可注入现有自建服务的数据源；仅暴露三个动态端点。"""
    async def dispatch(request):
        try:
            path = request.url.path
            expected_method = 'POST' if path == contract.CL1_PATH else 'GET'
            if request.method != expected_method:
                response = error_response(405, 'METHOD_NOT_ALLOWED', f'此端点只允许 {expected_method}')
                response.headers['Allow'] = expected_method
                return response
            device = identity(request)
            if path == contract.SERVER_PATH:
                params = query(request, ('region', 'server_id'))
                if params['region'] not in contract.REGIONS or not re.fullmatch(r'[1-9][0-9]*', params['server_id']):
                    raise ApiError(400, 'VALIDATION_ERROR', 'region 或 server_id 无效')
                try:
                    server_id = int(params['server_id'])
                except ValueError as exc:
                    raise ApiError(400, 'VALIDATION_ERROR', 'server_id 超出有效范围') from exc
                value = await run_in_threadpool(sources.server, params['region'], server_id)
                if value is None:
                    raise ApiError(404, 'SERVER_NOT_FOUND', '服务器不存在', {'field': 'server_id'})
                contract.server_document(value, params['region'])
                contract.require(value['server']['id'] == server_id, '数据源返回了其他服务器')
                return JSONResponse(value)
            if path == contract.MAINTENANCE_PATH:
                params = query(request, ('server',))
                if params['server'] not in contract.SERVERS:
                    raise ApiError(400, 'VALIDATION_ERROR', 'server 无效', {'field': 'server'})
                value = await run_in_threadpool(sources.maintenance, params['server'])
                return JSONResponse(contract.maintenance_document(value, params['server']))
            query(request, ())
            if request.headers.get('content-type', '').partition(';')[0].strip().lower() != 'application/json':
                raise ApiError(415, 'UNSUPPORTED_MEDIA_TYPE', 'CL1 请求必须为 application/json')
            length = request.headers.get('content-length')
            if length is not None:
                if not length.isdecimal():
                    raise ApiError(400, 'VALIDATION_ERROR', 'Content-Length 无效')
                if int(length) > contract.MAX_BODY_BYTES:
                    raise ApiError(413, 'PAYLOAD_TOO_LARGE', '正文超过 64 KiB')
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > contract.MAX_BODY_BYTES:
                    raise ApiError(413, 'PAYLOAD_TOO_LARGE', '正文超过 64 KiB')
            payload = strict_json(body)
            if isinstance(payload, dict) and 'device_id' in payload:
                if not valid_device_id(payload['device_id']):
                    raise ApiError(400, 'INVALID_DEVICE_ID', '正文设备 ID 无效', {'field': 'device_id'})
                if payload['device_id'] != device:
                    raise ApiError(400, 'DEVICE_ID_MISMATCH', '请求头与正文中的设备 ID 不一致', {'field': 'device_id'})
            try:
                contract.cl1_request(payload, device)
            except contract.CloudProtocolError as exc:
                raise ApiError(400, 'VALIDATION_ERROR', str(exc)) from exc
            return JSONResponse(await run_in_threadpool(store.accept, payload))
        except ApiError as exc:
            return error_response(exc.status, exc.code, exc.message, exc.details)
        except SnapshotConflict as exc:
            return error_response(409, exc.code, str(exc))
        except sqlite3.Error:
            logger.exception('云端快照事务失败')
            return error_response(503, 'SERVICE_UNAVAILABLE', '快照存储暂不可用')
        except (OSError, ValueError):
            logger.exception('云端数据源失败')
            return error_response(503, 'SOURCE_UNAVAILABLE', '数据源暂不可用')
        except Exception:
            logger.exception('云端接口内部错误')
            return error_response(500, 'INTERNAL_ERROR', '服务内部错误')

    methods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
    return Starlette(routes=[Route(path, dispatch, methods=methods) for path in (
        contract.SERVER_PATH, contract.MAINTENANCE_PATH, contract.CL1_PATH)])


def main():
    parser = argparse.ArgumentParser(description='启动独立自建云 API v2，不启动本地 WebUI')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8090)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(SnapshotStore(args.database), FileSources(args.source_dir)), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
