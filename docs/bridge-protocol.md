# Pull Bridge Protocol v1.0

协议于 2026-11-21 冻结为 v1.0。路径 `/bridge/v1/` 与版本头 `X-Harmony-Pilot-Protocol: 1` 继续使用线上的 v1 表示。当前客户端的 Action API 仍为 v1，新增插件最低版本字段属于本地能力发现信息。

> **协议定位**：服务端无关（Server-Agnostic）的跨 NAT 单向出站任务派发协议。  
> 任何外部调度系统、运维平台或控制面只要按本文档实现服务端（Broker），即可安全接入 `moyu-harmony-pilot` 边缘节点，双方保持彻底解耦。

---

## 1. 设计目标与安全原则

1. **单向出站（Zero Inbound Exposure）**：由运行在本地宿主机的 Bridge 主动向远端 Broker 发起 HTTPS 请求，本地无需公网 IP、无需端口映射、无需暴露任何调试服务。
2. **三权分立（Least Privilege）**：
   - **Bridge Token**：绑定唯一 `bridgeId`，仅可上报该 Bridge 名下设备心跳、拉取分配给它的任务、续租与提交回执；**无权创建任务**。
   - **Dispatcher Token**（由 Broker 侧向业务调用方签发）：绑定允许访问的 `deviceAlias` 与 `action` 白名单，仅可提交任务与查询状态。
   - **Admin**（Broker 侧管理员）：负责签发/吊销 Token、查看设备与任务状态、人工取消任务。
   - Broker 持久化存储 Token 时**仅保存单向哈希（如 SHA-256）**，使用恒定时间比较校验。
3. **脱敏边界**：协议载荷严禁携带原始硬件串号、应用密码、登录 Cookie、截图二进制或完整页面文本；单条回执大小严格受限（建议上限 `16 KiB`）。
4. **防重与不自动重放**：通过 `idempotencyKey` 实现提交去重；通过 `leaseToken` 实现单设备互斥；任务超时或断联进入 `unknown` 时禁止自动重放有副作用动作。

---

## 2. 通用约定

- **传输层**：必须使用 `HTTPS`（仅允许 `http://127.0.0.1` / `http://localhost` 用于本地测试）。
- **认证头**：`Authorization: Bearer <BRIDGE_TOKEN>`
- **内容类型**：`Content-Type: application/json; charset=utf-8`
- 时间戳使用带时区的 ISO-8601 字符串，发送端统一输出 UTC，例如 `"2026-11-19T08:00:00Z"`。
- **协议版本头**：`X-Harmony-Pilot-Protocol: 1`
- 请求体最多 16 KiB，响应体最多 64 KiB，JSON 对象键唯一且所有数值有限。任务对象采用 §4.2 的完整字段集合，客户端拒绝缺失或额外字段。
- 标识符 `bridgeId`、`jobId`、`deviceAlias` 和 `action` 使用 `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`。幂等键最长 256 字符，租约令牌最长 1024 字符，均须非空且无控制字符。
- `timeoutSeconds` 范围为 0.001–3600，`maxJobs` 为 1–32 的整数，`waitSeconds` 为 0–25 的整数。

---

## 3. 任务生命周期状态机

```text
queued ──▶ leased ──▶ running ──┬──▶ succeeded        (明确确认成功)
  │                             ├──▶ already_done     (幂等检查已完成)
  │                             ├──▶ human_required   (需人工介入，如验证码/登录失效)
  │                             ├──▶ device_offline   (执行前目标设备离线/未授权)
  │                             ├──▶ device_overheated(触发本地过温保护拒绝执行)
  │                             ├──▶ failed           (明确失败且未产生不确定副作用)
  │                             └──▶ unknown          (租约超时/中断/结果不可确认，禁止自动重放)
  ├──▶ expired                  (超过 expiresAt 仍未被认领)
  └──▶ canceled                 (未认领前被人工/调用方取消)
```

- **终态集合**：`succeeded`、`already_done`、`human_required`、`device_offline`、`device_overheated`、`failed`、`unknown`、`expired`、`canceled`。
- **单设备互斥约束**：Broker 必须保证同一个 `deviceAlias` 同一时刻最多仅有 **1** 个处于 `leased` 或 `running` 状态的任务。

---

## 4. Bridge 侧接口契约（`moyu-harmony-pilot` 客户端调用）

### 4.1 上报心跳与设备遥测

`POST /bridge/v1/heartbeat`

Bridge 周期性（默认 30–60 秒）上报自身版本、当前挂载设备及各设备支持的白名单动作。

#### 请求体
```json
{
  "bridgeId": "home-mac-01",
  "pilotVersion": "0.1.0",
  "hostPlatform": "darwin-arm64",
  "sentAt": "2026-11-19T08:00:00Z",
  "devices": [
    {
      "deviceAlias": "harmony-01",
      "connectionState": "online",
      "osFamily": "harmonyos-next",
      "osVersion": "5.0",
      "batteryPercent": 78,
      "charging": true,
      "temperatureC": 31.5,
      "screenState": "off",
      "busy": false,
      "capabilities": ["device.ping", "device.health"]
    }
  ]
}
```

- `connectionState` 枚举：`online`、`offline`、`unauthorized`、`unknown`。
- `screenState` 枚举：`on`、`off`、`unknown`。
- 缺失的硬件指标传 `null`，不伪造默认值。

#### 响应体（`200 OK`）
```json
{
  "ok": true,
  "serverTime": "2026-11-19T08:00:01Z",
  "recommendedPollIntervalSeconds": 15,
  "hasPendingJobs": true
}
```

---

### 4.2 认领待执行任务

`POST /bridge/v1/jobs/claim`

Bridge 仅上报当前处于 `online` 且非 `busy` 的设备及各自白名单能力，由 Broker 分配至多 N 个（默认 1 个/设备）可执行任务。Broker 可选支持 `waitSeconds`（例如 0–25 秒，避开常见反代超时）长轮询。

客户端通过 `max_jobs` 配置并发上限，认领请求中的 `maxJobs` 取该上限与可用设备数的较小值。一个响应中的任务 ID 和设备别名各自唯一，每个设备都应来自本次 `availableDevices`。客户端按批次并行执行，整批结束后继续认领。

#### 请求体
```json
{
  "bridgeId": "home-mac-01",
  "maxJobs": 1,
  "waitSeconds": 15,
  "availableDevices": [
    {
      "deviceAlias": "harmony-01",
      "capabilities": ["device.ping", "device.health"]
    }
  ]
}
```

#### 响应体（`200 OK`，有任务）
```json
{
  "jobs": [
    {
      "jobId": "job_01J...",
      "deviceAlias": "harmony-01",
      "action": "device.health",
      "idempotencyKey": "health:harmony-01:2026-11-19",
      "params": {},
      "timeoutSeconds": 120,
      "leaseToken": "lease_opaque_random_token",
      "leaseExpiresAt": "2026-11-19T08:01:00Z",
      "createdAt": "2026-11-19T08:00:00Z",
      "expiresAt": "2026-11-19T13:30:00Z"
    }
  ]
}
```

无可执行任务时返回 `200 OK` 与 `{"jobs": []}`。

---

### 4.3 续租与进度心跳

`POST /bridge/v1/jobs/{jobId}/lease`

任务执行期间（如超过 20–30 秒），Bridge 后台线程定期续租，防止任务被 Broker 误判失联。

#### 请求体
```json
{
  "bridgeId": "home-mac-01",
  "leaseToken": "lease_opaque_random_token",
  "extendSeconds": 60,
  "stage": "inspect"
}
```

- `stage` 可选受限枚举：`preparing`、`inspect`、`submit`、`confirm`、`cleaning`。

#### 响应
- `200 OK`：`{"ok": true, "leaseExpiresAt": "2026-11-19T08:02:00Z", "cancelRequested": false}`
- `409 Conflict` / `410 Gone`：租约已失效、任务已被取消或已进入终态；Bridge 停止续租并安全中止未提交副作用的操作。

---

### 4.4 提交最终回执

`POST /bridge/v1/jobs/{jobId}/complete`

任务结束时提交最终结构化回执。因网络抖动可能发生重传，Broker 必须以 `(jobId, leaseToken)` 保证幂等接收：相同终态重复提交返回 `200 OK`。

#### 请求体
```json
{
  "bridgeId": "home-mac-01",
  "leaseToken": "lease_opaque_random_token",
  "status": "succeeded",
  "errorCode": null,
  "summary": "health check passed",
  "startedAt": "2026-11-19T08:00:05Z",
  "finishedAt": "2026-11-19T08:00:09Z",
  "durationMs": 4120,
  "metrics": {
    "battery_percent": 78,
    "temperature_c": 31.5
  },
  "hasLocalDiagnostics": false
}
```

#### 字段约束
- `status` 使用执行终态 `succeeded`、`already_done`、`human_required`、`device_offline`、`device_overheated`、`failed` 或 `unknown`。排队期的 `expired` 与 `canceled` 由 Broker 管理。
- `errorCode`：有限蛇形错误码（如 `hdc_timeout`、`element_not_found`、`auth_expired`、`captcha_required`、`overheated`），不超过 64 字符；严禁放原始异常堆栈。
- `summary`：短脱敏说明（不超过 256 字符），严禁包含账号、手机号、Cookie 或原始页面文本。
- `metrics`：扁平键值对象，键数 `<= 32`，值仅允许 `int | float | bool | str(<=128) | null`。
- 整份请求体限制 `<= 16 KiB`。

#### 响应
- `200 OK`：`{"ok": true, "jobId": "job_01J...", "status": "succeeded"}`
- `409 Conflict`：任务已由其他终态关闭且与本次不一致。

Bridge 将相同 `jobId` 绑定到首次认领的设备、动作、参数、幂等键和租约令牌。重新投递应携带原始请求，改变请求会被拒绝。相同请求会补发已保存的不可变回执。

提交前取消可安全终止执行。客户端已持久化 `submitting` 后，在原预算内完成提交与确认，结果不确定时保留 `unknown`。Broker 关闭租约也须保留该防重语义。

---

## 5. Broker 侧推荐实现约定（供外部调度器参考）

> 本节为参考建议，不强制绑定 `moyu-harmony-pilot` 客户端代码。外部系统（如个人任务调度器或统一控制面）可按自身架构暴露 Dispatcher API。

1. **任务提交与查询建议**：
   - `POST /dispatch/v1/jobs`：接收 `deviceAlias`、`action`、`idempotencyKey`、`params`、`ttlSeconds`、`timeoutSeconds`；按 `idempotencyKey` 幂等返回已有或新建的 `jobId`。
   - `GET /dispatch/v1/jobs/{jobId}`：查询任务实时状态与脱敏回执。
2. **租约超时处理**：
   - 当 `leased` / `running` 任务的 `leaseExpiresAt` 过期且未收到 `complete` 时，Broker 应将其标记为 `unknown`（而非自动重新置回 `queued`），避免对已产生副作用的任务二次重放。
3. **过期清理**：
   - `queued` 任务超过 `expiresAt` 仍未被认领时标记为 `expired`（典型场景：本地宿主机整天未开机，次日不再补跑过期任务）。
   - Broker 建议仅保留最近 N 天或固定上限条数的历史任务，防止存储无限膨胀。
4. **客户端退避与错误码**：
   - `401 / 403`：Token 无效或越权，Bridge 记录受限错误并大幅拉长退避间隔（防止死循环轰炸）。
   - `429 / 5xx` / 网络超时：Bridge 使用带随机抖动的指数退避（例如 5s → 10s → 20s → 60s 上限）。

## 6. 本地离线协议验收

`moyu_harmony_pilot.testing.broker.MockBroker` 提供内存中的四端点契约夹具。它校验请求字段、设备能力、租约、回执大小与重复完成，支持注入 `BridgeWorker(client=broker)`。需要同时验证 HTTP 请求头、JSON 编码和认证时，将它作为 `HttpBridgeClient` 的 opener。

```python
from moyu_harmony_pilot.bridge import HttpBridgeClient
from moyu_harmony_pilot.testing.broker import MockBroker

# config 为测试构造的 BridgeConfig，job_payload 为完整的合成任务字典。
broker = MockBroker(config.bridge_id, token=config.token)
broker.enqueue(job_payload)
client = HttpBridgeClient(config, opener=broker)
# 把 client 注入使用 FakeHdcTransport 的 BridgeWorker 后执行 run_once()。
```

该工具全程使用内存，适用于离线协议验收。参考用例覆盖真实 worker 与 HTTP 编码器的组合、白名单过滤、设备互斥、取消信号、重复回执及冲突拒绝。

```bash
.venv/bin/pytest tests/test_mock_broker.py tests/test_bridge.py tests/test_bridge_parallel.py
```

多版本真机、断网恢复、受控副作用及服务退出验收项目见[发布检查清单](release-checklist.md)。

## 7. 回执保留与防重记录

`ReceiptStore.prune(older_than_days=N)` 将已关闭且超过保留期的 `succeeded` / `already_done` 回执载荷裁剪为任务防重记录。保留期限从最近确认回执时间计算，运行中、待发送和 `unknown` 记录完整保留。收到冲突或租约关闭响应的回执完整保留。旧数据库缺少正向确认来源，迁移后保留原回执，直到再次投递获得成功确认。

```bash
harmony-pilot receipts prune --database .state/bridge/WORKER_NAMESPACE/receipts.sqlite3 \
  --bridge-id example-worker --older-than-days 90 --json
```

`WORKER_NAMESPACE` 为该 Broker URL 与 Bridge ID 对应的本地状态目录。命令全程离线，返回 `archived` 数量与 `job_tombstones_retained=true`。已归档任务再次投递会拒绝执行，应到 Broker 查询原确认结果。防重记录长期保留，每个任务仍有一条小记录，SQLite 释放页由数据库复用，文件大小可能保持原值。
