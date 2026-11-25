# Moyu Harmony Pilot（`moyu-harmony-pilot`）

> **面向纯血鸿蒙（HarmonyOS NEXT 5 / 6+，API 12+）的轻量、无侵入 Python 自动化引擎与安全出站任务桥。**

`moyu-harmony-pilot` 是一款独立、通用的纯血鸿蒙真机自动化框架。它完全基于华为官方 `hdc` 与原生 `uitest` 工具链驱动真机，**无需在手机端安装任何辅助 App 或 Agent**；既可以直接通过命令行 / Python SDK 本机使用，也能通过服务端无关的 HTTPS 单向出站协议，把内网宿主机上连接的真机安全接入任意兼容的远端调度系统。

---

## 核心特性

- **专注纯血鸿蒙原生链路**：仅面向 HarmonyOS NEXT（5 / 6+），直接使用官方 `hdc`、`uitest`、`aa`、`hidumper`、`power-shell`、`snapshot_display`，不背负 Android / ADB 历史包袱。
- **零手机端侵入**：无需 Root、无需解锁 Bootloader、无需安装测试 HAP，也不依赖易随系统升级失效的私有守护进程逆向协议。
- **核心零重依赖**：默认安装仅依赖 Python 3.11+ 标准库；视觉标注、OCR 引擎和 MCP 服务均作为按需安装的可选扩展。
- **独立运行、生态解耦**：单机 CLI / Python SDK 开箱即用；远程派发采用公开、服务端无关的 HTTP Pull 协议，不绑定任何特定云端平台或调度器。
- **默认安全（Fail-Closed）与插件化**：具体业务流程留在外部 `ActionPlugin` 插件中维护；远端调度侧只能触发宿主机白名单显式放行的动作，绝不开放任意 Shell 透传。
- **原生适配 AI Agent**：提供统一 JSON 信封与稳定退出码（`0`–`6`）、低 Token 紧凑语义快照（`@e1` / `@e2` 引用 + `layout_hash`）、自描述契约（`harmony-pilot schema`）、防失控护栏、标准 Agent Skill 与可选轻量 MCP 适配层。
- **单向分层架构**：内部包按 `hdc → ui → device / vision → actions → bridge / cli / mcp` 严格单向依赖，并由自动化架构测试守护，便于后期按需拆分纯驱动 SDK。

**长期优先方向**：持续降低外部插件的开发、测试、接入与维护成本，让开发侧 Agent 能依据公开契约和示例协作，让运行侧 Agent 能发现能力、理解结果并安全处理异常与人工接管。后续设计和变更以此作为取舍与验收依据，同时坚守安全、幂等和公开仓库边界。现有能力及待办分别见下方项目进展与[路线图](docs/roadmap.md)。

---

## 快速上手

以下真机示例适用于恰好一台已授权、在线的设备，无需预先配置别名。示例只做只读观察，工具会创建并清理设备端受管临时文件；未经明确授权，不对真机执行其他写操作或有副作用点击。

```bash
# 1. 创建 Python 3.11+ 虚拟环境并安装开发依赖
uv venv --python python3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 2. 离线查看 CLI 完整契约（无需连接手机）
.venv/bin/harmony-pilot schema --json

# 3. 检查本机环境与已连接的纯血鸿蒙设备
.venv/bin/harmony-pilot devices --json
.venv/bin/harmony-pilot doctor --json

# 4. 导出低 Token 紧凑 UI 快照（@e1, @e2, ...）与屏幕截图
.venv/bin/harmony-pilot dump-ui --compact --json
.venv/bin/harmony-pilot screenshot --out .state/diagnostics/screen.jpeg --json
```

多台设备在线或需要使用固定别名时，先在本机私下填写设备别名与实际 `hdc` 目标串号的映射：可将 `devices.example.yaml` 复制为 `devices.local.yaml`（已有配置则沿用），设为 `0600` 权限，并替换示例中的占位串号，再通过 `--device <别名>` 指定设备。该本地配置默认被 Git 忽略；不要提交真实串号、配置、截图或原始布局。

官方 `hdc 3.x` 的安装方式与真机调试前置设置，请参考 [本地环境与真机准备指南](docs/device-setup.md)。

---

## 文档导航

### 入门与设计
- **[需求与架构设计](docs/requirements.md)** — 项目定位、非目标边界、分层架构、插件模型、Agent 友好契约与安全红线。
- **[本地环境与真机准备](docs/device-setup.md)** — macOS / Linux 安装官方 `hdc`、Python 环境搭建、真机开启调试、只读验证与 USB 排障。
- **[后续迭代路线图](docs/roadmap.md)** — M4 闭环后的演进优先级，重点涵盖插件开发体验、Agent 友好、幂等运维与多版本适配。
- **[更新日志](CHANGELOG.md)** — 各版本发布记录、已验证环境与已知边界。

### 插件与扩展

- **[创建与离线验证业务插件](docs/actions.md#从离线工程开始)** — 使用 `harmony-pilot plugins init ../team-pilot --namespace team` 生成独立工程，安装后运行 `python -m tests`，再逐步替换业务实现和测试场景。
- **[应用管理、交互与运行诊断](docs/app-management.md)** — HAP 下载部署、应用查询、引用式输入、账本对账、多设备执行与离线可视化检查和报告。
- **[系统短信与验证码读取](docs/sms.md)** — 读取当前短信、等待新消息，以及通过 SSH 或显式启用的 MCP 工具获取验证码。
- **[Action 插件与本地执行](docs/actions.md)** — 插件发现机制、参数与回执 Schema、执行预算、SQLite 幂等账本、视觉标注/OCR 与受控交互。
- **[独立插件模板](examples/plugin-template/README.md)** — 开箱即用的独立插件工程模板、API 兼容性声明、离线契约测试与 CI 配置。
- **[插件目录与维护指南](docs/plugin-catalog.md)** — 仓库通用示例清单、社区插件登记方式与插件上线/升级/下线流程。

### 远程接入与 AI Agent
- **[Pull Bridge 守护进程指南](docs/bridge-worker.md)** — 私有配置、单设备能力白名单、租约续期与取消、持久化回执、健康探针及 `systemd` / `launchd` 部署模板。
- **[跨机器接入与宿主机交付](docs/remote-integration.md)** — 宿主机准备、外部插件部署、契约导出、连接探测与通用远程任务验收。
- **[Pull Bridge 协议规范（v1.0）](docs/bridge-protocol.md)** — 服务端无关的 HTTPS 单向拉取协议（`heartbeat` / `claim` / `lease` / `complete`）。
- **[本地 MCP 适配层](docs/mcp.md)** — 可选 MCP v2 stdio 工具、默认安全模式白名单与显式开启的本地探索模式（Local Lab）。
- **[Harmony Pilot Agent Skill](skills/harmony-pilot/SKILL.md)** — 面向外部 AI Agent 的标准 Skill 说明（观测、受控执行、幂等约束与人工接管）。

- [兼容性矩阵](docs/compatibility.md) — 合成/真实样本来源、多显示器解析边界与待验收设备。
- [发布检查清单](docs/release-checklist.md) — 类型/分发检查、真机验收与 Tag 发布流程。

### 协作与安全
- **[AI Agent 协作指南](AGENTS.md)** — 分层依赖铁律、离线测试要求、提交前自检清单与安全红线。
- **[安全策略](SECURITY.md)** — 漏洞反馈方式、敏感信息边界、远程 Fail-Closed 规则与仓库清理要求。

---

## 项目进展

- **`M0`** ✅ — 仓库按开源标准初始化，完成架构设计、设备准备指南与 Pull Bridge 协议规范。
- **`M1`** ✅ — 落地核心零依赖驱动（`hdc`、`ui` 紧凑语义快照与 `layout_hash`、`device` 别名隔离/单设备锁/电池温度遥测与过温保护）、Agent 友好 CLI（`devices`、`doctor`、`dump-ui`、`screenshot`、`schema`）及全离线单测。
- **`M2`** ✅ — 落地插件执行与外部发现、SQLite 幂等账本、CLI 自描述、可选 macOS Vision / RapidOCR 适配器、`--som` 截图编号标注、显式弹窗规则，以及带新鲜引用校验和差分反馈的本地受控交互。
- **`M3`** ✅ — 落地出站 Pull Bridge（`serve`）、租约取消、持久化回执恢复、协议一致性测试、常驻服务模板、双模护栏 MCP v2 stdio 适配层与标准 Agent Skill。
- **`M4`** ✅ — 外部应用侧 Broker 与独立业务插件已在仓外完成基于 Pull Bridge Protocol v1 与 Action API v1 的端到端接入验证，全程未修改 `moyu-harmony-pilot` 核心代码。

> 说明：本仓库内的 M2/M3 验证覆盖离线模拟设备、本地 HTTP Broker 冒烟检查、真实 MCP stdio 握手、双 OCR 后端合成图像测试，以及真机只读验收（`doctor --deep` 与 SoM 截屏，且受管临时文件已全部清理）。M4 对应的外部 Broker、私有业务插件及生产部署配置均维护在本仓库之外。

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
