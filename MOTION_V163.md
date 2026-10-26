# v1.6.3 生命周期与统一绘制

旧版扩缩末尾将 SurfaceSnapshot QWidget 隐藏、移动 native host、把按钮重新挂入 card，然后切换为 MotionShadow 的实时 drawSource。125% 缩放下，native origin 的 18 DIP 调整对应 22.5 physical pixels，存在半像素相位变化；阴影、采样参数及绘制路径也不完全相同。

本版使用一个 MotionShadow compositor：动画从头就绘制实时目标，旧表面作为逐步消失的覆盖内容；进度为 1 时已经完全是实时绘制，结束无需换层。阴影、圆角裁切和采样设置共用。窗口 host 在开始时按 DPR 相位向外对齐，完成后保持不动；操作按钮一直属于同一个 owner。模型条目构建和媒体读取仍在动画后分批恢复，图片后台解码。

`test_motion_v163.py` 通过真实 ShelfWindow 入口，在 100%、125%、150%、200% 缩放比较 expand/collapse 的 identity 和 reveal 非 identity 姿态。四种结束边界均为 changed_pixels=0，host 与 card 全局原点保持不变，快速反转与完成回调只执行一次。卡片落入是独立动画，比较几何终点时先完成该动画，避免把正常时间推进当作几何跳变。证据在 `motion-v163-dpr-*.json`。仅验证 Qt 离屏图像，不声称验证了 DWM 帧率。

清空层原本属于 card，因此修前绘制缓存确实包含删除按钮像素；四种离屏场景未直接复现用户桌面的残留。新结构把确认层移到同一个 owner 的兄弟覆盖层，保持非原生弹窗，隔离于任何 card Effect/快照缓存。四种实际清空场景验证 card 捕获不含确认层、关闭后确认红色像素为 0、对象数量归零，并覆盖焦点、计时器及 owner/card 销毁。该改动消除可证实的缓存耦合；原用户残留的完整桌面条件未实测。

媒体回归在冻结旧代码上会因缺失源仍被调用 play 而失败，新入口对 open/play/seek/position 全部检查。真实临时 MP4 在副本模式下可删除原件后播放；引用模式删除后显示失败，不重开播放器。没有把模拟失败或离屏检查称作用户挂死时长的复现。
