# 自建云 API v2 与统一请求规范

> 独立于本地 WebUI 的云端契约、匿名设备身份、公共静态文档、月度快照及下载请求头。

## 1. 模块概述

公告、更新策略、单服状态、区服维护和 CL1 统计使用五个独立端点，路径直接表明 GET 或 POST。公告和策略由 Cloudflare Static Assets 提供，自建服务器只生成、校验和发布这些文件。

机器可读契约为 [cloud-api-v2.openapi.json](cloud-api-v2.openapi.json)，由 `dev_tools.export_cloud_contract` 生成。下文示例描述 v2 协议，不代表对应云端已上线。客户端部署顺序必须晚于云端。

## 2. 模块职责

### 负责

- 统一自建云客户端，以及 CDN、ATX、预构建前端、ADB 下载的浏览器请求头。
- 让部署阶段和运行阶段使用同一设备 ID 算法，保留运行期旧身份迁移。
- 校验五个业务契约；保持现有业务调用及游戏网关兜底。
- 生成两个完整静态目录，串行发布、验证和回滚对应 Worker。
- 提供独立 ASGI 动态服务和 SQLite 快照事务，供自建服务器部署或接入现有采集器。

### 不负责

- 本地 WebUI `/api/v1/ws`、启动器通信、茗交所或其他 Web 项目。
- CDN、WebRTC、ATX 的地址、版本选择、切源及协议迁移。
- Bug 上报；已删除的上报功能不恢复。
- 在客户端保存 Cloudflare 管理凭据；控制面认证由服务器端 Wrangler 管理。

## 3. 模块位置

```text
deploy/
├── device_id.py / http_headers.py / http.py
├── cloud_contract.py / cloud_schema.py / cloud_api.py
├── cloud_server.py / cloud_store.py
├── cloud_static.py
├── cloudflare/{notice,update}/
│   ├── wrangler.jsonc
│   └── public/api/v2/get/{notice,update}/data.json
└── downloads.py / download_patch.py
```

| 文件 | 作用 |
| --- | --- |
| `device_id.py` | 标准库身份来源，不加载日志、配置或游戏模块 |
| `http_headers.py`、`http.py` | 集中构造浏览器头；限定作用域的 requests Session |
| `cloud_contract.py`、`cloud_schema.py` | 轻量运行时校验、独立 OpenAPI/JSON Schema |
| `cloud_api.py` | 可配置的业务客户端与静态条件请求 |
| `cloud_server.py`、`cloud_store.py` | 独立动态服务、数据源适配及 CL1 原子存储 |
| `cloud_static.py`、`cloudflare/` | 静态构建、Worker 模板、预览验证与发布回滚 |
| `downloads.py`、`download_patch.py` | 共用下载器；首次部署补丁生成不提前依赖 requests |

## 4. 核心入口

| 入口 | 用途 |
| --- | --- |
| `ApiClient.fetch_announcement()` | 公告的 200、304、撤回与错误区分 |
| `GitManager.cloud_force_update_enabled()` | 静态策略适配，失败返回 `None` |
| `ServerChecker._load_server()` | 单服状态与原网关兜底 |
| `OperationHandover.handover_maintain_query()` | 当前区服维护安排 |
| `ApiClient.submit_cl1_data()` | 生成不可变快照并返回后台 Future |
| `deploy.cloud_server.create_app(store, sources)` | 自建服务入口，不注册进 WebUI |
| `python -m deploy.cloud_static` | 服务器端生成或发布静态文件，无客户端发布 API |
| `python -m dev_tools.export_cloud_contract` | 同步独立云契约；不生成 WebUI 契约 |

## 5. 核心组件与完整收发契约

JSON 字段使用 `snake_case`；响应 `schema_version` 固定为整数 `2`。时间为含时区的 RFC 3339。以下示例字段全部必需，可空项用 `null`。请求拒绝缺失、重复和未知字段，响应允许客户端忽略新增字段。

| 方法 | 端点 | 承载位置 |
| --- | --- | --- |
| GET | `/api/v2/get/notice/data.json` | 公告 Worker |
| GET | `/api/v2/get/update/data.json` | 策略 Worker |
| GET | `/api/v2/get/server?region=cn&server_id=28` | 自建服务 |
| GET | `/api/v2/get/maintenance?server=cn` | 自建服务 |
| POST | `/api/v2/post/cl1` | 自建服务 |

### GET 公告

```http
GET /api/v2/get/notice/data.json HTTP/1.1
Accept: application/json
If-None-Match: "上次有效响应的 ETag"
```

无查询参数；首次请求不发 `If-None-Match`。公共头见第 8 节。

`200 OK`：

```json
{
  "schema_version": 2,
  "updated_at": "2026-10-26T10:00:00Z",
  "announcement": {
    "id": "notice-20261026-001",
    "title": "更新公告",
    "content": "本次更新内容。",
    "url": "https://example.com/releases"
  }
}
```

`id`、`title` 为非空字符串，`content` 为字符串，`url` 为 HTTPS 地址或 `null`；内容与链接至少一项非空。改变内容时更新公告 ID 和 `updated_at`。

撤回或没有公告也发布文件，返回：

```json
{"schema_version":2,"updated_at":"2026-10-26T11:00:00Z","announcement":null}
```

200 公告替换缓存，200 null 清空旧公告；304 无正文，保留缓存；网络、HTTP、格式错误均保留最近有效公告。页面适配仍输出 `announcementId/title/content/url`，不改变本地 WebUI 方法。

### GET 更新策略

```http
GET /api/v2/get/update/data.json HTTP/1.1
Accept: application/json
If-None-Match: "上次有效响应的 ETag"
```

无查询参数。`200 OK`：

```json
{"schema_version":2,"updated_at":"2026-10-26T10:00:00Z","force_update":false}
```

`force_update` 只接受 JSON 布尔值，不接受字符串、数字和拼写容错。304 使用已缓存的有效策略；其他失败由原业务适配返回 `None`，按关闭强更处理。失败时不把旧缓存伪装成新成功，不回源自建服务器，不加时间戳绕缓存。

### GET 单服状态

```http
GET /api/v2/get/server?region=cn&server_id=28 HTTP/1.1
Accept: application/json
```

`region` 必填，限 `cn/cn_ios/cn_channel/en/jp/tw`；`server_id` 为真实服务器 ID，必须是正整数，不是列表下标。`200 OK`：

```json
{
  "schema_version": 2,
  "updated_at": "2026-10-26T10:00:00Z",
  "region": "cn",
  "server": {"id": 28, "name": "长弓计划", "status": "normal"}
}
```

`status` 限 `normal/full/reg_full/maintenance/unopened/unknown`。参数错误 400，服务器不存在 404，数据源不可用 503。客户端核对地区、ID、名称和本地元数据，保留原可用性判断、404 本地列表兼容、游戏网关兜底及恢复通知。

### GET 区服维护

```http
GET /api/v2/get/maintenance?server=cn HTTP/1.1
Accept: application/json
```

`server` 必填，限 `cn/en/jp/tw`。`200 OK`：

```json
{
  "schema_version": 2,
  "updated_at": "2026-10-26T10:00:00Z",
  "server": "cn",
  "maintenance": {
    "name": "国服",
    "starts_at": "2026-10-29T09:00:00+08:00",
    "ends_at": "2026-10-29T15:00:00+08:00"
  }
}
```

结束时间未知时 `ends_at: null`，已知时不得早于开始时间。客户端直接解析完整时间并转换成本机调度时间，不再拼接日期或推断区服时区。

成功确认没有安排：

```json
{"schema_version":2,"updated_at":"2026-10-26T10:00:00Z","server":"cn","maintenance":null}
```

数据源失败必须返回 503。业务适配用 `{}` 表示已成功确认无维护，`None` 表示查询失败，失败继续按原间隔重新检查。

### POST CL1 月度快照

完整请求如下；`Host` 由遥测基址配置决定，`Content-Length` 由客户端计算。

```http
POST /api/v2/post/cl1 HTTP/1.1
Host: <TELEMETRY_HOST>
Content-Type: application/json; charset=utf-8
Accept: application/json
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
X-AzurPilot-Ture: true
X-AzurPilot-ID: 0123456789abcdef0123456789abcdef
Cloudflare-Workers-Version-Key: 0123456789abcdef0123456789abcdef

{
  "request_id": "9b53d5a1-7bb6-4fc2-97c7-c602af093584",
  "device_id": "0123456789abcdef0123456789abcdef",
  "instance_id": "0123456789abcdef",
  "month": "2026-10",
  "collected_at": "2026-10-26T10:00:00.000Z",
  "battle_count": 1200,
  "akashi_encounters": 60,
  "akashi_ap": 4200
}
```

| 字段 | 约束 |
| --- | --- |
| `request_id` | UUID；重试同一快照必须保持原 ID 和正文 |
| `device_id` | 32 位小写十六进制，与设备头及 CF 亲和键一致 |
| `instance_id` | 沿用现有 16 位匿名实例哈希 |
| `month` | 有效 `YYYY-MM`，保持当前统计月份 |
| `collected_at` | 含时区采集时间；客户端用 UTC |
| `battle_count` | 正整数；零战斗不提交 |
| `akashi_encounters`、`akashi_ap` | 大于等于零的整数；零遭遇合法 |

正文上限 64 KiB，拒绝非法 JSON、未知/重复字段、布尔计数和身份不一致。客户端只发送原始月度计数，服务器计算以下派生指标：

```text
battle_rounds       = battle_count // 2
sortie_cost         = battle_rounds * 120
akashi_probability  = round(akashi_encounters / battle_rounds, 4)
average_stamina     = round(akashi_ap / akashi_encounters, 2)
net_stamina_gain    = akashi_ap
```

除数为零时对应结果为 0。成功 `200 OK`：

```json
{
  "schema_version": 2,
  "request_id": "9b53d5a1-7bb6-4fc2-97c7-c602af093584",
  "accepted": true,
  "duplicate": false,
  "received_at": "2026-10-26T10:00:01Z",
  "snapshot": {
    "device_id": "0123456789abcdef0123456789abcdef",
    "instance_id": "0123456789abcdef",
    "month": "2026-10",
    "collected_at": "2026-10-26T10:00:00.000Z",
    "battle_count": 1200,
    "battle_rounds": 600,
    "sortie_cost": 72000,
    "akashi_encounters": 60,
    "akashi_ap": 4200,
    "akashi_probability": 0.1,
    "average_stamina": 70,
    "net_stamina_gain": 4200
  }
}
```

按设备、实例、月份覆盖保存，不累加月度快照。去重记录保留 24 小时；同 ID、同 JSON 内容返回原回执及 `duplicate: true`，不要求 JSON 键顺序相同；同 ID、不同内容返回 409 `REQUEST_CONFLICT`。较旧 `collected_at` 返回 409 `STALE_SNAPSHOT`。快照和回执在同一事务提交，提交成功才确认接受。客户端校验整份回执后，后台完成回调才更新成功提交时间。

## 6. 静态文件生成与发布

两个 Worker 各自以发布目录的 `public/` 为 Static Assets 根目录，保持完整 URL 路径，不去前缀、不做 SPA 回退。

| Worker | Route | 文件 |
| --- | --- | --- |
| `ap-notice` | `api.example.com/api/v2/get/notice/*` | `public/api/v2/get/notice/data.json` |
| `ap-update-policy` | `api.example.com/api/v2/get/update/*` | `public/api/v2/get/update/data.json` |

`_headers` 指定 JSON、`Cache-Control: public, max-age=0, must-revalidate` 和 `nosniff`，ETag 由静态资产服务维护。缓存键不添加设备 ID，CF 版本亲和键用于渐进发布分流，不替代 ETag 或缓存策略。[官方版本亲和说明](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/version-affinity/)、[路由匹配规则](https://developers.cloudflare.com/workers/configuration/routing/routes/)、[静态响应头](https://developers.cloudflare.com/workers/static-assets/headers/)。

服务器按 Worker 加文件锁：读取现有生产版本 → 上传带标签和预览别名的版本 → GET 校验预览完整内容 → 切换生产 → GET 校验生产 → 写入本地成功记录。上传或预览失败不切流；切流、生产确认或本地保存失败会恢复先前版本流量比例及本地文件。回滚失败会使命令失败，不能记录为成功。公告撤回发布 null 文件，不删除文件。

## 7. 调用关系

上游为公告服务、更新管理器、服务器检查器、作战委托和 CL1 提交器；下载适配覆盖 GitOverCdn、uiautomator2 初始化、ADB 和安卓前端预构建资源。独立服务通过 `sources.server(region, server_id)` 与 `sources.maintenance(server)` 接入服务端已有采集逻辑；它不直连真实游戏账号。

下游是 requests/urllib、SQLite 和服务器端 Wrangler。没有修改网络库全局行为或本地 WebSocket 注册表。

## 8. 统一请求头与身份数据流

所有受本方案覆盖的业务 GET、POST、下载、存在性 GET 探测、重试、镜像回退和重定向携带：

```http
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
X-AzurPilot-Ture: true
X-AzurPilot-ID: 0123456789abcdef0123456789abcdef
Cloudflare-Workers-Version-Key: 0123456789abcdef0123456789abcdef
```

保留 `Ture` 拼写。JSON 使用 `Accept: application/json`；二进制及其校验文件使用 `Accept: */*`、`Accept-Encoding: identity`。CDN 额外保留同值 `X-Device-Id`。不注入固定 Origin、Referer。发送前重建受保护头，调用方传入的同名头不能覆盖；重定向及同一次 ATX 镜像回退保留原身份，下一次请求获取刷新后的身份。

部署优先读取 `log/device_id.json` 内合法的 32 位 ID；缺失时沿用硬件指纹算法初始化并原子保存。已有损坏文件或旧算法身份留在原处，部署阶段只在内存生成合法身份，运行时先读取旧资料、保存迁移 ID，再发布当前身份和覆盖文件。生成失败明确拒绝请求，不能以空 ID 降级发送。匿名实例 ID 不用于请求头。

## 9. 状态模型

公告缓存区分「尚未初始化」「有效公告」「已撤回」；后两者都受 TTL 和 ETag 管理。请求失败保留有效状态。策略缓存仅用于合法 304。CL1 提交器最多有一个 pending Future；排队不是成功，失败清理 pending 后允许下一轮采集。冲突请求不会修改已保存快照。

## 10. 配置

服务基址通过进程环境配置，必须为 HTTPS；回环 HTTP 仅用于隔离测试。基址不包含查询、片段或用户名密码，不在 JSON 中传 Cloudflare 凭据。

| 环境变量 | 默认基址 | 用途 |
| --- | --- | --- |
| `AZURPILOT_CLOUD_STATIC_URL` | `https://alas-apiv2.nanoda.work` | 两个静态 Worker 路由 |
| `AZURPILOT_CLOUD_SERVER_URL` | `https://server-checker.nanoda.work` | 单服状态 |
| `AZURPILOT_CLOUD_MAINTENANCE_URL` | `https://api-blhx-maintain.nanoda.work` | 区服维护 |
| `AZURPILOT_CLOUD_TELEMETRY_URL` | `https://alas-apiv2.nanoda.work` | CL1 |

默认值保留当前项目服务域名；启用前须由部署者确认这些域名已提供 v2。CDN、ATX 和预构建资源仍使用各自现有配置与公有云地址。

## 11. 异常与错误处理

动态服务统一返回：

```json
{
  "schema_version": 2,
  "error": {
    "code": "DEVICE_ID_MISMATCH",
    "message": "请求头与正文中的设备 ID 不一致",
    "details": {"field": "device_id"}
  }
}
```

`details` 总是对象，无详情为 `{}`。响应不暴露堆栈。

| 状态 | 错误码 |
| --- | --- |
| 400 | `INVALID_JSON`、`VALIDATION_ERROR`、`INVALID_DEVICE_ID`、`DEVICE_ID_MISMATCH` |
| 404 | `SERVER_NOT_FOUND` |
| 405 | `METHOD_NOT_ALLOWED`，附带 `Allow` |
| 409 | `REQUEST_CONFLICT`、`STALE_SNAPSHOT` |
| 413 | `PAYLOAD_TOO_LARGE` |
| 415 | `UNSUPPORTED_MEDIA_TYPE` |
| 429 | `RATE_LIMITED`，附带 `Retry-After` |
| 503 | `SOURCE_UNAVAILABLE`、`SERVICE_UNAVAILABLE` |
| 500 | `INTERNAL_ERROR` |

限流由服务器入口或反向代理实施时也须遵守 429 契约。静态 Cloudflare 错误按 HTTP 状态处理，不要求 Cloudflare 错误页返回上述 JSON。CL1 不自动重放重定向或冲突 POST，提交失败不更新成功时间。

## 12. 并发与线程模型

运行期设备初始化使用重入锁；身份来源和策略缓存分别加锁。业务遥测沿用后台执行器，回执回调和 pending 状态共用提交锁。独立 ASGI 服务把同步数据源及 SQLite 操作移入线程池，每次请求独立连接并在退出时关闭。

SQLite `BEGIN IMMEDIATE` 在查询去重前串行化写事务，保证并发同 ID 只创建一份回执。静态发布锁由操作系统跨进程维护，同一发布输出目录对应一个 Worker；调度器和多个服务器进程须共用该目录，避免绕开锁。

## 13. 缓存与持久化

设备文件使用原子替换；运行阶段保持原定时刷新机制。公告和策略缓存位于进程内。CL1 云端数据库与本地游戏统计库分开：`snapshots` 按设备、实例、月份保存一份，`requests` 保存摘要、原回执和 24 小时过期时间，过期记录在下一次提交时清理。

服务器静态输出 `dist/cloud-static/{notice,update}/`，只有生产读回确认后记录 `published.json`。生产部署和数据库应由服务器持久化备份，不把真实数据、ID 或凭据提交到仓库。

## 14. 生命周期

部署工具不加载运行期日志或游戏模块来生成身份。常规启动完成旧身份检测后，发布运行期身份供后续 Session 请求使用。下载缓存继续校验长度、ATX gzip/tar 内容和预构建包 SHA-256；失败清理残片，首次部署与运行期共用下载器。

## 15. 扩展方式

新增云字段先更新 `cloud_contract.py`、`cloud_schema.py`，同步收发测试与本文，再运行导出器。请求未知字段默认拒绝，响应新增字段允许忽略。给独立服务接入真实采集器时实现 sources 接口，成功返回完整文档；失败抛出异常，不能把失败转换为 null。

## 16. 修改注意事项

- 保持设备 ID 的字段顺序、哈希算法、旧身份读取先后和匿名实例算法。
- BrowserSession 仅用于本方案客户端，不能全局替换 requests 或修改游戏网关协议。
- 公告用空内容撤回文档，不能返回 404 代替撤回；策略不能通过动态后端回源。
- 发布工具没有面向客户端的管理端点，Cloudflare 凭据只在服务器进程环境或服务器凭据存储中提供。
- 本次请求头修复同时覆盖主部署补丁和 Windows 旧布局，避免首次安装绕过运行期适配。

## 17. 已知限制与上线边界

仓库实现和隔离测试不等于生产上线。本仓库不含原自建服务的采集器实现；独立服务支持注入已有采集器，CLI 的 `FileSources` 读取服务器采集器原子写入的文档：`server/<region>/<server_id>.json` 和 `maintenance/<server>.json`。缺失维护文件返回 503；服务器地区目录未初始化也返回 503，已初始化目录内不存在的服务器才返回 404。

实际域名、zone、Cloudflare 账户、Worker 首次初始化、采集器接入和服务端限流由部署环境提供。静态发布器要求 Worker 已有可回滚的生产版本。SQLite 事务用于单机服务，跨主机部署必须提供共享的事务存储，不能各写一份数据库。

## 18. 构建、部署与发布示例

只构建，不访问 Cloudflare：

```powershell
uv run python -m deploy.cloud_static notice deploy/cloudflare/notice/public/api/v2/get/notice/data.json --output dist/cloud-static --hostname api.example.com --zone example.com
uv run python -m deploy.cloud_static update deploy/cloudflare/update/public/api/v2/get/update/data.json --output dist/cloud-static --hostname api.example.com --zone example.com
npx wrangler deploy --dry-run --config dist/cloud-static/notice/wrangler.jsonc
npx wrangler deploy --dry-run --config dist/cloud-static/update/wrangler.jsonc
```

首次上线时，用实际 hostname、zone 和经审核的文档构建；在服务器设置 Cloudflare 凭据后分别执行 `wrangler deploy --config <对应目录>/wrangler.jsonc` 初始化两个 Worker，核对路由和生产 GET，再开始客户端发布。后续服务器端发布示例：

```powershell
uv run python -m deploy.cloud_static notice notice.json --output dist/cloud-static --hostname api.actual-domain.com --zone actual-domain.com --publish --preview-base https://cloud-publish-ap-notice.account-subdomain.workers.dev
uv run python -m deploy.cloud_static update update.json --output dist/cloud-static --hostname api.actual-domain.com --zone actual-domain.com --publish --preview-base https://cloud-publish-ap-update-policy.account-subdomain.workers.dev
```

`--preview-base` 必须对应同一 Worker 的 `cloud-publish` 预览别名，不能填生产地址。Wrangler 应安装在服务器 PATH，CLI 可用 `--wrangler` 指定可执行文件。实际发布按两个命令串行执行，不向客户端分发凭据。

独立动态服务（HTTPS 由服务器入口终止）：

```powershell
uv run python -m deploy.cloud_server --database server-data/cl1.sqlite --source-dir server-data/sources --host 127.0.0.1 --port 8090
```

服务就绪后按需把三个动态路径代理到它；静态 notice/update 路径仍交给两个 Worker。云端先通过 v2 验收，再配置客户端基址并发布客户端版本。

## 19. 调试与验证

```powershell
uv run python -m dev_tools.export_cloud_contract
uv run python -m unittest tests.test_cloud_http tests.test_cloud_api_v2 tests.test_cloud_server tests.test_cloud_static tests.test_cloud_submission tests.test_cloud_downloads tests.test_announcement_service tests.test_server_checker tests.test_atx_agent_download tests.test_git_over_cdn tests.test_frontend_build
```

网络夹具只监听回环地址，设备 ID 使用固定测试值，不发送真实统计。覆盖首次身份初始化、旧 ID 迁移、受保护请求头、身份刷新、跨站跳转、GET 探测、镜像回退、长度/归档/哈希、公告撤回、ETag、维护时区、快照冲突、过期去重、并发事务和发布回滚。CI 独立生成云契约并检查差异；原 WebUI 生成文件不变。

## 20. 相关模块

- [部署与启动](deploy.md)
- [基础层](../base/index.md)
- [统计与数据提交](statistics.md)
- [服务器可用性检查](notify-llm-logger.md)
- [作战委托](../game/misc.md)
