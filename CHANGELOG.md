# 更新日志（Changelog）

本项目遵循语义化版本（Semantic Versioning）规范。`0.x` 阶段保持核心契约稳定演进，重大变更会提前说明。

---

## [0.2.1] - 2026-11-28

统一内部／外部插件调用契约，支持业务插件组合调用通用操作，并发布本地文件与相册导出。

### 新增与改进

- 新增 `ctx.call(action_id, params=None, *, idempotency_key=None, timeout=None)`，可调用内部或外部插件，返回 `ActionOutcome`，共享设备锁、总时间和步数预算，失败状态自动向上传递。
- 新增内部插件 `sms.read`、`files.query`、`files.export`、`record.start`、`record.status`、`record.stop`。已有 `sms.open`、`app.install`、`app.uninstall` 等统一标记为内部插件，CLI 与 MCP 提供各自受控调用入口。
- 新增本地 `files query/export`，支持按图库名称查询媒体 URI，将图片、视频、音频和文档逐个按原始字节导出。普通文件经 `hdc` 拉取，媒体 URI 与图库 Photo 路径经 `mediatool` 受管临时文件导出。结果包含大小、SHA-256、传输方式与待清理项，文件权限为 `0600`，显式 `--overwrite` 成功后原子替换，失败保留旧文件。
- 新增结构化直接结果 `ActionOutcome.data`，上限 1 MiB。默认 `to_dict()` 输出安全回执，本地 CLI 或显式私有 MCP 入口通过 `to_dict(include_data=True)` 交付直接结果。私有正文仅留在调用结果中，参数与账本单条 JSON 载荷上限为 16 KiB。
- 完善组合动作幂等保护。有副作用子动作限父 `prepare/submit` 阶段调用并使用稳定幂等键。子动作真正到达提交预留阶段时，先递归将父／祖先请求预留为 `submitting`，再预留子请求并提交，各自记账。检查拒绝或命中已有成功记录时跳过新增父预留，父准备完成后更新持久化基线。已有子提交后的父失败、取消或崩溃保留 `unknown`，阻止整请求自动重放。
- 修复录屏启动与停止的预算和清理边界，停止超时或等待中断时保留仍在运行的会话，支持后续查询与恢复收尾。录屏会话可与业务操作并行。

### 外部使用方升级要点

- 使用 `ctx.call` 或新增结果元数据的插件声明 `min_pilot_version="0.2.1"`，并将自身包依赖下限设为 `>=0.2.1`。已有 Action API v1 插件继续兼容。独立模板固定测试基线升级为 `0.2.1`，CI 默认标签为 `v0.2.1`，最低兼容版本为 `0.2.0rc1`。
- 能力发现消费方需接收新增动作 ID，以及 `source`（`internal` / `external`）、`result_schema`、`private_content`、`lock_device` 字段。外部插件应使用自身命名空间，避免占用本版新增内部动作 ID。通过 `.data` 获取结构化结果，日志、指标和账本仅保存脱敏信息。
- 远程调用逐个校验父子动作白名单与私有内容授权。MCP 短信子调用需同时启用 `--enable-sms`、放行父动作和 `--allow-action sms.read`，导航还需 `--allow-action sms.open`。Bridge 拒绝私有正文插件。文件与录屏插件为 `local_only`，其 `readonly` 表示设备只读，宿主机仍会产生文件和会话。MCP 专用录屏入口由 `--enable-recording` 授权。
- 子动作或组合请求出现 `unknown` 时，先按子动作原 ID、幂等键与参数只读对账，再核验父动作，人工结案沿用 `ledger resolve` 流程。尚无子提交的普通准备失败仍可按原键重试。`record stop` 超时保留会话，使用 `record status` 查询后再次 `record stop` 收取结果。

### 验收范围

本版复用改造阶段已通过的 219 项定向离线测试（含独立分发安装）、类型检查与 lint 结果。正式发布提交的完整 CI 结果以 Release 工作流为准。本次插件组合、文件／相册导出及录屏恢复流程的真机验收待完成。

## [0.2.0] - 2026-11-27

正式发布 `0.2.0` 版本：包含 `0.2.0rc1` / `0.2.0rc2` 的全部插件生态、应用管理、OCR/内联截图、短信读取与发布工程能力，并新增面向产品验收与调试的本地 MP4 录屏能力。核心默认依赖仍保持为空（仅依赖 Python 3.11+ 标准库）。

- **本地 MP4 录屏（方式一：独立会话 / 定时录屏）**：基于官方 `snapshot_display` 按单调时间戳连续采帧，并通过宿主机 `ffmpeg` 合成 1:1 实时节奏的 H.264 MP4（`+faststart`、`0600` 权限）。新增 `harmony-pilot record start / status / stop / capture` 命令，采帧不占用 `DeviceLock`，可与 `dump-ui`、`interact`、`run` 并发运行；后台会话经 `stdin` 传递串号，不落盘或暴露真实设备串号，结束后自动清理本地临时帧与设备端 `/data/local/tmp/moyu_pilot_*`。
- **跟随动作自动录屏（方式二）**：`harmony-pilot run` 新增 `--record`、`--record-out`、`--record-on-failure`、`--record-fps`、`--record-max-width`，支持全程录制或仅在动作未成功时保留录像，录制异常不掩盖原动作状态与幂等账本；`doctor` 新增信息性 `ffmpeg_binary` 自检，`schema` 同步自描述。
- **MCP 可选录屏工具**：`harmony-pilot-mcp` 新增显式 `--enable-recording` 启动开关，暴露 `record_start` / `record_status` / `record_stop` 及 `run_action(record=..., record_on_failure=...)`，输出文件严格限定在本地 `0700` `diagnostics/` 目录，不向模型回传视频字节流，默认 Safe Mode 保持不变。
- **核心离线测试套件治理**：收口并强制校验核心套件规模上限（300 项）与代表性参数化上限，保持全量离线运行快速稳定。
- 插件模板固定测试基线与 CI 默认标签升级至 `0.2.0`，最低兼容版本 `min_pilot_version` 与依赖下界保持 `0.2.0rc1`。

### 验收范围

- 离线全量测试、严格类型检查（`mypy --strict`）、`ruff` 与源码包/wheel 离线构建及 `twine check --strict` 全部通过。
- HarmonyOS 6.1（API 24）真机完成只读端到端录屏实测：方式一后台录屏并发 `dump-ui`、方式二 `run --action device.health --record-out` 均产出 `ffprobe` 校验通过的 H.264 MP4，结束后设备 `/data/local/tmp/moyu_pilot_*` 为 0。

## [0.2.0rc2] - 2026-11-25

新增系统短信读取，支持通过本地 CLI、SSH 或显式开启的 MCP 获取当前可见消息，核心仅依赖 Python 标准库。

- 新增 `sms.open` 标准动作与 `sms open` 命令，复用设备锁、温度保护和 SQLite 幂等账本，支持唤醒、启动短信应用及从详情返回会话列表。同一幂等键防止重复导航，提交后不确定状态保留 `unknown` 并支持只读对账。
- 新增 `sms read`，返回当前可见会话摘要或正文、发送方、原始时间标签和验证码候选，支持发送方精确过滤及条数、字符预算。
- 等待模式以调用开始时的可见消息建立基线，排除已有消息与相对时间标签变化，新增内容返回 `new_message`，到期返回 `timeout` 和空消息列表。
- MCP 通过 `--enable-sms` 选择开启 `sms_read`，导航通过 `--allow-action sms.open` 授权。补充 SSH 远程调用流程，正文和验证码通过读取结果交付，导航账本与 Bridge 回执仅保留状态指标。
- 兼容真实焦点窗口缺少 bundle 的情况，通过焦点窗口 ID 与布局包名交叉确认短信页面归属。
- 修复已识别短信语义内容被敏感屏规则误判的问题，结合包名、窗口和完整文本结构识别正文。密码、验证码输入表单及权限确认触发人工接管。
- 修复正文截断处的验证码候选误读，提取时结合完整正文与返回长度，排除跨越裁剪边界的数字串。
- 插件模板固定测试基线与 CI 默认标签升级至 `0.2.0rc2`，同步 README 测试说明，最低兼容版本 `min_pilot_version` 与依赖下界为 `0.2.0rc1`。

### 验收范围

- 全量 1524 项离线测试通过，覆盖短信解析、等待基线、CLI/MCP 接口与安全回归。
- 真机已验证列表读取、详情正文、返回列表、等待 `timeout` 及导航记录的只读对账，结束时受管临时文件为 0。
- 真实新短信到达待验收。使用方法与已验证应用版本见 [短信使用指南](docs/sms.md)。

## [0.2.0rc1] - 2026-11-21

面向社区插件开发的首个 `0.2.0` 候选版本，通过 GitHub Release 提供 wheel、源码包与 SHA-256 校验和。插件模板的依赖、最低宿主版本、离线测试基线与 CI 默认标签统一为 `0.2.0rc1`。

- 新增 `IdempotentAction.prepare()` 可选提交前准备阶段（支持 `ctx.interact()` 交互、前置指标合并与公共契约断言），在落盘 `submitting` 前处理拉起应用、导航与环境阻塞，避免前置失败锁死幂等键；新增本机 `ledger release` 与 `ActionRunner.release()`，支持人工确认无副作用后带审计备注释放不确定预留以原键重试。
- 完成 6.1 / API 23 与 7.0 / API 26 双设备只读入口及 Bridge 双子进程验收，验证目录插件发现、设备锁、重复任务防重、回执与退出清理，记录显式 `display_id=0` 观察结果。
- 新增 `plugins init` 离线工程生成，复用随包模板并同步社区插件的分发包名、模块名、动作 ID、测试和 CI，返回结构化的后续命令。分发测试覆盖安装态生成与独立插件契约。
- 公共 Fake 新增按控件 ID 与包名验证的预期点击序列，幂等契约同步检查目标匹配，独立模板增加误点回归，避免错误点击被成功界面或预期 `unknown` 掩盖。
- 分发包排除示例和测试目录中的私有配置、插件与诊断文件，发布草稿续传时验证附件集合与构建文件清单一致。
- 修复 Linux 严格类型检查中 macOS OCR 初始化被判定不可达的问题，运行时继续按宿主平台校验可选后端。
- 修复候选 Fixture 中纯空白文本脱敏改变剪枝/折叠的问题，紧凑快照补齐同窗口祖先包名以支持真实布局的弹窗规则。
- 完成当前 6.1 / API 23 手机经授权的亮灭屏、成功防重、确认失败后 `unknown` 防重与只读对账，补充 Bridge 回执恢复和优雅退出记录。
- 新增插件 `min_pilot_version` 注册校验，支持开发版、预发布版及正式版的顺序比较，已有 `0.2.0.dev0` API 声明保持兼容。
- 分离检查阶段与最终成功指标，保留 `human_required` / `failed` 的回执语义。新增上下文绑定交互与弹窗辅助，复用设备锁和预算，限制各阶段副作用。
- 新增公共 `moyu_harmony_pilot.testing`、通用三段式插件、内存 Mock Broker 与 `fixture capture` 候选布局采集，供独立插件离线验收。
- 新增本地 OCR CLI 与显式开启的 MCP 只读 OCR、受大小限制的内联 `ImageContent`。细化密码控件、生物识别和高危权限检测，弹窗规则支持正则与节点 ID。
- 新增 `receipts prune`，裁剪已关闭成功回执载荷并保留任务防重记录，运行中、未确认和不确定回执完整保留。
- 添加 `py.typed`、全源码 strict mypy、下游类型契约测试及 macOS / Linux × Python 3.11 / 3.12 四组合 CI。离线套件包含 956 项测试，独立插件模板另有 9 项契约测试。Pull Bridge 协议冻结为 v1.0，新增 Tag 构建、校验和与发布工作流。
- 新增明确标为合成的 5.x / 6.x 手机、平板、折叠屏兼容样本与发布验收清单。当前 6.1 / API 23 单机只读和亮灭屏幂等复验通过，真实跨版本样本、应用部署及输入等场景待对应环境验收。
- 新增 `plugins validate` 离线插件参数预检，在校验错误中定位已声明字段；未知字段名、输入值和未预期异常正文不再回显。
- 新增本地 HAP 安装更新、卸载、应用列表与版本/入口查询、HTTP(S) 下载与可选摘要校验。安装和卸载接入持久化幂等账本与结果确认。
- 安装与下载支持鸿蒙 `.app` 分发包和应用内 HSP，提供 `--package` 通用参数。APP 主机侧展开、元数据核对与原始包摘要复核覆盖 CLI、Action 和设备 SDK，临时文件自动清理。
- 单个 APP 在 API 22+ 设备上通过原生完整包安装，保留外层签名；较早 API 和组合输入沿用模块展开流程。补充原生安装失败后防重及签名环节说明。
- 应用列表解析兼容真机 `bm dump -a` 的 `ID: <用户编号>:` 分组输出，保留旧文本与 JSON 格式支持。
- 新增本地 CLI 交互命令及 MCP 引用式输入、长按、桌面返回、应用启动、等待与滚动查找。修复 UI 等待剩余预算和窗口过滤透传。
- 新增账本查询、只读对账、人工成功核销、历史裁剪及 v1 数据库事务迁移。
- Bridge 新增可配置 `max_jobs` 和按设备隔离的子进程执行槽位，父进程维护租约和回执。
- 新增自包含 HTML 界面检查页与执行报告。
- 增加安装不确定状态、下载失败、Unicode 输入、旧账本迁移与真实子进程并发离线回归。新功能的多版本与副作用真机验收继续按发布清单推进。

### 验收范围

- 真机覆盖 6.1 / API 23 和 7.0 / API 26 两台设备的只读入口与 Bridge 并发，受控亮灭屏幂等验收覆盖 6.1 设备。记录包含实际测试源码、设备状态及清理结果，见 [兼容性矩阵](docs/compatibility.md)。
- 5.x、平板、折叠切换、其他显示器编号、成功应用部署、中文/特殊字符输入、真实弹窗点击及物理断连/真实断网仍待对应环境验收。公开夹具中的合成样本保留来源标记，真实脱敏候选待人工审查。

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
