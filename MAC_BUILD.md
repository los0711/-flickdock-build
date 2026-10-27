# FlickDock macOS build and platform boundary

## Current verification boundary

Both arm64 and x86_64 native builds passed on GitHub's macOS hosts in private
run 40297249021 (application source commit
04da69953be17f0f1591be77338395d5b0d0f0e1). The jobs produce real `.app`, update
ZIP, `.dmg` and a native `.pkg` installer, run platform and cloud-sync checks from each frozen app, and
verify cloud deletion releases space. Windows cannot build a usable Mac app;
`build_macos.py` rejects a non-Mac host before creating artifacts.

The platform tests never register real global shortcuts, modify real login
items, play media, install an app, or demonstrate on the user's desktop.

## One-command native build

The public GitHub build mirror uses `.github/workflows/macos-build.yml` on
the `cloud-build` branch with standard `macos-15` (arm64) and
`macos-15-intel` jobs. A push to that branch starts both jobs; the workflow can
also be dispatched manually. Public standard runners do not consume paid
Actions minutes, although an account billing lock can still block all Actions.
The workflow builds both native artifacts, runs frozen real-cloud verification
offscreen and retains artifacts for three days. It does not publish a public
release or update manifest. Artifact ZIPs
must be downloaded and checked against the Actions digests, then each inner
release checked against its native build report before public promotion.

Use a Mac with Python 3.12 and Xcode 16 Command Line Tools or newer. The pinned
Qt 6.12 runtime targets macOS 14.4 or newer, as listed in
[Qt's supported platforms](https://doc.qt.io/qt-6/supported-platforms.html).
In this source folder:

```sh
python3 build_macos.py
```

The script creates `build/macos-venv`, installs native PySide6, PyInstaller and
PyObjC Quartz dependencies, renders the original SVG icon offscreen, runs the
platform tests, builds `dist/FlickDock.app`, and runs a frozen offscreen import,
font and native media-backend smoke check using a disposable profile. It then
creates a versioned update ZIP, drag-to-Applications DMG, macOS Installer `.pkg`
for `/Applications`, and SHA256 report under `../outputs/`. It expands the
package to verify identity and version without installing it. No artifact is
installed or launched as a normal desktop app. Existing versioned artifacts
are never overwritten. The app appears in the Dock so it can be reopened after
hiding the shelf.

Build arm64 on Apple Silicon and x86_64 on Intel Macs. `--target-arch universal2`
requires a universal2 Python interpreter and universal2 versions of **all**
binary dependencies; PyInstaller must reject incompatible inputs. Do not label
a single-architecture binary universal2. Never import this workspace's Windows
`py/` dependencies on a Mac.

Optional Developer ID signing and notarization use local Keychain credentials:

```sh
python3 build_macos.py \
  --sign-identity 'Developer ID Application: Your Organization (TEAMID)' \
  --notary-profile 'flickdock-notary'
```

Use `--entitlements /absolute/path/entitlements.plist` only when the selected
Python/PyObjC distribution needs specific hardened-runtime entitlements. The
script does not embed signing credentials. Without signing/notarization, the
result is a local build; Gatekeeper distribution acceptance remains unverified.
The `.pkg` needs a separate Developer ID Installer identity for signed public
distribution; the current automated package is unsigned.

## Shared UI integration

- `platform_native.data_directory()` selects
  `~/Library/Application Support/FlickDock` on macOS and retains APPDATA on Windows.
- `InstanceGuard` uses `QLocalServer` and is shared between platforms.
- `native_input_available()`, `mouse_buttons_mask()` and `window_move_active()`
  replace Windows-only polling. Quartz reads global button state and window
  geometry in logical screen points. It does not capture screenshots or move
  the pointer. The real MIME gate must still accept a content drag before a
  shake may reveal the shelf; a window whose bounds move stays blocked until
  mouse release.
- `MacHotkeyBackend` implements the existing register/unregister interface with
  Carbon app hotkeys, which do not use global keyboard event monitoring. Connect
  its activation callback to the hotkey manager's dispatch function and call
  `close()` on shutdown. Qt `Ctrl` represents Command on macOS; Qt `Meta`
  represents the physical Control key. Alt represents Option. Test actual
  keyboard layouts, Spaces and reserved system combinations on a Mac.
- Native Save As, drag payloads, dock-edge geometry and screen bounds remain Qt
  operations. macOS Retina/multiple-screen placement and cross-app drag delivery
  require Mac verification; Windows offscreen tests cannot establish them.
- `runtime_macos.py` selects Qt's AVFoundation (`darwin`) media plugin. The
  Windows runtime hook must not run in the macOS bundle. Playback codecs depend
  on the native macOS backend and still need silent-first-frame/source-release
  verification on the target Mac.

## Login startup

`StartupManager` writes one current-user Run value on Windows, or one
`~/Library/LaunchAgents/com.flickdock.app.plist` on macOS. macOS uses the current
bundle executable with `RunAtLoad`, Aqua session scope and no KeepAlive; changing
the setting does not unload or terminate the running app. Moving the app to a
new install folder updates its own entry the next time saved-enabled settings
are reconciled. Run the installed app after placing it in Applications.

Preferences must be saved against the actual `StartupStatus`. An unchanged
checked checkbox still calls `reconcile(True, explicit=True)` to repair a missing
entry. Windows Task Manager's disabled startup record is preserved during
background reconciliation and cleared only for an explicit user enable. No
fallback task scheduler or second Startup-folder shortcut is added.

## macOS online updates

Windows `.exe` update manifests must never be offered to the Mac build. Publish
separate verified HTTPS indexes for each supported macOS architecture. The 1.8.4
client accepts ZIP updates only. Version 1.8.7 is a ZIP bridge so those clients
can upgrade; it also publishes DMG and PKG installers separately. A later update
channel can use DMG after supported clients have the DMG-aware updater. Each
index names the exact versioned update package and its whole-file size/SHA256;
publish only after the native Mac build report and anonymous full download pass.
`release_macos.py` checks the native build and frozen cloud reports, local package,
bundle identity/version/CPU header and anonymous complete download. It writes
`updates/macos-arm64.json` or `updates/macos-x86_64.json` only after every check
passes. Initial publication requires `--initial-release`; existing versions
cannot acquire different bytes. Mac packages in this release have no Developer
ID signature or notarization; Gatekeeper distribution acceptance is unverified.

For DMG manifests, the 1.8.6 and later in-app updater downloads and verifies
the versioned DMG, then opens it for the user to drag FlickDock into Applications.
The application remains running until the user completes that native step.
ZIP manifests continue to use
`PrepareMacUpdateJob(zip_path, manifest, current_version=..., installed_app=...)`
runs hashing and extraction in a Qt worker. Call `start()`, retain the job until
`finished(prepared)`/`failed(message)`/`cancelled()`, and use `cancel()` on shutdown.
The installed app and its parent directory must be user-writable. Preparation
does not change the installed app. A protected system Applications directory
fails clearly instead of silently requesting elevation.

After an explicit Install action, call
`launch_update_helper(prepared, quit_callback=manager.quit)`. Only successful
helper process creation requests quit. `mac_entry.py` handles the helper flag
before constructing a GUI. The helper waits for the original PID to exit,
rechecks the app identity/version and complete staged tree hash, atomically
renames the old bundle to a unique backup, and replaces it with the new one. A
replacement or relaunch-command failure restores the old bundle. It never
updates a live app in place. Failure diagnostics remain in the job's own stage.

ZIP validation rejects absolute/traversing/duplicate/case-colliding paths,
special files, encrypted entries, oversized payloads and links outside the app.
Normal internal framework symlinks are preserved and checked after extraction.
The app's Info.plist identity/version, executable permissions and Mach-O header
must agree with the expected release. Helpers reject stale or modified staging.
Tests use only byte fixtures and injected launch callbacks; they do not prove
real macOS launchd, Carbon, Gatekeeper, AVFoundation or update installation.

Reference: [Apple launchd configuration](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5)
and [Apple event-source state documentation](https://developer.apple.com/documentation/coregraphics/cgeventsourcestateid).
