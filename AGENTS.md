# FlickDock repository instructions

## Delivery

- Source repository: `https://gitee.com/rooogerluo/flick-dock`; keep it private.
- Public release repository: `https://gitee.com/rooogerluo/flick-dock-releases`; only public release notes, installers and the update index belong there.
- In this project, a request to upload source or publish a changed version includes completing the release: inspect current remote state, bump the version when application code changes, run relevant regressions, build and verify the frozen application and installer, push the selected sources and immutable version tag to the private repository, publish the public installer, verify its anonymous download, and publish `updates/latest.json` last. Read it back and test with the shipped updater. Follow `PUBLISHING.md` and `release_manifest.py`.
- Do not report online update as completed after only a source push. On failure, preserve the previous working index and report the specific unfinished step. Never replace bytes under an existing version or move a published version tag.
- `app_version.py`, `FlickDock.iss`, and `version_info.txt` must agree. Default to a patch version bump for application fixes unless the user specifies another version.
- Credentials come from an authorized session, credential manager or environment. Never put credentials in source, artifacts, URLs, logs or application binaries. Do not copy the whole workspace into either repository; use the explicit source manifest.

## Verification

- Do not demonstrate on the user's desktop. Use Qt offscreen windows, disposable APPDATA profiles and simulated native backends. Do not install an application, change real autostart settings, move the mouse or register real global hotkeys for QA.
- Preserve the single permanent shelf, native text drag payloads, reference/copy modes, hover preview restoration, native Save As and file-handle cleanup.
- Preserve preallocated native animation host geometry, surface transactions and the source-pixmap compositor. Changing native window dimensions during the expand/collapse animation reintroduces flicker.
- Shake sensitivity controls actual acceleration, speed and reversal counts. Keep high-resolution `perf_counter` timestamps and the separation between MIME probe and configured gesture detection.
- UI uses bundled HarmonyOS Sans, consistent original SVG glyphs, clear Chinese labels and restrained feedback. No default tooltip with the full text content.
