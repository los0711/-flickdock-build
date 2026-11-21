# 展开与收起动画修复

## 复现与原因

使用真正的 `ShelfWindow.show_contents()` / `collapse_contents()` 入口，125 项原生文字内容、图标视图、125% DPI、隔离用户目录、Qt offscreen。没有操作用户桌面，没有播放声音。

修前每次打开都会同步重建 125 个条目。几何过渡每一帧还会改变 live card 尺寸，引发布局、子控件绘制以及 18 层阴影路径绘制。连续反转时同步重建叠加，造成明显的事件循环停顿。

## 修改

- 缓存内容布局签名；无内容变化的暖打开复用原来的条目。首屏 12 项同步构造，其余每批 12 项让出事件循环；逻辑引用与总数始终完整。
- 过渡开始时只布局一次，捕获两个带真实 DPR 的端点，230ms 过渡只绘制快照与缓存阴影。卡片轮廓持续保持圆角。结束恢复 live SVG 和文字，静止状态没有额外的缩放链。
- 原生窗口在整个过渡期间固定于两端的联合范围；绘制右边缘固定。标题控制按钮保留实时交互，可以连续点击反转，反转捕获当前显示状态。
- `SurfaceMorph.complete()` / `finish()` 可在打开子级确认浮层前落到当前目标；回调一次，非活动状态调用不做事。
- 默认展开重置到“全部”；确认期间禁止切换；甩动拖放小窗隐藏包括 pin 的全部标题按钮。
- 无跨次展开的 compact 内容快照缓存，每次捕获当前 live 内容。过渡期间新增的内容会在恢复 live 时刷新。

## 同条件测量

原始数据：[morph-before.json](morph-before.json)、[morph-after.json](morph-after.json)。心跳为 4ms Qt 定时器的实际回调间隔；不能等同于原生 Windows 显示帧率。

| 125 项实际入口 | 修前 | 修后 |
| --- | ---: | ---: |
| 首次展开入口耗时 | 109.9ms | 66.5ms |
| 暖展开入口耗时 | 78.9–79.6ms | 8.7–10.0ms |
| 暖展开最大心跳间隔 | 78.9–79.7ms | 9.9–10.8ms |
| 暖展开 Resize 事件 | 265 | 9 |
| 暖展开 Paint 事件 | 632 | 234–240 |
| 暖展开 live shadow draw 次数 | 16 | 2 |
| 连续快速反转最大心跳间隔 | 176.8ms | 14.1ms |
| 连续快速反转 Resize 事件 | 778 | 41 |

首次展开依然包含字体、SVG 和首屏预览的一次初始化成本，连续两轮为 49–67ms。没有据此宣称完整消除首次初始化延迟。原生 Windows 桌面帧率本次没有测试。

## 验证

- `test_surface_morph_v161.py`：125% DPI、125 项真实入口、完整构造 125 项、暖开复用、全部分类重置、连续点击反转、右边缘固定、每帧不 resize、不 capture live、确认 guard、一次完成回调、过渡中新内容、drag 标题隐藏。
- `test_flickdock_v16.py`：原有 14 组集成检查通过，包括媒体 stop、拖出保留、125 项、右侧锚点、原生联合范围。
- `test_svg_hidpi.py`：125% DPI，静止有无阴影控件像素平均差为 0，无 painter 错误。
- `test_content_tile_completion.py`：一次完成、连续新 drop 与 reduced motion 完成契约通过。

复现命令（先将 Python runtime 和 `work/py` 加入环境）：

```powershell
$env:PYTHONPATH=(Resolve-Path '.\work\py').Path+';'+(Resolve-Path '.\work').Path
$env:QT_QPA_PLATFORM='offscreen'
$env:QT_SCALE_FACTOR='1.25'
python .\work\test_surface_morph_v161.py
python .\work\bench_morph_v161.py measurement
```

快照样例：`morph-v161-mid-125.png` / `morph-v161-expanded-125.png`。仅短暂过渡使用栅格图，结束恢复清晰 live 界面。
