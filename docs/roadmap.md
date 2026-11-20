# Moyu Harmony Pilot — 后续迭代路线图

> **文档状态**：活文档（基于 `0.1.0.dev1`，M0–M3 仓库实现与 M4 仓外应用侧端到端集成已完成）<br>
> **目标平台**：HarmonyOS NEXT（HarmonyOS 5 / 6+，API 12+）物理设备<br>
> **演进原则**：所有迭代均严格遵守 [requirements.md](requirements.md) 与 [AGENTS.md](../AGENTS.md) 确立的硬边界——仅使用官方工具链、核心零重依赖、单向分层 DAG、Fail-Closed 安全护栏、机器优先 CLI 契约，且不引入 Android/ADB 逻辑、私有商业业务脚本或任意 shell 透传能力。

---

## 1. 迭代优先级总览

| 优先级 | 主题 | 核心目标 | 涉及模块 |
| :--- | :--- | :--- | :--- |
| **P0** | 账本运维与 `unknown` 对账核销 | 补齐幂等异常终态的只读对账、人工结案与历史裁剪闭环 | `actions`, `bridge`, `cli` |
| **P0** | 多版本真机验收与 `0.1.0` 正式发版 | 冻结协议 v1.0、补齐类型检查、多版本兼容 Fixture 与发布流水线 | `device`, `ui`, CI/发版 |
| **P1** | 单宿主机多设备并发与 Bridge 运维增强 | 突破单 worker `maxJobs=1` 限制，完善心跳元信息、TLS/密钥轮换与残留清扫 | `bridge`, `actions`, `hdc` |
| **P1** | 底层驱动健壮性补全 | 完善 `wait_for` 预算透传、应用/Want 控制、无线 `hdc` 辅助与传输层演进评估 | `hdc`, `ui`, `device` |
| **P2** | AI Agent 本地探索与视觉能力升级 | 扩展 `--local-lab` 安全交互原语、MCP 图片/OCR 观测与敏感屏/弹窗规则精度 | `actions`, `mcp`, `vision`, `ui` |
| **P2** | 插件开发体验（DX）与契约测试工具 | 沉淀公共测试工具包、脱敏 Fixture 采集命令、三段式通用示例与协议 Mock Broker | `testing`, `cli`, `examples` |
| **长期** | 核心驱动 SDK 独立拆分 | 在接口充分稳定后按 DAG 切分线评估拆出独立纯驱动包 | `hdc`, `ui`, `device`, `vision` |
| **仓外协同** | 外部 Broker 与插件生态演进（M4 已闭环 ✅） | 在独立仓库持续维护外部调度面与业务插件，保持对协议 v1 / Action API v1 的兼容演进 | 本仓之外 |

---

## 2. P0 近期高优先：补齐生产运维闭环与 `0.1.0` 正式发版

### 2.1 账本运维与 `unknown` 只读对账/核销闭环

- **现状与缺口**：
  - `ActionLedger` 目前仅提供执行期的 `lookup` / `reserve` / `finish`；[docs/actions.md](actions.md) 已标注自动对账与运维核销命令属于后续规划。
  - 当有副作用动作进入 `unknown` 状态时，运维人员缺乏标准 CLI 入口查看账本、发起只读复核或显式结案；长期无人值守运行后，本地 `actions.sqlite3` 与 `receipts.sqlite3` 亦缺少归档/裁剪机制。
- **规划交付项**：
  1. 新增 `harmony-pilot ledger` 命令组（全部支持 `--json` 信封与语义退出码，严禁输出原始硬件串号）：
     - `ledger list`：按设备别名、`action_id`、状态（`submitting` / `unknown` / `succeeded` / `already_done`）筛选历史记录；
     - `ledger inspect`：查看单条记录的脱敏元数据、状态与最近回执摘要；
     - `ledger reconcile`：针对 `unknown` 记录在单设备锁保护下发起**只读对账**（仅调用插件 `confirm()` 或 `inspect()`，**严禁重放 `submit()`**），明确确认后收敛状态；
     - `ledger resolve`：支持运维人员在人工核验后显式标记终态并记录受限脱敏备注；
     - `ledger prune`：按保留天数或最大条数清理已完结历史记录，强制保留 `submitting` / `unknown` 与 Bridge `pending` 回执。
  2. 同步更新 `harmony-pilot schema` 自描述契约与 [skills/harmony-pilot/SKILL.md](../skills/harmony-pilot/SKILL.md)。
- **验收标准**：
  - 离线单测覆盖 `submitting` 崩溃恢复、只读对账收敛、人工核销防误用、裁剪不误删未决记录，以及 `stdout` 纯 JSON 输出契约。

### 2.2 多版本真机验收与 `0.1.0` 正式发版

- **现状与缺口**：
  - 当前版本为 `0.1.0.dev1`（Pre-Alpha），遥测解析目前主要在 HarmonyOS 6.1 / API 23 真机完成实测验证；
  - CI 尚未集成静态类型检查（`mypy` / `pyright`），尚未提供 `py.typed` 标记与自动化发版校验流程；
  - [docs/bridge-protocol.md](bridge-protocol.md) 仍处于 `v1 Draft` 状态。
- **规划交付项**：
  1. **跨版本与多形态兼容矩阵**：补充 HarmonyOS 5.x / 6.x 及平板/折叠屏多 `display_id` 设备的脱敏 `hidumper`（`BatteryService`、`PowerManagerService`、`WindowManagerService`）和 `dumpLayout` Fixture，验证解析容错；
  2. **SDK 类型契约**：在 `src/moyu_harmony_pilot/` 添加 `py.typed`，在 `[project.optional-dependencies].dev` 与 `.github/workflows/ci.yml` 中接入严格静态类型检查；
  3. **协议冻结与发版工程化**：将 Pull Bridge 协议冻结为 `v1.0`，补充 `CHANGELOG.md`、真机验收 Checklist（覆盖只读、受控副作用、断网重连、服务优雅退出）以及 Tag 触发的构建与校验和发布流程。
- **验收标准**：
  - macOS / Linux × Python 3.11 / 3.12 离线 CI（含类型检查与分发包构建测试）全绿，至少完成一轮目标真机完整验收后发布 `0.1.0`。

---

## 3. P1 核心增强：Bridge 生产可用性与驱动健壮性

### 3.1 单宿主机多设备并发调度与 Bridge 运维增强

- **现状与缺口**：
  - `actions/budget.py` 使用主线程 POSIX `SIGALRM` 实施硬超时，导致单个 `BridgeWorker` 每次仅能 `maxJobs=1` 串行执行，一台宿主机挂载多台真机时需要手动维护多份独立服务配置；
  - `BridgeWorker` 心跳载荷中 `osVersion` 目前固定为 `null`，未采集缓存系统版本；
  - `HttpBridgeClient` 每次请求新建 opener 且仅支持系统默认 SSL 信任库，暂不支持企业内网自签 CA、mTLS 证书或无停机密钥轮换。
- **规划交付项**：
  1. **按设备隔离的子进程执行池**：单 `serve` 守护进程在保持每个执行子进程独享主线程 `SIGALRM` 预算与物理 `DeviceLock` 的前提下，支持同时为多台不同 `deviceAlias` 认领并执行任务（同一物理设备仍严格保持单任务互斥）；
  2. **心跳元信息缓存**：按设备在线会话缓存 `osVersion` / `api_version` 等低频变动属性，在心跳中补全 `osVersion` 字段而不增加额外轮询开销；
  3. **网络与凭据运维增强**：
     - 复用 HTTPS 连接减少长轮询握手开销；
     - 支持在私密配置中显式指定 `ca_bundle`（内网私有 CA）与客户端证书（mTLS）；
     - 支持 `SIGHUP` 或鉴权失败前重读 `token_file`，配合零停机密钥轮换；
  4. **设备端残留临时文件清扫**：在 `doctor --cleanup` 与 worker 启动自检阶段，按严格前缀 `/data/local/tmp/moyu_pilot_*` 清理因异常断连/强杀遗留的远端临时文件。
- **验收标准**：
  - 离线模拟多设备并发 Broker 测试通过（验证跨设备并行、同设备互斥、单子进程超时回收不影响其他设备）；
  - 断网重连、Token 轮换与异常退出后的回执不丢失、不重复提交。

### 3.2 底层驱动（`hdc` / `ui` / `device`）健壮性补全

- **现状与缺口**：
  - `UiDriver.wait_for` / `wait_gone` 内部调用 `dump_tree()` 时未透传剩余超时与 `bundle_name` / `window_id` / `display_id` 过滤参数，慢布局容易超出外层预算；
  - `UiDriver.input_text` 仅接受裸像素坐标，缺乏结合 `@eN` 引用的安全封装及中文/特殊字符输入兼容性验证；
  - `HarmonyDevice.start_app` 尚不支持受控 Want URI/参数，也未封装 `bm dump` 查询已安装应用与主 Ability；
  - 无线 `hdc`（局域网 `hdc tconn`）连接检查与断线恢复尚未封装。
- **规划交付项**：
  1. 为 `wait_for` / `wait_gone` 透传每次抓取的剩余超时预算与窗口过滤参数；
  2. 新增 `input_text_ref` 与设备端中文/特殊字符输入能力探测及最佳实践文档；
  3. 扩展 `device` 应用控制：`start_app` 支持严格校验的 Want 参数/URI，补充只读应用包信息查询（`bm dump` 脱敏解析）；
  4. 补充无线 `hdc` 连接管理辅助（显式白名单主机/端口校验，避免隐式开放风险）；
  5. **传输层性能评估**：结合真机高频交互实测耗时，评估是否基于已预留的 `HdcTransport` 协议（严守不逆向私有守护协议）优化调用开销。
- **验收标准**：
  - 全部新增参数具备严格输入校验，严禁未转义 shell 拼接；核心层仍保持零第三方依赖与 DAG 单向依赖约束。

---

## 4. P2 体验与生态：AI Agent 交互、视觉能力与插件 DX

### 4.1 AI Agent 本地探索体验与视觉能力升级

- **现状与缺口**：
  - `InteractionSession` 与 MCP `--local-lab` 目前仅支持 `lab_observe`、`click_ref`、`swipe`、`back`，探索新 App 或辅助编写插件时缺少文本输入、长按、桌面返回、启动应用与显式等待能力；
  - MCP `screenshot` 当前仅返回本地文件路径元数据；
  - OCR 能力目前仅通过 Python SDK 提供，CLI 与 MCP 尚未暴露受控的本地 OCR 定位观测工具；
  - `detect_sensitive_signals` 依赖固定中英文关键词正则，在“密码管理”等普通设置页存在潜在误报，对部分风控/生物识别弹窗存在漏报风险；`PopupWatcher` 目前仅支持精确文案匹配。
- **规划交付项**：
  1. **扩展 `--local-lab` 探索原语**（仅本地显式开启，严格受步数/时间预算、`layout_hash` 新鲜度、过温与敏感屏门控约束，绝不经 Bridge 暴露）：
     - `input_text_ref`（拒绝向密码类或敏感界面输入）；
     - `long_click_ref`、`home`、`start_app`、`wait_for`；
  2. **多模态 MCP 观测增强**：在本地 MCP 模式下为 `screenshot` 提供可选内联 `ImageContent` 返回（受显式开关与尺寸上限约束，保持本地文件 `0600` 权限与滚动清理）；
  3. **本地 OCR 观测命令与工具**：提供受控的本地 `harmony-pilot ocr` CLI 与可选 MCP 只读工具（仅本地落盘/返回，不进入远程 Bridge），辅助处理 WebView / Canvas / 图片按钮定位；
  4. **敏感屏与弹窗规则精细化**：
     - 区分输入控件类型/上下文与普通菜单文案，降低敏感屏误报并覆盖系统生物识别与高危权限弹窗；
     - `PopupWatcher` 支持可选正则/节点 ID 匹配，并在 `ActionContext` 中提供标准化辅助接口供插件复用。
- **验收标准**：
  - 新增交互工具全量覆盖步数熔断、过期 `layout_hash` 拒绝、敏感屏即时中断（退出码 `4`）与 post-input 不确定状态强制重观测的离线单测。

### 4.2 插件开发体验（DX）与契约测试生态

- **现状与缺口**：
  - 外部插件仓库的离线 Fake 驱动与契约测试目前需从 `examples/plugin-template/tests/support.py` 复制维护；
  - 外部开发者从真机获取可入库的脱敏布局 Fixture 缺乏自动化工具；
  - 内置与示例插件当前多为只读示例，缺少可直接参照的通用三段式 `IdempotentAction` 示例；
  - 外部 Broker 开发者缺少独立的本地协议一致性测试夹具。
- **规划交付项**：
  1. **公共测试工具模块（`moyu_harmony_pilot.testing`）**：沉淀 `FakeHdcTransport`、假设备构建器与可复用合约断言套件，让外部插件直接依赖并一键验证幂等、超时、过温、敏感屏和回执边界；
  2. **脱敏 Fixture 采集命令**：提供本地工具（如 `harmony-pilot fixture capture --redact`），抓取 `dumpLayout` 后按规则脱敏敏感文本与数字，直接生成可用于离线 CI 的测试夹具；
  3. **通用三段式插件示例**：补充一个完全通用的 `IdempotentAction`（`inspect → submit → confirm`）参考实现（不涉及任何第三方商业 App），演示完整幂等与对账写法；
  4. **本地协议验收 Mock Broker**：提供极简的本地离线协议测试工具，供 Broker 与插件开发者离线校验 Pull Bridge Protocol v1 一致性（严守边界：仅做协议契约验收，不做业务调度平台）。
- **验收标准**：
  - 外部独立插件模板切换为直接复用 `moyu_harmony_pilot.testing` 后，离线契约测试保持零跳过、全绿运行。

---

## 5. 长期架构演进：核心驱动 SDK 独立拆分

依据 [docs/requirements.md](requirements.md#L90-L142) 确立的单向依赖 DAG：

```text
纯驱动 SDK 候选边界：hdc → ui → device (+ 可选 vision)
上层执行与接入边界：actions → bridge / cli / mcp
```

待 HarmonyOS NEXT 多版本真机实践验证底层接口（`HdcClient`、`UiDriver`、`CompactSnapshot`、`HarmonyDevice`、`OcrEngine`）趋于稳定后，可按社区与生态需求评估将底层纯驱动包独立发布，供仅需 HarmonyOS NEXT Python 自动化驱动、无需 Bridge/MCP 的开发者零负担引入；`moyu-harmony-pilot` 保持对驱动包的组合封装与向后兼容。

---

## 6. 仓外协同与生态演进（M4 已闭环，维护在本仓之外）

M4 已在仓外完成外部兼容 Broker 与独立业务插件的端到端接入闭环（未修改 `moyu-harmony-pilot` 核心代码）。按照项目职责边界，以下内容继续**保持在本仓库之外**，通过稳定的 `ActionPlugin API v1` 与 `Pull Bridge Protocol v1` 与本仓解耦协同演进：

- **外部 Broker / 控制面**：持续维护协议 v1 四个 Bridge 端点与 Dispatcher API，按需对接定时编排、任务队列、Token 轮换/吊销、设备在线监控以及 `human_required` / `unknown` 人工接管通知闭环；
- **业务侧外部插件仓库**：基于 [examples/plugin-template](../examples/plugin-template/README.md) 持续迭代独立业务插件，配合本仓 `0.1.0` 及后续版本的协议/驱动演进通过 [docs/plugin-catalog.md](plugin-catalog.md) 记录已验证的 Pilot 兼容版本与契约测试结果。
