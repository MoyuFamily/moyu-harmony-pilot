# Pull Bridge Protocol Specification (v1 Draft)

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
- **时间戳**：统一使用 UTC ISO-8601 字符串（如 `"2026-11-19T08:00:00Z"`）或整型 Unix 秒（本协议统一采用 **UTC ISO-8601 字符串**）。
- **协议版本头**：`X-Harmony-Pilot-Protocol: 1`

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
- `status`：必须是第 3 节定义的执行终态枚举之一。
- `errorCode`：有限蛇形错误码（如 `hdc_timeout`、`element_not_found`、`auth_expired`、`captcha_required`、`overheated`），不超过 64 字符；严禁放原始异常堆栈。
- `summary`：短脱敏说明（不超过 256 字符），严禁包含账号、手机号、Cookie 或原始页面文本。
- `metrics`：扁平键值对象，键数 `<= 32`，值仅允许 `int | float | bool | str(<=128) | null`。
- 整份请求体限制 `<= 16 KiB`。

#### 响应
- `200 OK`：`{"ok": true, "jobId": "job_01J...", "status": "succeeded"}`
- `409 Conflict`：任务已由其他终态关闭且与本次不一致。

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
