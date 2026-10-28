## KMLink @VERSION@

选择与你的电脑对应的压缩包，解压后运行。两台电脑都需要安装 KMLink。

| 平台 | 下载 |
| --- | --- |
| Windows 64 位 | [KMLink-windows-x64.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-windows-x64.zip) |
| Mac Apple Silicon（M 系列） | [KMLink-macos-arm64.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-macos-arm64.zip) |
| Mac Intel | [KMLink-macos-x64.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-macos-x64.zip) |
| 源码 | [KMLink-source.zip](https://github.com/@REPOSITORY@/releases/download/@TAG@/KMLink-source.zip) |
| 文件校验 | [SHA256SUMS.txt](https://github.com/@REPOSITORY@/releases/download/@TAG@/SHA256SUMS.txt) |

Windows 版请保留整个 KMLink 文件夹，运行其中的 KMLink.exe。Mac 版解压后将 KMLink.app 放入“应用程序”。

Mac 构建使用临时本机签名，尚未配置 Apple Developer ID 签名或公证；首次打开可能收到系统提示。使用键鼠共享前，在系统设置中授予辅助功能、输入监控，以及系统要求的本地网络权限。

程序支持局域网设备发现、双向控制申请、配对数字确认、加密键鼠连接和可选文本剪贴板同步。当前限制为两台电脑的主屏，不支持文件传输及 UAC/登录锁屏控制。

三个安装包由相应平台的 GitHub Actions runner 分别构建，经过自动测试和启动检查。自动测试不代替 Windows ↔ Mac 真机对权限、中文输入法、键盘布局和跨屏手感的验收。

详细使用方法见 [README](https://github.com/@REPOSITORY@/blob/@TAG@/README.md)。
