# 插件目录与维护指南

`moyu-harmony-pilot` 主仓库只维护动作契约、通用示例与兼容性测试工具；具体的第三方业务插件由各自维护者在独立仓库中开发、打包与发布。本文用于汇总官方通用示例与社区插件的兼容信息，方便宿主机运维者在安装前完成评审。

---

## 1. 仓库内置与官方通用示例

以下示例均由 MoyuFamily 在本仓库 MIT 协议下维护，全部实现 Action API v1，并在 Pilot 离线 CI（macOS / Linux，Python 3.11 / 3.12）中持续验证：

| 动作 ID | 来源与部署方式 | Pilot 版本要求 | 离线契约覆盖 |
| :--- | :--- | :--- | :--- |
| `example.battery_check` | [独立插件模板](../examples/plugin-template/README.md)，通过 Python 包 entry point 安装 | `>=0.1.0,<0.2` | wheel 安装发现、参数校验、远程白名单、只读设备调用 |
| `example.health_check` | [Bridge 目录示例](../examples/bridge/plugins/health_check.py)，目录 + `actions.json` | 当前检出版本，API v1 | Fake 设备通过原生 Bridge 与本地 HTTP Broker 端到端联调 |
| `example.ui_summary` | [本地目录示例](../examples/actions/ui_summary.py)，目录 + `actions.json` | 当前检出版本，API v1 | Fake 设备发现、本地执行与有界紧凑结果 |

> 注：独立插件模板的独立 CI 会固定测试指定的 Pilot 提交版本，同时在包依赖中声明目标兼容区间；真机与目标 App 的业务验收由各插件维护者自行记录。

---

## 2. 登记社区插件

欢迎外部开源插件通过提交 PR 将自身条目登记到本目录。登记信息需包含：

- 插件名称、PyPI / 分发包名、提供的动作 ID 列表及简短用途说明；
- 源码仓库地址、安装方式与维护者联系方式；
- 开源协议及推荐安装的发布 Tag 或完整 Commit SHA；
- Action API 版本、支持的 Pilot 版本范围、Python 与宿主机平台要求；
- 副作用分类（`readonly` / `mutating`）以及是否包含 `local_only` 动作；
- 对应各宣称支持 Pilot 版本的离线契约测试结果链接；
- 真机 / 目标应用验收情况（含已测系统版本与已知限制）。

目录评审会核对登记信息是否完整且与插件实际声明一致。由于插件代码运行在 Pilot 进程权限下，宿主机管理员在安装前仍须自行审阅源码与依赖。具体业务插件代码不进入主仓库。

---

## 3. 开发与兼容性验证

1. 从 [独立插件模板](../examples/plugin-template/README.md) 起步，并对照 [Action 插件开发指南](actions.md) 定义契约；
2. 使用模板中的 `python -m tests` 校验已安装的 entry point（任何测试失败、被跳过或空套件都会使校验不通过），并参考模板中的有副作用测试场景补充针对自家业务和脱敏布局 Fixture 的离线测试；
3. 目录式插件保持 `actions.json` 映射清晰并在同目录文档写明依赖要求；包形式插件通过 `moyu_harmony_pilot.actions` entry point 分组注册，并在 `ActionSpec` 与 `pyproject.toml` 中明确声明版本；
4. 扩大支持的 Pilot 版本范围前，请先针对新版本或固定开发提交跑通契约测试，并留存插件版本、Pilot 版本、运行矩阵与测试链接。

---

## 4. 上线、升级、回滚与下线流程

1. **安装部署**：在服务使用的 Python 虚拟环境中安装经审阅的插件版本（或部署固定版本的目录并在 Bridge 配置 `plugin_paths`）。
2. **契约预检**：本地模式运行 `harmony-pilot plugins describe <action_id> --json`；Bridge 模式运行 `harmony-pilot serve --config <bridge.local.json> --check --json`，核对导出的动作 ID、API 版本与参数/指标 Schema。
3. **白名单放行**：将目标动作加入对应设备的 `allowed_actions`，重启服务使新代码与配置生效。
4. **平滑升级**：等待在途任务结束后再切换版本，**务必保留共享的 `state_dir`**，并确认新版本组合已通过离线契约测试。若修改了动作 `version` 或参数结构，复用旧幂等键会触发校验失败，请先核对处理遗留的 `unknown` 记录。
5. **版本回滚**：切回上一版插件包/目录及对应配置，保留账本目录，重新运行 `--check` 后重启服务。
6. **插件下线**：先从远程白名单移除授权，等待在途任务收尾并重启服务，再卸载插件包或移出发现目录；按站点保留策略留存历史账本，并及时更新本目录中的维护状态或替代方案。

> 建议：源码部署用固定 Tag 或 Commit SHA 锁定版本，包部署建议固定版本号与 wheel 校验和。主仓库的包元数据与测试始终保持与外部社区插件解耦。
