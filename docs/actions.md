# Action 插件开发与本地执行指南

Pilot 的插件分为随主程序分发的内部插件与独立安装的外部插件。两者共享 `ActionPlugin` / `IdempotentAction` 契约，通过 CLI、MCP、Pull Bridge 或插件内的 `ctx.call(...)` 调用。本文的组合调用与结构化直接结果约定面向 `0.2.1`，安装版本的具体参数与入口限制可通过 `plugins describe` 查看。

## 从离线工程开始

在已安装当前 Pilot 的 Python 环境中，生成一个独立插件工程。命令会同步包名、Python 模块名、动作 ID、测试与 CI 配置，并输出后续命令的 `cwd` 和 `argv`。

```bash
harmony-pilot plugins init ../team-pilot --namespace team --json
cd ../team-pilot
python -m pip install --no-deps -e .
python -m tests
harmony-pilot plugins describe team.demo_toggle --json
harmony-pilot plugins validate team.demo_toggle --params-json '{"enabled":true}' --idempotency-key demo-check --json
```

目标使用新目录，创建成功后文件清单会随 JSON 返回。包名默认来自目录名，可用 `--package` 单独指定，例如目标路径含空格时使用 `--package team-pilot`。Python 模块名转为小写，并将连续的点、下划线和连字符合并为下划线。工程创建使用 Pilot 随包提供的模板文件，独立插件的安装是下一步操作。

生成工程含只读电量检查和三段式开关示例。先运行现成的离线契约，再逐步替换业务逻辑，每次只改对应的实现和测试场景。

| 需要修改的内容 | 主要位置 | 验证入口 |
| --- | --- | --- |
| 业务参数、结果和描述 | `src/<模块名>/battery.py` 或 `toggle.py` 的 `ActionSpec` | `plugins describe`、`plugins validate` |
| 操作前检查、提交与结果核验 | `toggle.py` 的 `inspect`、`submit`、`confirm` | `python -m tests` |
| 业务界面与异常场景 | `tests/support.py`、`tests/test_mutating_contract.py` | 成功防重、超时后 `unknown`、只读对账、敏感屏与过温断言 |
| 安装发现与动作 ID | `pyproject.toml` 中的 entry point、实现与测试中的动作 ID | 重新安装后执行 `plugins describe` |
| Pilot 兼容版本 | 包依赖、`min_pilot_version`、`requirements-test.txt` 与 CI 的 `PILOT_REF` | 对目标 Pilot 版本运行全部离线契约 |

离线测试使用公共 `moyu_harmony_pilot.testing` 和合成界面。改造成真实业务插件时，将包名、控件 ID、检查依据和确认依据替换为目标应用的公开界面契约。已有工程的详细运行步骤见生成目录内的 `README.md`。

---

## 1. 查看与运行内部插件

```bash
harmony-pilot plugins list --json
harmony-pilot plugins describe device.health --json
harmony-pilot plugins validate device.health --params-json '{}' --json
harmony-pilot run --device harmony-01 --action device.health --json
harmony-pilot run --device harmony-01 --action ui.inspect --params-json '{"max_elements":40}' --json
harmony-pilot run --device harmony-01 --action device.health --dry-run --json
```

内部插件提供以下通用操作。插件来源及调用约束以 `plugins list/describe` 为准。

| 动作 ID | 作用说明 | 远程可见性 |
| :--- | :--- | :--- |
| `device.ping` | 检测官方 `uitest` 服务是否正常响应 | 需白名单显式放行 |
| `device.health` | 读取电量、充电态、电池温度、亮灭屏与过温标志 | 需白名单显式放行 |
| `ui.inspect` | 抓取紧凑 UI 快照保存到本地诊断目录，并返回 `layout_hash` 与元素数 | 仅限本地（`local_only`） |
| `app.install`、`app.uninstall` | 安装与卸载应用，使用幂等账本 | 仅限本地（`local_only`） |
| `sms.open` | 打开系统短信列表，使用幂等账本 | 需白名单显式放行 |
| `sms.read` | 读取可见短信，正文放在私有直接结果中 | MCP 需显式短信授权与动作白名单，Bridge 禁止调用 |
| `files.query`、`files.export` | 查询媒体 URI、导出单个文件 | 仅限本地（`local_only`） |
| `record.start`、`record.status`、`record.stop` | 管理可与业务操作并行的录屏会话 | 仅限本地（`local_only`），MCP 专用录屏入口需显式开启 |

文件与录屏插件的 `side_effect=readonly` 表示设备只读，宿主机仍会产生文件或录屏会话，调用时只需提供动作参数。

`plugins describe` 同时提供以下元数据，外部插件采用相同的描述契约。

| 字段 | 含义 |
| --- | --- |
| `source` | `internal` 为随 Pilot 分发的内部插件，`external` 为通过 entry point 或目录发现的外部插件 |
| `result_schema` | `ActionOutcome.data` 的结构化直接结果契约 |
| `private_content` | 直接结果是否包含需要私有入口授权的内容 |
| `lock_device` | 执行是否需要设备独占锁，子调用复用当前调用链持有的锁 |

CLI 子命令和 MCP 工具是插件的调用入口。复用同一插件时，各入口仍按自己的白名单、本地执行限制和私有内容授权进行门控。

### 使用注意

- **共享状态目录**：默认状态目录为 `.state`（诊断目录权限 `0700`、文件权限 `0600`）。凡是操作同一台物理手机的进程或命令，请统一使用相同的 `--state-dir`，确保单设备锁与 SQLite 账本落在同一位置。
- **`--dry-run` 预检**：仅校验插件存在性、参数 Schema、幂等键格式以及目标设备是否在线，不会触发任何 UI 交互。
- **离线参数预检**：`plugins validate <action_id>` 校验插件注册信息、参数和有副作用动作的幂等键格式，不需要 `hdc`、设备或配置文件，也不执行动作。成功结果中的 `device_checked` 和 `executed` 均为 `false`；它不检查设备状态、远程白名单或实际执行结果。插件发现仍会导入并实例化插件，因此只能加载可信代码。
- **保持初始化纯净**：插件发现阶段会导入 Python 模块并实例化插件类，因此**模块顶层与构造函数内严禁发起任何设备或网络 I/O**。

---

## 2. 外部插件发现机制

> 安全提示：插件代码与宿主机 Pilot 进程享有相同的运行权限，请只安装和启用经过代码审计的可信插件。

可通过 `plugins init` 生成 [独立插件模板](../examples/plugin-template/README.md)，其中包含标准包元数据、entry point 契约测试与 macOS/Linux CI 配置。

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
harmony-pilot plugins validate example.ui_summary --params-json '{}' --json
harmony-pilot run --device harmony-01 --action example.ui_summary --json
```

前三条命令无需连接设备；最后一条会读取已连接设备的界面。要验证有副作用动作，需同时提供 `--idempotency-key <测试键>`，预检不会创建账本。参数不符合 Schema 时，`configuration_error` 会给出已声明字段的位置（例如 `$["items"][0]["limit"]`）；未知字段名称和参数值不会回显。可以先用 `plugins describe` 查看完整 Schema，再调整参数重新预检。

### 发现规则补充

- 多个目录用宿主路径分隔符拼接（macOS / Linux 下为 `:`）；
- 若存在重复的动作 ID 或元数据不一致，发现阶段会立即以 `configuration_error` 中止；修改插件代码后需重启进程重新加载；
- Bridge 服务可在私有 JSON 配置中设置 `plugin_paths`（相对路径基于配置文件目录解析），程序内可调用 `ActionRegistry.discover(plugin_paths=...)`。两者都会覆盖当次的环境变量设置，同时仍保留已安装包 entry point 的发现。

---

## 3. 插件开发契约

从 `moyu_harmony_pilot.actions` 导入插件契约类型。

- **只读插件**：继承 `ActionPlugin`，实现 `execute(ctx, params) -> ActionOutcome`；
- **有副作用插件**：继承 `IdempotentAction`，实现三段式 `inspect` / `submit` / `confirm`；
- 在类属性 `spec` 中显式声明参数 Schema、返回指标 Schema、版本号、描述、超时与副作用级别。提供结构化直接结果时声明 `result_schema`，并按实际内容与执行方式设置 `private_content`、`lock_device`。

### 3.1 版本与兼容性声明

对外分发插件时，请明确维护以下四个版本维度。

1. **`api_version`（动作接口契约版本）**：在 `ActionSpec(api_version=1, ...)` 中显式声明整数版本号（自 `0.1.0.dev1` 起支持；省略时默认 v1）。注册时会对照宿主 `ACTION_API_VERSION` 校验，不兼容则在执行前直接返回 `configuration_error`。`harmony-pilot schema`、`plugins list/describe` 及 Bridge `--check` 都会透出该字段。
2. **`version`（动作行为版本）**：表示插件自身的业务逻辑版本，并参与幂等账本的请求指纹计算。修改 `version` 后复用旧幂等键会触发校验失败，升级前请保留账本并处理好未决任务。
3. **包依赖范围**：打包分发的插件在 `pyproject.toml` 中声明支持的 Pilot 版本区间（`project.dependencies`）和 Python 版本区间（`project.requires-python`），并针对支持的每个版本跑通离线契约测试；目录式插件请在 `actions.json` 同级文档中注明环境要求。
4. 在 `ActionSpec.min_pilot_version` 中声明最低宿主版本，注册与发现时会先校验。`inspection_metrics`、`reconciling`、`inspection_metrics_schema`、`ctx.interact()` 和 `ctx.dismiss_popup()` 从 `0.2.0.dev0` 起提供。新插件可沿用模板的 `min_pilot_version="0.2.0rc1"` 与包依赖下限，阶段指标 Schema 与上下文交互接口会拒绝缺失或过低的声明。

`ctx.call(...)` 示例要求 Pilot `>=0.2.1`。采用该接口的插件声明 `min_pilot_version="0.2.1"`，并为自身包依赖和调用用例设置相应下限。独立插件模板的测试基线为 `0.2.1`，最低兼容版本仍为 `0.2.0rc1`，已有 Action API v1 插件继续兼容。开发期间的 `0.2.1.dev0` 最低版本声明仍可注册，机器自描述保留这一接口起始版本。

最低版本接受 `X.Y.Z` 及规范的 `aN`、`bN`、`rcN`、`.postN`、`.devN` 后缀，按开发版、预发布版、正式版顺序比较。已有开发插件的 `0.2.0.dev0` 声明仍可注册。仅使用原有 Action API v1 的插件可以继续省略最低版本字段。

### 3.2 参数、回执边界与超时控制

`ActionOutcome.data` 承载符合 `result_schema` 的私有或结构化直接结果。`to_dict()` 默认只序列化安全回执，本地 CLI 或显式授权的私有 MCP 入口使用 `to_dict(include_data=True)` 交付直接结果。安全回执与账本只记录状态、脱敏摘要和指标，短信正文等私有内容仅用于直接调用结果。

- **JSON Schema 子集**：支持显式类型、闭合对象属性（对象 Schema 必须声明 `additionalProperties: false`）、`required`、数组元素 Schema、`enum`、数值范围及字符串/数组长度限制；整数参数须为 JSON 整型字面量，布尔值与整数严格区分。可通过 `harmony-pilot schema --json` 查看全部支持关键字。
- **体积与隐私硬上限**：
  - 参数、插件元数据、安全回执及账本中的单条 JSON 载荷上限为 **16 KiB**，嵌套深度不超过 **8 层**。直接结果 `data` 的规范化 JSON 上限为 **1 MiB**，同时受插件的 `result_schema` 与内容上限约束。
  - 回执 `metrics` 仅允许已声明的扁平标量字段：最多 **32 个键**，键名 ≤ **64 字符**，字符串值 ≤ **128 字符**，数字须为有限值；
  - `summary` ≤ **256 字符**，`error_code` ≤ **64 字符**（小写蛇形命名）；
  - 插件作者须确保摘要、指标和日志中绝不包含凭据、硬件串号、原始 UI 文本或个人隐私信息。
- **检查点与步数/时间预算**：
  - 每次界面交互前和循环体内应调用 `ctx.checkpoint()`，并将 `ctx.remaining_timeout` 透传给驱动调用；
  - `--max-steps` 统计运行器阶段边界与插件显式检查点，默认上限 `100`；
  - 在 macOS / Linux 上，运行器通过 POSIX `SIGALRM` 实施阶段硬超时，因此需在**进程主线程**运行 `ActionRunner` 且避免占用系统实时定时器。`--timeout` 可用于缩短插件默认超时。插件代码应正确传播取消信号并使用有界驱动调用，取消后仍会执行设备清理与账本落盘。
- 按 `lock_device` 声明获取或复用物理设备独占锁。非诊断类动作须通过电池过温门控，有副作用动作在提交前还会额外复检敏感屏信号与电池温度。建议把 `submit` 阶段尽量做小，导航途中主动关注敏感界面。

### 3.3 在插件内调用其他插件

在 Runner 提供的上下文中使用 `ctx.call(action_id, params=None, *, idempotency_key=None, timeout=None)`，可调用已发现的内部插件或外部插件，返回 `ActionOutcome`。通过 `.data` 读取结构化直接结果，子动作失败会自动向上传递状态，包括 `human_required` 与 `unknown`。

- 父子动作使用同一设备，并共享设备锁与总时间、步数预算。`timeout` 可缩短子调用时限，实际执行受父动作剩余预算约束。
- `mutating` 子动作仅可在父动作的 `prepare` 或 `submit` 阶段调用，调用时必须提供由业务请求及具体步骤派生的稳定幂等键。
- 父 `prepare` 内首次有副作用子动作到达 reserve 提交阶段时，Runner 先递归将父／祖先请求预留为 `submitting`，再预留子请求，最后提交子动作，各自写入动作账本。提前预留覆盖子动作已提交而父动作尚未完成的故障窗口。
- 子动作在 `inspect` 阶段拒绝或命中已有成功记录时，跳过新增父预留。父 `prepare` 正常完成后更新持久化基线。
- 一旦已有子提交，后续父失败、取消或崩溃保留 `unknown`，阻止整请求自动重放。子动作 `unknown` 同样向父传播，插件捕获异常后仍保持该状态。尚无子提交的普通准备失败沿用原键可重试。
- 远程调用逐个校验父动作和子动作的白名单、`local_only` 与私有内容授权，具体边界见第 5 节。

恢复时先按子动作原 `action_id`、`idempotency_key` 和参数执行 `ledger reconcile` 只读对账，再按父动作原身份核验组合结果。需要人工结案时，沿用核验后执行 `ledger resolve` 的流程。[账本维护](app-management.md#不确定结果与账本维护)

短信导航与读取的外部插件准备阶段示例见[系统短信调用](sms.md#在外部插件中读取短信)。

---

## 4. 有副作用动作与幂等账本

有副作用的业务动作必须继承 `IdempotentAction`，拆分为以下明确阶段：

1. **`inspect(ctx, params)`（只读检查）**：检查当前界面状态，返回 `Inspection`（状态为 `ready`、`already_done`、`human_required` 或 `failed`），可携带仅供后续阶段使用的私有上下文。
2. **`prepare(ctx, inspection, params)`（可选，提交前幂等准备）** 在 `inspect` 返回 `ready` 且插件覆写该方法时执行，适合拉起应用、关闭通用弹窗和导航等准备步骤。返回 `ready` 时继承并合并检查基线，已有提前预留时更新持久化基线。子动作真正到达 reserve 阶段会先递归预留父／祖先请求。尚无子提交的普通准备失败可在修复环境后重试，已有子提交后的失败按 `unknown` 对账。返回 `already_done` 时记录已完成。依赖该阶段的插件建议声明 `min_pilot_version="0.2.0rc1"`，使用 `ctx.call` 时提高到 `0.2.1`。
3. **`submit(ctx, inspection, params)`（至多一次提交）**：仅在运行器先把 `submitting` 预留写入 SQLite 账本后才会被调用，负责执行真正产生副作用的操作。
4. **`confirm(ctx, params)`（独立复核）**：提交完成后独立核验界面结果，返回有界 `ActionOutcome`。

### 阶段指标与回执

| 返回阶段 | 指标校验 |
| --- | --- |
| `inspect` / `prepare` 返回 `ready` | 使用可选的 `inspection_metrics_schema`，省略时沿用最终 Schema 的字段和类型并放宽必填项 |
| `inspect` / `prepare` 返回 `human_required` / `failed` | 校验检查阶段的已声明字段、类型与大小，允许缺少成功指标 |
| `inspect` / `prepare` 返回 `already_done` | 完整校验最终成功指标 |
| 最终 `succeeded` / `already_done` | 完整校验 `metrics_schema` 的必填项 |
| 其他最终结果 | 保留字段、类型与大小校验，允许缺少成功指标 |

`Inspection.summary` 与 `Inspection.error_code` 可携带脱敏的接管原因。检查基线随预留落盘，`prepare` 正常完成后更新持久化基线，`confirm` 通过 `ctx.inspection_metrics` 读取。只读对账会设置 `ctx.reconciling=True`，旧账本缺少的基线字段按空字典处理，插件应返回 `unknown` 并请求人工核验。

### 插件内交互

在 Runner 调用的阶段内使用 `session = ctx.interact()`。该会话复用设备锁、阶段计时器、步数预算和提交前取消信号，每次观察、等待或交互都会计入总预算。Runner 返回后，会话随上下文失效。

- `inspect`、只读 `execute` 和 `confirm` 可调用 `observe`、无弹窗处理的 `wait_for`。
- `prepare` 可调用交互、应用启动、滚动查找与弹窗处理，用于把界面带到可提交状态。子提交会提前触发父／祖先预留，此后遇敏感屏、过温或取消信号中止时按 `unknown` 处理。
- `submit` 可在账本预留完成后调用点击、输入、滑动、导航及弹窗处理。每次写操作仍校验新鲜 `layout_hash`、敏感屏和温度。
- `ctx.dismiss_popup(watcher)` 复用同一交互接口。插件在提交后遇到不确定结果时应立即结束，让 Runner 保留 `unknown`，后续通过 `ledger reconcile` 只读复核。

使用原始 `ctx.device` 的可信插件也须遵守阶段只读约束。独立本地探索可以继续使用 `InteractionSession(device)`。

### 账本与状态流转规则

- **幂等身份**：每次有副作用执行都必须显式提供 `--idempotency-key`（1–256 字符）。SQLite 账本以「哈希后的物理设备串号 + 动作 ID + 哈希后的幂等键」作为唯一主键，并用请求指纹绑定参数与插件 `version`；同一幂等键若更换参数或版本号会返回 `configuration_error`。
- `inspect` 与 `prepare` 响应取消信号。当前提交动作的 `submitting` 预留落盘后，运行器优先在原预算内完成其 `submit` 与 `confirm`。父准备阶段因子提交而提前预留后，后续取消使父请求保留 `unknown`。
- **防重放与崩溃恢复**：
  - 已确认完成的记录，再次调用会直接短路返回 `already_done`，不会重复唤醒手机或调用插件；
  - 提交阶段发生报错、超时、中断、无法确认或落盘失败，均会落为 `unknown`；进程重启后，持锁检查到遗留的 `submitting` 记录也会自动恢复为 `unknown`；
  - 对 `unknown` 记录重复使用同一幂等键会持续返回 `unknown`。可通过 `ledger reconcile` 调用只读 `confirm()`，在人工核验确认已生效后使用 `ledger resolve` 记录成功结案，或在人工核验确认副作用未发生后使用 `ledger release` 释放不确定预留以便原键重试。命令和保留期限见[应用管理与诊断](app-management.md)。
- **纯净输出**：插件在发现或执行期间向标准输出打印的进度信息，在 CLI 模式下会被重定向到 `stderr`，确保 `stdout` 始终只输出单个合法 JSON 信封。

---

## 5. 远程执行边界

CLI 面向本机执行，Pull Bridge 与 MCP 的通用动作入口经由 `ActionRegistry` / `ActionRunner` 的 `remote=True` 门控与显式动作白名单（默认为空）。

- 远程发现阶段先按白名单过滤 entry point 名称与目录清单 ID，已放行的外部插件才会被导入。
- 远程调用阶段对父动作及每次 `ctx.call` 分别核验授权。`local_only` 动作（包括 `ui.inspect`、`files.*`、`record.*`）限本地调用，远程白名单也受该限制约束。
- MCP 中的短信子调用同时要求 `--enable-sms`、`--allow-action sms.read` 和父动作白名单，流程包含导航时还需 `--allow-action sms.open`。
- Pull Bridge 禁止执行含私有正文的插件（如 `sms.read`），同样检查子调用。私有正文只通过本地直接结果或显式授权的私有 MCP 入口交付。
- MCP 的专用本地录屏工具由 `--enable-recording` 开启，其目录与结果边界见 [MCP 使用指南](mcp.md#本地录屏验收与调试)。

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

本地 CLI 可以直接捕获屏幕并输出有界识别结果。

```bash
harmony-pilot ocr --device harmony-01 --backend auto --max-width 1280 --json
```

结果携带识别图像尺寸、原图尺寸和缩放比例。位置使用识别图像的像素坐标，界面变化后需重新观测。MCP 的 `--enable-ocr` 和内联图片配置见 [MCP 接入说明](mcp.md)。

### 6.3 受控交互会话（`InteractionSession`）与弹窗规则（`PopupWatcher`）

- **`InteractionSession`**：提供 `observe`、`click_ref`、`input_text_ref`、`long_click_ref`、`swipe`、`back`、`home`、`start_app`、`wait_for`、`scroll_until`，共享固定墙钟时长与步数预算。直接交互传入最新 `layout_hash`，滚动查找逐步复采。执行前加锁并校验温度、敏感屏，执行后返回新快照和差分。操作结果不确定时要求重新观测。
- `PopupWatcher` 支持精确文案 `text`、正则 `text_regex` 或节点 `node_id`，并始终要求精确 `bundle_name`。文案与正则互斥，节点 ID 可叠加。触发条件为唯一可用可点击控件、当前非敏感屏、同一布局最多一次且处于弹窗处理预算内。默认规则为空。

## 7. 公共离线测试与候选 Fixture

`moyu_harmony_pilot.testing` 提供 `FakeHdcTransport`、`make_fake_device`、`OfflineTestCase` 和可复用断言。外部插件可复用 `assert_idempotent_success`、`assert_unknown_no_replay`、`assert_pre_submit_blocked`、`assert_readonly` 验证成功防重、不确定状态、过温/敏感屏与只读边界。独立模板含可安装的三段式示例与离线测试，使用方式见[插件模板](../examples/plugin-template/README.md)。

界面流程可通过 `FakeDeviceState.click_targets` 声明按顺序预期的 `FakeClickTarget(node_id, bundle_name)`。Fake 在当前布局内查找唯一、可用的可点击控件，核对点击中心后推进到下一份布局。错误目标会记录失败，公共幂等断言在 Runner 返回后重新检查该记录，预期 `unknown` 的用例也会暴露误点。自定义场景结束时调用 `transport.assert_click_targets_consumed()` 检查失败记录及尚未发生的预期点击。

声明空列表可验证零次点击。省略该字段时，已有 `advance_layout_on_click` 场景继续使用原行为。新模板采用精确目标，并覆盖点击旁边控件时应失败的回归。

采集候选布局使用只读命令。

```bash
harmony-pilot fixture capture --device harmony-01 --out .state/candidate-layout.json --json
```

采集器在本地脱敏文本、包名和节点标识，结果始终标记 `manual_review_required=true`。可重复使用 `--keep-text` 与 `--keep-bundle` 显式保留审查过的通用值。人工检查候选文件后才能复制到公开测试目录。真实设备样本与合成测试样本的来源和验收状态见[兼容矩阵](compatibility.md)。
