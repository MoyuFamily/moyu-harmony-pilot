# 兼容性矩阵与验收记录

目标平台为 HarmonyOS NEXT 5 / 6+、API 12+，通过官方 `hdc`、`uitest`、`hidumper` 等工具访问设备。兼容性记录同时注明源码提交、宿主机环境、系统版本、设备形态和证据来源。

## 宿主机 CI 矩阵

| 宿主机 runner | Python | 配置的检查范围 | 结果依据 |
| --- | --- | --- | --- |
| `ubuntu-latest` | 3.11 | 编译、ruff、离线测试、mypy、sdist/wheel | 对应提交的 Actions 运行 |
| `ubuntu-latest` | 3.12 | 编译、ruff、离线测试、mypy、sdist/wheel | 对应提交的 Actions 运行 |
| `macos-latest` | 3.11 | 编译、ruff、离线测试、mypy、sdist/wheel | 对应提交的 Actions 运行 |
| `macos-latest` | 3.12 | 编译、ruff、离线测试、mypy、sdist/wheel | 对应提交的 Actions 运行 |

工作流使用 GitHub 托管 runner。记录验收证据时保存实际 runner 镜像、架构和 Python 完整版本，因为 `*-latest` 会随平台维护更新。安装开发依赖后，测试和分发构建通过 `--no-isolation` 与 `PIP_NO_INDEX=1` 使用本地依赖。

分发测试会检查 `py.typed`、元数据、独立插件模板及其离线契约，随后在干净虚拟环境中离线安装 wheel，运行 CLI 自描述和下游 mypy 正反例。CI 定义见 [ci.yml](../.github/workflows/ci.yml)。

## 设备与样本矩阵

`tests/fixtures/compatibility/` 中的四份 JSON 全部为 **SYNTHETIC 手工合成说明性样本**，每份文件均带 `device_capture=false`。文件名中的版本与形态表示覆盖目标，真实系统字段和设备行为仍需采集验收。

| 覆盖目标 | 样本来源 | 已覆盖的离线解析规则 | 真机验收状态 |
| --- | --- | --- | --- |
| 5.x 手机 | `synthetic_harmony_5_phone.json`，SYNTHETIC | `attributes`、字符串布尔值、`key` 后备 ID、字符串 bounds、隐藏祖先、大小写/下划线遥测字段、窗口键值后备 | 待验收 |
| 6.x 手机 | `synthetic_harmony_6_phone.json`，SYNTHETIC | 扁平节点、原生布尔值、`origBounds`、列表/字典 bounds、数值窗口 ID、温度与电压单位、焦点列识别 | 待验收 |
| 6.x 平板 | `synthetic_harmony_6_tablet.json`，SYNTHETIC | 横屏尺寸、双列控件、零面积剪枝、调整列序后的窗口表、多 `DisplayId` 行 | 待验收 |
| 6.x 折叠屏 | `synthetic_harmony_6_foldable.json`，SYNTHETIC | 内外屏独立布局、几何变化与快照哈希、多个显示器电源行、后移的 `WinId` 列 | 待验收 |
| 6.1 / API 23 历史记录 | 既有设备准备文档和遥测测试记录 | 已记录的 `hdc 3.2.0c` 连接、遥测与只读冒烟路径 | 2026-11-21 单机只读复验通过，详见下节 |

### 2026-11-21 当前设备只读复验

本次对 `0.2.0.dev0` 工作区执行了只读复验。设备报告 `OpenHarmony-6.1.0.115`、API 23、`uitest 6.0.2.3`，布局尺寸为 1260 × 2720。

- `devices` 和 `doctor --deep --json` 成功，电池温度为 20°C。
- 布局采集得到 116 节点、15 个紧凑元素，`fixture capture` 成功生成需人工审查的候选文件。
- 截图缩放至 720 × 1554，文件权限为 `0600`。
- 本地 CLI OCR 使用 macOS Vision 返回 5 个文本框，包含双轴缩放元数据。
- MCP 内联截图适配入口成功，图片为 811187 字节，位于默认 2 MiB 上限内。
- 结束后检查 `/data/local/tmp/`，受管 `moyu_pilot_*` 文件数为 0。

原始截图、候选布局及验收摘要保存在被 Git 忽略的本地 `.state/p0-readonly/`。该节记录当前单机只读路径，受控亮灭屏与 Bridge 验收见下节，5.x、平板和折叠屏仍待设备接入。候选布局需人工审查后再决定是否作为公开样本。

### 2026-11-21 受控副作用与 Bridge 恢复验收

本次基于提交 `1094c5601c9618c825f6cbff15ed0dd853a709b3` 的 P0 实现，在同一台 6.1 / API 23 手机完成了经用户明确授权的可逆亮灭屏测试。

- 自定义通用测试动作经 `ActionRunner` 执行两次提交，分别调用官方唤醒和休眠接口，初始与最终屏幕状态均为 `off`。
- 提交时检查 `submitting` 已在独立 SQLite 连接中可见。成功动作使用相同幂等键再次调用返回 `already_done`。
- 在第二次真实提交后注入本地确认异常，结果进入 `unknown`。重建 Runner 后相同键仍为 `unknown`，提交次数保持为 2。
- `reconcile` 通过只读电源状态确认后成功结案。四次只读阶段写交互尝试均在设备调用前被拒绝。
- 当前设备配合内存 Mock Broker 验证确认回执丢失后持久化补发、worker 重建后原样确认，以及遗留 `running` 恢复为 `unknown`。
- 测试进程分别收到 SIGINT 和 SIGTERM 后退出，待发回执均为 0。设备锁已释放，设备端受管临时文件为 0。

故障由本地测试传输注入，真实断网和物理拔线尚待独立验收。亮灭屏覆盖当前设备电源副作用，应用安装/卸载、中文输入、真实弹窗点击及其他设备形态仍需对应测试环境。私有摘要保存在 `.state/p0-controlled/` 和 `.state/p0-bridge-acceptance/`。

### 候选布局审查发现与修复

自动化审查确认两份候选均可解析，所有检查过的值符合结构字段或脱敏占位格式，人工公开样本审查仍待完成。审查额外复现了两个通用缺陷，并使用独立合成回归验证修复。

- 纯空白文本脱敏保持空白语义，避免新增可见占位文本改变剪枝和标签折叠。
- 紧凑元素解析最近窗口祖先的包名，遇到不同窗口且无显式包名时保留未识别状态。在本地候选上，15 个紧凑元素获得有效包名，6 个可点击元素可被精确规则唯一匹配。该检查只进行规则选择。

历史环境说明见 [真机准备指南](device-setup.md)，对应既有回归在 `tests/test_device.py` 与 `tests/fixtures/harmony_61_home_layout.json`。新增合成样本的来源限定为当前 `ui/parser.py` 与 `device/telemetry.py` 已实现的解析规则。

## 多显示器解析边界

- 窗口表按表头定位 `WinId`，再匹配 `Focus window`。合成用例将其他行的 `DisplayId` 或 `Pid` 设为同一个数值，验证焦点选择使用正确列。
- `ForegroundWindow` 返回单个焦点窗口的包名、窗口名、窗口 ID 和 ability。显示器选择、分屏焦点切换及采集路由属于真机验收项目。
- `PowerTelemetry` 使用首个匹配的电源状态行。折叠屏样本先出现 `Display[0] State: OFF`，因此返回 `off`，交换两行后返回 `on`。该聚合状态的解释需要结合设备实际显示器状态。
- 每份 `dumpLayout` 按单棵树独立解析。夹具外围的 `display_id` 是测试描述元数据，解析器输入为该记录的 `dump` 字段。内外屏各自生成尺寸、哈希和 `@eN` 引用。
- 切屏、旋转或折叠后重新观察，并基于新的快照选择引用。截图、布局和输入坐标是否对应同一显示器须逐项验收。

## 离线回归

```bash
.venv/bin/python -m pytest tests/test_compatibility.py tests/test_ui.py tests/test_device.py
.venv/bin/python -m pytest tests/test_distribution.py
```

兼容性测试同时验证确定性快照排序、布尔值与文本后备、隐藏/屏外/零面积控件剪枝、未知遥测保留 `null` 或 `unknown`，以及缺失焦点窗口时保留未识别状态。新增语法变体前先核对当前解析器入口，并为实际缺陷提供独立回归。

## 补充真实脱敏样本

1. 由维护者明确设备与测试范围，记录采集日期、提交 SHA、系统完整版本/API、设备形态、宿主机/Python、`hdc` 与 `uitest` 版本。
2. 先按发布清单执行只读检查。原始输出、截图和设备映射保存在受限本地目录。
3. 对候选样本去除串号、账号、手机号、凭据、私有域名、业务文本及真实截图。用 `com.example.*` 等示例值替换业务标识，保留触发解析行为所需的字段形状、列序与单位。
4. 为新样本附上脱敏方法、人工审查记录与适用范围。来源写为真实采集并脱敏，手工补出的字段单独标注为合成。
5. 增加独立样本文件与预期结果，运行全部离线回归，再把对应矩阵行更新为该提交和该设备组合的验收结果。

## 真机记录模板

| 记录项 | 待填写内容 |
| --- | --- |
| 候选版本 / 提交 SHA | 版本与完整提交标识 |
| 日期 / 执行人 | 日期、时区与维护者 |
| 宿主机 | 系统、架构、Python、`hdc` 版本 |
| 设备环境 | 私有设备别名、系统完整版本、API、`uitest` 版本、形态 |
| 显示状态 | 内外屏、横竖屏、分屏、焦点窗口及实际显示器映射 |
| 只读结果 | 枚举、诊断、布局、截图、遥测、临时文件清理 |
| 授权副作用 | 授权人、时间、动作、参数范围、幂等键管理与实际结果 |
| 故障恢复 | 设备断连、网络中断、回执补发、`unknown` 只读对账 |
| 退出与重启 | SIGINT/SIGTERM、退出耗时、锁释放、持久状态恢复 |
| 判定与证据 | 通过 / 失败 / 待验收，脱敏日志位置、问题编号 |

完整操作顺序与协议验收项目见 [发布检查清单](release-checklist.md)。
