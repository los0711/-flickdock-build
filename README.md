# FlickDock

Windows 10/11 x64 的文件、图片和文字搁架。当前版本 **1.8.0**。

单个永久搁架，支持内容拖放、快速甩动呼出、可配置快捷键、屏幕贴边、图片与视频预览、文字直接拖出、多选拖出和 ZIP 打包。设置支持明暗主题、复制或原位置引用、保存位置、自启动、触发区域和甩动灵敏度。

新增设置内账号注册/登录、多设备列表和撤销、离线队列及文件/文字云同步；圆角设置窗、行内更新和启动项自动修复。邮箱验证服务仍需发件域名配置。Mac 原生适配和更新构建流程已准备，尚无经过真实 macOS 验证的安装包；见 [MAC_BUILD.md](MAC_BUILD.md)。云同步协议/免费额度见 [CLOUD_SYNC.md](CLOUD_SYNC.md)。

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
