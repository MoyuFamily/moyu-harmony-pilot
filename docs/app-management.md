# 应用管理、交互与运行诊断

这些能力基于官方 `hdc`、`bm`、`aa`、`uitest` 工具链。核心依赖保持为 Python 标准库。示例中的 `harmony-01` 是设备别名。

## 应用查询与部署

```bash
harmony-pilot apps list --device harmony-01 --json
harmony-pilot apps info --device harmony-01 --bundle com.example.demo --json
harmony-pilot apps install --device harmony-01 --hap ./entry.hap --idempotency-key demo-v1 --json
harmony-pilot apps uninstall --device harmony-01 --bundle com.example.demo --idempotency-key remove-v1 --json
```

重复传入 `--hap` 可安装同一应用、同一版本的多个 HAP。覆盖更新使用同一安装命令，每次独立部署使用新的幂等键。包内 `module.json` 必须包含 `app.bundleName` 和整数 `app.versionCode`。系统负责检查签名、调试权限和安装策略，Pilot 验证工具成功回执以及安装后的应用版本。

```bash
harmony-pilot apps download --url https://packages.example.org/entry.hap --out ./entry.hap --json
harmony-pilot apps install --device harmony-01 --url https://packages.example.org/entry.hap --idempotency-key deploy-v1 --json
```

支持 HTTP(S) 下载，`--sha256` 可提供预期文件摘要。下载有大小上限和超时检查，通过校验后原子替换目标文件。URL 安装缓存位于状态目录的 `packages/` 下。安装会将文件复制到私有临时目录，再次验证摘要后提交。

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
harmony-pilot ledger prune --older-than-days 90 --json
```

`resolve` 记录成功核验和备注，并继续保留幂等键。`prune` 只清理已成功结案的历史，保留 `submitting` 和 `unknown`。裁剪后的历史幂等键将失去防重记录，应按业务防重期限设置保留天数。账本 v1 会在事务中自动迁移，保留原有记录。

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

## 验证范围

新增离线测试覆盖 HAP 安装与卸载、防重复提交、安装已生效但回执超时、相同版本更新不确定、下载校验和原子替换、旧账本迁移、中文及特殊字符参数转义、等待预算和过滤透传、并发子进程重叠执行、单子进程崩溃与超时、HTML 转义和文件权限。

2026 年 11 月 20 日本次只读设备枚举触发 `hdc_timeout`。新安装链路、中文实际输入效果、HarmonyOS 5.x / 6.x 多版本及断线重连真机验收仍待完成。解析器测试采用合成数据，已有 HarmonyOS 6.1 脱敏布局继续参与回归。
