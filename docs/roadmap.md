# Moyu Harmony Pilot — 后续迭代路线图

> **文档状态** — 活文档（候选版本 `0.2.0rc1`，M0–M3 仓库实现与 M4 仓外应用侧端到端集成已完成）<br>
> **目标平台**：HarmonyOS NEXT（HarmonyOS 5 / 6+，API 12+）物理设备<br>
> **演进原则**：长期优先降低外部插件开发与维护成本，提升开发侧和运行侧 Agent 友好度，并以此作为后续变更的决策与验收依据。所有迭代同时遵守 [requirements.md](requirements.md) 与 [AGENTS.md](../AGENTS.md) 确立的官方工具链、核心零重依赖、单向分层 DAG、Fail-Closed、幂等及公开仓库边界，不引入 Android/ADB、私有业务脚本或远程任意 Shell 透传。

---

## 当前实现进展（0.2.0rc1）

本轮已实现应用查询与 HAP 下载部署、引用式输入与常用本地交互、UI 等待预算透传、账本查询/只读对账/人工成功结案/裁剪、Bridge 多设备子进程执行、自包含 HTML 检查页及报告，以及插件离线参数预检 `plugins validate`。使用方式见[应用管理、交互与运行诊断](app-management.md)和[Action 插件](actions.md)。

本轮 P0 已补齐阶段指标与上下文交互、最低版本校验、公共测试工具、三段式独立插件、脱敏 Fixture 采集、本地 Mock Broker、OCR 与 MCP 内联图片、敏感屏/弹窗规则、回执保留、严格类型检查和 Tag 发布工作流。社区插件可通过 `plugins init` 生成独立工程并运行精确点击契约。6.1 / API 23 与 7.0 / API 26 两台设备已通过只读入口与 Bridge 双子进程验收，6.1 设备完成经授权的亮灭屏幂等、防重与只读对账、Bridge 回执恢复及退出验证。离线套件已扩展至 956 项，历史 CI 和具体设备覆盖记录见 [兼容矩阵](compatibility.md)，发布步骤见 [发布检查清单](release-checklist.md)。

## 1. 迭代优先级总览

| 优先级 | 主题 | 核心目标 | 涉及模块 |
| :--- | :--- | :--- | :--- |
| **P0** | 插件开发体验（DX）与契约测试工具 | 降低独立插件的开发、离线验证、接入与升级成本，完善阶段回执与受控交互 | `actions`, `cli`, `examples` |
| **P0** | 开发侧与运行侧 Agent 友好 | 加强自描述、受控本地探索、结构化反馈与安全接管 | `actions`, `cli`, `mcp`, `vision`, `ui` |
| **P0** | 账本运维与 `unknown` 对账保护 | 巩固已实现的只读对账、人工成功结案与历史裁剪 | `actions`, `bridge`, `cli` |
| **P0** | 多版本真机兼容、类型检查与发布自动化 | 冻结协议 v1.0、补齐 `mypy`/`py.typed`、跨版本兼容 Fixture 与 Tag 自动发版 | `device`, `ui`, CI/发版 |
| **P1** | 单宿主机多设备并发与 Bridge 运维增强 | 加固已有并发能力，完善心跳元信息、TLS/密钥轮换与残留清扫 | `bridge`, `actions`, `hdc` |
| **P1** | 底层驱动健壮性补全 | 验证已有等待与输入能力，完善 Want 控制、无线 `hdc` 辅助与传输层评估 | `hdc`, `ui`, `device` |
| **长期** | 核心驱动组件独立拆分 | 在接口充分稳定后按 DAG 切分线评估拆出独立纯驱动包 | `hdc`, `ui`, `device`, `vision` |
| **仓外协同** | 外部 Broker 与插件生态演进（M4 已闭环 ✅） | 在独立仓库持续维护外部调度面与业务插件，保持对协议 v1 / Action API v1 的兼容演进 | 本仓之外 |

---

## 2. P0 安全与可靠性：幂等运维和发布工程化

### 2.1 账本运维与 `unknown` 只读对账/核销

- **已实现**：`ledger list` / `inspect` 查询记录，`reconcile` 在设备锁保护下调用插件只读 `confirm()`，`resolve` 在人工核验后记录成功结案及备注，`prune` 按天数裁剪已成功记录并保留 `submitting` / `unknown`。具体用法见[应用管理、交互与运行诊断](app-management.md)。
- `ledger prune` 裁剪成功动作记录会移除对应防重记录，须按业务防重期限设置保留天数。新增 `receipts prune` 按确认时间裁剪已关闭成功回执的载荷，保留永久任务防重记录及全部运行中、待发送和不确定回执。旧库事务迁移与并发裁剪已纳入离线验证。
- **验收标准**：保留提交前 `submitting` 预留；崩溃遗留的 `submitting` 恢复为 `unknown`，无法确认时保持 `unknown`，只读对账不得重放 `submit()`；测试覆盖人工结案、防误裁剪及纯 JSON 输出。

### 2.2 多版本真机兼容、类型检查与发布自动化

- 已添加 `py.typed`，全部源码启用 strict mypy。分发测试在独立环境离线安装 wheel 并验证下游类型发现、正确调用与错误类型拒绝。
- CI 已通过 macOS / Linux × Python 3.11 / 3.12 的编译、ruff、830 项离线测试、mypy 与分发构建检查，验收提交为 `d8ddbf6`。`v*` 标签工作流复用 CI，校验版本、构建 sdist/wheel、生成 SHA-256 校验和，复验上传附件后发布。
- Pull Bridge 协议已冻结为 [v1.0](bridge-protocol.md)，继续使用协议头和路径中的 `v1`。
- 兼容矩阵新增 5.x / 6.x 手机、平板、折叠屏四份合成遥测和布局样本，验证字段形状、多显示器布局、窗口焦点列和解析容错。所有新增样本均标记 `SYNTHETIC` 和 `device_capture=false`。
- 当前 6.1 / API 23 单机只读与亮灭屏幂等复验已通过，受管临时文件已清理。尚待完成经人工审查的真实脱敏样本补充、其他设备形态及应用部署/输入等副作用验收。操作与记录格式见[兼容矩阵](compatibility.md)和[发布检查清单](release-checklist.md)。
- 新增 6.1 / API 23 与 7.0 / API 26 双设备只读验收，覆盖默认及显式 `display_id=0` 的布局与截图、OCR、MCP 内联图片，以及外部目录插件在 Bridge 双子进程中的并发执行和重复任务防重。

---

## 3. P1 核心增强：Bridge 生产可用性与驱动健壮性

### 3.1 单宿主机多设备并发调度与 Bridge 运维增强

- **现状与缺口**：
  - 已支持单个 Bridge 通过 `max_jobs`（默认 1，范围 1–32）为不同设备启动执行子进程；每个子进程使用主线程 `SIGALRM` 预算，同一物理设备仍由设备锁互斥；
  - `BridgeWorker` 心跳载荷中 `osVersion` 目前固定为 `null`，未采集缓存系统版本；
  - `HttpBridgeClient` 每次请求新建 opener 且仅支持系统默认 SSL 信任库，暂不支持企业内网自签 CA、mTLS 证书或无停机密钥轮换。
- **规划交付项**：
  1. **加固已有并发执行**：持续验证多设备子进程隔离、超时回收与单设备互斥，并补充多版本真机实践；
  2. **心跳元信息缓存**：按设备在线会话缓存 `osVersion` / `api_version` 等低频变动属性，在心跳中补全 `osVersion` 字段而不增加额外轮询开销；
  3. **网络与凭据运维增强**：
     - 复用 HTTPS 连接减少长轮询握手开销；
     - 支持在私密配置中显式指定 `ca_bundle`（内网私有 CA）与客户端证书（mTLS）；
     - 支持 `SIGHUP` 或鉴权失败前重读 `token_file`，配合零停机密钥轮换；
  4. **设备端残留临时文件清扫**：规划显式启用的清扫方式，严格限定 `/data/local/tmp/moyu_pilot_*` 受管前缀，处理异常断连/强杀留下的文件；默认只做只读检查。
- **验收标准**：
  - 离线模拟多设备并发 Broker 测试通过（验证跨设备并行、同设备互斥、单子进程超时回收不影响其他设备）；
  - 断网重连、Token 轮换与异常退出后的回执不丢失、不重复提交。

### 3.2 底层驱动（`hdc` / `ui` / `device`）健壮性补全

- **现状与缺口**：
  - `UiDriver.wait_for` / `wait_gone` 已透传剩余抓取预算与窗口、屏幕过滤参数；`input_text_ref` 和中文/特殊字符参数处理已有离线测试，仍需更多设备兼容性验证；
  - 已封装 `bm dump` 查询应用与主 Ability；`HarmonyDevice.start_app` 尚不支持受控 Want URI/参数；
  - 无线 `hdc`（局域网 `hdc tconn`）连接检查与断线恢复尚未封装。
- **规划交付项**：
  1. 持续验证等待预算、过滤参数及引用式输入在不同系统版本上的行为；
  2. 完善设备端中文/特殊字符输入能力探测及最佳实践文档；真机输入须经明确授权；
  3. 扩展 `device` 应用控制：评估为 `start_app` 增加严格校验的 Want 参数/URI；
  4. 补充无线 `hdc` 连接管理辅助（显式白名单主机/端口校验，避免隐式开放风险）；
  5. **传输层性能评估**：结合真机高频交互实测耗时，评估是否基于已预留的 `HdcTransport` 协议（严守不逆向私有守护协议）优化调用开销。
- **验收标准**：
  - 全部新增参数具备严格输入校验，严禁未转义 shell 拼接；核心层仍保持零第三方依赖与 DAG 单向依赖约束。

---

## 4. P0 长期优先：插件 DX 与 Agent 友好

### 4.1 AI Agent 本地探索体验与视觉能力升级

- MCP 新增显式 `--inline-images`、`--max-image-bytes` 开关。`screenshot(inline=true)` 返回有界 `ImageContent`，保留本地私有文件、清理策略、敏感屏与布局一致性门控。
- `harmony-pilot ocr` 与 MCP `--enable-ocr` 复用本地观察接口，返回有界文本框、原图尺寸与缩放比例，支持自动选择已安装后端。
- 敏感屏检测结合控件类型、输入上下文和系统提示，补充生物识别及高危权限用例，并覆盖普通密码管理菜单的误报回归。
- `PopupWatcher` 支持精确文案、正则和节点 ID，继续要求显式包名、唯一命中、非敏感界面及处理预算。插件可通过 `ctx.dismiss_popup()` 复用。
- CLI 自描述、MCP 发现和离线测试覆盖新开关、结构化错误、退出码、步数/时间预算、引用新鲜度与不确定交互后的重观测。使用方式见 [Action 指南](actions.md)与 [MCP 指南](mcp.md)。

### 4.2 插件开发体验（DX）与契约测试生态

- 新增 `plugins init`，从已安装 Pilot 的模板生成独立工程，同步包名、模块名、动作 ID、测试及 CI。分发回归覆盖干净环境中的生成、构建、安装发现与离线契约，作者可以先运行示例，再逐步替换业务实现。
- 公共 `moyu_harmony_pilot.testing` 已提供 Fake 传输、假设备构建器、离线 I/O 护栏及可复用契约断言。生产模块保持对测试工具零依赖。
- 独立插件模板复用公共工具，包含可安装的只读电量插件与通用三段式演示开关插件，使用合成 UI 验证发现、参数/指标、提交前预留、幂等、超时、敏感屏和过温边界。
- Fake 支持按控件 ID 与包名声明预期点击序列，公共幂等断言会检查误点和遗漏。模板采用精确目标推进布局，并覆盖点击无关按钮时的失败回归。
- `fixture capture` 在设备锁下只读获取布局，主机内存脱敏后原子写入私有候选文件，始终返回 `manual_review_required=true`。人工审查后才能纳入公开夹具。
- `testing.broker.MockBroker` 提供四端点内存协议验收及 HTTP opener 接口，真实 Worker/HTTP 编码器的组合已纳入离线回归。
- `plugins validate` 继续提供离线参数和幂等键预检。可信插件的发现和预检无需设备连接，详细流程见[插件模板](../examples/plugin-template/README.md)。

后续优先围绕实际编写与排错成本继续打磨。

- 将选择器查询与同次采样的紧凑引用衔接，提供唯一目标解析，并验证零匹配、多匹配、窗口包名继承、剪枝、敏感屏与引用新鲜度。
- 为离线开发提供显式启用的阶段诊断，区分契约校验错误、插件编程错误和预期超时，暴露可定位且脱敏的信息。提交后的不确定结果继续保持 `unknown` 防重。
- 以独立插件从已安装 wheel 起步的完整路径持续验收创建、契约测试、能力发现和版本升级，检查文档、模板与机器可读说明的一致性。

### 4.3 插件契约、受控交互与版本兼容

- 已区分检查阶段与最终成功指标。`Inspection` 支持摘要、错误码和可选的 `inspection_metrics_schema`，人工接管/失败可以省略成功指标，保留闭合字段、类型和隐私大小限制。
- `ctx.interact()` 复用 Runner 的设备锁、计时器、步数与提交前取消信号。`inspect` / `confirm` 仅可观察，写交互仅在持久化预留后的 `submit` 阶段开放，Runner 返回后会话失效。
- 候选版本为 `0.2.0rc1`。新增 `min_pilot_version` 注册校验，阶段指标 Schema 与上下文交互要求声明至少 `0.2.0.dev0`，独立模板以 `0.2.0rc1` 固定依赖与测试基线。旧字段消费者与已有账本保留兼容路径。
- 离线覆盖成功指标必填时的人工接管/失败、最终成功校验、共享锁/计时器、预算/取消、阶段副作用边界、`unknown` 防重与旧版本拒绝。迁移说明见 [Action 插件指南](actions.md)。

---

## 5. 核心驱动组件的长期拆分规划

驱动组件与上层执行、接入层沿既有单向依赖 DAG 划分。[架构约定](requirements.md)

```text
驱动组件候选边界    hdc → ui → device (+ 可选 vision)
上层执行与接入边界  actions → bridge / cli / mcp
```

待 HarmonyOS NEXT 多版本真机实践验证底层接口（`HdcClient`、`UiDriver`、`CompactSnapshot`、`HarmonyDevice`、`OcrEngine`）趋于稳定后，可按社区与生态需求评估将底层纯驱动包独立发布，供仅需 HarmonyOS NEXT Python 自动化驱动、无需 Bridge/MCP 的开发者零负担引入；`moyu-harmony-pilot` 保持对驱动包的组合封装与向后兼容。

---

## 6. 仓外协同与生态演进（M4 已闭环，维护在本仓之外）

M4 已在仓外完成外部兼容 Broker 与独立业务插件的端到端接入闭环（未修改 `moyu-harmony-pilot` 核心代码）。按照项目职责边界，以下内容继续**保持在本仓库之外**，通过稳定的 `ActionPlugin API v1` 与 `Pull Bridge Protocol v1` 与本仓解耦协同演进：

- **外部 Broker / 控制面**：持续维护协议 v1 四个 Bridge 端点与 Dispatcher API，按需对接定时编排、任务队列、Token 轮换/吊销、设备在线监控以及 `human_required` / `unknown` 人工接管通知闭环；
- **业务侧外部插件仓库**：基于 [examples/plugin-template](../examples/plugin-template/README.md) 持续迭代独立业务插件，配合本仓 `0.1.0` 及后续版本的协议/驱动演进通过 [docs/plugin-catalog.md](plugin-catalog.md) 记录已验证的 Pilot 兼容版本与契约测试结果。
