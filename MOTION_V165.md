# FlickDock 1.6.5: gesture entry and first-frame repair

## Evidence before changes

The installed v1.6.4 executable's SHA256 matched the shipped executable, and
the saved sensitivity was 1. No user configuration was modified. Real dialog
tests confirmed 1/100 reached SettingsStore and the active detector after Save.
However, slider drags only changed the form draft until Save. An active probe
and an already queued OLE validation survived a later sensitivity commit.
settings-gesture-v165.log records the two failing stale-probe cases.

The actual Python 3.12.14 runtime reports monotonic=GetTickCount64 with .015625s
resolution, versus perf_counter=QueryPerformanceCounter with .0000001s resolution.
poll used the former for numerical acceleration. Its quantisation could reject
consecutive valid turns or introduce false acceleration spikes. The real Qt-clock
gesture entry test exposed unstable low-sensitivity counting with that clock.

The old pipeline counted a gesture and only then waited for OLE type validation.
A delayed MIME result could dominate the visible timing at both settings. The
new pipeline first inspects MIME after one fast candidate turn, then starts the
configured, timestamped acceleration/turn detector for the validated held drag.
It cannot reveal a shelf or store a drop merely from preliminary inspection.

## Changes

- Gesture sampling uses perf_counter; idle timers retain monotonic.
- Slider/spin/reset immediately preview the real detector. Cancel restores the
  saved configuration, Save persists it. Configuration changes invalidate pending
  probes, cached type readiness and queued callbacks.
- OLE validation and the configured gesture are separate gates. Low/high settings
  require six/two effective reversals at 20000/2200 DIP/s² respectively; the full
  continuous mapping and default 50 are retained.
- A completed gesture passes measured pointer velocity to the reveal spring,
  producing a direction-dependent elastic entrance.
- Before showing a compact shelf, reserve its compact/expanded native envelope.
  Normal expansion/collapse never rebases a visible HWND. Source capture, target
  layout and compositor activation publish atomically with updates disabled.
- The overview shortcut reserves its known target before revealing its host.

## First-frame reproduction

The supplied recording shows the compact image jump left at expansion startup.
The old prepare path first moved the visible native host and only later moved
the card/bound the compositor. A controlled synchronous flush in that interval
painted the old compact frame 412px left and 12px up, followed by a bare target.
Natural offscreen events did not reproduce Windows DWM's flush; the test records
this distinction. test_expand_flash_v165.py checks both natural and controlled
paints from an actual button press/release before pausing the animation.

## Verification

test_gesture_entry_v165.py runs the production 20ms manager poll with a real Qt
clock, simulated held-pointer trajectory and real MIME gate. It verifies visible
reveal timing, delayed type delivery, coarse idle clocks, rejected empty MIME,
window moves, premature drops, stale settings and direction-dependent springs.
The source tests do not move the OS cursor, register hotkeys or edit the registry.

test_settings_gesture_v165.py drives the actual slider/spin/buttons and checks
live detection, persistence, rollback and stale OLE delivery. release_dpr_v165.py
checks 100/125/150/200% scaling, stable visible host bounds and identical terminal
pixels. These are Qt offscreen checks and controlled native-flush models; actual
Explorer OLE latency and Windows compositor FPS were not desktop-tested.
