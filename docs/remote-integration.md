# 跨机器接入与 Pilot 宿主机交付

远端业务通过 Broker 提交任务。连接 HarmonyOS 手机的 macOS/Linux 宿主机运行 Pilot Bridge，主动向 Broker 发起 HTTPS 请求，执行本机已安装且在白名单内的动作，再上报结构化回执。

```text
远端业务 ── 提交、查询 ──▶ HTTPS Broker
                              ▲
                              │ 拉取、续租、回执
                       Pilot 手机宿主机
                              │ 官方 hdc
                         HarmonyOS 手机
```

## 接入双方交付物

| 部署位置 | 交付物 |
| --- | --- |
| 手机宿主机 | Pilot、官方 hdc、设备授权、外部插件及其依赖、设备别名、Bridge 配置、持久化状态目录、常驻服务 |
| Broker | [协议 v1](bridge-protocol.md) 的四个 Bridge API、认证、任务和租约持久化、单设备互斥、终态回执幂等接收 |
| 业务调用方 | 提交与查询适配、稳定幂等键、业务日期与超时、状态和指标映射、业务通知 |

Pilot 提供出站 Bridge 客户端、插件 SDK、配置检查、连接探测、离线测试和服务模板。Broker 与业务插件由接入方独立维护。业务方的提交/查询 API 可以采用协议第 5 节的建议路径。

## 1. 准备宿主机

安装 Python 3.11+ 和官方 hdc，按[设备准备指南](device-setup.md)完成手机调试授权。将经过验证的 Pilot wheel 安装进宿主机虚拟环境，并固定部署版本。OCR 插件按需安装对应的可选依赖。服务与交互式检查使用同一个解释器、hdc 路径和设备访问用户。

从源码分发包或仓库根目录复制通用示例。以下命令在手机宿主机执行。

```bash
mkdir -p .state/remote-integration
chmod 700 .state .state/remote-integration
cp -R examples/bridge/plugins .state/remote-integration/
cp examples/bridge/bridge.example.json .state/remote-integration/bridge.local.json
cp devices.example.yaml .state/remote-integration/devices.local.yaml
chmod 600 .state/remote-integration/bridge.local.json
chmod 600 .state/remote-integration/devices.local.yaml
```

编辑设备配置，保留要接入的设备别名并填写本机串号。编辑 Bridge 配置中的 `bridge_id`、HTTPS Broker 地址和 `allowed_actions`。示例白名单允许 `device.health` 和只读取电量的 `example.health_check`。

通过宿主机的凭据管理方式写入 `bridge-token.local`，将文件权限设为 `0600`。也可以设置 `HARMONY_PILOT_BRIDGE_TOKEN`。Token 从服务进程的环境或配置文件加载，Broker 侧绑定对应 `bridgeId` 和设备范围。

`devices_config`、`state_dir`、`token_file` 和 `plugin_paths` 的相对路径均相对于 Bridge 配置文件。已有本地调用应继续使用同一个持久化状态目录。

## 2. 导出部署契约并探测连接

```bash
harmony-pilot serve --config .state/remote-integration/bridge.local.json --check --json
harmony-pilot serve --config .state/remote-integration/bridge.local.json --probe --json
```

`--check` 在本地校验插件和配置，输出每台设备的白名单以及动作版本、参数 Schema、指标 Schema、超时与副作用级别。将这些动作契约交给 Broker/业务开发者。公开元数据由插件作者维护，分享前核对其中的描述和示例。

`--probe` 读取设备遥测并发送心跳。退出码 `0` 表示 Broker 已确认心跳，设备是否在线由 `data.devices[].connectionState` 表示。该命令保留队列任务及已保存回执，并更新本地健康探针文件。常驻 worker 运行期间使用 `--health` 查看最近的心跳，避免实例锁冲突。

## 3. 完成一次通用远程任务

业务调用方通过 Broker 的提交 API 创建以下任务，日期和任务键由调用方生成。此例是派发 API 的建议请求体。

```json
{
  "deviceAlias": "harmony-01",
  "action": "example.health_check",
  "idempotencyKey": "integration-health-001",
  "params": {"minimum_battery": 20},
  "ttlSeconds": 300,
  "timeoutSeconds": 30
}
```

Broker 将请求转为协议 v1 的 claim 响应，补齐 `jobId`、`leaseToken`、`leaseExpiresAt`、`createdAt` 和 `expiresAt`。在宿主机运行一次 worker。

```bash
harmony-pilot serve --config .state/remote-integration/bridge.local.json --once --json
```

`--once` 会执行所认领的白名单动作。检查 Broker 收到的 complete 回执，电量达到阈值时应为 `succeeded`，指标包含 `battery_percent` 和 `ready`。随后通过业务查询入口验证结果关联到原 `jobId`。

## 4. 部署业务插件与常驻服务

外部插件可以作为 Python 包安装到 Pilot 虚拟环境，通过 `moyu_harmony_pilot.actions` entry point 注册。也可以部署包含 `actions.json` 的目录，并加入 `plugin_paths`。插件模块之间需要包导入时使用安装包方式。详细契约见[动作开发指南](actions.md)。

在设备对应的 `allowed_actions` 中加入业务动作，重新执行 `--check`。有副作用的动作实现 `IdempotentAction`，声明参数与指标 Schema，并通过 `inspect → submit → confirm` 完成核验。模块导入和构造函数应保持纯初始化，以便服务预检能够安全加载。

按宿主机选择 [systemd](../deploy/systemd/harmony-pilot.service) 或 [launchd](../deploy/launchd/org.example.harmony-pilot.plist) 模板，填入解释器、配置、日志和工作目录的绝对路径。配置文件中的 `plugin_paths` 使服务可直接发现外部目录。hdc 路径通过进程环境或 `--hdc-path` 配置，确保服务用户拥有设备访问权限。

```bash
harmony-pilot serve --config .state/remote-integration/bridge.local.json --json
harmony-pilot serve --config .state/remote-integration/bridge.local.json --health --json
```

第一条命令用于前台启动或服务的执行入口，第二条由独立进程执行健康检查。常驻服务的具体安装和退出行为见[Bridge 运维说明](bridge-worker.md)。

## 本地执行迁移与恢复

- 所有控制同一物理设备的进程共用同一个 `state_dir`。停止旧执行进程后切换配置，持续保留 `actions.sqlite3` 和 Bridge 回执数据库。
- 保持动作 ID、版本、参数和幂等键一致。已确认的本地执行经 Bridge 重复派发会返回 `already_done`，已有 `unknown` 会继续要求核对。改变幂等键会形成新的执行身份。
- Broker URL 或 `bridge_id` 变更会建立新的回执命名空间。迁移前完成旧配置下的回执投递，或保留旧配置用于恢复投递。动作账本仍由共用的 `state_dir` 管理。
- 设备别名是远端路由标识，Broker 需限定其归属。业务账号身份通过接入方的稳定业务标识维护，远程回执仅返回允许的脱敏指标。
- 业务进程重启后查询已保存的 `jobId`。任务提交重试使用同一个幂等键。未领取任务的 TTL 应覆盖预期排队时间，业务日期在参数中显式传递。
- `succeeded` 和 `already_done` 表示确认完成。`human_required` 进入人工处理，`device_offline` 和 `device_overheated` 交给调度策略处理，`unknown` 保持停止重放。现阶段的未知结果通过只读核对或人工复核处理。

## 验收与协议测试

`tests/test_bridge.py` 使用 Fake Broker、Fake hdc 和内存 HTTP 响应，覆盖真实 CLI 的插件加载、请求编码、回执解码、断网重传、重启恢复、取消、过期租约和本地账本迁移。测试全程离线。

```bash
.venv/bin/pytest tests/test_bridge.py
```

部署验收依次完成配置检查、心跳探测、只读示例派发、相同任务重传以及服务重启后的回执恢复。业务插件的真机副作用验收使用经明确授权的目标流程。
