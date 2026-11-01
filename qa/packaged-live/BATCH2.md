# Optional normal frozen Windows UI batch 2

These QA-only recipes use an existing `_UIABackend` inside its disposable UIA
worker, not the controller-side `Driver` proxy. Production is never imported or
patched. All input is physical OS mouse/keys. UIA/COM are read only.

Integration owned by root (not applied here): expose a `batch2(recipe)` worker
operation that calls `native_batch2.run(self, recipe)`, allow it in the existing
worker dispatcher, and call through a hard-bounded `Driver._rpc(..., seconds=75)`.
Wrap each call in the existing Recorder and save returned events beside its
actual frames. The helper soft deadline is 65 seconds; COM itself needs the
existing outer process timeout. Do not call helper directly from the controller.

`ui-batch2-actions.json` contains seven independent `kind=batch2` actions and
explicit ordering. **history-folder must run before the first baseline
history-delete-one**, which deletes synthetic record0. Running only before Clear
is too late. It tests both modes and returns overview. Run the remaining six
after baseline/extra1, in JSON order. `grid-delete-width` removes only tiny.bin's
shelf reference. `empty-zones-last` must be last and clears disposable shelf
references through the existing confirmation. No Send action is issued.

The selection recipe resets prior selection with the existing real 全选/弃选
button. Mini context transfer compares its actual queue to the full current UIA
IconItem inventory, including already received synthetic peer items. It does
not mistake “original four included” for “all sent.” The parent must keep the
fixture small enough that all fixture cards are visible; it must not add/remove
items concurrently during this visual batch.

Folder reveal requires a new Explorer HWND, exactly one visible TabItem named
for the fixture folder with its actual SelectionItem pattern selected,
Shell COM `Document.Folder.Self.Path`, and
`SelectedItems` with the complete tiny.bin path. A matching caption, inactive tab,
or filename without an extension is insufficient. Actual run60 showed that the
address toolbar Name does not contain the full path; it remains diagnostic only.
Multiple visible tabs, no selection, wrong complete folder/file, or an old HWND
must fail. Only that positively verified
new window is focused and closed by actual Alt+F4; no process termination or
global Explorer close is permitted. A missing record/path/address remains a
bounded QA failure.

Every recipe records its preflight tree, step timestamps and measured target
bounds. The immediate synthetic event journal is `base/batch2-events.jsonl` so
partial evidence survives a failed recipe. Return values require manual frame
review. They do not mark the 15 groups, animations, Mac testing, physical display
FPS, or public-cloud/P95 as passed. Source-derived classes/roles still require
the normal EXE's actual UIA preflight; failure is never silently skipped.

Portable tests replace every OS/UIA/COM interface. They verify safety, selection
inputs, recipe ordering and bounded behavior only. They do not execute a native
desktop, system clipboard, installer or production cloud.
