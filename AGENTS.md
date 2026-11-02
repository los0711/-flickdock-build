# FlickDock repository instructions

## Continuing global engineering contract (2026-11-02)

- Deliver complete runnable files/resources, no omitted logic or placeholder implementation. Prefer minimal dependencies for new isolated work; keep this existing PySide6/native/client-server architecture and all previously confirmed UI/behavior.
- Validate external boundaries, timeouts and ownership. Controlled internal invariants fail visibly; do not add empty catch blocks or redundant null guards to conceal errors. Required lifecycle, authorization, ordering and cancellation fences remain.
- Separate state/logic/UI, centralize meaningful constants/configuration, prefer supported modern APIs, and comment intent rather than syntax. Handle stale callbacks, task cancellation, timers and subscriptions explicitly.
- New visual work needs complete interaction states and natural motion, but this does not authorize restyling existing FlickDock or restoring cancelled opacity, blur or G2 effects.
- Root-cause fixes and truthful actual verification remain required; preserve unrelated functionality and complete artifacts. New conflicts with existing requirements require two plain-language options and the user's decision before dependent changes.

## User change contract (2026-11-02, continuing)

- Before each new change, identify the requested behavior and preserve all unrelated functions, layouts, animations, controls, shortcuts, platform integration and data. Record the relevant before/after behavior and regression scope; passing one new test is not proof that the entire product works.
- If a new request actually conflicts with a previously required feature or is infeasible under its constraints, do not implement the conflicting part. Explain the conflict in plain Chinese, give two concrete alternatives and wait for the user's choice. Continue independent authorized work. Routine implementation choices and verified fixes without such a conflict do not require new approval.
- Diagnose causes before changing behavior. Do not conceal errors, simulate success, add arbitrary delays or delete required safety/authorization/lifecycle checks to make a test pass. Clean-room refactoring must preserve the established behavior contracts.
- Review all existing user flows and perform appropriate actual regression and frozen-application validation. Mental review alone is not validation. Preserve original failures and state untested/failed limits honestly; never claim complete preservation or completion from a partial suite.
- Deliver complete usable source/software artifacts rather than omitted code fragments. Current delivery order: complete the requested clean-room review/refactoring first, deliver its complete source and short diagnosis, then continue the remaining authorized development without waiting for another confirmation. No intermediate installer or website publication is authorized at present. Do not inspect or operate the user's current computer; use isolated QA only.

## Delivery

- Source repository: `https://gitee.com/rooogerluo/flick-dock`; keep it private.
- The user subsequently explicitly authorized the existing GitHub build mirror `https://github.com/los0711/-flickdock-build` to be public. This is an exception for the reviewed source manifest used by macOS CI; it does not authorize publishing credentials, user data, workspace outputs, or a source ZIP. Do not change repository visibility or enable paid build overages.
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
- UI follows the supplied HTML's platform font stack and installed font weights; do not force the bundled HarmonyOS Sans Regular face over the OS fonts. Use consistent SVG glyphs with appropriate source attribution, clear Chinese labels and restrained feedback. No default tooltip with the full text content.
