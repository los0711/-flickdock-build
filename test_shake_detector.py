"""Deterministic gesture and outside-click regressions; no OS input calls."""
import math
import os
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
isolated_state = tempfile.TemporaryDirectory(prefix="flickdock-shake-test-")
os.environ["APPDATA"] = isolated_state.name

from shake_detector import MousePressTracker, ShakeConfig, ShakeDetector


def feed(detector, samples, **kwargs):
    return [when for when, x, y in samples
            if detector.update(x, y, when, button_down=True, **kwargs)]


def wrist_samples(axis="x"):
    values = [0, 15, 40, 70, 45, 15, -15, 15, 45, 75, 45, 15, -15]
    return [(i * .025, value if axis == "x" else 0,
             value if axis == "y" else 0) for i, value in enumerate(values)]


def triangle_samples(intervals, duration=1.1, amplitude=65, frequency=3.0):
    samples, when, index = [], 0.0, 0
    while when <= duration:
        x = amplitude * 2 / math.pi * math.asin(math.sin(2 * math.pi * frequency * when))
        samples.append((when, x, 0))
        when += intervals[index % len(intervals)]
        index += 1
    return samples


class ShakeTests(unittest.TestCase):
    def test_default_thresholds_require_deliberately_fast_shaking(self):
        config = ShakeConfig()
        self.assertEqual(config.reversals, 3)
        self.assertEqual(config.minimum_peak_speed, 750.0)
        self.assertEqual(config.minimum_average_speed, 450.0)
        self.assertEqual(config.minimum_acceleration, 6500.0)

    def test_quick_wrist_three_reversals(self):
        detector = ShakeDetector()
        triggers = feed(detector, wrist_samples())
        self.assertEqual(len(triggers), 1)
        self.assertGreaterEqual(triggers[0], .25)
        self.assertLessEqual(triggers[0], .3)
        self.assertTrue(detector.intent_recent(.5))
        self.assertFalse(detector.intent_recent(2.0))

    def test_vertical_and_diagonal(self):
        self.assertEqual(len(feed(ShakeDetector(), wrist_samples("y"))), 1)
        diagonal = [(t, x, x * .7) for t, x, _ in wrist_samples()]
        self.assertEqual(len(feed(ShakeDetector(), diagonal)), 1)

    def test_two_reversals_are_not_enough(self):
        self.assertFalse(feed(ShakeDetector(), wrist_samples()[:10]))

    def test_slow_repeated_drags_never_trigger(self):
        samples = triangle_samples([.025], duration=6, amplitude=65, frequency=.45)
        self.assertFalse(feed(ShakeDetector(), samples))

    def test_medium_reversals_lack_speed(self):
        samples = triangle_samples([.025], duration=3, amplitude=42, frequency=2)
        self.assertFalse(feed(ShakeDetector(), samples))

    def test_former_medium_shake_is_rejected_by_new_defaults(self):
        # This 600 px/s triangle passed the old defaults. The new 750 px/s
        # peak threshold must reject it even though its turns are deliberate.
        samples = triangle_samples([.025], duration=1.2, amplitude=50, frequency=3)
        peak = max(abs((right[1] - left[1]) / (right[0] - left[0]))
                   for left, right in zip(samples, samples[1:]))
        self.assertGreaterEqual(peak, 500)
        self.assertLess(peak, 750)
        old = ShakeConfig(minimum_peak_speed=500.0,
                          minimum_average_speed=300.0,
                          minimum_acceleration=4000.0)
        self.assertTrue(feed(ShakeDetector(old), samples))
        self.assertFalse(feed(ShakeDetector(), samples))

    def test_genuinely_fast_triangle_is_accepted(self):
        # 90 px amplitude at 3 Hz gives 1080 px/s on each straight leg.
        samples = triangle_samples([.025], duration=1.2, amplitude=90, frequency=3)
        self.assertTrue(feed(ShakeDetector(), samples))

    def test_fast_straight_drag_and_jitter_do_not_trigger(self):
        self.assertFalse(feed(ShakeDetector(), [(i * .025, i * 30, 0) for i in range(50)]))
        self.assertFalse(feed(ShakeDetector(), [(i * .025, (-1) ** i * 6, 0) for i in range(50)]))

    def test_windows_moves_and_internal_drags_cannot_trigger(self):
        self.assertFalse(feed(ShakeDetector(), wrist_samples(), window_moving=True))
        self.assertFalse(feed(ShakeDetector(), wrist_samples(), blocked=True))

    def test_recording_timer_jitter_uses_timestamps(self):
        # Keep the physical trajectory genuinely fast when timers skip frames;
        # a borderline 780 px/s trajectory can legitimately be under-sampled.
        schedules = {
            "normal": [.025],
            "recording_jitter": [.015, .030, .025, .060, .018, .045, .028],
            "110ms_delay": [.025, .025, .11, .025, .035, .075],
            "120ms_delay": [.02, .12, .02, .05],
        }
        for name, intervals in schedules.items():
            with self.subTest(schedule=name):
                samples = triangle_samples(intervals, duration=1.2, amplitude=90, frequency=3)
                self.assertTrue(feed(ShakeDetector(), samples))

    def test_very_fast_wide_shake_is_not_limited_to_96_pixels(self):
        samples = [(i * .05, value, 0) for i, value in enumerate([0, 100, -100, 100, -100])]
        self.assertEqual(len(feed(ShakeDetector(), samples)), 1)

    def test_pause_and_release_reset_reversal_history(self):
        detector = ShakeDetector()
        feed(detector, wrist_samples()[:10])
        detector.update(75, 0, .7, button_down=True)
        self.assertFalse(feed(detector, [(t + .725, x + 75, y) for t, x, y in wrist_samples()[:10]]))
        detector.update(0, 0, 1.2, button_down=False)
        self.assertFalse(feed(detector, [(t + 1.3, x, y) for t, x, y in wrist_samples()[:10]]))

    def test_wait_before_drag_does_not_dilute_leg_velocity(self):
        detector = ShakeDetector()
        feed(detector, [(i * .025, 0, 0) for i in range(40)])
        self.assertTrue(feed(detector, [(t + 1.0, x, y) for t, x, y in wrist_samples()]))

    def test_stale_invalid_and_duplicate_samples(self):
        detector = ShakeDetector()
        self.assertFalse(detector.update(0, 0, 0, button_down=True))
        self.assertFalse(detector.update(100, 0, 0, button_down=True))
        self.assertFalse(detector.update(math.nan, 0, .1, button_down=True))
        self.assertFalse(detector.update(1000, 0, 3, button_down=True))


class OutsidePressTests(unittest.TestCase):
    def test_single_outside_down_edge(self):
        tracker = MousePressTracker()
        first = tracker.update(True, 900, inside_surface=False, owned_windows={100})
        self.assertTrue(first.pressed and first.outside_press and first.foreground_outside)
        held = tracker.update(True, 900, inside_surface=False)
        self.assertFalse(held.pressed or held.outside_press)
        released = tracker.update(False, 900, inside_surface=False)
        self.assertTrue(released.released)

    def test_nonactivating_shelf_uses_geometry(self):
        tracker = MousePressTracker()
        event = tracker.update(True, 900, inside_surface=True, owned_windows={100})
        self.assertTrue(event.pressed and event.foreground_outside)
        self.assertFalse(event.outside_press)

    def test_foreground_switch_separate_from_mouse_press(self):
        tracker = MousePressTracker()
        tracker.update(False, 100, inside_surface=False, owned_windows={100})
        event = tracker.update(False, 900, inside_surface=False, owned_windows={100})
        self.assertTrue(event.foreground_changed and event.foreground_outside)
        self.assertFalse(event.outside_press)


if __name__ == "__main__":
    unittest.main()
