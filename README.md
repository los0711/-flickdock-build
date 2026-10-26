# FlickDock

适用于 Windows 10/11 x64 和 macOS 14.4 以上（Apple 芯片、Intel）的文件、图片和文字搁架。当前源码版本 **1.8.7**；Windows 发行版为 1.8.6。

单个永久搁架，支持内容拖放、快速甩动呼出、可配置快捷键、屏幕贴边、图片与视频预览、文字直接拖出、多选拖出和 ZIP 打包。设置支持明暗主题、复制或原位置引用、保存位置、自启动、触发区域和甩动灵敏度。

复制与上传保留正常彩色图标，旁边显示小进度环；总览的“传输中”单独列出未完成项目，右上角显示剩余上传数，完成后短暂显示绿色勾。传输可取消，另一台设备会看到待完成项目。账号面板支持设备改名、修改密码并退出其他设备。局域网传输支持接收确认；设置控件不会被滚轮误改。性能与免费服务边界见 [FEATURE_V181.md](FEATURE_V181.md)。

新增设置内账号注册/登录、多设备列表和撤销、离线队列及跨 Windows/Mac 的文件与文字同步；圆角设置窗、行内更新和启动项自动修复。本版不绑定邮箱。Mac 两种原生架构均通过云端构建与冻结程序同步验证；见 [MAC_BUILD.md](MAC_BUILD.md)。云同步协议/免费额度见 [CLOUD_SYNC.md](CLOUD_SYNC.md)。

上传保留原始字节，图片不缩放、不重新编码；下载校验完整 SHA256。没有额外的 64 MB 单文件限制；全部账号共享 8 GiB 云空间，并受 Cloudflare 免费额度约束。超过 500 MiB 的云端文件在上传完成 24 小时后删除，搁架删除也会同步清理云端，本机原文件保留。上传与下载支持断点续传；达到服务商额度后云同步暂停至重置，本地搁架仍可使用。

## 运行与构建

使用 Python 3.12：

```powershell
python -m pip install -r requirements.txt
python flickdock.py
python -m PyInstaller --noconfirm --distpath dist --workpath build FlickDock.spec
ISCC.exe FlickDock.iss
```

安装包生成在项目上一级 `outputs/`。Inno Setup 6 安装向导支持选择目录；桌面快捷方式默认选中，并提供开始菜单及 Windows 程序卸载入口。应用以当前用户权限运行。

## 验证

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python check_release_v16.py
python release_dpr_v165.py
```

回归使用离屏窗口、独立配置、模拟的快捷键和自启动后端。不要在用户桌面演示、移动鼠标、触发真实全局快捷键或安装软件。媒体测试使用另行提供的测试素材；用户录屏不属于源码。

冻结程序检查：

```powershell
FlickDock.exe --verify-media C:/test/video.mp4 --verify-output media-result.json
FlickDock.exe --verify-transfers --verify-output transfers-result.json
FlickDock.exe --verify-update --verify-current 1.6.5 --verify-output update-result.json
```

两者均需 `QT_QPA_PLATFORM=offscreen`。更新检查入口只验证公开索引、下载安装包并核对文件，不执行安装。

## 在线更新

设置和托盘菜单都提供“检查更新”。正式安装版启动后会自动检查，并在发现新版本时提示；下载经过 HTTPS、大小和 SHA256 校验。用户点击安装后覆盖当前安装目录、保留搁架和设置，并重新启动应用。开发运行不会自动检查或执行安装程序。

源码仓库保持私有。公开安装包与更新索引位于 [FlickDock Releases](https://gitee.com/rooogerluo/flick-dock-releases/releases)。发布完整步骤见 [PUBLISHING.md](PUBLISHING.md)。仅推送源代码不会产生可安装更新，需一并构建和发布安装包，最后更新索引。

## 字体与许可

FlickDock uses HarmonyOS Sans Fonts. / 本软件使用 HarmonyOS Sans 字体。

使用未修改的 HarmonyOS Sans SC Regular；Qt 合成显示字重。字体许可与校验见 `licenses/HarmonyOS-Sans-LICENSE.txt` 和 `font_manifest.json`。Python、Qt/PySide6、PyInstaller 许可与动态库替换说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。媒体解码采用 Windows 原生后端，格式支持取决于已安装的编解码器。依赖裁剪与审计见 [PACKAGE_AUDIT.md](PACKAGE_AUDIT.md)。

Author @Roger
