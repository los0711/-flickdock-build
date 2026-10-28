# FlickDock 1.8.14 界面实现与功能映射

## 范围

软件界面以用户提供的 `最终版.html` 为参考；SHA-256 为
`b427bce38e22d225cab0e8a80736abc173df4b30c8eb06f1f1ad24da70ed573a`。
后续 HIG 改版在此基础上修正布局、状态和可访问性；第二份官网 HTML 用于独立官网，发布目标为 Cloudflare Worker。

继续使用 Python 3.12、PySide6 6.12.0 和既有 Qt Widgets 架构。没有引入浏览器运行时、网页服务桥接、新依赖或新协议。生产入口仍为 `flickdock.py` / `mac_entry.py`，使用既有 PyInstaller、Inno Setup 与 DMG 构建流程。

## 页面与尺寸

尺寸是可见卡片的逻辑像素，不含透明阴影宿主；高 DPI 下由 Qt 映射到物理像素。

| 页面 | 可见尺寸 | 结构 |
| --- | --- | --- |
| 小搁架 | 332 × 318 | 平台标题区、最多五张叠卡；甩动唤出隐藏账号和局域网入口 |
| 内容总览 | 1040 × 600 | 210px 分类栏、弹性内容区、340px 预览区 |
| 设置 | 860 × 600 | 账号与外观双列、触发区域、快捷键与同步方向、固定底栏 |
| 局域网互传 | 980 × 704 宿主 | 左侧发送、右侧传输及历史，比例 5:7；内部队列与历史独立滚动 |
| 登录 / 注册 / 密码 | 390px 宽 | 真实账号表单和服务状态 |
| 关于 | 400px 宽 | 实际版本、作者与许可 |
| 重命名 | 380px 宽 | 原文件名编辑器、取消、确定 |
| 更新 | 420px 宽 | 实际版本说明、下载进度、校验和与操作 |
| 清空确认 | 340px 宽 | 数量、说明、取消与清空 |
| 云方向菜单 | 256px 宽 | 上传和接收两个真实开关 |

Windows 按已安装的 HarmonyOS Sans SC → Noto Sans SC → Source Han Sans SC → Segoe UI Variable → Segoe UI → 系统默认回退；不显式选用微软雅黑或苹方。macOS 使用系统 GeneralFont 及系统中文回退，不再分发 Apple 字体。所有面板继承字体和真实字重。小屏幕使用分类/内容/预览的渐进布局。

## 原功能对应入口

HTML 中的示例账号、文件、网络速度和定时动画不会作为产品数据。所有操作继续绑定原实现。

| 能力 | 新界面入口 | 原实现 |
| --- | --- | --- |
| 文件、图片、SVG、文字、网页拖入 | 小搁架、总览、添加/粘贴菜单 | ShelfWindow、ContentRepository、ContentResolver |
| 复制或引用、保存目录 | 设置“放入文件”“内容保存位置” | SettingsStore、CopyFilesJob、owned_files |
| 文字直接复制或拖到输入框 | 复制、Ctrl+C、拖出 | 原 MIME / clipboard 实现 |
| 快速甩动、两个全局快捷键、贴边区域 | 设置相关分组 | ShakeDetector、HotkeyManager、DockHandle |
| 永久保存、最新在前、五层叠放 | 总览列表/网格、小搁架 | 原 shelf 顺序、StackTile 动画 |
| 多选与分组拖出 | 行复选框、Ctrl / Shift / Ctrl+A | 原 selected / chosen_refs / drag_files |
| 打包所选、打包全部 | 总览工具栏、分组预览、右键 | ZipArchiveJob 与原生 Save As |
| 打开、定位、路径、重命名、移动 | 文件右键及预览操作 | 原 open_path、reveal_ref、rename_ref、move_files |
| 移除、清空确认 | 悬停删除、右键、清空全部 | 原确认生命周期和文件所有权清理 |
| 图片、视频、音频及文字预览 | 右侧预览 | 原 ImagePreview、MediaPreview 与真实解码器 |
| 选中项预览恢复 | 离开其它悬停项或进入预览区 | 原 hover / selected 归属规则 |
| 上传 / 接收独立开关 | 云菜单与设置 | 原 settings 键及 ShelfSync.set_directions |
| 进度、百分比、剩余时间、取消、重试 | 传输分类、底部两条摘要、预览、右键 | 原 transfers / TransferRateTracker / sync 队列 |
| 账号注册、登录、改密、设备改名/撤销 | 账号卡、独立表单、设备列表 | 原 AccountService 和安全凭据存储 |
| 局域网发送与接收 | 侧栏左对齐“局域网互传” | 原 LanTransferService，不自动导入搁架 |
| 历史删除、接收位置、文件/文件夹选择 | 局域网窗口右栏和发送区 | 原 LAN 模型与平台文件选择器 |
| 版本、检查/立即更新、更新提示、跳过版本 | 设置及应用内更新窗 | 原 UpdateClient / UpdateController |
| 钉住、缩放、收边、菜单与退出 | 平台工具栏、页面右键、系统托盘 | 原 shelf / manager 生命周期 |

参考没有覆盖的入口以相同样式保留。产品仍使用真实六位局域网确认码；保留原有设置完整范围、原生路径选择器、更新“跳过此版本”等能力。局域网和设置窗口可与搁架并用。

### 云状态徽标

文件缩略图右下角采用圆形轮廓内的状态符号，来自真实同步状态；不代表文件类型。颜色之外的符号也可辨认状态：

| 状态 | 颜色 |
| --- | --- |
| 已同步 | 绿色 `#30D158` |
| 上传中 | 蓝色 `#0A84FF` |
| 等待上传 | 青色 `#64D2FF` |
| 仅本机 | 灰色 `#A1A1AA` |
| 已暂停 | 橙色 `#FF9F0A` |
| 失败 | 红色 `#FF453A` |

上传、复制和下载仍保留正常彩色缩略图，进度使用旁边的小环或专用进度区。云方向任一关闭时云指示变红，两方向均开启时变绿。数量只在传输分类和底部摘要显示。

## 主要改动文件

- `native_ui_common.py`：参考颜色、圆角、内联 SVG 图标。
- `reference_widgets.py`：原生布局、缩略图、按钮、状态点、重命名和分组预览展示。
- `flickdock.py`、`content_grid.py`、`image_preview.py`、`transfer_visuals.py`：现有事件绑定和展示层。
- `app_settings.py`、`account_panel.py`：设置、登录、设备、密码、关于页。
- `lan_panel.py`：发送、接收确认、进度、记录与窗口控件。
- `confirm_clear.py`、`update_dialog.py`、`update_notice.py`：确认和更新展示。
- `ui_theme.py`、`flickdock_icons.py`、`surface_morph.py`：主题、图标、圆角和半透明画面正确交叉淡入；原预分配动画宿主、快照合成架构保持。
- `platform_native.py`：本次额外授权的 Mac 启动修复。
- `build_macos.py`、`test_mac_cocoa_spaces.py`：增加实际 Cocoa 启动验证，构建产物格式不变。
- `release_manifest.py`：显式登记新增原生 UI 模块、图标和测试；不打包原型或测试数据。
- 版本文件与文档：统一为 1.8.14。

网络服务层、请求参数、鉴权、数据库、同步存储与 LAN 协议没有因改版修改。未完成的浏览器 UI 原型不属于运行时或源码交付清单。

## Mac 启动修复

实机错误为 `NSInternalInconsistencyException`：同一窗口不能同时使用
`NSWindowCollectionBehaviorCanJoinAllSpaces` 和 `NSWindowCollectionBehaviorMoveToActiveSpace`。

原生窗口行为现在先清除冲突标志，再设置跨空间/全屏辅助行为；贴边窗口重复抬升保持幂等。可恢复 Cocoa 异常会恢复原层级，程序错误仍会暴露。Mac 构建同时执行真实 Cocoa 冲突标志回归和冻结应用启动检查，覆盖贴边及搁架实际展示入口。

## 验证方法与实际边界

- 浏览器以相同逻辑尺寸渲染原 HTML，分别截取 Windows/Mac、深浅色参考；Qt 使用 offscreen 和隔离配置生成对应截图。
- 新原生 UI 用例验证卡片尺寸、左右分栏、左对齐、完整按钮与四边描边、六种真实状态映射、真实所选 ZIP 和原菜单入口。
- 既有测试继续验证文字 MIME、拖放、复制/引用、文件句柄、清空、快捷键、灵敏度、账号安全、同步取消/超时/队列、LAN 和更新行为。
- 因 HTML 更改，旧测试中硬编码的旧尺寸、按钮文案和颜色已更新；文件内容、取消、权限、请求次数和队列语义断言保留。
- 动画检查包括不改变原生宿主几何、快速反向切换、末帧与静止界面像素一致。修复了新滚动区透明填充擦除快照及底部按钮未跟随转场的位置问题。
- 构建/验证不安装到用户电脑、不注册真实快捷键、不修改自启动、不移动鼠标、不在用户桌面演示。

顶层可读表面采用不透明深浅色，透明区域只用于圆角和阴影；内部低对比度层次叠加于不透明底色，不透出桌面文字。没有模拟原生 Liquid Glass 或引入新系统 backdrop。Qt 与 Chromium 字形栅格化、系统文件选择器与媒体控制存在平台差异。Mac 云构建不等于用户 M3/macOS 27 实机完整验证。遵照用户要求不做 Developer ID 签名/公证，首次运行确认仍由 macOS 决定。

完整本轮测试、构建结果和交付产物见随包验证报告；未完成项应明确列出，不以离屏截图替代实机结论。
