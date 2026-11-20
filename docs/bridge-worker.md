# Pull Bridge 守护进程运行指南

如果你正在从零搭建跨机器接入环境，建议先配合阅读 [跨机器接入与 Pilot 宿主机交付](remote-integration.md)。本文重点说明 `harmony-pilot serve` 的配置规则、运行模式、容错恢复机制与常驻服务部署。

---

## 1. 配置准备

先复制示例配置，填入 Broker 地址、设备别名与白名单能力，并收紧私有文件权限：

```bash
cp bridge.example.json bridge.local.json
chmod 600 bridge.local.json devices.local.yaml
```

### 关键配置规则

- 可选 `max_jobs` 设置并发执行上限，范围为 1–32，默认值为 1。大于 1 时，Bridge 为不同设备启动独立执行子进程，各自拥有主线程超时计时器，父进程维护租约和持久化回执。同一物理设备继续通过共享状态目录内的设备锁保持互斥。
- **路径解析**：`devices_config`、`state_dir`、`token_file`、`plugin_paths` 中的相对路径，均**相对于 Bridge 配置文件所在目录**解析，避免服务工作目录变化导致找不到文件。
- **凭据加载**：优先读取环境变量 `HARMONY_PILOT_BRIDGE_TOKEN`（或自定义的 `token_env`）；环境变量为空时，从权限为 `0600` 的 `token_file` 读取。切勿将运行配置和凭据提交到版本库。
- **插件目录（`plugin_paths`）**：填写包含 `actions.json` 的外部插件目录数组。显式配置后会覆盖当前进程的 `HARMONY_PILOT_PLUGIN_PATHS` 环境变量（配置为空数组 `[]` 表示禁用目录发现）；省略该字段则沿用环境变量。无论如何配置，已安装 Python 包的 entry point 仍会参与发现，并统一受设备白名单限制。
- **传输安全**：远端 Broker 必须使用经过证书校验的 `HTTPS`，且拒绝 HTTP 重定向；本地联调仅显式允许 `http://127.0.0.1` 与 `http://localhost`。

---

## 2. 运行模式一览

`harmony-pilot serve` 提供 5 种运行方式，其中 4 个模式开关互斥：

```bash
harmony-pilot serve --config bridge.local.json --check --json
harmony-pilot serve --config bridge.local.json --probe --json
harmony-pilot serve --config bridge.local.json --once --json
harmony-pilot serve --config bridge.local.json --json
harmony-pilot serve --config bridge.local.json --health --json
```

| 模式 | 行为说明 | 适用场景与退出码 |
| :--- | :--- | :--- |
| `--check` | **纯离线校验**私有配置、设备别名映射及白名单插件元数据，输出 `protocol_version`、`bridge_id`、每设备 `allowed_actions` 及完整参数/指标 Schema | 上线前预检、向 Broker 导出动作契约（注意：加载插件模块与构造函数时严禁触发真机 I/O） |
| `--probe` | 读取本机设备在线态与硬件遥测，向 Broker 发送一次心跳；**不认领任务、不改动待发回执**，并刷新本地健康文件 | 验证网络连通与 Token 是否有效；`0` 表示心跳被确认（设备是否在线看 `devices[].connectionState`），Broker 报错返回 `1`，与运行中 worker 锁冲突返回 `6` |
| `--once` | 完成一轮“恢复回执 → 心跳 → 认领并执行（如有）→ 上报回执”循环后退出 | 单次冒烟验收或受控联调（会真实执行白名单动作） |
| 默认（无模式参数） | 持续常驻运行 | 生产守护进程入口 |
| `--health` | 独立检查最近一次成功心跳是否新鲜，不争抢运行实例锁 | 外部监控探针；新鲜返回 `0`，缺失或过期返回 `1`，配置错误返回 `2` |

---

## 3. 执行、租约与崩溃恢复机制

- **并发与锁隔离**：单个 worker 每次最多认领并执行 1 个任务；每台物理设备拥有独立的设备锁，因此同一台宿主机上可以通过多个独立 worker 并发驱动不同设备。同时，每个 `(broker_url, bridge_id)` 命名空间持有独立实例锁，防止两个进程误操作同一份回执状态。
- **白名单与严格校验**：每台设备只向 Broker 上报自己的白名单 `capabilities`。发现外部插件时会先按白名单过滤再加载模块，标记为 `local_only` 的动作在配置校验阶段即被拒绝。认领到任务后，所有字段、参数 Schema、时间戳与租约截止时间都会先做校验。
- **后台续租与取消边界**：长任务执行期间，后台线程自动续租并同步发送 `busy` 心跳（心跳失败不会中断续租）。如果租约被拒、过期或收到 Broker 取消信号，会在准备或 `inspect` 阶段的下一个检查点安全中止；一旦有副作用动作在账本提交了 `submitting` 预留，系统会允许 `submit` 与 `confirm` 在原预算和任务过期时间内尽量走完，避免半途留下未确认状态。
- **空闲轮询调度**：空闲周期会刷新设备连接与锁状态，仅在到达心跳间隔时采样电量与电源遥测；长轮询等待时间会计入建议轮询间隔，收到 Broker 的 `hasPendingJobs` 提示时会立即发起下一轮认领。
- **持久化回执（SQLite Outbox）**：任务预留与最终回执保存在 `0600` 权限的 SQLite 数据库中。回执会先落盘再发 HTTP 请求，网络重试期间内容不可变，进程重启后未送达的回执会自动补发；崩溃时残留的 `running` 任务会恢复为 `unknown`。若 Broker 返回 `409` 或 `410`，本地会停止重试并保留已记录的结果。
- **优雅退出与退避重连**：收到 `SIGTERM` / `SIGINT` 后停止拉取新任务、通知取消守卫、等待已提交预留的动作收尾并尝试投递最终回执；未送达的回执会在下次启动时继续投递。网络抖动采用带随机抖动的指数退避；鉴权或协议错误则拉长重试间隔并在 `stderr` 输出脱敏告警。
- **跨升级保留状态**：升级或重启服务时请务必保留 `state_dir`。其中动作账本保护物理设备级幂等身份，回执库按 `(broker_url, bridge_id)` 隔离。
- **脱敏上报边界**：远端回执中的摘要由执行状态生成，指标须通过插件声明的 Schema 校验并自动剔除已知凭据与硬件串号；截图、UI 布局树、任意 Shell 和任意文件读写始终不进入 Bridge 通道。

---

## 4. 常驻服务部署（`systemd` / `launchd`）

仓库提供了开箱可用的服务模板，分别位于 `deploy/systemd` 与 `deploy/launchd`，两者均调用同一个 `harmony-pilot serve` 入口。

部署前通用检查：
1. 将模板中的解释器、配置文件、工作目录与日志路径全部替换为宿主机绝对路径；
2. 确保运行服务的系统账号具备官方 `hdc` 执行权限，且手机已对该账号完成调试授权；
3. 提前准备好 `0700` 权限的 `state_dir` 和 `0600` 权限的配置文件/凭据文件，先跑通 `--check` 再启用服务。

- **Linux（`systemd`）**：将 unit 文件安装到宿主机服务目录后，使用 `systemctl enable --now` 启动。
- **macOS（`launchd`）**：替换模板中所有 `/ABSOLUTE/PATH` 占位符、设置唯一的 `Label`，并在拥有手机连接权限的用户会话下加载 plist。服务关闭超时建议保留至 90 秒，以便完成有界网络收尾与本地清理。
