# 本地 MCP 适配层使用指南

`moyu-harmony-pilot` 提供可选的轻量 MCP v2 stdio 适配层（`harmony-pilot-mcp`），方便本地 AI 客户端以标准 MCP 工具方式观测和操作纯血鸿蒙真机。适配层本身不包含重复业务逻辑，全部复用了核心的 `actions`、`device`、`ui` 与安全护栏。

## 安装与启动

安装可选依赖 `[mcp]` 后，使用权限为 `0600` 的本地设备别名配置启动服务：

```bash
python -m pip install 'moyu-harmony-pilot[mcp]'
harmony-pilot-mcp --config devices.local.json --allow-action device.health
```

也可以通过模块方式启动：`python -m moyu_harmony_pilot.mcp`。如果不使用 MCP，核心 CLI 依然只需 Python 标准库即可运行。

---

## 默认安全模式（Safe Mode）

未显式开启探索模式时，服务运行在**安全模式**，仅暴露以下 6 个工具：

| 工具名 | 性质 | 说明 |
| :--- | :--- | :--- |
| `devices` | 只读 | 列出已配置的设备别名、连接状态与设备锁占用情况 |
| `health` | 只读 | 读取电量、充电状态、电池温度与亮灭屏遥测 |
| `dump_ui` | 只读 | 返回紧凑语义快照（`@eN` 引用、`layout_hash` 与敏感屏信号） |
| `screenshot` | 只读 | 在本地私有诊断目录生成截图并返回文件元数据；`som=true` 时附带编号标注与对应快照（需安装 `[vision]`） |
| `list_actions` | 只读 | 返回当前已授权动作的 Schema 与副作用级别 |
| `run_action` | 受控 | 执行经 `--allow-action` 显式放行的动作 |

### 安全约束

- **默认空白名单**：必须通过重复传入 `--allow-action <ACTION_ID>` 显式放行动作；不传时仅能使用只读观测工具，且标记为 `local_only` 的动作会被注册表拒绝。
- **仅接受配置别名**：所有设备参数必须匹配私有配置中的别名，不会暴露真实串号；截图文件名由适配层自动生成并保存在受控诊断目录中。
- **统一返回与共享护栏**：所有工具返回值都包含 `ok`、`data` 与结构化 `error`，失败时同步设置 MCP `isError=true`。插件过程日志会重定向到 `stderr`，不污染 stdio 协议通道。有副作用动作与 CLI / Bridge 共用同一套 `ActionRunner`、设备锁、过温保护与 SQLite 幂等账本。
- **受控错误信息**：可控的设备/UI/视觉异常保留明确的错误码、提示和可重试标记；包含底层子进程输出的 `hdc` 错误会被裁剪为有界提示，避免泄露无关原文；未预期异常仅返回通用错误；提交后不确定的结果保持 `retryable=false`，需人工核对或只读对账。

---

## 本地探索模式（Local Lab Mode）

当你需要让本地 Agent 探索新页面或辅助开发外部插件时，可以显式加上 `--local-lab` 启动：

```bash
harmony-pilot-mcp --config devices.local.json --local-lab --max-steps 20 --timeout 60
```

开启后额外提供 4 个受限交互工具：`lab_observe`、`click_ref`、`swipe`、`back`。

- **固定生命周期预算**：某个设备别名首次调用交互工具时开始计时，在整个服务进程生命周期内共享这份步数与耗时预算，工具调用本身无法自行续期。
- **新鲜布局校验**：先调用 `lab_observe` 获取当前 `layout_hash`，后续每步操作都必须带上该哈希。底层 `InteractionSession` 会重新校验引用有效性、检测敏感界面与电池温度，执行后返回最新快照以及布局、前台包名和焦点变化差分。
- **不确定状态熔断**：若输入后无法确认页面结果，会话会要求先重新观测再继续操作。
- **串行执行说明**：服务通过 stdio 通信并在主线程执行回调，以保证 POSIX 定时器预算正常工作。如需同时长期操作多台设备，请为不同设备启动独立进程，并在涉及同一物理设备时共用同一个 `--state-dir`。

---

## 客户端接入示例

在 MCP 客户端配置中使用**绝对路径**指向虚拟环境中的可执行文件与私有配置文件（外层结构按各自客户端格式微调）：

```json
{
  "mcpServers": {
    "harmony-pilot": {
      "command": "/ABSOLUTE/PATH/.venv/bin/harmony-pilot-mcp",
      "args": [
        "--config", "/ABSOLUTE/PATH/devices.local.json",
        "--state-dir", "/ABSOLUTE/PATH/.state",
        "--allow-action", "device.health"
      ]
    }
  }
}
```

### Agent Skill 位置

仓库内置了面向外部 Agent 的标准 Skill：[skills/harmony-pilot/SKILL.md](../skills/harmony-pilot/SKILL.md)。通过 wheel 安装后，该文件也会释放到 Python 安装前缀下的 `share/moyu-harmony-pilot/skills/harmony-pilot`，可直接复制到你所用 Agent 平台的 Skill 目录中。
