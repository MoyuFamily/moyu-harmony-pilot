# 安全策略（Security Policy）

## 漏洞与问题反馈

在公开的 GitHub Issue 或 Pull Request 中，**请勿贴出**以下任何信息：

- 线上有效的 Bearer Token 或各类认证凭据
- 设备真实硬件串号（`hdc list targets` 的原始输出）
- 包含账号、手机号、余额、通知或个人信息的屏幕截图与原始 UI 布局 JSON
- 私有服务域名、内网地址或业务侧私有插件代码

如果排查问题确需提供敏感线索，请先通过私密渠道联系仓库维护者，并只提供最小化、已脱敏的复现信息。

---

## 敏感数据边界（仅限本机留存）

以下文件与数据均视为敏感资产，严禁通过远程 Bridge 协议上报，也严禁提交到 Git 仓库：

- Bridge / API 认证凭据（如 `HARMONY_PILOT_BRIDGE_TOKEN`、`token_file`）
- 鸿蒙设备真实硬件串号
- 屏幕截图（`snapshot_display` / `uitest screenCap`）与界面控件树（`uitest dumpLayout`）
- 本地配置文件与运行状态目录（`devices.local.yaml`、`bridge.local.json`、`.env`、`.state/`、`diagnostics/`）
- 第三方或私有业务插件及其运行凭据

> 提示：截图与布局文件很容易暴露账号名、手机号、余额、短信或个人隐私。本地诊断目录必须保持 `0700`、诊断文件保持 `0600`，且只在宿主机本机使用。

---

## 远程执行安全边界（Fail-Closed）

当 `moyu-harmony-pilot` 以远程 Pull Bridge 模式运行时，强制遵守“默认拒绝”原则：

- **显式白名单**：远端 Broker 只能触发本地配置中显式加入 `allowed_actions` 白名单的动作 ID，未列出的动作一律拒绝。
- **禁止危险透传**：远程协议绝不开放任意 `hdc shell`、任意文件传输或未受控的裸坐标点击。
- **别名隔离**：对外暴露的始终是管理员自定义的 `device_alias`，不暴露真实硬件串号。
- **禁止不确定重放**：有副作用的动作一旦超时、中断或无法确认结果，状态会落为 `unknown`，系统不会自动重放提交动作，避免重复产生副作用。

---

## 仓库公开与提交前自查

将私有 Fork、分支或仓库转为公开前，请务必审计完整 Git 历史，确认从未误提交过 Token、真实设备串号、私有地址、真机截图或私有业务插件。

本仓库 `.gitignore` 已默认忽略 `.env`、`.env.*`、`.state/`、`artifacts/`、`diagnostics/`、`plugins_local/` 与 `devices.local.*`。
