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

## Verification

Run the maintained offscreen regressions with `QT_QPA_PLATFORM=offscreen` and
disposable profiles. `test_architecture.py` checks dependency directions, source
delivery completeness, shared-scheduler wiring, late notifications after shutdown,
and disabled manual-listener lifecycle. Existing shelf, clipboard, transfer,
account-switch, selection and animation regressions continue to exercise their
real entry points. Compare the same light/dark compact and overview scenes against
the published source to detect visual changes. Native macOS window behavior still
requires verification on a Mac; offscreen Windows tests do not substitute for it.
