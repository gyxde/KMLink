## KMLink @VERSION@

选择对应平台的文件。桌面版解压后运行，Android 版直接安装 APK。两端都需要安装本版本的 KMLink。

本次更新：

- 新增应用内更新：自动检查 GitHub 正式版，校验后下载并安装。桌面版自动替换、启动验证和重启，Android 使用系统安装确认。
- 修复偶发切换后只有键盘可用的问题：双方确认鼠标实际到位后才交接控制，失败自动恢复本机。
- 增加 Windows 鼠标钩子失效检测，以及远端鼠标停滞时自动返回，避免控制状态卡住。

0.3.x 尚无更新器，需要手动安装本版一次；之后可从软件内更新。桌面安装若需要额外权限，会由系统请求授权；更新失败会保留旧版或备份。

| 平台 | 下载 |
| --- | --- |
| Windows 64 位 | [KMLink-windows-x64.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-windows-x64.zip) |
| Mac Apple Silicon（M 系列，macOS 14+） | [KMLink-macos-arm64.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-macos-arm64.zip) |
| Mac Intel（macOS 14+） | [KMLink-macos-x64.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-macos-x64.zip) |
| Android 8.0+ | [KMLink-android.apk](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-android.apk) |
| 源码 | [KMLink-source.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-source.zip) |
| 文件校验 | [SHA256SUMS.txt](https://github.com/@REPOSITORY@/releases/download/@TAG@/SHA256SUMS.txt) |

Windows 版请保留整个 KMLink 文件夹，运行其中的 KMLink.exe。Mac 版解压后将 KMLink.app 放入“应用程序”。

本次 Mac 预编译包要求 macOS 14 或以上。Mac 构建使用临时本机签名，尚未配置 Apple Developer ID 签名或公证；首次打开可能收到系统提示。使用键鼠共享前，在系统设置中授予辅助功能、输入监控，以及系统要求的本地网络权限。

程序支持局域网设备发现、双向控制申请、配对数字确认、加密键鼠连接和可选文本剪贴板同步。当前限制为两台设备的多显示器桌面，不支持文件传输及 UAC/登录锁屏控制。

桌面安装包和 Android APK 由 GitHub Actions 分别构建，经过自动测试和启动检查。自动测试不代替 Windows / Mac / Android 真机对权限、中文输入法、键盘布局和控制手感的验收。Android 分支构建还运行模拟器启动与连接服务检查。

Android 被控模式需要手动启用辅助功能服务，手机保持解锁；控制电脑时使用 KMLink 应用内触控板。服务通知提供停止按钮。受保护界面和部分输入框可能无法操作，不支持其他应用中的全局输入截获或锁屏控制。Android APK 使用项目专用签名，正式 APK 的签名和非调试状态已在构建时校验。

详细使用方法见 [README](https://github.com/@REPOSITORY@/blob/@TAG@/README.md)。
