# KMLink · 无界键鼠

KMLink 是参考 Mouse Without Borders 使用方式开发的 Windows / macOS / Android 键鼠共享程序：一套键盘和鼠标，通过局域网控制另一台电脑。两端都运行 KMLink；本项目采用独立通信协议，不与现有 PowerToys Mouse Without Borders 互通，也未使用微软项目的源代码。

当前为 MVP 预览版。Windows 和 macOS 均可承担控制端或接收端，每次会话连接两台设备，由一个物理键鼠端控制。应用打开后可以发现局域网内其他 KMLink 设备，并直接申请控制或邀请对方控制本机。macOS 代码和打包入口已提供，仍需要 Mac 真机验证权限、输入法和跨屏体验；不能将 Windows 上的测试视为 Mac 验收。

## 下载软件

前往 [最新正式版](https://github.com/gyxde/KMLink/releases/latest)，或直接选择你的平台：

| 平台 | 直接下载 |
| --- | --- |
| Windows 64 位 | [KMLink-windows-x64.zip](https://github.com/gyxde/KMLink/releases/latest/download/KMLink-windows-x64.zip) |
| Mac Apple Silicon（M1/M2/M3/M4 等，macOS 14+） | [KMLink-macos-arm64.zip](https://github.com/gyxde/KMLink/releases/latest/download/KMLink-macos-arm64.zip) |
| Mac Intel（macOS 14+） | [KMLink-macos-x64.zip](https://github.com/gyxde/KMLink/releases/latest/download/KMLink-macos-x64.zip) |
| Android 8.0 及以上 | [KMLink-android.apk](https://github.com/gyxde/KMLink/releases/latest/download/KMLink-android.apk) |
| 完整源码 | [KMLink-source.zip](https://github.com/gyxde/KMLink/releases/latest/download/KMLink-source.zip) |

当前提供的 Mac 预编译包要求 **macOS 14 或以上**。Windows 解压后保留整个 `KMLink` 文件夹，运行 `KMLink.exe`。Mac 解压后将 `KMLink.app` 放入“应用程序”。Mac 包带有临时本机签名，尚无 Apple Developer ID 签名或公证，首次启动可能需要你在系统设置中确认打开。使用前仍需授予辅助功能、输入监控及本地网络权限。

## 分支与发布

- `main`：主分支，维护准备发布的稳定代码。
- `develop`：开发分支，日常功能开发和集成。
- `vX.Y.Z` 标签：正式版本，桌面三平台和 Android 构建与测试全部成功后自动创建 GitHub Release。最新正式版自动出现在上方下载链接。

每次推送 `main` / `develop` 或提交 PR 都会运行跨平台测试和构建。开发分支的临时构建可在 [Actions](https://github.com/gyxde/KMLink/actions/workflows/build-release.yml) 对应运行的 Artifacts 中下载；正式软件统一从 Releases 下载。Android 分支构建提供测试用 debug APK，正式标签构建使用专用签名密钥生成 release APK。

## 已实现的使用范围

- 自动发现局域网中运行 KMLink 的设备，显示设备名称、系统、IP 地址与可连接/使用中状态。
- 在设备列表申请控制对方，或邀请对方控制本机；两端核对六位配对数字，由收到请求的一方明确同意后连接。
- 保留手动输入局域网 IP 与配对密钥的连接方式，默认会话 TCP 端口 **24880**。
- 高熵配对密钥与加密传输；密钥仅分享给希望连接的电脑。
- 键盘、鼠标移动、按键与滚轮转发。
- 在两端的**多显示器桌面**之间切换控制，支持左、右、上、下排列。
- 在控制端按 **F8** 切换控制，Mac 可能需要 **Fn + F8**。
- **Ctrl + Alt + Esc**：控制端紧急返回本机；接收端断开连接。
- 可选交换 Ctrl 与 Command / Win，适配常用跨平台快捷键。
- **两端都主动开启后**同步纯文本剪贴板，每条文本最多 64 KiB。

暂未提供文件传输、开机自启、多台同时控制、UAC/锁屏控制，也未兼容既有 Mouse Without Borders 协议。管理员窗口、游戏、远程桌面及系统安全界面的输入行为可能受系统限制。

## Android 连接

Android 客户端需要 **Android 8.0 或以上**。从 Releases 下载 `KMLink-android.apk` 安装，电脑端也升级到 **KMLink 0.3.0 或以上**。Android 与电脑连接到同一局域网，点击「开始发现设备」后即可在设备列表中互相发现，仍需核对配对数字并由收到请求的一方同意。

支持两种方向：

- **电脑控制 Android**：在 Android 系统设置中手动启用 KMLink 的辅助功能服务。鼠标通过可见的屏幕指针操作点击、滑动和拖动，文本发送到当前可编辑输入框；手机需保持解锁。
- **Android 控制电脑**：保持 KMLink 界面在前台，使用应用内触控板、鼠标按钮和文本发送控件。Android 客户端不截获其他应用中的全局触摸或键盘输入。

连接服务运行时会显示持续通知，可从通知或「停止所有连接」停止连接；「打开辅助功能设置」用于进入系统授权页。复制粘贴仅通过界面上的明确操作发送文本，不在后台读取手机剪贴板。辅助功能受 Android 和目标应用限制；受保护界面、密码框和部分自定义输入控件可能不支持操作。没有屏幕视频传输或锁屏控制。

Android 发布 APK 使用固定的项目签名，便于后续覆盖安装。签名材料不在源码中；请勿将 keystore 或密码提交到 Git。下载页面中的 APK 为直接安装包，不需要在手机上安装 Python。

## 从源码运行

需要 **Python 3.11 或以上版本**和可下载依赖的网络。安装脚本只在项目目录创建 `.venv`，不会更改全局 Python 环境。

### Windows

在项目目录打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

如果 `python` 没有指向实际安装的 Python，可指定路径：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -PythonCommand "C:\Python311\python.exe"
```

`ExecutionPolicy Bypass` 只作用于这次脚本进程，不修改系统执行策略。也可直接使用已安装的 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\.venv\Scripts\python.exe -m kmlink
```

### macOS

将整个项目放到 Mac，使用与 Mac 架构匹配的 Python，在项目目录打开终端：

```bash
bash scripts/setup.sh
bash scripts/run.command
```

指定其他 Python：

```bash
KMLINK_PYTHON=/path/to/python3 bash scripts/setup.sh
```

首次启动时，在 **系统设置 → 隐私与安全性** 中为实际运行程序的主体授予 **辅助功能**、**输入监控** 权限。源码运行可能显示 Terminal、Python 或你使用的终端程序；打包运行则为 KMLink。授权后完全退出并重新启动程序。若系统显示本地网络访问请求，按需要允许 KMLink 访问局域网。程序不会自动修改防火墙规则。

## 从设备列表连接

1. 两台电脑连接到同一局域网，并分别打开 KMLink。在「发现设备」中等待对方出现；列表显示设备名称、系统、IP 地址和当前状态，可以点击「刷新」重新查找。
2. 在将要使用物理键鼠的电脑上，先设置另一台电脑位于本机的方向，以及是否交换 Ctrl 与 Command / Win。连接期间这些选项会锁定。
3. 选中一台「可连接」设备，点击「申请控制选中设备」，表示使用本机键鼠控制对方；点击「邀请对方控制本机」，则表示让对方使用它的键鼠控制本机。
4. 两台电脑会显示六位配对数字。**核对数字完全一致**后，由收到请求的一方点击「数字一致，允许控制」或「数字一致，接受邀请」。默认按钮是「拒绝」。设备名称和 IP 信息用于选择设备，不能代替数字核对。
5. 同意后应用自动准备接收端并建立加密键鼠连接，无需手动复制密钥。请求者可点击「取消请求」；收到请求的一方可以拒绝。未确认的请求约 45 秒后失效，准备连接超时也会取消。
6. 在控制端按 F8（Mac 可能需要 Fn + F8），或将鼠标移过整组显示器最外侧的边缘。切换前松开其他按键和鼠标按钮。

一次只能处理一个请求或连接。设备正在配对或共享时会显示为使用中。关闭应用后设备会从其他电脑列表中移除，通常最多需要约 7 秒。发现只发送设备基础信息，不会广播配对密钥、剪贴板内容或键鼠输入；后续邀请与控制分别通过加密连接交换。

### 多显示器切换

本机的所有显示器作为同一桌面处理。在本机屏幕之间移动不会切换电脑；只有到达所选方向整组显示器最外侧的边缘，才把键鼠交给另一台设备。支持显示器位于主屏左侧、上方等负坐标布局，返回时光标会落在真实屏幕内。连接期间增加、拔除显示器或修改排列后，会停止当前共享并更新布局，重新连接即可。

### 鼠标流畅度与光标显示

从 0.3.2 起，鼠标消息使用低延迟传输，接收端按刷新周期更新光标，保留每次位移、点击和跨屏返回顺序。被控电脑的系统光标隐藏时，会显示不拦截点击的备用指针，停止控制后自动隐藏。两端都更新并重新连接后生效；备用指针仅用于普通桌面，无法覆盖 UAC、锁屏等安全桌面。

## 手动连接两台电脑

1. 两台电脑连接到同一局域网，关闭会隔离客户端的访客 Wi-Fi，分别打开 KMLink。
2. 在被控制的电脑选择「手动接收」，使用自动生成的会话配对密钥，点击「开启接收」。
3. 在插有键盘鼠标的电脑选择「手动连接」，填写接收端的局域网 IPv4 地址、端口和配对密钥；设置接收端相对于控制端的位置，再点击「连接另一台电脑」。位置与按键映射需在连接前设置，修改时先停止共享。
4. 连接成功后，在控制端使用 F8（Mac 可能需要 Fn + F8），或将鼠标移过整组显示器最外侧的边缘。**切换前松开其他按键与鼠标按钮**；不要从屏幕角落触发切换。
5. 需要同步复制粘贴时，在**两台电脑上**勾选「同步文本剪贴板」。剪贴板中的文本也会发送给已配对电脑；任一端关闭后，该端不再发送或接收文本同步。
6. 控制端按 Ctrl + Alt + Esc 紧急返回本机；接收端按此组合则断开连接。完成后点击「停止共享」。关闭窗口也会停止共享。

两台电脑都必须运行 KMLink。想反过来使用另一台电脑的物理键鼠时，先停止当前共享，再从设备列表发送相反方向的请求，或交换手动连接的角色。不要同时建立两个方向的会话。

### 发现与连接排查

- 列表为空时，确认两端都运行新版 KMLink、位于同一局域网，且 Wi-Fi 没有开启客户端隔离。部分访客网络、VPN 或路由器会阻止组播/广播，此时可用「手动接收」与「手动连接」。
- 自动发现使用 **UDP 24881**。邀请服务使用系统分配的临时 TCP 端口，并通过发现消息告知对端；实际键鼠会话默认使用 **TCP 24880**。防火墙应允许 KMLink 在可信的专用网络上通信。
- 手动连接失败时，确认接收端已开启、两端端口一致、IP 与完整密钥正确。程序不会自动创建防火墙规则，无需开放公网端口或配置路由器端口转发。
- 配对数字不一致、对方设备不认识或请求方向不符合预期时，点击拒绝或取消，再检查选择的设备。

### Android 源码构建

安装 JDK 17 和 Android SDK 36（含 Build Tools 35.0.0），设置 `JAVA_HOME` 和 `ANDROID_HOME`，然后使用仓库内的 Gradle Wrapper：

```bash
cd android
./gradlew :protocol:test :app:assembleDebug
```

Windows 使用 `gradlew.bat`。测试 APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。构建采用 AGP 8.13.2、Gradle 8.13，编译及目标 API 为 36，最低 API 为 26。Android 17 在应用目标升级至 API 37 时才要求新的局域网运行时权限；本版本遵循目标 API 36 的平台权限模型。

正式 APK 的签名通过环境变量提供：`KMLINK_ANDROID_KEYSTORE_PATH`、`KMLINK_ANDROID_STORE_PASSWORD`、`KMLINK_ANDROID_KEY_ALIAS`、`KMLINK_ANDROID_KEY_PASSWORD`。GitHub 标签构建从仓库 Secrets 恢复签名材料，验证 APK 签名后发布；缺少签名时发布任务会失败，不会把未签名 APK 当成正式包。

## 打包桌面程序

先完成当前平台的依赖安装，再执行：

```powershell
# Windows
.\.venv\Scripts\python.exe scripts\build.py
```

```bash
# macOS
.venv/bin/python scripts/build.py
```

Intel Mac 在本地打包前，需要静态链接 cryptography 的 OpenSSL，避免它与 Python 自带的同名动态库冲突（GitHub Actions 已自动处理）。安装 Homebrew 的 `openssl@3` 后执行：

```bash
OPENSSL_DIR="$(brew --prefix openssl@3)" OPENSSL_STATIC=1 \
  .venv/bin/python -m pip install --no-cache-dir --force-reinstall \
  --no-binary cryptography 'cryptography>=44,<51'
.venv/bin/python scripts/build.py
```

输出位于 `dist/`：Windows 为 `dist/KMLink/KMLink.exe`，macOS 为 `dist/KMLink.app`。Windows 分发时需要保留整个 `KMLink` 文件夹。生成器只收集当前系统的 pynput 后端，并为 Windows 配置 PerMonitorV2 DPI 声明、为 Mac 配置应用标识与权限用途描述。

必须在目标系统和架构上打包：Windows 不能直接构建可用的 Mac 应用；Apple Silicon 与 Intel Mac 也需使用相应的 Python 和依赖。Mac 构建完成后会验证临时本机签名；尚未配置 Apple 开发者证书签名、公证或自动更新。

仅生成打包配置、不进行构建：

使用上述虚拟环境内的 Python，把构建命令改为 `scripts/build.py --spec-only` 即可。

## 开发与验证

```powershell
# Windows：先完成源码安装
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

```bash
# macOS：先完成源码安装
.venv/bin/python -m unittest discover -s tests -v
```

源码位于 `src/kmlink/`，安装后入口为 `python -m kmlink` 或 `kmlink`。`python -m kmlink --smoke-test` 只检查界面启动与退出，不启动发现、邀请或键鼠监听。测试覆盖发现消息、设备失效、数字配对与用户同意、双向请求角色、取消/超时及原有控制协议；这些检查不代替真实 Windows ↔ Mac 网络、输入法、快捷键、断线释放、混合 DPI 与多显示器测试。

## 平台参考

- [Mouse Without Borders 功能说明](https://learn.microsoft.com/windows/powertoys/mouse-without-borders)
- [pynput 平台限制与 macOS 权限](https://pynput.readthedocs.io/en/latest/limitations.html)
- [pynput 事件拦截说明](https://pynput.readthedocs.io/en/latest/faq.html)
- [Apple 对输入监控与辅助功能权限的说明](https://developer.apple.com/videos/play/wwdc2019/701/)
- [Windows SendInput 的权限限制](https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-sendinput)
- [Windows DPI awareness](https://learn.microsoft.com/windows/win32/hidpi/setting-the-default-dpi-awareness-for-a-process)

本项目代码以 [MIT License](LICENSE) 发布。第三方依赖保留其各自的许可证。
