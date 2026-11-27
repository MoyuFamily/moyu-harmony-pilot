# 本地环境与纯血鸿蒙真机准备指南

> 本文档记录 `moyu-harmony-pilot` 宿主机（macOS Apple Silicon / Linux）环境搭建、官方 `hdc` 安装、纯血鸿蒙（HarmonyOS 5 / 6+，API 12+）真机前置设置，以及端到端只读验证与常见连接排障方法。

---

## 1. 宿主机安装官方 `hdc`（macOS Apple Silicon 示例）

纯血鸿蒙（HarmonyOS 5 / 6+）真机**必须使用新版 `hdc 3.x`**（已实测 `Ver: 3.2.0c` 兼容 HarmonyOS 6.1 / API 23），不可混用早期 OpenHarmony 4.x 的旧 `hdc_std`。

### 方式 A：从华为云官方 OpenHarmony 镜像直接提取 `toolchains`（推荐，全自动无需登录）

华为云镜像站 `https://repo.huaweicloud.com/openharmony/os/` 公开托管了各正式 Release 的 Public SDK 及 `.sha256` 校验文件。以 `6.1-Release`（macOS ARM64）为例，可仅提取 `toolchains/`（包含原生 `hdc` 与其依赖的 `libusb_shared.dylib`）并自动清理临时安装包：

```bash
set -euo pipefail
WORK=$(mktemp -d /tmp/ohos-hdc-install.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

BASE_URL="https://repo.huaweicloud.com/openharmony/os/6.1-Release"
curl -fL --retry 3 "$BASE_URL/L2-SDK-MAC-M1-PUBLIC.tar.gz.sha256" -o "$WORK/sdk.tar.gz.sha256"
curl -fL --retry 3 "$BASE_URL/L2-SDK-MAC-M1-PUBLIC.tar.gz" -o "$WORK/sdk.tar.gz"

# 严格校验 SHA-256
EXPECTED=$(awk '{print $1}' "$WORK/sdk.tar.gz.sha256")
ACTUAL=$(shasum -a 256 "$WORK/sdk.tar.gz" | awk '{print $1}')
test "$EXPECTED" = "$ACTUAL"

# 仅解压 toolchains 到标准目录
tar -xzf "$WORK/sdk.tar.gz" -C "$WORK"
TOOLCHAINS_ZIP=$(find "$WORK" -name "toolchains*.zip" | head -n 1)
DEST="$HOME/Library/OpenHarmony/Sdk/6.1-Release"
rm -rf "$DEST/toolchains"
mkdir -p "$DEST"
unzip -q -o "$TOOLCHAINS_ZIP" -d "$DEST"
chmod +x "$DEST/toolchains/hdc"

# 软链到 ~/.local/bin（保持整个 toolchains 目录完整，确保 @rpath/libusb_shared.dylib 可解析）
mkdir -p "$HOME/.local/bin"
ln -sf "$DEST/toolchains/hdc" "$HOME/.local/bin/hdc"
```

将 `toolchains` 目录与端口配置写入 `~/.zshrc`（如尚未包含 `~/.local/bin` 一并加入）：

```bash
export HDC_SERVER_PORT=7035
export PATH="$HOME/Library/OpenHarmony/Sdk/6.1-Release/toolchains:$HOME/.local/bin:$PATH"
```

生效并检查版本：

```bash
source ~/.zshrc
hdc -v
# 预期输出示例：Ver: 3.2.0c
```

> **提示**：
> - `hdc` 动态依赖同目录下的 `@rpath/libusb_shared.dylib`，请勿只拷贝单个 `hdc` 文件而丢弃同目录动态库；保留完整 `toolchains/` 目录再做软链即可正常解析。
> - 若需在 Linux x86_64 宿主机安装，从同一发布目录选择对应 Linux Public SDK 压缩包并提取 `linux/toolchains` 即可。

### 方式 B：通过华为开发者联盟 Command Line Tools / DevEco Studio 获取（备选）

如果本机已安装 **DevEco Studio** 或从华为开发者联盟下载了官方 **Command Line Tools for HarmonyOS**，直接使用其内置路径即可：

```bash
# DevEco Studio 默认内置路径示例（macOS）
export PATH="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
```

---

## 2. 宿主机 Python 3.11+ 开发环境准备

要求 Python 3.11 或更高版本，推荐使用 `uv` 创建隔离虚拟环境：

```bash
cd moyu-harmony-pilot
uv venv --python python3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 校验 CLI 入口与基础包
.venv/bin/harmony-pilot --version
.venv/bin/python -m pytest -q -x tests/test_cli.py::test_schema_command_offline_and_synced_with_parser
```

（若未安装 `uv`，也可使用 `python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"`。）

日常开发的验证范围、执行次数和时间预算遵循 [AGENTS.md](../AGENTS.md)。完整测试套件由 CI 或明确要求的全量/发布验收任务执行。

---

## 3. 纯血鸿蒙设备前置设置清单

### 3.1 必做项（调试连通）

1. **开启开发者模式**
   - 进入 `设置` → `关于本机`（或 `通用 / 系统` → `关于手机`）。
   - 连续点击 **「HarmonyOS 版本」/「软件版本号」7 次**，按提示输入锁屏密码确认；若系统提示重启，等待重启完成。
2. **开启 USB 调试**
   - 进入 `设置` → `系统和更新`（或 `通用`）→ `开发者选项`。
   - 打开 **「开发者选项」** 总开关与 **「USB 调试」**。
   - 若同页存在 **「仅充电模式下允许调试」**，建议一并打开，防止插线久置后自动切回仅充电模式导致调试断联。
3. **连接数据线并完成主机公钥授权**
   - 使用确认**具备数据传输能力**的 USB-C 数据线直连宿主机（避免纯充电线或不稳定扩展坞）。
   - 首次执行 `hdc list targets` 时，手机会弹出 **「是否允许 USB 调试？」** 授权对话框，勾选 **「始终允许此电脑调试」** 并点击 **允许**。
   - *(备选)* 如需临时无线联调，可在同 Wi-Fi 下开启开发者选项中的「无线调试」，用 `hdc tconn <IP>:<PORT>` 连接；但无线端口在重启或切换网络后会变化，**7×24h 长期挂机强烈首选 USB 有线直连**。

### 3.2 强烈建议项（长期挂机稳定性与硬件安全）

4. **开启电池充电上限保护（防长期插电鼓包）**
   - 进入 `设置` → `电池` → `更多电池设置`，开启 **「智能充电模式」** 或 **「自定义充电上限（推荐 70%–80%）」**。
5. **亮屏与锁屏策略**
   - 开发者选项中可按需开启 **「不锁定屏幕（充电时保持常亮）」**，或保持正常息屏、在任务执行时自动唤醒并在结束后熄屏。
   - 若为**专用闲置挂机设备**，建议关闭锁屏密码（`moyu-harmony-pilot` 遵循安全边界，不存储也不模拟输入锁屏 PIN 码）。
6. **网络与待自动化应用预热**
   - 保持连接稳定的家庭/本地直连网络（若目标应用对代理或异地 IP 敏感，不要在手机端挂载无关代理/VPN）。
   - 提前在手机上手动完成目标应用登录，并处理完首次启动的隐私协议与系统权限弹窗。

---

## 4. 端到端只读连通验证

完成上述准备后，可执行以下**纯只读**命令验证真机通路（所有设备端临时文件均在验证后立即删除，不产生任何界面点击）：

```bash
# 1. 检查连接与授权状态（确认状态为 Connected）
hdc list targets -v

# 2. 获取目标串号（单机环境）
TARGET=$(hdc list targets | tr -d '\r' | head -n 1)

# 3. 读取设备型号、系统版本与 API Level
hdc -t "$TARGET" shell "param get const.product.model; param get const.product.software.version; param get const.ohos.apiversion; param get const.ohos.fullname"

# 4. 检查原生 uitest 版本与能力
hdc -t "$TARGET" shell "uitest --version"

# 5. 验证控件树导出（dumpLayout）并清理临时文件
hdc -t "$TARGET" shell "uitest dumpLayout -p /data/local/tmp/pilot_verify.json && ls -l /data/local/tmp/pilot_verify.json && rm -f /data/local/tmp/pilot_verify.json"

# 6. 验证系统截屏（snapshot_display）并清理临时文件
hdc -t "$TARGET" shell "snapshot_display -f /data/local/tmp/pilot_verify.jpeg && ls -l /data/local/tmp/pilot_verify.jpeg && rm -f /data/local/tmp/pilot_verify.jpeg"

# 7. 验证电池与温度遥测输出
hdc -t "$TARGET" shell "hidumper -s BatteryService -a '-i'"

# 8. 验证电源与亮屏状态输出
hdc -t "$TARGET" shell "hidumper -s PowerManagerService -a '-s'"
```

### 已实测通过的参考基线（HarmonyOS 6.1 / API 23）

- `hdc` 版本：`Ver: 3.2.0c`（macOS ARM64）
- `uitest` 版本：`6.0.2.3`，原生支持 `dumpLayout`（`-p/-i/-a/-b/-w/-m/-d/-e`）、`uiInput`、`screenCap`
- `BatteryService` 关键字段：`capacity`（电量百分比）、`chargingStatus`（`1` 为充电中）、`pluggedType`（`2` 为 USB 供电）、`temperature`（单位为 **`0.1°C`**，例如 `300` 表示 `30.0°C`）
- `PowerManagerService` 关键字段：`Current State: AWAKE`、`ScreenOffTime: Timeout=...ms`

---

## 5. 常见连接排障速查

| 现象 | 常见原因 | 排查与解决方法 |
| :--- | :--- | :--- |
| `hdc list targets` 返回 `[Empty]` | 手机未开启 USB 调试、仅暴露了照片传输接口，或使用了纯充电线 | 在 macOS 执行 `ioreg -r -c IOUSBHostDevice -n "HDC Device" -l` 查看 USB 接口：<br>• 若找不到设备，检查线材是否为数据线并直插主机；<br>• 若只看到 `bInterfaceClass = 6`（PTP/MTP 照片传输），说明手机未真正开启「USB 调试」；开启后切换一次 USB 连接方式或重新拔插即可暴露调试接口。 |
| 状态显示 `Unauthorized`，报错 `[Fail][E000002]` | 首次连接尚未在手机上信任该宿主机公钥 | 亮屏查看手机弹出的「是否允许 USB 调试？」对话框，勾选「始终允许此电脑调试」并点击允许；若未弹窗，尝试 `hdc kill && hdc start -r` 后重插数据线。 |
| `hdc` 服务无响应或端口冲突 | 宿主机残留多个不同版本的 `hdc` 服务进程 | 执行 `hdc kill && hdc start -r` 重启守护进程，并在 `$TMPDIR/hdc.log` 中查看详细握手日志。 |
| 运行 `hdc` 提示找不到 `libusb_shared.dylib` | 只单独复制了 `hdc` 可执行文件，丢失同目录动态库 | 保留完整 `toolchains/` 目录，不要单独搬移 `hdc`；通过软链或将该目录加入 `PATH` 使用。 |
