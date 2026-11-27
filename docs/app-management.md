# 应用管理、文件导出、交互与运行诊断

这些能力基于官方 `hdc`、`bm`、`aa`、`uitest`、`mediatool` 工具链。核心依赖保持为 Python 标准库。示例中的 `harmony-01` 是设备别名。

## 应用查询与部署

```bash
harmony-pilot apps list --device harmony-01 --json
harmony-pilot apps info --device harmony-01 --bundle com.example.demo --json
harmony-pilot apps install --device harmony-01 --hap ./entry.hap --idempotency-key demo-v1 --json
harmony-pilot apps install --device harmony-01 --package ./release.app --idempotency-key release-v1 --json
harmony-pilot apps uninstall --device harmony-01 --bundle com.example.demo --idempotency-key remove-v1 --json
```

`--package` 接受本地 `.hap`、应用内 `.hsp` 和鸿蒙 `.app` 分发包，扩展名大小写均可。原有 `--hap` 参数继续可用，也接受这些格式。重复传参可组合部署同一应用、同一版本的多个模块。覆盖更新使用同一安装命令，每次独立部署使用新的幂等键。系统负责检查签名、调试权限和安装策略，Pilot 验证工具成功回执以及安装后的应用版本。

Pilot 在主机的私有临时目录中展开 `.app` 以验证 `pack.info` 中的应用标识与全部 HAP/HSP 的 `module.json` 一致。安装单个 `.app` 时，API 22+ 设备收到完整的原始 APP 文件，保留外层签名字节；较早 API 设备收到展开后的模块。组合输入多个包时沿用一次提交 HAP/HSP 模块组的方式。单次安装最多 32 个模块，APP 模块总展开大小上限为 512 MiB。路径穿越、符号链接、重复成员及损坏的包会被拒绝，临时文件在成功或异常退出后清理。

幂等参数记录原始 `.app` 文件路径和 SHA-256，提交前复制并复核原文件。安装中断后的只读对账可继续使用原请求参数。多设备类型、CPU 架构或重复模块的具体适配仍由系统安装器校验。

应用市场上架包可以对外层 APP 签名，由市场验证后拆包并对 HAP 签名。设备侧安装会检查内部 HAP/HSP 的签名，系统返回 `9568320 / no signature file` 时应检查具体模块。构建工程中设置 `packOptions.appWithSignedPkg=true` 可额外生成内部模块已签名的 `*-all-signed.app`，也可使用同次构建的已签名 HAP。

签名是否受目标设备接受由系统安装器校验。在商用 HarmonyOS NEXT 真机上通过 `hdc` 侧载时，`app_gallery` 分发类型的 `release` 签名包会因非信任安装源被拒绝（`9568322`），未包含当前设备 UDID 的 `debug` 签名包会因设备未授权被拒绝（`9568423`）；真机直装需使用包含目标设备 UDID 授权的调试/内测签名或受设备信任的企业签名。提交失败后保持 `unknown` 防重记录，先只读查询安装状态。同一请求通过幂等键阻止重放，新的已授权部署单独记录。

```bash
harmony-pilot apps download --url https://packages.example.org/entry.hap --out ./entry.hap --json
harmony-pilot apps install --device harmony-01 --url https://packages.example.org/entry.hap --idempotency-key deploy-v1 --json
harmony-pilot apps download --url https://packages.example.org/release.app --out ./release.app --json
harmony-pilot apps install --device harmony-01 --url https://packages.example.org/release.app --idempotency-key deploy-app-v1 --json
```

支持 HTTP(S) 下载，`--sha256` 可提供原始下载文件的预期摘要。下载有大小上限和超时检查，通过结构与摘要校验后原子替换目标文件。URL 安装缓存位于状态目录的 `packages/` 下，按 URL 路径的 `.app` / `.hsp` 扩展名识别格式，其他路径沿用 `.hap`。下载接口没有格式扩展名时，可先用 `apps download --out ./release.app` 指定保存格式，再从本地安装。Python 提供 `download_package()` 和 `package_metadata()` 通用入口，原有 `download_hap()` 与 `hap_metadata()` 可继续使用。

`app.install`、`app.uninstall` 使用现有 `ActionRunner`、设备锁、过温门控和 SQLite 幂等账本。当前两项动作设为本地执行，CLI 同时提供 `plugins describe` 参数契约。宿主机文件路径和下载地址由本地部署方管理。

Python SDK 提供 `HarmonyDevice.list_apps()`、`app_info()`、`install_app()`、`uninstall_app()`。直接调用 SDK 安装原语时，调用方负责持有设备锁并管理提交状态。需要持久化幂等保证时使用 `ActionRunner`。`start_app(bundle, ability_name=None)` 会查询入口，多个候选入口无法明确选择时要求显式传入 Ability。省略 `ability_name` 继续使用原有 `EntryAbility` 默认值。

## 不确定结果与账本维护

安装或卸载提交后出现断连、超时、确认失败时，状态保持 `unknown`。恢复连接后可以使用原请求参数执行只读对账。`apps install --json` 返回的 `data.params` 包含可复用参数。

```bash
harmony-pilot ledger list --device harmony-01 --status unknown --json
harmony-pilot ledger inspect --device harmony-01 --action app.install --idempotency-key deploy-v1 --json
harmony-pilot ledger reconcile --device harmony-01 --action app.install --idempotency-key deploy-v1 --params-json '<原始 data.params JSON>' --json
```

对账仅调用插件 `confirm()`，成功后写入审计记录。原安装版本和目标版本相同时，单凭版本号无法确认新构建是否生效，对账保持 `unknown`。旧账本缺少安装前版本证据时也保持 `unknown`。人工完成核验后可结案。

```bash
harmony-pilot ledger resolve --device harmony-01 --action app.install --idempotency-key deploy-v1 --params-json '<原始 data.params JSON>' --note '已人工确认应用构建与运行结果' --json
harmony-pilot ledger release --device harmony-01 --action app.install --idempotency-key deploy-v1 --params-json '<原始 data.params JSON>' --note '已人工确认未产生副作用，释放重试' --json
harmony-pilot ledger prune --older-than-days 90 --json
```

`resolve` 记录成功核验和备注，并继续保留幂等键。`release` 仅面向本机人工操作，在持物理设备锁并核对原请求参数与插件版本后，释放 `submitting` / `unknown` 预留并向 `resolutions` 表写入不可变 `released` 审计记录，允许随后复用原幂等键重试；已确认 `succeeded` / `already_done` 的记录不可释放。`prune` 只清理已成功结案的历史，保留 `submitting` 和 `unknown`。裁剪后的历史幂等键将失去防重记录，应按业务防重期限设置保留天数。账本 v1 会在事务中自动迁移，保留原有记录。

## 手机文件查询与导出

通过本地 CLI 或 Python SDK 查询图库媒体，并将设备上的单个文件导出到宿主机。图片、视频、音频和文档均按原始字节传输，每次导出一个文件。

### 按图库名称查询

`files query` 使用设备提供的 `mediatool query NAME -u` 查询媒体 URI。将示例名称替换为图库中的文件名，导出时使用查询返回的 URI。

```bash
harmony-pilot files query --device harmony-01 --name '旅行照片.jpg' --limit 100 --timeout 30 --json
```

`--name` 为必填参数。`--limit` 默认为 100，取值 1–1000。`--timeout` 默认为 30 秒。成功时 JSON 信封的 `data` 包含以下字段。

| 字段 | 含义 |
| --- | --- |
| `alias` | 设备别名 |
| `name` | 本次查询的名称 |
| `uris` | 按字典序排序、受 `--limit` 限制的媒体 URI 列表 |
| `count` | 本次返回的 URI 数量 |
| `total` | 匹配的 URI 总数 |
| `truncated` | 是否因条数上限而截断结果 |

### 导出到指定本地文件

`files export` 的 `--source` 接受设备上的绝对文件路径或 `file://media/Photo/<id>[/...]` 媒体 URI，`--out` 指定包含文件名的本地目标路径。以下示例中的媒体 ID 和设备路径按实际文件填写。

```bash
# 使用查询返回的 URI 导出图片或视频
harmony-pilot files export --device harmony-01 --source 'file://media/Photo/100' --out .state/exports/photo.jpg --timeout 300 --json
harmony-pilot files export --device harmony-01 --source 'file://media/Photo/101' --out .state/exports/video.mp4 --json

# 使用设备绝对文件路径导出普通文件
harmony-pilot files export --device harmony-01 --source /data/local/tmp/example.pdf --out .state/exports/example.pdf --json

# 显式授权更新已有目标文件
harmony-pilot files export --device harmony-01 --source 'file://media/Photo/100' --out .state/exports/photo.jpg --overwrite --json
```

目标默认拒绝覆盖已有文件。传入 `--overwrite` 后，导出成功时原子替换目标，接收或校验失败时保留旧文件。导出文件权限为 `0600`，本地临时目录权限为 `0700`。`--timeout` 默认为 300 秒，上限为 3600 秒。

发布新文件时优先使用硬链接。目标卷缺少硬链接支持时，使用独占创建并复制已校验的内容。该兼容路径在复制期间会出现目标文件，复制失败时清理本次创建的目标。

普通文件通过 `hdc file recv` 拉取。媒体 URI，以及 `/storage/media/local/files/Photo/...` 或 `/storage/media/<UID>/local/files/Photo/...` 路径，先由 `mediatool recv` 导出到设备端受管临时文件，再通过 `hdc` 拉取。设备临时文件在 `finally` 阶段清理，清理另有 10 秒预算。

成功时 JSON 信封的 `data` 包含以下字段。

| 字段 | 含义 |
| --- | --- |
| `alias` | 设备别名 |
| `source` | 本次导出的设备文件路径或媒体 URI |
| `path` | 宿主机上的导出文件路径 |
| `size_bytes` | 导出文件的字节数 |
| `sha256` | 导出文件的 SHA-256 摘要 |
| `transport` | `hdc` 表示普通文件拉取，`mediatool` 表示经媒体工具导出后拉取 |
| `cleanup_pending` | 本次待清理项，通常为空数组。每项的 `location` 为 `device` 或 `host`，`path` 为对应路径 |

完成导出后的清理异常通过 `cleanup_pending` 返回。设备恢复连接或宿主机恢复写入权限后，可按返回的位置和路径清理这些文件。

### 设备权限与 SDK 调用

图库查询和媒体导出需要设备提供官方 `mediatool`，可访问范围由设备当前用户和权限决定。隐藏相册及云端尚未下载媒体的可访问性，以官方工具在该设备上的实际返回为准。

CLI 调用期间持有单设备锁。Python SDK 提供 `HarmonyDevice.query_media()` 与 `HarmonyDevice.export_file()`，调用方通过 `device.acquire_lock()` 持锁后调用。文件保存在执行命令或 SDK 的宿主机上。

外部 Action 插件可调用上述 SDK。MCP 通过 `--allow-action` 显式授权插件动作，Bridge 通过逐设备 `allowed_actions` 授权，插件接入遵循现有参数契约、回执和敏感信息边界。

## 本地 Agent 交互

```bash
harmony-pilot interact observe --device harmony-01 --local-lab --json
harmony-pilot interact input --device harmony-01 --local-lab --ref @e3 --layout-hash '<最新哈希>' --text '中文查询' --json
harmony-pilot interact long-click --device harmony-01 --local-lab --ref @e4 --layout-hash '<最新哈希>' --json
harmony-pilot interact start --device harmony-01 --local-lab --bundle com.example.demo --layout-hash '<最新哈希>' --json
harmony-pilot interact wait --device harmony-01 --local-lab --selector-json '{"id":"result"}' --json
harmony-pilot interact scroll --device harmony-01 --local-lab --selector-json '{"text":"目标条目"}' --max-swipes 8 --json
```

还提供 `click`、`swipe`、`back`、`home`。直接交互使用最新布局哈希，滚动查找会在每次滑动前重新观测和检查。输入支持单行、最多 4096 字符，敏感屏转人工处理。

CLI 每次调用创建一份 `--timeout` / `--max-steps` 预算。MCP 本地探索会跨工具调用共享同一个会话预算。Python `wait_for` / `wait_gone` 已将剩余抓取时间和窗口、屏幕过滤参数向下传递。SDK 的超时为 0 时保留原有单次观测语义，采用布局抓取默认超时。临时文件清理另有最多 1 秒的收尾预算。

## 多设备并发

在私有 Bridge 配置增加 `max_jobs` 字段，取值 1–32，默认值为 1。父进程负责续租、心跳与持久化回执，子进程各自拥有主线程超时计时器和物理设备锁。

每批认领最多 `max_jobs` 个任务，每台设备分配一个任务。该批结束后继续认领，子进程退出或超时会形成失败或不确定回执，其余设备继续完成。所有服务必须共用相同状态目录，以协调同一物理设备。

## 可视化检查与报告

```bash
harmony-pilot inspect --device harmony-01 --out .state/diagnostics/inspect.html --json
harmony-pilot ledger report --out .state/diagnostics/ledger.html --limit 100 --json
harmony-pilot report --input ./saved-result.json --out .state/diagnostics/run.html --json
```

检查页是自包含的本地 HTML，截图内嵌，可点击元素框查看属性并按文字筛选。捕获前后校验布局哈希。执行报告支持保存的 JSON 信封和账本记录。文件采用 `0600` 权限，截图、页面文字和报告应保留在本地私有目录。

## 产品验收与调试录屏

由于 HarmonyOS NEXT 官方命令行不内置 Android 风格的 `screenrecord`，Pilot 基于官方 `snapshot_display` 按实际时间戳连续采帧，并通过宿主机 `ffmpeg` 合成 1:1 实时节奏的标准 H.264 MP4（`yuv420p`、`+faststart`）。采帧不占用 `uitest` 独占设备锁，可与交互操作和 Action 执行并发运行，核心 Python 依赖仍保持零第三方包。

### 方式一：独立会话录屏（适合手工验收与多命令调试）

```bash
# 后台启动录屏（不阻塞后续 dump-ui / interact / run 等命令）
harmony-pilot record start --device harmony-01 --out .state/diagnostics/demo.mp4 --fps 2 --max-duration 120 --json

# 查询录屏状态
harmony-pilot record status --device harmony-01 --json

# 停止录屏并封装 MP4（自动清理临时帧与设备端 /data/local/tmp/ 临时文件）
harmony-pilot record stop --device harmony-01 --json

# 或直接前台定时录一段（支持 Ctrl+C 提前结束并正常封装）
harmony-pilot record capture --device harmony-01 --duration 10 --out .state/diagnostics/clip.mp4 --json
```

### 方式二：跟随动作执行自动录屏（适合自动化验收与失败留证）

```bash
# 全程录屏并自动保存到 .state/diagnostics/
harmony-pilot run --device harmony-01 --action device.health --record --json

# 指定输出路径，或仅在动作未成功时保留录像（成功时自动清理）
harmony-pilot run --device harmony-01 --action device.health --record-out .state/diagnostics/run.mp4 --record-on-failure --json
```

- 依赖与自检：需要宿主机已安装 `ffmpeg`（如 macOS 执行 `brew install ffmpeg`），`harmony-pilot doctor` 会报告 `ffmpeg_binary` 检测状态；缺失时录屏命令会在触达设备前快速报错（退出码 `2`）。
- 安全与隐私约束：录屏仅限本地执行；后台会话元数据与进程参数不落盘或暴露真实设备串号；最终 MP4 文件强制 `0600` 权限，最长录制时长受 `--max-duration`（上限 600 秒）保护，录制结束自动清理临时帧目录。

## 验证范围

文件查询与导出当前为离线实现，普通文件、图库图片和视频的真机验收待完成。

新增离线测试覆盖 HAP/APP 安装、内含 HSP 的多模块展开、卸载、防重复提交、安装已生效但回执超时、相同版本更新不确定、下载校验和原子替换、异常包拒绝与临时目录清理、旧账本迁移、中文及特殊字符参数转义、等待预算和过滤透传、并发子进程重叠执行、单子进程崩溃与超时、HTML 转义和文件权限。

2026 年 11 月 20 日在允许访问本机 hdc 服务的运行环境中，HarmonyOS 6.1 / API 23 真机通过设备枚举、健康检查、布局和截屏验证。应用查询补充了实测出现的 `ID: 100:` 用户分组列表格式，离线回归使用合成包名覆盖该格式与旧格式。

本次真实 `.app` 通过主机解析、展开和字节一致性核验。文件结构检查发现外层 APP 有签名块，内层 HAP 未发现对应签名块。先前展开 HAP 的提交和后续 API 23 完整 APP 的原生提交均返回 `9568320 / no signature file`，两次请求分别保留 `unknown` 记录。只读复查确认应用未安装，相关受管临时文件已清理。外层签名块检查仅确认结构，签名的密码学有效性尚未验证。

2026 年 11 月 21 日补充对两份已签名 `.hap` 样本执行了官方 `hap-sign-tool` 离线密码学验签（`verify-app` 与 `verify-profile` 均通过，包含 `Hap Signing Block v3`、代码签名与 SHA-256 摘要校验）及真机安装验证。`release / app_gallery` 签名样本在真机 `hdc` 通道返回 `9568322 / signature verification failed due to not trusted app source`；绑定其他设备 UDID 的 `debug` 签名样本经离线比对确认不包含本机 UDID，真机安装返回 `9568423 / device is unauthorized`。经 `harmony-pilot apps install` 提交后均保留 `unknown` 防重记录，复用同一 `--idempotency-key` 重放时直接阻断未重复提交；只读 `apps info` 确认未安装，宿主机与设备侧临时文件均无残留。

受商用 HarmonyOS NEXT 侧载签名策略限制，成功安装及覆盖更新真机验收需使用已授权当前设备 UDID 的调试签名包或受信任企业签名包；该项与中文实际输入效果、更多 HarmonyOS 5.x / 6.x 版本及断线重连真机验收仍待完成。
