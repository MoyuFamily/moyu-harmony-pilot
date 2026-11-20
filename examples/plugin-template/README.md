# Harmony Pilot 独立插件工程模板

本目录是一个可直接复制开新仓的独立插件模板（复制时请一并包含 `.github/`）。模板内置了一个打包好的只读电量检查动作（`example.battery_check`）、基于真实安装态的 entry point 契约测试，以及仅供测试演示的有副作用模拟用例。本模板由 MoyuFamily 按 MIT 协议维护。

---

## 1. 兼容性说明

- 动作实现 **Action API v1**；
- 包元数据声明支持 **Python 3.11+** 及 `moyu-harmony-pilot>=0.1.0.dev1,<0.2`；
- `requirements-test.txt` 固定了 CI 审阅通过的 Pilot 开发版本；
- `.github/workflows/contract.yml` 在 macOS / Linux × Python 3.11 / 3.12 矩阵下运行离线契约测试。

改造成自己的插件后，请以实际跑通的 Pilot 版本与宿主机测试记录为准，及时更新依赖版本范围。

---

## 2. 本地安装与契约验证

在复制出来的插件目录中执行以下命令（测试全程使用临时目录与标准库 Mock 设备，无需连接真机）：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/python -m tests
.venv/bin/harmony-pilot plugins describe example.battery_check --json
```

如果正在对接本地源码检出的 Pilot 仓库，先安装本地 Pilot，再用 `python -m pip install --no-deps .` 安装当前插件，随后同样运行 `pip check` 与 `python -m tests`。

### 契约套件会检查什么？

`python -m tests` 只要遇到 SDK 缺失、entry point 未注册、契约失败、测试套件为空或**任意测试被跳过**都会以失败退出：
- 通过真实的已安装 entry point 发现插件，经 `ActionRunner` 验证参数校验、远程白名单门控与有界结果；
- Mock 设备会记录每次底层调用，确保只读动作不产生意外交互；
- 有副作用测试示例会验证只读 `inspect`、提交前持久化预留、确认后的幂等去重，以及跨重启的 `unknown` 保护。

---

## 3. 改造成自有插件（5 步清单）

1. **更换命名空间**：在 `pyproject.toml`、源码与测试中重命名分发包名、Python 模块名和 `example.battery_check` 动作 ID，使用你自己的命名空间。
2. **声明动作契约**：在 `ActionSpec` 中写清参数 Schema、返回指标 Schema、超时和副作用级别；保持 `api_version=1` 显式声明，用 `version` 标识业务行为版本。
3. **完善离线 Fixture**：按插件实际调用的驱动方法扩展 `tests/support.py` 中的 Mock 设备，并补充脱敏布局 Fixture。务必保证模块导入与构造函数中不做任何设备/网络 I/O。
4. **实现有副作用动作（如需要）**：继承 `IdempotentAction`，将 `tests/test_mutating_contract.py` 改造为覆盖你自己的已安装动作；所有会产生副作用的导航与提交步骤都要放在账本预留之后的 `submit()` 中。
5. **维护发布信息**：每次发版同步更新固定的 Pilot 测试版本、兼容区间、协议与维护者信息；宿主机升级时注意保留 `state_dir` 账本目录。

---

## 4. 接入远程 Bridge 与本地 MCP

将插件安装到 Pilot 服务使用的同一个 Python 环境中，在私有 Bridge 配置里把动作 ID 加入目标设备的 `allowed_actions`，然后运行预检并重启服务：

```bash
harmony-pilot serve --config /private/bridge.local.json --check --json
```

`--check` 会导出动作的 API 版本、参数 Schema 与指标 Schema，可直接交给 Broker 侧对接。若要在本地 MCP 服务中启用，只需在启动时加上 `--allow-action <你的动作ID>`。

完整的插件开发与执行规范请参阅主仓库的 `docs/actions.md`。
