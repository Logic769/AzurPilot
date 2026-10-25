"""独立云契约的 JSON Schema；不参与 WebUI 契约生成。"""

from deploy import cloud_contract as c


def obj(properties, *, extra=True):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': extra}


def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


TEXT = {'type': 'string'}
NONEMPTY = {'type': 'string', 'minLength': 1}
TIME = {'type': 'string', 'format': 'date-time',
        'pattern': r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$'}
INTEGER = {'type': 'integer', 'minimum': 0}
BOOL = {'type': 'boolean'}
DEVICE_ID = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
META = {'schema_version': {'type': 'integer', 'const': 2}, 'updated_at': TIME}
NOTICE_ITEM = obj({'id': NONEMPTY, 'title': NONEMPTY, 'content': TEXT,
                   'url': nullable({'type': 'string', 'format': 'uri', 'pattern': '^https://'})})
NOTICE_ITEM['anyOf'] = [
    {'properties': {'content': {'type': 'string', 'pattern': r'\S'}}},
    {'properties': {'url': {'type': 'string', 'format': 'uri', 'pattern': '^https://'}}},
]
NOTICE = obj({**META, 'announcement': nullable(NOTICE_ITEM)})
POLICY = obj({**META, 'force_update': BOOL})
SERVER = obj({**META, 'region': {'type': 'string', 'enum': list(c.REGIONS)},
              'server': obj({'id': {'type': 'integer', 'minimum': 1}, 'name': NONEMPTY,
                             'status': {'type': 'string', 'enum': list(c.STATUSES)}})})
MAINTENANCE = obj({**META, 'server': {'type': 'string', 'enum': list(c.SERVERS)},
                   'maintenance': nullable(obj({'name': NONEMPTY, 'starts_at': TIME, 'ends_at': nullable(TIME)}))})
CL1_PROPERTIES = {'request_id': {'type': 'string', 'format': 'uuid'}, 'device_id': DEVICE_ID,
                  'instance_id': {'type': 'string', 'pattern': '^[0-9a-f]{16}$'},
                  'month': {'type': 'string', 'pattern': r'^(?!0000)\d{4}-(0[1-9]|1[0-2])$'},
                  'collected_at': TIME, 'battle_count': {'type': 'integer', 'minimum': 1},
                  'akashi_encounters': INTEGER, 'akashi_ap': INTEGER}
CL1_REQUEST = obj(CL1_PROPERTIES, extra=False)
CL1_RECEIPT = obj({'schema_version': {'type': 'integer', 'const': 2},
                   'request_id': CL1_PROPERTIES['request_id'], 'accepted': {'const': True, 'type': 'boolean'},
                   'duplicate': BOOL, 'received_at': TIME,
                   'snapshot': obj({**{key: value for key, value in CL1_PROPERTIES.items() if key != 'request_id'},
                                    'battle_rounds': INTEGER, 'sortie_cost': INTEGER,
                                    'akashi_probability': {'type': 'number', 'minimum': 0},
                                    'average_stamina': {'type': 'number', 'minimum': 0},
                                    'net_stamina_gain': INTEGER})})
ERROR = obj({'schema_version': {'type': 'integer', 'const': 2},
             'error': obj({'code': NONEMPTY, 'message': TEXT, 'details': {'type': 'object'}})})


def openapi_document():
    """生成可交给自建服务实现的完整端点、请求头和收发模型。"""
    headers = [
        {'name': 'User-Agent', 'in': 'header', 'required': True, 'schema': NONEMPTY},
        {'name': 'Accept-Language', 'in': 'header', 'required': True, 'schema': NONEMPTY},
        {'name': 'X-AzurPilot-Ture', 'in': 'header', 'required': True, 'schema': {'const': 'true', 'type': 'string'}},
        {'name': 'X-AzurPilot-ID', 'in': 'header', 'required': True, 'schema': DEVICE_ID},
        {'name': 'Cloudflare-Workers-Version-Key', 'in': 'header', 'required': True, 'schema': DEVICE_ID},
    ]

    def operation(name, schema, parameters=(), static=False, request=None):
        responses = {'200': {'description': '合法业务响应', 'content': {'application/json': {'schema': schema}}}}
        if static:
            responses['304'] = {'description': '未修改，无正文'}
            parameters = [*parameters, {'name': 'If-None-Match', 'in': 'header', 'schema': TEXT}]
        else:
            for status in (400, 404, 405, 409, 413, 415, 429, 500, 503):
                responses[str(status)] = {'description': '错误响应', 'content': {'application/json': {'schema': ERROR}}}
            responses['429']['headers'] = {'Retry-After': {'required': True, 'schema': TEXT}}
        value = {'operationId': name, 'parameters': [*headers, *parameters], 'responses': responses}
        if request:
            value['requestBody'] = {'required': True, 'content': {'application/json': {'schema': request}}}
        return value

    def query(name, schema):
        return {'name': name, 'in': 'query', 'required': True, 'schema': schema}

    return {'openapi': '3.1.0', 'info': {'title': 'AzurPilot 自建云 API', 'version': '2.0.0'},
            'paths': {
                c.NOTICE_PATH: {'get': operation('getNotice', NOTICE, static=True)},
                c.UPDATE_PATH: {'get': operation('getUpdatePolicy', POLICY, static=True)},
                c.SERVER_PATH: {'get': operation('getServerStatus', SERVER, [
                    query('region', {'type': 'string', 'enum': list(c.REGIONS)}),
                    query('server_id', {'type': 'integer', 'minimum': 1})])},
                c.MAINTENANCE_PATH: {'get': operation('getMaintenance', MAINTENANCE, [
                    query('server', {'type': 'string', 'enum': list(c.SERVERS)})])},
                c.CL1_PATH: {'post': operation('postCl1Snapshot', CL1_RECEIPT, request=CL1_REQUEST)},
            },
            'x-cl1-semantics': {'key': ['device_id', 'instance_id', 'month'], 'idempotencyHours': 24,
                                'rejectOlderSnapshot': True, 'maximumBodyBytes': c.MAX_BODY_BYTES}}
