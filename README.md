# FlickDock

适用于 Windows 10/11 x64 和 macOS 14.4 以上 Apple 芯片设备的文件、图片和文字搁架；Intel Mac 版本已停止更新。当前源码版本 **1.8.30**。

模块边界、状态归属和生命周期说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

Windows 视频预览使用 Qt FFmpeg 和原生视频渲染，进度条支持点击跳转与拖动，关闭后释放文件占用。按实际内容识别系统剪贴板的重复通知与格式转换，避免同一份内容反复写入历史记录和重复显示复制成功。文件展示恢复原来的两行列表及图标卡片，紧凑工具栏保留；传输界面沿用原版布局。

搁架与剪贴板仅使用现有 R2 云端同步；剪贴板保持最高优先级，并区分云端备份和接收设备实际写入。手动互传恢复 1.8.17 协议：局域网使用 TLS 直传，远程使用原版 libdatachannel WebRTC 文件流，直连失败时使用原有 TURN 中继。两端需使用当前测试版本，四位混合接收码、同账号免确认和不同账号接收确认保留。

总览提供视频解析入口，小预览不增加入口。采用 yt-dlp 与独立 FFmpeg 进程，支持主流视频网站、X、Facebook 和可提取的通用网页视频；可选画质、下载视频或音频、下载后传输或加入搁架，并更改保存目录。链接提示五秒自动收起，同一链接两分钟内不重复提示，可在解析页和设置中关闭。登录限制、受保护内容、直播及网站接口变化可能导致提取失败，界面显示实际错误。

小预览与总览共用右上角状态及窗口按钮，版块位移与展开动画同步。设置、文件互传及剪贴板预览使用可反向缩放；剪贴板按钮可切换弹窗，点击弹窗任意位置或隐藏所属搁架会关闭预览。复制提示关闭后，成功、失败与延迟结果都不提示。右侧预览提供打开、重命名、复制路径、打包 ZIP、发送和移出搁架六个操作；发送为蓝色，移出为红色，重命名弹窗置顶。删除弹窗可选择已选、全部或取消，移出保留外部原文件。

单个永久搁架，支持内容拖放、快速甩动呼出、可配置快捷键、屏幕贴边、图片与视频预览、文字直接拖出、多选拖出和 ZIP 打包。设置支持明暗主题、复制或原位置引用、保存位置、自启动、触发区域和甩动灵敏度。

复制与上传保留正常彩色图标，旁边显示小进度环；本机文件上传时仍在“全部”中可用，传输数量显示在“传输中”分类和总览底栏。传输可取消，另一台设备会看到待完成项目。账号面板支持设备改名、修改密码并退出其他设备。未认证的局域网传输保留接收确认；设置控件不会被滚轮误改。性能与免费服务边界见 [FEATURE_V181.md](FEATURE_V181.md)。

设置内提供账号注册/登录、多设备列表和撤销、离线队列及跨 Windows/Mac 的文件与文字同步；圆角设置窗、行内更新和启动项自动修复。本版不绑定邮箱。历史 Mac 版本有 Apple 芯片、Intel 两种构建，本轮 1.8.23 尚未构建或实测 Mac 安装包；见 [MAC_BUILD.md](MAC_BUILD.md)。云同步协议/免费额度见 [CLOUD_SYNC.md](CLOUD_SYNC.md)。

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


视频引擎准备：安装 requirements.txt 后运行 `python p2p_assets.py` 与 `python video_assets.py`，再构建 PyInstaller。Windows 视频工具使用固定 SHA256 的 BtbN LGPL FFmpeg 9.0.2 构建，位于独立目录，macOS 构建使用 imageio-ffmpeg 0.6.0 的独立命令行程序。来源和许可见 THIRD_PARTY_NOTICES.md 与 licenses/Video-LICENSES.txt。
