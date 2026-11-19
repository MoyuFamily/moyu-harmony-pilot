# Moyu Harmony Pilot — AI Agent 开发与协作指南

> 本文档面向参与本仓库开发、测试与维护的 **AI Coding Agent** 与人类工程师。本仓库按**公开开源标准**建设，任何变更必须严格遵守下述架构边界、验证流程与安全红线。

---

## 1. 项目定位与硬边界

- **唯一目标平台**：**HarmonyOS NEXT（HarmonyOS 5 / 6+，API 12+）** 原生官方工具链（`hdc`、`uitest`、`aa`、`hidumper`、`power-shell`、`snapshot_display`）。
- **严禁引入的范围**：
  - 禁止引入 Android / ADB 兼容逻辑；
  - 禁止引入任何第三方商业/民生业务 App 的私有自动化脚本（具体业务必须作为外部插件在仓库之外维护）；
  - 禁止硬编码任何私有服务域名、外部特定控制面/调度器耦合逻辑、账号、手机号或设备真实串号；
  - 禁止提供绕过验证码、逆向加密协议、破解锁屏 PIN 或向远端透传任意 `hdc shell` 的能力。

详见 [docs/requirements.md](docs/requirements.md) 与 [docs/bridge-protocol.md](docs/bridge-protocol.md)。

---

## 2. 分层分包与单向依赖铁律（DAG）

`src/moyu_harmony_pilot/` 采用严格单向依赖，**禁止反向导入或循环依赖**，确保后期 `hdc + ui + device (+ vision)` 可无痛拆分为独立驱动 SDK：

```text
hdc        (L1: 仅依赖标准库 — 官方 hdc CLI 封装与文件收发)
 └── ui    (L2: 仅依赖 hdc + 标准库 — dumpLayout 解析、紧凑语义快照 @eN、选择器与输入原语)
      └── device (L3: 依赖 hdc, ui — 别名隔离、单设备锁、电源/应用/截屏、电池温度遥测、诊断落盘)
      └── vision (L3 可选: 仅依赖协议/可选 OCR 后端 — OcrEngine、弹窗 Watcher、SoM 标注)
           └── actions (L4: 依赖 device, ui, vision 协议 — ActionPlugin、白名单注册、幂等账本)
                ├── bridge (L5: 标准出站 Pull Bridge 客户端)
                ├── cli    (L5: 机器优先 CLI、JSON 信封、语义退出码、Schema 自描述)
                └── mcp    (L5 可选: 薄 MCP 适配层，严禁复制业务逻辑)
```

- **核心零重依赖原则**：`pyproject.toml` 的默认 `dependencies` 保持为空（仅依赖 Python 3.11+ 标准库）；重依赖（如 OCR 后端、MCP SDK）只能放入 `[project.optional-dependencies]`。

---

## 3. Agent 友好契约（不可破坏）

修改 CLI、插件或协议层时，必须维持以下契约稳定：

1. **纯净输出分离**：`--json` 模式下 `stdout` 仅输出合法 JSON 信封，人类日志和警告一律输出到 `stderr`。
2. **稳定语义退出码**：`0` 成功 / `1` 执行失败 / `2` 参数或配置错误 / `3` 设备离线或未授权 / `4` 需人工介入 (`human_required`) / `5` 硬件过温保护 / `6` 设备忙或锁冲突。
3. **低 Token 语义快照**：`ui` 层紧凑快照（`@e1`, `@e2`, …）必须保持确定性排序与字段精简，不得将未经剪枝的 100KB+ 原始 `dumpLayout` 直接倾倒给上层 Agent。
4. **自描述同步**：新增命令或 Action 插件参数时，同步更新 `schema` / `plugins describe` 输出与单元测试。
5. **Fail-Closed 护栏**：远程 Pull Bridge 与默认 MCP Safe Mode 仅允许执行显式白名单动作；涉及副作用的 `IdempotentAction` 必须先落本地 SQLite `submitting` 事务再提交，超时/不确定状态保持 `unknown` 且禁止自动重放。

---

## 4. 环境、测试与提交前自检

本仓库要求 **Python 3.11+**，本地虚拟环境位于 `.venv/`（创建方式见 [docs/device-setup.md](docs/device-setup.md)）。

### 单元测试红线
- `tests/` 必须 **100% 离线运行**（使用 Fake/Mock `HdcRunner` 与脱敏布局 JSON Fixture），绝不依赖真实连接手机或访问外网。
- 需要真机实测时，默认先执行只读检查（`devices` / `doctor` / `dump-ui` / `screenshot`），并在结束后清理手机 `/data/local/tmp/` 下的临时文件；未经人类明确指令，不对真机执行写操作或有副作用点击。

### 提交前必跑清单
```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/ruff check src tests
.venv/bin/pytest
```

### 零敏感信息自检
- 严禁提交真实设备串号、Token、`.env`、`devices.local.*`、真机截图、原始布局 dump 或私有插件代码。
- 提交前检查 `git status` 与 `git diff`，确保无临时诊断文件与敏感字面量泄漏。
