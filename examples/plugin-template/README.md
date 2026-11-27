# Harmony Pilot 独立插件工程模板

本目录是一个可直接复制开新仓的独立插件模板（复制时请一并包含 `.github/`）。模板打包了两个动作，并通过真实安装态的 entry point 运行离线契约测试。本模板由 MoyuFamily 按 MIT 协议维护。

在已安装 Pilot 的环境中，可运行 `harmony-pilot plugins init ../team-pilot --namespace team` 生成工程。生成器同步替换分发包名、Python 模块名和两个动作的命名空间，保留完整测试与 CI。已生成的工程可直接从第 2 节开始安装与验证。

| 动作 | 用途 | 参数 | 成功指标 |
| --- | --- | --- | --- |
| `example.battery_check` | 只读电量检查 | `minimum_battery`，0–100 的整数 | `battery_percent`、`ready` |
| `example.demo_toggle` | 设置合成演示界面的开关状态 | `enabled`，布尔值 | `enabled` |

`example.demo_toggle` 使用 `IdempotentAction` 与 `ctx.interact()`，只匹配合成包名 `com.example.pilotdemo` 下 ID 为 `demo_toggle` 的 `Toggle` 控件。离线测试用两份合成布局表示开关状态，覆盖开启和关闭方向。插件按目标状态决定是否提交，所有点击均在 `submit()` 中执行，`inspect()` 与 `confirm()` 只观测界面。

---

## 1. 兼容性说明

- 动作实现 **Action API v1**。
- 包元数据声明支持 **Python 3.11+** 及 `moyu-harmony-pilot>=0.2.0rc1,<0.3`。
- 两个动作均声明 `min_pilot_version="0.2.0rc1"`。公共测试包、精确点击断言、上下文交互和独立检查指标 Schema 采用这一候选版本基线。
- `requirements-test.txt` 固定 Pilot 测试基线为 `0.2.1`，可由匹配版本的源码或 wheel 提供。
- `.github/workflows/contract.yml` 在 macOS / Linux × Python 3.11 / 3.12 矩阵下运行离线契约测试。

改造成自己的插件后，请以实际跑通的 Pilot 版本与宿主机测试记录为准，及时更新依赖版本范围。

CI 从 `MoyuFamily/moyu-harmony-pilot` 检出 Pilot 源码并安装，使用仓库变量 `PILOT_REF` 指定版本，默认值为 `v0.2.1`。升级测试基线时，同步调整该引用与 `requirements-test.txt`，并确认依赖范围覆盖目标版本。安装步骤会核对固定版本并执行 `pip check`，随后运行离线测试、两个动作的能力发现与参数预检。测试期间由公共 `offline_guard` 拦截网络、DNS 与子进程调用。

---

## 2. 本地安装与契约验证

在复制出来的插件目录中执行以下命令。将 `/path/to/moyu-harmony-pilot` 替换为 `v0.2.1` 检出的 Pilot 源码目录或从 GitHub Release 下载的对应 wheel 路径。测试全程使用临时目录与标准库 fake。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install /path/to/moyu-harmony-pilot
.venv/bin/python -m pip install --no-index --no-deps -r requirements-test.txt
.venv/bin/python -m pip install --no-deps .
.venv/bin/python -m pip check
.venv/bin/python -m tests
.venv/bin/harmony-pilot plugins describe example.battery_check --json
.venv/bin/harmony-pilot plugins describe example.demo_toggle --json
```

如果正在对接本地源码检出的 Pilot 仓库，先安装本地 Pilot，再用 `python -m pip install --no-deps .` 安装当前插件，随后同样运行 `pip check` 与 `python -m tests`。

安装后，还可以通过 `plugins validate` 离线检查两个已安装动作的参数。

```bash
.venv/bin/harmony-pilot plugins validate example.battery_check --params-json '{"minimum_battery":20}' --json
.venv/bin/harmony-pilot plugins validate example.demo_toggle --params-json '{"enabled":true}' --idempotency-key synthetic-demo --json
```

预检会发现并加载可信插件，但不连接设备、不执行动作，也不检查远程白名单；有副作用动作还需传入 `--idempotency-key`。它补充契约测试，不能代替测试。

### 契约套件会检查什么？

`python -m tests` 只要遇到 Pilot 缺失、entry point 未注册、契约失败、测试套件为空或**任意测试被跳过**都会以失败退出。

- 通过真实的已安装 entry point 发现两个插件，经 `ActionRunner` 验证参数校验、最低宿主版本、远程白名单门控与有界结果。
- `FakeHdcTransport` 记录底层调用，配合真实 `HarmonyDevice`、锁、布局解析和热保护路径运行。`tests/support.py` 只维护本模板的合成布局。
- `assert_idempotent_success` 检查真实提交、提交前已持久化的 `submitting` 记录、只读检查与确认，以及原 runner 和重建 runner 的成功去重。
- `assert_unknown_no_replay` 覆盖写入后的超时和确认失败，检查重建 runner 后继续返回 `unknown`。只读 `reconcile` 成功后，再次调用会返回 `already_done`。
- `assert_pre_submit_blocked` 检查过温和敏感界面返回 `device_overheated` 或 `human_required`，并验证写操作、提交阶段与账本预留均未发生。

每个场景使用新的临时 `state_dir` 和 fake。`make_fake_device()` 返回的 `device`、`state`、`transport` 分别用于执行、配置场景和检查调用。`FakeDeviceState.layouts` 接受合成布局 JSON，`click_targets=[FakeClickTarget(控件ID, 包名)]` 按顺序核对当前界面中唯一控件的中心点击，匹配后推进布局。模板额外放置一个无关按钮，误点会令契约失败。

`assert_idempotent_success` 和 `assert_unknown_no_replay` 会检查点击目标，`transport.assert_click_targets_consumed()` 可用于自定义场景的结束核验。`click_targets=[]` 表示预期零次点击，`None` 沿用原有 `advance_layout_on_click` 行为。`timeout_after_mutation` 模拟写入成功后丢失回执，单份布局可模拟点击后状态仍未确认。更多驱动命令可通过 `shell_results` 提供精确响应。

`example.demo_toggle` 的检查指标为 `current_enabled`，最终成功指标为 `enabled`，两个阶段各自声明 Schema。出现 `human_required` 时由人类接管并重新观测。出现 `unknown` 时保留原幂等键和账本，通过只读对账确认结果。

---

## 3. 改造成自有插件（5 步清单）

1. 使用 `plugins init` 生成工程时，分发包名、模块名及动作命名空间已自动同步。手工复制模板时需在 `pyproject.toml`、源码、测试及 CI 中同步这些名称。继续自定义动作 ID 时，也需一并更新 entry point 与测试。
2. 在 `ActionSpec` 中声明参数、检查指标、最终指标 Schema、超时和副作用级别，保持 `api_version=1` 和实际所需的 `min_pilot_version`，用 `version` 标识业务行为版本。
3. 使用公共 `moyu_harmony_pilot.testing` 工具构建离线 Fixture，在 `tests/support.py` 中维护插件特定的合成或脱敏布局。模块导入与构造函数保持无设备及网络 I/O。
4. 将 `DemoToggle` 改造为自有 `IdempotentAction`，通过已安装 entry point 测试。所有产生副作用的导航和提交步骤放在账本预留之后的 `submit()` 中，交互通过 `ctx.interact()` 共享锁、预算和阶段约束。上下文仅在 runner 的当前执行阶段内使用。
5. 发版时同步更新 Pilot 测试基线、`PILOT_REF`、兼容区间、协议与维护者信息，宿主机升级时保留 `state_dir` 账本目录。

---

## 4. 接入远程 Bridge 与本地 MCP

将插件安装到 Pilot 服务使用的同一个 Python 环境中，在私有 Bridge 配置里把动作 ID 加入目标设备的 `allowed_actions`，然后运行预检并重启服务。

```bash
harmony-pilot serve --config /private/bridge.local.json --check --json
```

`--check` 会导出动作的 API 版本、参数 Schema 与指标 Schema，可直接交给 Broker 侧对接。若要在本地 MCP 服务中启用，只需在启动时加上 `--allow-action <你的动作ID>`。

完整的插件开发与执行规范请参阅主仓库的 `docs/actions.md`。
