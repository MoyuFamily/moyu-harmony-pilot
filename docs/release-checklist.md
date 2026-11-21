# 发布检查清单

当前候选版本为 `0.2.0rc1`。候选版本的源码、分发包与 CI 绑定同一提交，设备验收记录保留实际测试提交及适用变更范围。维护者完成验收并决定发布版本后，再推送对应 `v<版本>` 标签触发 GitHub Release 工作流。

## 源码与离线检查

- [ ] 核对 `pyproject.toml` 的版本与 `src/moyu_harmony_pilot/__init__.py` 的 `__version__` 一致，记录目标提交 SHA。
- [ ] 审查 `git status` 与 `git diff`，保留协作中的改动，确认发布提交中的文件范围与内容。
- [ ] 检查默认 `dependencies = []`、单向依赖 DAG、CLI JSON 信封、语义退出码及插件自描述。
- [ ] 检查仓库与分发内容中的设备映射、凭据、真实串号、原始布局、截图和私有业务代码，完成敏感信息审查。
- [ ] 完成下列离线检查，并保留实际命令与结果。

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/pytest
```

mypy 对全部 `src` 启用 `strict`、`check_untyped_defs`、不可达代码警告和未跟踪导入的 `Any` 检查。严格模式要求完整函数签名、泛型参数和显式公开导出。六个可选外部依赖 `Foundation`、`Quartz`、`Vision`、`objc`、`rapidocr_onnxruntime`、`yaml` 使用精确模块级 `ignore_missing_imports`，用于缺失或未携带类型信息的外部导入。项目源码的类型错误会使检查失败。

- [ ] macOS / Linux × Python 3.11 / 3.12 四个 CI 组合全部通过，包括分发测试和类型检查。保存 Actions 运行地址及实际工具版本。
- [ ] 检查 wheel 与 sdist 中的 `py.typed`、版本、许可证、Python 最低版本、插件模板和兼容性夹具。`tests/test_distribution.py` 会离线安装 wheel，验证 CLI 和下游类型发现，并拒绝错误类型调用。
- [ ] 使用新输出目录生成候选包并检查元数据。以下命令中的路径应指向本次验收的空目录。

```bash
PIP_NO_INDEX=1 .venv/bin/python -m build --no-isolation --outdir /tmp/harmony-pilot-release-candidate
.venv/bin/python -m twine check --strict /tmp/harmony-pilot-release-candidate/*
```

开发依赖安装属于联网准备阶段。构建通过默认的 sdist → wheel 顺序验证源码包完整性，`--no-isolation` 复用已安装的构建依赖。工具版本与依赖安装范围一并记录，字节级可复现性另行检验。

## 设备准备与只读验收

- [ ] 在 [兼容性矩阵](compatibility.md) 填写本次设备组合。新增 5.x、平板和折叠屏样本当前均为 `SYNTHETIC`，对应真机状态为待验收。
- [ ] 确认设备调试授权、私有别名配置与本地状态目录权限，记录系统/API、宿主机、Python、`hdc`、`uitest` 版本。
- [ ] 先执行 `devices`、`doctor --deep`、`dump-ui`、`screenshot`，验证版本信息、温度、电量、电源、窗口焦点、布局尺寸和截图尺寸。
- [ ] 检查 `--json` 的 stdout 可单独解析，日志进入 stderr。检查快照排序、引用确定性、敏感内容脱敏，以及截图和布局的本地访问权限。
- [ ] 在每次成功、失败、超时和文件传输失败后，核对本次操作创建的 `/data/local/tmp/` 受管文件已清理。对遗留文件记录所属操作并执行定向清理。

以下示例的 `acceptance-device` 为私有配置中的测试别名，输出保存在本机受限状态目录。

```bash
harmony-pilot devices --json
harmony-pilot doctor --device acceptance-device --deep --json
harmony-pilot dump-ui --device acceptance-device --json
harmony-pilot screenshot --device acceptance-device --out .state/acceptance.jpeg --json
```

- [ ] 对平板和折叠屏分别记录横竖屏、内外屏、分屏与焦点变化。每次形态切换后重新观察，验证截图、布局、输入坐标对应的显示器，并记录多显示器聚合电源状态的解释。
- [ ] 核对退出码 `0` 成功、`1` 执行失败、`2` 参数/配置错误、`3` 离线/未授权、`4` 人工介入、`5` 过温、`6` 锁冲突。异常和保护路径优先用离线 Fake/Mock 验证。

## 明确授权后的副作用验收

真机写操作须符合人类已明确授权的范围。验收记录应注明测试设备、测试应用、动作和参数，并依据实际需要记录时间与恢复步骤。

- [ ] 在自有测试应用或已授权的通用测试环境执行最小可逆操作，记录授权与前后只读观察结果。业务插件继续在仓外维护。
- [ ] 检查本地设备锁、参数 Schema、设备/远程白名单、时间与步数预算、过温保护和敏感屏接管。
- [ ] 对 `IdempotentAction` 验证 `inspect → submitting 持久化 → submit → confirm` 顺序。相同幂等键再次提交时验证提交次数仍为一次。
- [ ] 分别覆盖提交前失败、提交后超时与确认不确定。结果不确定时保留 `unknown`，通过账本查询、只读对账或人工核实恢复。
- [ ] 遇到 `human_required`，验证机器可读错误和接管提示，交还人类后重新观察。验证码、密码和支付确认按仓库护栏处理。

## 协议与运行侧契约验收

- [ ] 对照 [Bridge 协议](bridge-protocol.md) 核对文档版本、协议头 `X-Harmony-Pilot-Protocol: 1`、请求字段、UTC 时间戳和终态枚举，记录协议冻结审查结果。
- [ ] 对外部 Broker 运行 `serve --check --json`，检查离线配置、显式白名单、插件参数/结果 Schema 与最低版本声明。运行 `serve --probe --json` 前确认心跳目的地及凭据。
- [ ] 检查设备枚举与能力发现、`plugins describe`、CLI `schema`、默认 MCP Safe Mode 的可调用动作集合与错误语义。
- [ ] 验证 `heartbeat`、`claim`、续租和回执链路，以及未知动作、错误参数、过期任务、租约失效、重复任务/设备和越权任务的拒绝路径。
- [ ] 用离线 Broker/Fake 覆盖 HTTP 鉴权失败、协议错误、重定向拒绝、`409`/`410` 终态处理及响应大小边界。核对脱敏回执、受限 metrics 和凭据日志处理。
- [ ] 用独立插件模板执行发现与契约测试，验证预检失败、`human_required`、`unknown`、重复请求和跨本地/Bridge 防重语义。

```bash
.venv/bin/python -m pytest tests/test_bridge.py tests/test_bridge_parallel.py tests/test_mcp.py
.venv/bin/python -m pytest tests/test_actions.py tests/test_actions_cli.py tests/test_distribution.py
```

## 设备断连与网络恢复

- [ ] 人类授权故障注入窗口后，先在空闲期断开设备连接，检查连接状态、退出码和可认领设备列表，重新连接并授权后重新采集只读状态。
- [ ] 对正在执行的动作注入设备断连，区分提交前失败与提交后结果不确定，确认后者进入 `unknown` 并进入只读对账路径。
- [ ] 暂停宿主机到测试 Broker 的网络连接，检查有界超时、退避重连、租约取消与本地回执落盘。恢复网络后核对回执补发内容与原记录一致。
- [ ] 重启 worker 并保留 `state_dir`，验证动作账本和 Outbox 恢复、遗留任务状态以及同一幂等键的防重保护。

## 服务停止、关机与重启

- [ ] 在空闲、`inspect` 阶段和已持久化 `submitting` 的阶段分别发送 SIGINT / SIGTERM，记录停止认领、取消检查点和原预算内的提交/确认收尾。
- [ ] 验证最终回执落盘与发送尝试、临时文件清理、子进程/线程收尾和设备/worker 锁释放，记录实际退出耗时。
- [ ] 回执发送失败后重新启动，检查补发和去重。对异常退出遗留的 `running` / `submitting` 状态核对 `unknown` 恢复与人工对账路径。
- [ ] 核对 systemd / launchd 的停止超时足以容纳有界收尾。授权的宿主机关机演练先停止 worker，开机后沿用状态目录并重新观察设备。
- [ ] 设备电源关机演练须有单独授权，由人类控制关机与开机，验证离线识别、重连授权、只读复验及幂等账本连续性。

服务参数与恢复行为见 [Bridge 运行指南](bridge-worker.md)。

## 标签工作流与发布后检查

`release.yml` 在维护者推送 `v*` 标签后复用四组合 CI。全部检查通过后，构建任务逐项校验标签、项目版本、包内版本与两种分发格式的元数据，生成 wheel、sdist 和 `SHA256SUMS`。`dev`、`a`、`b`、`rc` 版本标记为 GitHub 预发布。

构建任务使用只读仓库权限。发布任务获得 `contents: write`，下载本次运行的构建产物并复验校验和。它创建或续传草稿，下载已上传附件再次校验，最后发布草稿。`--verify-tag` 要求远端标签已存在。已发布版本的重跑执行附件一致性检查，出现差异时失败并保留现场。

- [ ] 发布前确认标签精确等于 `v` 加版本字符串。当前 `0.2.0rc1` 对应 `v0.2.0rc1`，正式版本 `0.2.0` 对应 `v0.2.0`。
- [ ] 检查候选提交四组合 CI、协议审查和适用设备验收记录。未完成的设备组合继续标为待验收。
- [ ] 发布后下载 wheel、sdist 与 `SHA256SUMS`，在下载目录复验哈希，并核对 Release 的预发布标记、版本、提交与记录。
- [ ] 上传中断留下的草稿可重跑发布任务续传。完整重跑可能因构建时间或依赖版本产生不同字节，已发布附件存在差异时应调查原产物及构建记录。

```bash
# Linux
sha256sum --check SHA256SUMS
# macOS
shasum -a 256 --check SHA256SUMS
```

## 官方工具语义参考

- mypy 的 `strict` 检查集合可能随版本变化，验收记录应包含实际 mypy 版本。[mypy 配置](https://mypy.readthedocs.io/en/stable/config_file.html)
- 类型标记通过包数据进入分发包，并覆盖包内子模块。[Python 类型信息分发规范](https://typing.python.org/en/latest/spec/distributing.html)
- `build` 默认先构建 sdist，再从 sdist 构建 wheel。[PyPA build 用法](https://build.pypa.io/en/stable/how-to/basic-usage.html)
- CI 与发布复用同一工作流入口。[GitHub 可复用工作流](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
- Actions 运行时与输入参数按官方说明维护。[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[upload-artifact](https://github.com/actions/upload-artifact)、[download-artifact](https://github.com/actions/download-artifact)
- 发布草稿、附件与远端标签检查使用 GitHub CLI。[gh release create](https://cli.github.com/manual/gh_release_create)
