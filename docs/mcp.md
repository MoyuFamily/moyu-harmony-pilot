# 本地 MCP 适配层使用指南

`moyu-harmony-pilot` 提供可选的轻量 MCP v2 stdio 适配层（`harmony-pilot-mcp`），方便本地 AI 客户端以标准 MCP 工具方式观测和操作纯血鸿蒙真机。MCP 工具提供内部插件与外部插件的调用入口，底层复用 `actions`、`device`、`ui` 与安全护栏。

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
| `screenshot` | 只读 | 在本地私有诊断目录生成截图并返回文件元数据。`som=true` 附带编号标注与对应快照（需安装 `[vision]`）。启动时加 `--inline-images` 后可按次请求内联图片 |
| `list_actions` | 只读 | 返回已授权插件的 `source`（`internal` / `external`）、参数／指标／直接结果 Schema、`private_content`、`lock_device` 与副作用级别 |
| `run_action` | 受控 | 执行经 `--allow-action` 显式放行的动作 |

### 安全约束

- 通用动作入口默认使用空白名单，通过重复传入 `--allow-action <ACTION_ID>` 放行父动作与每个子动作。每次 `ctx.call` 都校验远程授权，`local_only` 动作限本地调用。
- **仅接受配置别名**：所有设备参数必须匹配私有配置中的别名，不会暴露真实串号；截图文件名由适配层自动生成并保存在受控诊断目录中。
- **统一返回与共享护栏**：所有工具返回值都包含 `ok`、`data` 与结构化 `error`，失败时同步设置 MCP `isError=true`。插件过程日志会重定向到 `stderr`，不污染 stdio 协议通道。有副作用动作与 CLI / Bridge 共用同一套 `ActionRunner`、设备锁、过温保护与 SQLite 幂等账本。
- **受控错误信息**：可控的设备/UI/视觉异常保留明确的错误码、提示和可重试标记；包含底层子进程输出的 `hdc` 错误会被裁剪为有界提示，避免泄露无关原文；未预期异常仅返回通用错误；提交后不确定的结果保持 `retryable=false`，需人工核对或只读对账。

插件内的 `ctx.call(...)` 共享设备锁与总预算，失败状态自动向上传递。有副作用子动作限父 `prepare` 或 `submit` 调用并使用稳定幂等键。子动作到达 reserve 阶段时，先递归预留父／祖先请求，再预留子请求并提交，以保护已发生的子提交。子 `unknown` 或后续父失败、取消、崩溃均使父请求保留 `unknown`。先按子动作原 ID、幂等键与参数只读对账，再核验父动作，人工结案沿用 `ledger resolve` 流程。

`ActionOutcome.to_dict()` 默认提供安全回执，显式授权的私有 MCP 入口通过 `to_dict(include_data=True)` 交付 `.data`，其上限为 1 MiB。参数与账本中的单条 JSON 载荷上限为 16 KiB。

## 系统短信读取

通过 `--enable-sms` 显式增加内部插件 `sms.read` 的私有入口 `sms_read`。参数包括设备别名、发送方、条数上限、等待时长和总超时，结果包含当前可见短信原文及验证码候选。内部插件 `sms.open` 通过 `run_action` 和幂等键执行导航。

```bash
harmony-pilot-mcp --config devices.local.json --enable-sms \
  --allow-action sms.open --allow-action sms.read
```

外部父插件通过 `ctx.call("sms.read", ...)` 读取短信时，必须同时满足 `--enable-sms`、`--allow-action sms.read` 和父动作白名单。流程先调用 `sms.open` 时也需授权该动作，例如父动作名为 `team.message_review`。

```bash
harmony-pilot-mcp --config devices.local.json --enable-sms \
  --allow-action team.message_review \
  --allow-action sms.open --allow-action sms.read
```

`private_content=true` 标记短信私有结果。原文仅留在直接调用结果中，客户端历史也需要按个人敏感数据管理。Pull Bridge 禁止执行私有正文插件，包括父插件内部的子调用。SSH 接入、准备阶段示例和结果字段见[系统短信与验证码读取](sms.md)。

## 内联截图

启动时通过 `--inline-images` 授权内联图片，并用 `--max-image-bytes` 设置单张图片的原始文件字节上限。默认值为 2 MiB（2097152），可设范围为 1 至 16 MiB（16777216）。

```bash
harmony-pilot-mcp --config devices.local.json \
  --inline-images --max-image-bytes 2097152
```

调用 `screenshot` 时显式传入 `inline=true` 才会附加 MCP `ImageContent`。例如下面的工具参数同时请求缩放与 SoM 标注。

```json
{"alias": "phone", "max_width": 640, "som": true, "inline": true}
```

`max_width` 默认为 1280，允许 1 至 8192 像素。返回的第一个内容块是 JSON 文本信封，与 `structuredContent` 一致，随后附加 `image/png` 图片块。文件路径、尺寸、字节数和可选快照始终可以直接 JSON 序列化，base64 数据仅存于图片块。工具发现结果的 `_meta.inline_images` 与 `_meta.max_image_bytes` 提供本次启动的能力和上限。

- 图片缩放和 SoM 标注完成后检查字节数，超限返回 `image_too_large`，可降低 `max_width` 后重新捕获。传输时 base64 编码的体积约为原始图片的 4/3。
- 内联捕获前后在同一设备锁内检查敏感屏与布局。敏感内容返回 `human_required`，布局变化返回可重试的 `layout_changed`。截图使用启动参数 `--timeout` 的时间预算。
- 成功图片保存在 `0700` 诊断目录，文件权限为 `0600`，沿用滚动保留策略。失败、中断、超限或图片编码失败时清理本次本地截图，设备临时文件由共享驱动清理。

## 本地 OCR 观测

通过独立的 `--enable-ocr` 开关注册只读 `ocr` 工具。它可以在默认安全模式使用，使用单次调用预算。后端按调用延迟加载，可选安装 `vision-macos` 或 `vision-rapidocr`，缩放复用现有 Pillow 或 macOS `sips` 实现。

```bash
python -m pip install 'moyu-harmony-pilot[mcp,vision,vision-rapidocr]'
harmony-pilot-mcp --config devices.local.json --enable-ocr
```

```json
{"alias": "phone", "backend": "auto", "max_width": 1280, "timeout": 30.0}
```

`backend` 接受 `auto`、`macos`、`rapidocr`。`auto` 优先选择已安装的 macOS Vision，再选择 RapidOCR。`max_width` 允许 1 至 8192，`timeout` 必须为大于 0 且至多 3600 秒的有限数值，默认 30 秒。缺少后端返回 `vision_unavailable`。

成功信封的 `data` 包含以下字段。

| 字段 | 含义 |
| --- | --- |
| `alias`、`backend` | 配置别名与实际使用的本地后端，注入引擎时后端为 `custom` |
| `width`、`height` | OCR 输入图片的像素尺寸 |
| `source_width`、`source_height` | 缩放前截图的像素尺寸 |
| `scale.x`、`scale.y` | 输入尺寸除以原始尺寸，包含整数取整带来的轴向差异 |
| `resized` | 是否进行了缩放 |
| `boxes` | 按上、左、文字排序的对象列表，每项包含 `text`、`confidence` 和 `bounds` |
| `truncated` | 总文字预算导致返回列表截断时为 `true` |

`bounds` 为输入图片左上角坐标系中的 `[left, top, right, bottom]`。映射回原图时，横坐标除以 `scale.x`，纵坐标除以 `scale.y`。每次至多返回 1000 个框，每框最多 4096 字符，总文字最多 32768 字符。置信度必须是 0 至 1 的有限数值，矩形必须位于图片范围内。

OCR 捕获、后端加载和识别共用设备锁与主线程 POSIX 时间预算。捕获前后检查敏感屏和布局，识别结果通过同一套敏感内容检测后才会导出。`human_required` 需要人工接管，`layout_changed` 需要重新观测，`action_budget_exceeded` 可在调整预算后重新调用。OCR 的 `0600` 临时截图位于私有目录中，成功、失败和超时都会清理。结果在本地 CLI 或 MCP 调用中返回，OCR 作为独立观测函数接入。

主 CLI 与本地适配层共用 `src/moyu_harmony_pilot/actions/observation.py` 的公开接口。

```python
def capture_ocr(
    device: HarmonyDevice,
    *,
    backend: str = "auto",
    max_width: int = 1280,
    timeout: float = 30.0,
    engine: OcrEngine | None = None,
) -> dict[str, Any]: ...
```

## 本地录屏（验收与调试）

通过 `--enable-recording` 显式开启本地录屏能力（依赖宿主机 `ffmpeg`）：

```bash
harmony-pilot-mcp --config devices.local.json --enable-recording --allow-action device.health
```

开启后会额外注册 `record_start`、`record_status`、`record_stop` 三个工具，并允许在 `run_action` 中传入 `record=true` 或 `record_on_failure=true`。所有生成的 MP4 均受限于本地私有 `diagnostics/` 目录（权限 `0600` 并沿用滚动清理），MCP 仅返回路径与时长/帧率等元数据，不向模型回传视频字节流。

这三个工具对应内部插件 `record.start`、`record.status`、`record.stop` 的受控本地入口，录屏会话可与业务动作并行。录屏插件及 `files.query/export` 均为 `local_only`，`readonly` 表示设备只读，宿主机仍会产生文件和会话。通用远程 `run_action` 与其 `ctx.call` 子调用须满足本地执行限制。专用录屏入口由 `--enable-recording` 独立授权，`record_stop` 中断后保留会话，可通过 `record_status` 查看并再次 `record_stop` 恢复收尾。

---

## 本地探索模式（Local Lab Mode）

当你需要让本地 Agent 探索新页面或辅助开发外部插件时，可以显式加上 `--local-lab` 启动：

```bash
harmony-pilot-mcp --config devices.local.json --local-lab --max-steps 20 --timeout 60
```

开启后额外提供 `lab_observe`、`click_ref`、`swipe`、`back`、`input_text_ref`、`long_click_ref`、`home`、`start_app`、`wait_for`、`scroll_until`。文本输入、启动应用和直接交互均复用会话护栏，等待与滚动查找接受声明式选择器对象。

- **固定生命周期预算**：某个设备别名首次调用交互工具时开始计时，在整个服务进程生命周期内共享这份步数与耗时预算，工具调用本身无法自行续期。
- **新鲜布局校验**：先调用 `lab_observe` 获取当前 `layout_hash`，直接交互携带该哈希。`scroll_until` 会在每次滑动前内部重新观测。底层 `InteractionSession` 校验引用有效性、敏感界面和电池温度，执行后返回最新快照以及布局、前台包名和焦点变化差分。
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
