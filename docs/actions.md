# Action 插件开发与本地执行指南

本文说明如何在本地查看与运行内置动作、接入外部插件、编写符合规范的 `ActionPlugin` / `IdempotentAction`，以及使用本地视觉标注（SoM）、OCR 和受控交互会话。

---

## 1. 查看与运行内置动作

```bash
harmony-pilot plugins list --json
harmony-pilot plugins describe device.health --json
harmony-pilot run --device harmony-01 --action device.health --json
harmony-pilot run --device harmony-01 --action ui.inspect --params-json '{"max_elements":40}' --json
harmony-pilot run --device harmony-01 --action device.health --dry-run --json
```

仓库默认内置 3 个通用只读动作：

| 动作 ID | 作用说明 | 远程可见性 |
| :--- | :--- | :--- |
| `device.ping` | 检测官方 `uitest` 服务是否正常响应 | 需白名单显式放行 |
| `device.health` | 读取电量、充电态、电池温度、亮灭屏与过温标志 | 需白名单显式放行 |
| `ui.inspect` | 抓取紧凑 UI 快照保存到本地诊断目录，并返回 `layout_hash` 与元素数 | 仅限本地（`local_only`） |

### 使用注意

- **共享状态目录**：默认状态目录为 `.state`（诊断目录权限 `0700`、文件权限 `0600`）。凡是操作同一台物理手机的进程或命令，请统一使用相同的 `--state-dir`，确保单设备锁与 SQLite 账本落在同一位置。
- **`--dry-run` 预检**：仅校验插件存在性、参数 Schema、幂等键格式以及目标设备是否在线，不会触发任何 UI 交互。
- **保持初始化纯净**：插件发现阶段会导入 Python 模块并实例化插件类，因此**模块顶层与构造函数内严禁发起任何设备或网络 I/O**。

---

## 2. 外部插件发现机制

> 安全提示：插件代码与宿主机 Pilot 进程享有相同的运行权限，请只安装和启用经过代码审计的可信插件。

推荐直接从 [独立插件模板](../examples/plugin-template/README.md) 起步，内置了标准包元数据、entry point 契约测试与 macOS/Linux CI 配置。

Pilot 支持两种外部插件加载方式：

### 方式一：已安装的 Python 包（Entry Point）

在插件包的 `pyproject.toml` 中，以动作 ID 作为 entry point 名称注册到 `moyu_harmony_pilot.actions` 分组：

```toml
[project.entry-points."moyu_harmony_pilot.actions"]
"example.ui_summary" = "example_package.ui_summary:UiSummary"
```

### 方式二：本地插件目录（`actions.json`）

在插件目录根路径下放置 `actions.json`，将动作 ID 映射到同目录下的 Python 文件与类名（文件必须直接位于该目录内）：

```json
{
  "example.ui_summary": "ui_summary.py:UiSummary"
}
```

仓库自带了一个完整的只读目录示例 `examples/actions`，在仓库根目录即可直接试跑：

```bash
export HARMONY_PILOT_PLUGIN_PATHS="$PWD/examples/actions"
harmony-pilot plugins describe example.ui_summary --json
harmony-pilot run --device harmony-01 --action example.ui_summary --json
```

### 发现规则补充

- 多个目录用宿主路径分隔符拼接（macOS / Linux 下为 `:`）；
- 若存在重复的动作 ID 或元数据不一致，发现阶段会立即以 `configuration_error` 中止；修改插件代码后需重启进程重新加载；
- Bridge 服务可在私有 JSON 配置中设置 `plugin_paths`（相对路径基于配置文件目录解析），Python SDK 也可调用 `ActionRegistry.discover(plugin_paths=...)`；两者都会覆盖当次的环境变量设置，同时仍保留已安装包 entry point 的发现。

---

## 3. 插件开发契约

从 `moyu_harmony_pilot.actions` 导入 `ActionPlugin`、`ActionSpec`、`ActionContext`、`ActionOutcome`：
- **只读插件**：继承 `ActionPlugin`，实现 `execute(ctx, params) -> ActionOutcome`；
- **有副作用插件**：继承 `IdempotentAction`，实现三段式 `inspect` / `submit` / `confirm`；
- 在类属性 `spec` 中显式声明参数 Schema、返回指标 Schema、版本号、描述、超时与副作用级别。

### 3.1 版本与兼容性声明

对外分发插件时，请明确维护以下三个版本维度：

1. **`api_version`（动作接口契约版本）**：在 `ActionSpec(api_version=1, ...)` 中显式声明整数版本号（自 `0.1.0.dev1` 起支持；省略时默认 v1）。注册时会对照宿主 `ACTION_API_VERSION` 校验，不兼容则在执行前直接返回 `configuration_error`。`harmony-pilot schema`、`plugins list/describe` 及 Bridge `--check` 都会透出该字段。
2. **`version`（动作行为版本）**：表示插件自身的业务逻辑版本，并参与幂等账本的请求指纹计算。修改 `version` 后复用旧幂等键会触发校验失败，升级前请保留账本并处理好未决任务。
3. **包依赖范围**：打包分发的插件在 `pyproject.toml` 中声明支持的 Pilot 版本区间（`project.dependencies`）和 Python 版本区间（`project.requires-python`），并针对支持的每个版本跑通离线契约测试；目录式插件请在 `actions.json` 同级文档中注明环境要求。

### 3.2 参数、回执边界与超时控制

- **JSON Schema 子集**：支持显式类型、闭合对象属性（对象 Schema 必须声明 `additionalProperties: false`）、`required`、数组元素 Schema、`enum`、数值范围及字符串/数组长度限制；整数参数须为 JSON 整型字面量，布尔值与整数严格区分。可通过 `harmony-pilot schema --json` 查看全部支持关键字。
- **体积与隐私硬上限**：
  - 参数、插件元数据、单条回执的规范化 JSON 均不得超过 **16 KiB**，嵌套深度不超过 **8 层**；
  - 回执 `metrics` 仅允许已声明的扁平标量字段：最多 **32 个键**，键名 ≤ **64 字符**，字符串值 ≤ **128 字符**，数字须为有限值；
  - `summary` ≤ **256 字符**，`error_code` ≤ **64 字符**（小写蛇形命名）；
  - 插件作者须确保摘要、指标和日志中绝不包含凭据、硬件串号、原始 UI 文本或个人隐私信息。
- **检查点与步数/时间预算**：
  - 每次界面交互前和循环体内应调用 `ctx.checkpoint()`，并将 `ctx.remaining_timeout` 透传给驱动调用；
  - `--max-steps` 统计运行器阶段边界与插件显式检查点，默认上限 `100`；
  - 在 macOS / Linux 上，运行器通过 POSIX `SIGALRM` 实施阶段硬超时，因此需在**进程主线程**运行 SDK Runner 且避免占用系统实时定时器；`--timeout` 可用于缩短插件默认超时；插件代码应正确传播取消信号并使用有界驱动调用，取消后仍会执行设备清理与账本落盘。
- **安全门控**：所有动作都会先获取物理设备独占锁；非诊断类动作须通过电池过温门控；有副作用动作在提交前还会额外复检敏感屏信号与电池温度。建议把 `submit` 阶段尽量做小，导航途中主动关注敏感界面。

---

## 4. 有副作用动作与幂等账本

有副作用的业务动作必须继承 `IdempotentAction`，拆分为三个明确阶段：

1. **`inspect(ctx, params)`（只读检查）**：检查当前界面状态，返回 `Inspection`（状态为 `ready`、`already_done`、`human_required` 或 `failed`），可携带仅供本轮 `submit` 使用的私有上下文。
2. **`submit(ctx, inspection, params)`（至多一次提交）**：仅在运行器先把 `submitting` 预留写入 SQLite 账本后才会被调用，负责执行真正产生副作用的操作。
3. **`confirm(ctx, params)`（独立复核）**：提交完成后独立核验界面结果，返回有界 `ActionOutcome`。

### 账本与状态流转规则

- **幂等身份**：每次有副作用执行都必须显式提供 `--idempotency-key`（1–256 字符）。SQLite 账本以「哈希后的物理设备串号 + 动作 ID + 哈希后的幂等键」作为唯一主键，并用请求指纹绑定参数与插件 `version`；同一幂等键若更换参数或版本号会返回 `configuration_error`。
- **取消边界**：准备与 `inspect` 阶段（包括写入预留前的最后一刻）都会响应取消信号；一旦 `submitting` 预留落盘，运行器会优先在原时间和步数预算内走完 `submit` 与 `confirm`，关机或租约取消在其后生效。
- **防重放与崩溃恢复**：
  - 已确认完成的记录，再次调用会直接短路返回 `already_done`，不会重复唤醒手机或调用插件；
  - 提交阶段发生报错、超时、中断、无法确认或落盘失败，均会落为 `unknown`；进程重启后，持锁检查到遗留的 `submitting` 记录也会自动恢复为 `unknown`；
  - 对 `unknown` 记录重复使用同一幂等键会持续返回 `unknown`。可通过 `ledger reconcile` 调用只读 `confirm()`，或在人工核验后使用 `ledger resolve` 记录成功结案。命令和保留期限见[应用管理与诊断](app-management.md)。
- **纯净输出**：插件在发现或执行期间向标准输出打印的进度信息，在 CLI 模式下会被重定向到 `stderr`，确保 `stdout` 始终只输出单个合法 JSON 信封。

---

## 5. 远程执行边界

CLI 面向本机执行；Pull Bridge 与 MCP 则统一经由 `ActionRegistry` / `ActionRunner` 的 `remote=True` 门控与显式 `allowed_actions` 白名单（默认为空）：
- 远程发现阶段会先按白名单过滤 entry point 名称与目录清单 ID，未放行的插件模块甚至不会被导入；
- 远程调用阶段会再次核验白名单，并拒绝所有 `local_only` 动作（包括 `ui.inspect`）。

---

## 6. 本地视觉标注、OCR 与受控交互

### 6.1 截屏缩放与 Set-of-Mark（`--som`）

安装 `moyu-harmony-pilot[vision]` 后，即可使用 Pillow 进行截屏缩放与 `@eN` 编号框标注：

```bash
harmony-pilot screenshot --out .state/screen.png --som --max-width 720 --json
```

该命令在设备锁保护下抓取截屏，并在截屏前后校验布局一致性、按缩放比例精准绘制编号框，同时返回对应的紧凑快照。若截屏瞬间界面发生变化，会返回 `layout_changed` 提示调用方重新采集。

### 6.2 本地 OCR 适配器

按平台按需安装 `[vision-macos]` 或 `[vision-rapidocr]`：

```python
from moyu_harmony_pilot.vision import create_ocr_engine, find_text

engine = create_ocr_engine("macos")  # 或 "rapidocr"
boxes = engine.recognize(".state/screen.png")
matches = find_text(boxes, "确认", min_confidence=0.8)
```

- 每个 `TextBox` 包含识别文本、左上角原点的像素矩形 `bounds`、置信度与中心坐标；`find_text` 支持精确文本或正则匹配。
- 后端依赖均为懒加载，未安装对应扩展时明确抛出 `VisionUnavailableError`。所有 OCR 结果仅保留在本地，不经远程协议上报。

### 6.3 受控交互会话（`InteractionSession`）与弹窗规则（`PopupWatcher`）

- **`InteractionSession`**：提供 `observe`、`click_ref`、`input_text_ref`、`long_click_ref`、`swipe`、`back`、`home`、`start_app`、`wait_for`、`scroll_until`，共享固定墙钟时长与步数预算。直接交互传入最新 `layout_hash`，滚动查找逐步复采。执行前加锁并校验温度、敏感屏，执行后返回新快照和差分。操作结果不确定时要求重新观测。
- **`PopupWatcher`**：可传入 `wait_for`，按显式配置的「精确文案 + 精确包名」规则兜底关闭非破坏性弹窗（默认规则列表为空）。触发条件为命中唯一可用可点击控件、当前非敏感屏、同一布局最多触发一次且不超过总弹窗处理预算。
