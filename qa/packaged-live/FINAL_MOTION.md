# Final hosted Windows motion checks

QA only: `native_final_motion.py` imports no Qt or application modules and does
not modify the frozen EXE. All accessible operations are reads; inputs are
physical mouse actions or the existing registered global shelf hotkeys.
Refuses to run unless all four Windows / GitHub Actions / Windows runner /
`github-hosted` guards match. No local desktop run is authorized.

The existing UIA worker should call `native_final_motion.run(backend, recipe,
placement=...)`. The parent Recorder must already be recording before the call
and remain active through restoration. Give each call a **25 s outer hard
timeout**; its own waits/new inputs have a **20 s soft deadline**. UIA itself can
block, so a worker hard timeout is necessary. Do not call from the controller
process directly or replace screenshots with rectangle-based approval.

Required calls, each separately captured:

| Recipe | Placement | Actual coverage |
|---|---|---|
| `header-fixed-expand` | not used | Physically pin if necessary, show mini, sample six real header controls, physically click expand, await stable overview, sample again. All six absolute endpoint rectangles must match exactly. No subtraction of a shared translation or tolerance. |
| `host-real-leave-close` | `default-edge` | Physically unpin; close current shelf, physically hover the real edge, open overview from the mini when needed. Open clipboard, move cursor to an exposed producer-owned point outside the union of actual shelf and popup native hosts plus 32 px. Both modes are tested. |
| `host-real-leave-close` | `manual-position` | Physically unpin; use existing real hotkey for each mode, physically drag its blank header by (-100,+50), confirm at least 40 px actual displacement, open clipboard, perform the same real cursor-only leave. Both modes are tested. |

Each call requires an initially visible normal shelf and a usable current
clipboard preview. It restores the original visible mini/overview mode and
actual pin state; it does **not** claim to restore screen coordinates. The two
leave calls cover all four placement × mode combinations without exceeding a
single-call budget. Place them before the destructive final empty-shelf recipe.

`final-motion-events.jsonl` contains raw absolute bounds, per-observation
monotonic timestamps, before/after foreground HWND/PID, actual cursor coordinates,
and each host/popup visibility observation. Both actual native Windows must be
absent; disappearance of one child button is insufficient. Default dock closes
and manual idle closes are observed with up to 4.4 s per case, not forced by a
click. The leave phase contains **no clicks, keys, focus/activation call, app
command, Invoke or Toggle**. If FlickDock was not the foreground process, the
mouse-only phase must not activate it. Natural focus restoration after closing
an already-active FlickDock window is recorded, not treated as focus stealing.

Selectors are from actual frozen Windows UIA trees:

| Control | Name | Role / class |
|---|---|---|
| Shelf | FlickDock | Window / ShelfWindow |
| Network | 云端网络… or 正在检查云端网络… | Group / NetworkStatus |
| Cloud | 云同步：… | Button / ToolbarButton |
| Pin | 取消钉住 or 钉住搁架 | CheckBox / ToolbarButton |
| Expand | 展开详情 or 收起详情 (also accepts the older 展开内容 variant) | Button / ToolbarButton |
| Menu | 更多操作 | Button / ToolbarButton |
| Collapse | 收起到屏幕边缘 | Button / ToolbarButton |
| Clipboard source | 放大查看剪贴板 | Button / ClipboardButton |
| Clipboard host | FlickDock · 剪贴板预览 | Window / ClipboardPreview |

The pre-existing observed edge title `FlickDock edge` has no invented role/class.
Every selector must resolve uniquely within the owned app scope. Each physical
input checks actual target PID and native ancestor before being sent. The leave
destination must be an exposed point of the disposable producer; the helper
will fail rather than click or activate an unrelated application or guess that
a screen corner lies outside a transparent preallocated host.

If the current overview covers the entire producer, the helper first considers
the four actual screen corners, then bounded inset variants. It may temporarily
move **only** the owned producer with `SetWindowPos` flags
`NOSIZE | NOZORDER | NOACTIVATE` (`0x15`). It never resizes that producer or moves
FlickDock. The chosen free point must additionally pass actual native PID
ownership; taskbar-covered candidates cannot pass. The original producer
rectangle is saved and restored even on a failed leave. No actual free point
produces an explicit failure, not a relaxed owner/inside-window assertion.

Portable fixtures cover safety and strict negative assertions, not native
visual acceptance. Recorder frames still require review for mid-transition
header movement/flicker, actual clipboard/owner closing motion, and the manual
versus edge exit paths. No display FPS, Mac result, GPU result, or user hardware
performance is inferred from these fixtures or UIA callback rates.
