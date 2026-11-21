# FlickDock module boundaries

The application keeps Python/PySide6, the existing native WebRTC worker, and the
TypeScript Cloudflare backend. This reorganization does not change wire formats,
storage formats, synchronization rules, window geometry, controls, or animations.

## Composition and ownership

`flickdock.py` is the desktop composition root. `ShelfManager` connects user
commands and presentation signals, owns utility dialogs and desktop input, and
coordinates startup and shutdown. It does not implement cloud replication or
manual transfer protocols.

| Boundary | Modules | Responsibility |
| --- | --- | --- |
| Shelf presentation | `shelf_window.py`, `shelf_drop_zone.py`, `dock_handle.py` | The permanent native window, compact stack/drop feedback, and screen-edge triggers |
| Overview presentation | `overview_ui.py`, `file_preview.py` | Navigation, file rows, selection, previews and existing actions |
| Shared presentation | `shelf_ui_common.py` | Existing geometry constants, font configuration, menus and motion preference |
| Local file helpers | `shelf_file_tools.py` | Existing local naming, ZIP creation and short descriptions |
| Automatic sync lifetime | `sync_runtime.py` | Account, one event stream, one shared scheduler, shelf and clipboard replication |
| Sync boundary | `sync_contracts.py`, `sync_adapter.py` | The local-state interface and the desktop adapter that applies state to the existing shelf |
| Automatic sync engines | `shelf_sync.py`, `clipboard_sync.py`, `sync_scheduler.py`, `cloud_events.py` | Existing versioned cloud replication and notification scheduling |
| Manual transfer lifetime | `transfer_runtime.py` | LAN/WAN construction, account authorization, listener startup and shutdown |
| Manual transfer protocols | `lan_transfer.py`, `lan_account.py`, `wan_transfer.py`, `webrtc_stream.py`, `p2p/` | Existing direct TLS, signaling, WebRTC and TURN implementations |

The dependency direction is composition → views/services. Views do not import the
composition root. Automatic sync does not import manual transfer services; manual
transfers do not import sync engines. No compatibility re-export modules or extra
frameworks are introduced.

## State and lifecycle

The existing content repository remains authoritative for local shelf contents.
`ShelfStatePort` describes the five operations already consumed by replication:
snapshot, title, restore, cancel pending intake, and remove an owned local copy.
`ShelfAdapter` applies received state while retaining the existing selection,
thumbnail cache and account-epoch cancellation behavior. Original user files are
still governed by the existing ownership rules.

Startup creates the native shelf and loads local state before constructing the
cloud runtime. The manager then connects view feedback and incoming-offer handlers
before starting manual listeners. Account resume remains asynchronous, and
offscreen/Cocoa-smoke checks retain their existing restrictions.

Cloud and manual runtimes each own their shutdown. Closing either twice is safe;
closing cloud sync stops both registrations, its event socket and account jobs.
The existing native animation host, source-pixmap compositor and GUI-thread
ownership remain unchanged.

UI callbacks that cross an event-loop boundary use `qt_lifetime.guard/later`.
They resolve targets and dependencies weakly before invocation, and retain the
normal exception hook for live callback errors. Constructors must initialize any
state read by virtual event callbacks before calling a base widget constructor.

`text_preview.py` uses the existing disk worker pool and an epoch fence. It reads
at most 64 KiB plus one byte to detect truncation. Closing or changing a preview
invalidates the result; copying a truncated preview copies the complete file.

`crash_diagnostics.py` extends `GuiWatchdog`: bounded UI action/paint rings feed an
asynchronous writer, unchanged idle snapshots are skipped, logs have size and
retention limits. Diagnostic export contains no clipboard body. Paint arrivals
are explicitly not measurements of displayed GPU frames.

`theme_accent.py` owns the validated user accent and cached contrasting action
tokens. Settings apply validated edits automatically through `settings_live.py`. A
single disk lane coalesces repeated slider changes, commits the existing atomic
JSON transaction, and announces changes back on the GUI thread. Text paths and
shortcuts apply when editing finishes; a cancelled color picker keeps its prior
selection. There are no Save/Cancel buttons in the settings footer. The default black theme retains inverse actions in dark appearance.
All progress bars and capacity rings use the accent; local blue and cloud green
remain semantic file-presence colors.
Reference primary-button styles resolve semantic tokens when refreshed, so an
existing split button does not retain its previous accent after a settings save.
`accent_presets.py` provides seven native swatches using the same live accent
pipeline; it does not create a separate palette or settings transaction.

## Native edge magnetic field

The previous full-screen effect was replaced in 1.8.39. `edge_magnetic_model.py`
contains the bounded, delta-time particle state and all visual configuration.
`edge_absorb.py` draws a small input-transparent field, clipped to the current
screen in logical coordinates, using the existing display-paced `FramePulse`.
`magnetic_settings.py` validates independent per-screen, per-edge input regions.
The defaults enable left/right, at 80 DIP wide and half the edge length; an edge
can cover the full screen length. `monitor_diagram.py` is a storage-independent
shared editor preview for `magnetic_editor.py` and the existing display trigger
editor. The latter retains one shelf trigger per display and its legacy pixel
configuration while supporting full-edge percentage length.
`edge_file_drag.py` owns fixed native destinations on the enabled regions of the
display. It prepares them before the visual trigger band, retains the
first pointer movement, and requires fresh file MIME proof after native Leave.
The configured strip is a valid file drop region; the existing shelf intake
still owns the files. `edge_drop_animation.py` draws an accepted file's visual
copy, first at a safe resting distance and then through a bounded affine Genie
mesh. `edge_native_icons.py` obtains the actual per-file system icon in an isolated
windowless helper and delivers a bounded cached QImage through a queued signal.
No shell or media IO is performed on the GUI paint/drop path. Late results are
fenced by the current drop and owner lifetime. These independent
windows do not participate in shelf surface morphs.
Idle, cancellation, drop, display changes and shutdown retire their animation
and input targets. See `EDGE_MAGNETIC.md` for verification and native limits.

## Verification

Run the maintained offscreen regressions with `QT_QPA_PLATFORM=offscreen` and
disposable profiles. `test_architecture.py` checks dependency directions, source
delivery completeness, shared-scheduler wiring, late notifications after shutdown,
and disabled manual-listener lifecycle. Existing shelf, clipboard, transfer,
account-switch, selection and animation regressions continue to exercise their
real entry points. Compare the same light/dark compact and overview scenes against
the published source to detect visual changes. Native macOS window behavior still
requires verification on a Mac; offscreen Windows tests do not substitute for it.

## 1.8.42 visual and input policy

Window surfaces, shadow masks and animation endpoint clips share
`RADIUS_WINDOW_PX=22`; all five existing internal control/card radius tokens are
unchanged. `DESKTOP_MAGNETIC_CONFIG` is injected by `ShelfManager`, independently
of trigger-band dimensions. File stacks prefetch at most four system icons,
compose once, and reuse one mesh for any number of accepted files.

`EdgeFileDrag` accepts optional preview policy callbacks. Production supplies
visible compact-window regions, confirms only fresh native file MIME, excludes
those regions from the edge input targets, and dispatches a preview-position
Drop to the existing preview intake before considering edge absorption. The
same commit/reentrancy fence covers both paths. Shelf and DropZone native
move events sustain the field while the pointer is over the preview. Protocol,
synchronization and animation host geometry do not participate in this policy.
