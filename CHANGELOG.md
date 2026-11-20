# 更新日志（Changelog）

本项目遵循语义化版本（Semantic Versioning）规范。`0.x` 阶段保持核心契约稳定演进，重大变更会提前说明。

---

## [0.1.0] - 2026-11-20

首个正式可用基线版本，完整覆盖 **M0–M3** 仓内核心能力，并已完成 **M4** 仓外应用侧端到端接入闭环验证。

### 核心能力

- **纯血鸿蒙原生驱动（核心零第三方依赖）**
  - `hdc`：自动发现官方 `hdc 3.x`、显式 `-t` 目标隔离、安全参数转义、超时进程组回收、`file send/recv` 与 `/data/local/tmp/moyu_pilot_*` 自动清理；
  - `ui`：官方 `uitest dumpLayout` 解析、强类型 `UiNode` 树、声明式选择器与等待、`uiInput` 输入原语、低 Token `@eN` 紧凑语义快照、`layout_hash` 结构差分与敏感屏信号检测；
  - `device`：设备别名隔离（不外泄真实硬件串号）、跨进程/跨线程单设备独占锁、`BatteryService` / `PowerManagerService` / `WindowManagerService` 遥测解析、电池过温保护门控与 `0700`/`0600` 本地滚动诊断落盘。
- **插件框架与幂等防重（Action API v1）**
  - 标准 `ActionPlugin` 与三段式 `IdempotentAction`（`inspect → submit → confirm`），显式 `api_version` 声明校验；
  - 本地 SQLite 幂等账本：先落 `submitting` 预留再提交，成功自动短路为 `already_done`，超时/不确定进入 `unknown` 且禁止自动重放；
  - 支持 Python 包 `entry_points` 与目录 `actions.json` 双通道外部发现，内置 `device.ping`、`device.health`、`ui.inspect`，并提供独立插件工程模板与离线契约套件。
- **本地视觉标注、OCR 与受控交互（可选扩展）**
  - `[vision]`：Pillow 截屏缩放与 Set-of-Mark（`--som`）`@eN` 编号标注；
  - `[vision-macos]` / `[vision-rapidocr]`：懒加载本地 OCR 引擎适配；
  - `InteractionSession` 与 `PopupWatcher`：固定步数/耗时预算、新鲜 `layout_hash` 校验、敏感屏熔断与显式弹窗规则。
- **机器优先 CLI、Pull Bridge 与可选 MCP**
  - `harmony-pilot` 统一 `--json` 信封、稳定语义退出码（`0`–`6`）与自描述契约（`schema`、`plugins list/describe`）；
  - `harmony-pilot serve`：完整实现服务端无关 Pull Bridge Protocol v1（`heartbeat` / `claim` / `lease` / `complete`），含后台续租、租约取消、SQLite 持久化回执、优雅退出、`--check`/`--probe`/`--once`/`--health` 模式及 `systemd` / `launchd` 服务模板；
  - `[mcp]`：薄 MCP v2 stdio 适配层（默认 Safe Mode 与显式开启的 `--local-lab` 模式），附带标准 Agent Skill（`skills/harmony-pilot/SKILL.md`）。

### 已验证范围与已知边界

- **离线 CI**：macOS / Linux × Python 3.11 / 3.12 全量 237 项离线单测、DAG 依赖约束测试、sdist/wheel 打包及独立插件模板契约测试全通过。
- **真机实测**：已在 HarmonyOS 6.1 / API 23 真机（`hdc 3.2.0c`、`uitest 6.0.2.3`）完成 `doctor --deep`、SoM 截屏与仓外 M4 外部 Broker + 外部插件端到端运行验证。
- **已知边界**：`unknown` 账本记录的只读对账/核销 CLI、单宿主机多设备并发执行与更多 HarmonyOS 5.x/6.x 机型适配已列入后续规划，详见 [docs/roadmap.md](docs/roadmap.md)。
