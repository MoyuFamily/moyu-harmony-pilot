# 系统短信与验证码读取

内部插件 `sms.open` 负责短信列表导航，`sms.read` 通过官方 `uitest dumpLayout` 读取当前可见的会话摘要或正文，返回发送方、原始时间标签和验证码候选。可通过 CLI、SSH、显式授权的 MCP 入口使用，也可由外部插件通过 `ctx.call(...)` 组合调用。

## 本机使用

`phone` 是已配置的设备别名。只有一台在线设备时，CLI 可省略 `--device`。先打开系统短信列表，再读取当前可见消息。

```bash
harmony-pilot sms open --device phone --idempotency-key sms-session-001 --json
harmony-pilot sms read --device phone --limit 10 --json
```

`sms open` 会唤醒屏幕、启动 `com.ohos.mms`，在已识别的会话详情页点击返回列表。它使用 `ActionRunner` 的设备锁、温度保护和 SQLite 防重账本，`plugins describe sms.open` 提供参数契约。每次独立的导航请求使用新的幂等键，同一请求重试沿用原键。已完成的键返回历史成功状态，随后应通过 `sms read` 获取新观测。

读取指定发送方使用精确匹配，以界面实际显示的号码或名称为准。

```bash
harmony-pilot sms read --device phone --sender 示例服务 --json
```

等待新短信时，先启动等待，再在需要登录的网站或应用请求发送验证码。

```bash
harmony-pilot sms read --device phone --wait-seconds 20 --timeout 30 --json
```

等待会记录调用开始时的可见消息，并轮询当前页面。新增内容返回 `reason=new_message`，等待到期返回 `reason=timeout` 和空 `messages`。普通读取返回 `reason=snapshot`。等待模式只识别本次观察范围内新增的内容，已有验证码和相对时间标签变化会被排除。

`--wait-seconds` 允许 0–300 秒，必须小于 `--timeout`，后者包含设备查询和布局采集时间。屏幕息屏、页面切换或锁定后，按返回的恢复提示重新打开列表并开始一次新的观察。手机的自动息屏时间会影响连续等待时长。

## 在外部插件中读取短信

下面的 `ctx.call` 示例要求 Pilot `>=0.2.1`，采用此接口的外部插件声明 `min_pilot_version="0.2.1"`，并在自身包依赖中设置相应下限。通过 `plugins describe sms.open` 与 `plugins describe sms.read` 查看参数、`result_schema` 和 `private_content`。示例是外部 `IdempotentAction` 的 `prepare` 片段，父插件的参数 Schema 需声明必填字符串 `request_id`，同一业务请求的重试沿用该标识。

```python
from hashlib import sha256

from moyu_harmony_pilot.actions import Inspection


def prepare(self, ctx, inspection, params):
    open_key = sha256(
        f"{params['request_id']}:sms.open".encode("utf-8")
    ).hexdigest()
    ctx.call("sms.open", {}, idempotency_key=open_key)
    sms_data = ctx.call("sms.read", {"limit": 1}).data
    return Inspection(state={"has_visible_message": bool(sms_data["messages"])})
```

此片段只把“是否有可见消息”交给后续阶段，短信正文保留在本次调用结果中。接入实际业务时，在准备阶段按流程切回目标 App 并重新观察，再进入业务提交。正文、验证码等私有内容须限制在当前调用内，日志、指标和账本仅记录脱敏状态。遇到验证码输入表单、密码、权限确认等敏感交互时交由人工处理。

`sms.open` 是有副作用的子动作，仅能在父动作 `prepare` 或 `submit` 中调用。真正进入 reserve 提交阶段时，先递归预留父／祖先请求，再预留短信子请求并提交，各自写入账本。同一业务请求的同一导航步骤沿用 `open_key`，不同步骤使用各自稳定键。历史 `already_done` 表示该步骤曾完成，此时沿用已有记录，当前界面以重新观测为准。

提前预留保护已发生的子提交。后续父失败、取消或崩溃，以及子动作 `unknown`，都会使父请求保留 `unknown`，阻止整请求重放。先用子动作 `sms.open`、原 `open_key` 和参数 `{}` 执行 `ledger reconcile`，再核验父组合动作。需要人工结案时沿用核验后 `ledger resolve` 的流程。

## 在公司远程读取

运行 Pilot 的主机需要连接手机，并已完成设备调试授权。下面假定公司电脑已经能通过 SSH 主机别名 `pilot-host` 登录该主机，远端可以执行 `harmony-pilot`。设备别名配置和状态目录由远端主机管理。

```bash
ssh -T pilot-host 'harmony-pilot sms open --device phone --idempotency-key sms-session-002 --json'
ssh -T pilot-host 'harmony-pilot sms read --device phone --json'
```

也可以通过同一 SSH 通道执行等待命令。短信正文和验证码直接随本次调用结果返回，命令退出后结束观察。

```bash
ssh -T pilot-host 'harmony-pilot sms read --device phone --wait-seconds 20 --timeout 30 --json'
```

## MCP 调用

在连接手机的主机启动 stdio 服务时，显式启用短信读取工具。需要远程打开列表时，同时授权 `sms.open`。

```bash
harmony-pilot-mcp --config devices.local.json --enable-sms \
  --allow-action sms.open --allow-action sms.read
```

客户端按顺序调用以下两个工具。

```json
{"tool":"run_action","arguments":{"alias":"phone","action_id":"sms.open","params":{},"idempotency_key":"sms-session-003"}}
{"tool":"sms_read","arguments":{"alias":"phone","limit":10}}
```

`sms_read` 是内部插件 `sms.read` 的私有 MCP 调用入口，使用与 CLI 相同的结果结构，通过 `--enable-sms` 显式开启，工具元数据标记 `private_content=true`。MCP 客户端可用 SSH 启动远端 stdio 进程，调用结果将包含短信原文，应按个人敏感数据处理客户端历史和日志。

若父动作 `team.message_review` 在内部调用上述两个短信插件，启动时还需分别放行父动作、`sms.open` 和 `sms.read`，并启用短信私有访问。

```bash
harmony-pilot-mcp --config devices.local.json --enable-sms \
  --allow-action team.message_review \
  --allow-action sms.open --allow-action sms.read
```

每次子调用单独检查授权。Pull Bridge 禁止调用 `sms.read` 这类私有正文插件，包括由外部父插件发起的子调用。

## 结果约定

短信 CLI 与专用 MCP 工具的成功结果位于 JSON 信封的 `data` 中。插件内调用返回 `ActionOutcome`，以下字段位于其 `.data`，结构由 `result_schema` 描述。默认 `to_dict()` 只输出安全回执，本地 CLI 或显式私有 MCP 入口通过 `to_dict(include_data=True)` 交付直接结果。

| 字段 | 含义 |
| --- | --- |
| `page` | `conversations` 会话列表，或 `thread` 会话详情 |
| `scope` | `visible`，覆盖当前可见内容 |
| `messages[].sender` | 页面显示的发送方，无法确定时为 `null` |
| `messages[].body` | UI 提供的正文或摘要，保留原文 |
| `messages[].time_text` | 页面显示的原始时间标签，缺失时为 `null` |
| `messages[].verification_codes` | 有明确验证码语境的 4–8 位数字候选，保留前导零 |
| `messages[].preview` | 列表摘要为 `true`，详情正文为 `false` |
| `messages[].body_truncated` | 本次长度裁剪或可见省略号 |
| `messages[].direction` / `unread` | 当前分别保留为 `unknown` / `null` |
| `truncated` | 条数或字符预算导致结果裁剪 |
| `observed_at` | 本次观察的 UTC 时间 |
| `reason` | `snapshot`、`new_message` 或 `timeout` |

最多返回 50 条，单条正文上限 4096 字符，发送方、时间和正文的合计预算为 16384 字符。列表按屏幕顺序取前若干条，详情取当前可见消息的最后若干条。摘要可能由短信应用自身省略，`preview=true` 表明需要结合详情判断完整性。

验证码候选由明确的中文验证码或英文 OTP、verification code 等语境提取。多个候选保留原文顺序，电话号码、日期和时长按上下文排除。复杂表述、字母数字混合或分段验证码仍可从 `body` 人工读取，候选有效期由短信原文和使用方判断。

## 隐私、异常与验证范围

正文和验证码只保留在内部插件 `sms.read` 的私有直接结果中，供本次外部插件调用、本地 CLI 或显式私有 MCP 入口使用。读取过程在内存中处理结果，布局采集临时文件随调用清理。Action 账本和安全回执只记录脱敏状态，Pull Bridge 对私有正文插件执行拒绝门控。所有真机验收输出采用条数和字段长度，公开测试使用合成短信。

系统短信的已识别正文节点采用内容角色判断，普通验证码短信可以读取。密码、验证码输入表单、权限确认及其他敏感交互继续触发人工接管。

| 返回情况 | 恢复方式 |
| --- | --- |
| `human_required` | 打开短信应用，必要时在设备上解锁或完成权限确认 |
| `sms_layout_unsupported` | 回到支持的列表或纯文本会话页，并采集脱敏样本验证新版本 |
| `sms_view_changed` | 页面或焦点变化后重新观察，等待模式重新建立基线 |
| `unknown` | 先按原动作 ID、幂等键与参数只读对账，组合调用再核验父动作，人工结案沿用 `ledger resolve` 流程 |
| `device_busy` | 等待当前设备任务释放锁 |

2026 年 11 月 25 日已在真机 `com.ohos.mms` 6.1.20.314 验证列表读取、进入会话后的正文读取、返回列表和等待到期。验收还覆盖前台窗口仅返回窗口名时，使用焦点窗口 ID 与布局包名交叉确认归属。导航未确认记录经只读对账成功结案，结束时设备锁已释放、受管临时文件为零。其他短信应用版本需要对应样本验证。

读取范围取决于当前可见页面。等待期间应保持同一页面，完全相同的消息摘要且可见重复条数相同的情况可能无法区分。真实新短信到达和不同系统版本仍需相应环境验收。
