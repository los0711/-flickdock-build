# FlickDock 1.6.4 motion repair

## Reproduction

The supplied 10.5 second recording shows expanded content reaching its final
height before its animated width lands. A terminal-frame-only comparison cannot
detect this. test_morph_bounds_v164.py creates a controlled LegacyMotionShadow
subclass: QWidget drawSource bypasses the caller's painter transformation/clip.
At progress zero the old draw path paints opaque content 276–552 physical pixels
beyond the intended compact bounds at 100–200% display scaling.

## Implementation

MotionShadow now obtains sourcePixmap(NoPad) and explicitly draws that pixmap.
The same operation is used at rest and in transitions. The live card remains at
its target geometry and the native host remains at a fixed union rectangle;
only the compositor's visual rectangle and independent header controls move.
The effect's dirty region stays constant. Interrupted transitions capture the
two stored endpoints without calling QWidget.render beneath a transform.

show_overview computes the destination before show_contents; it no longer starts
an animation and immediately cancels it with jump. show_shortcut plans a single
collapse/reposition transition instead of starting two competing ones.

## Accepted drop lifecycle

Reference intake commits immediately; copy intake commits after its worker has
succeeded. Only then does the incoming stack land. A separate intake clock waits
for any folding transition and any additional copy jobs. It is not owned by an
interruptible visual animation callback. A pinned/reopened overview shows a
transparent stack overlay while preserving the selected preview. Explicit close
or clear invalidates the intake generation, callbacks, deadline and success tick.
Unpinned edge/shake/overview intakes tuck after visible landing; pin stays honored.

## Evidence and limits

test_morph_bounds_v164.py checks actual alpha silhouettes in both directions,
rapid reversals and neutral header feedback. New bounds differ by at most two
physical pixels, and no opaque content leaks below the visual shadow. Existing
terminal comparisons remain pixel identical at 100/125/150/200% scaling.

test_morph_clock_v164.py uses real QEventLoop timers, 125 mixed items and 25 large
JPEGs. It never pauses/seeks the animation. It checks multiple distinct frames,
fixed source/host geometry, no item construction during motion, picture previews,
hotkey entry and rapid reversals. Timing is stored in morph-clock-v164.json.

test_intake_entry_v164.py uses actual MIME/drop entry points and controlled copy
workers to cover dock/shake/overview, close/clear, pin, overlapping copies and a
reopened overview. All tests run offscreen with isolated profiles and native
input/registry/hotkeys mocked. These are Qt raster/event-loop measurements, not
Windows DWM display FPS measurements. No user desktop demonstration is performed.
