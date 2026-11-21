"""Physical sensitivity trajectories; pure timestamps, no desktop/input hooks."""
from dataclasses import replace
import math
import unittest

from shake_detector import ShakeConfig, ShakeDetector, config_for_sensitivity


def sine_samples(amplitude=190, frequency=2.5, duration=1.4, interval=.02):
    return [(index * interval, amplitude * math.sin(2 * math.pi * frequency * index * interval), 0)
            for index in range(round(duration / interval) + 1)]


def triggers(config, samples, **kwargs):
    detector = ShakeDetector(config)
    return [when for when, x, y in samples
            if detector.update(x, y, when, button_down=True, **kwargs)]


def actual_turns(samples, until):
    """Count direction changes of the supplied physical trajectory itself."""
    direction, count = 0, 0
    for before, after in zip(samples, samples[1:]):
        if after[0] > until:
            break
        difference = after[1] - before[1]
        if abs(difference) < .001:
            continue
        sign = 1 if difference > 0 else -1
        if direction and sign != direction:
            count += 1
        direction = sign
    return count


class SensitivityTests(unittest.TestCase):
    def test_same_quick_rounded_gesture_needs_two_three_and_six_turns(self):
        samples = sine_samples()
        first = {level: triggers(config_for_sensitivity(level), samples)[0]
                 for level in (1, 50, 100)}
        self.assertEqual({level: actual_turns(samples, when) for level, when in first.items()},
                         {1: 6, 50: 3, 100: 2})
        self.assertLess(first[100] + .1, first[50])
        self.assertLess(first[50] + .4, first[1])
        print('Same 20 ms rounded trajectory first trigger:', first)

    def test_low_level_window_can_retain_all_six_valid_reversals(self):
        samples = sine_samples(duration=1.4)
        low = config_for_sensitivity(1)
        self.assertTrue(triggers(low, samples))
        # Changing only the old .85 s history limit makes six turns impossible
        # on this otherwise accepted 2.5 Hz physical gesture.
        self.assertFalse(triggers(replace(low, gesture_window=.85), samples))

    def test_acceleration_alone_separates_a_moderate_observed_gesture(self):
        # At a delayed 150 ms poll these 180 px legs have 1200 px/s peaks and
        # 16000 px/s^2 reversal acceleration. All low-level distance/speed/time
        # gates pass; its 20000 acceleration floor is the deciding condition.
        samples = [(index * .15, 180 * (index % 2), 0) for index in range(12)]
        low, middle, high = (config_for_sensitivity(level) for level in (1, 50, 100))
        self.assertFalse(triggers(low, samples))
        self.assertTrue(triggers(middle, samples))
        self.assertTrue(triggers(high, samples))
        relaxed_acceleration = replace(low, minimum_acceleration=15000)
        accepted = triggers(relaxed_acceleration, samples)
        self.assertTrue(accepted)
        self.assertEqual(actual_turns(samples, accepted[0]), 6)
        print('Same moderate trajectory:', {level: triggers(config_for_sensitivity(level), samples)[:1]
                                           for level in (1, 50, 100)})

    def test_all_sensitivity_steps_are_attainable_and_monotonic(self):
        previous = config_for_sensitivity(1)
        for value in range(1, 101):
            with self.subTest(value=value):
                config = config_for_sensitivity(value)
                self.assertTrue(triggers(config, sine_samples()))
                self.assertLessEqual(config.reversals, previous.reversals)
                self.assertLessEqual(config.minimum_acceleration, previous.minimum_acceleration)
                self.assertLessEqual(config.minimum_peak_speed, previous.minimum_peak_speed)
                self.assertGreaterEqual(config.gesture_window, (config.reversals - 1) * config.maximum_turn_gap)
                previous = config
        self.assertEqual(config_for_sensitivity(50), ShakeConfig())
        self.assertEqual(config_for_sensitivity(-1), config_for_sensitivity(1))
        self.assertEqual(config_for_sensitivity(101), config_for_sensitivity(100))

    def test_slow_drag_noise_single_turn_and_window_move_stay_rejected(self):
        slow = sine_samples(amplitude=65, frequency=.45, duration=6)
        noise = [(index * .02, 6 * (-1) ** index, 0) for index in range(100)]
        straight = [(index * .02, index * 40, 0) for index in range(100)]
        one_turn = [(index * .02, x, 0) for index, x in enumerate((0, 40, 80, 120, 80, 40, 0))]
        for value in (1, 15, 16, 35, 36, 49, 50, 79, 80, 100):
            with self.subTest(value=value):
                config = config_for_sensitivity(value)
                for samples in (slow, noise, straight, one_turn):
                    self.assertFalse(triggers(config, samples))
                self.assertFalse(triggers(config, sine_samples(), window_moving=True))
                self.assertFalse(triggers(config, sine_samples(), blocked=True))

    def test_vertical_and_diagonal_use_the_same_physical_sensitivity(self):
        samples = sine_samples()
        for value in (1, 50, 100):
            config = config_for_sensitivity(value)
            expected = triggers(config, samples)
            self.assertEqual(triggers(config, [(when, 0, x) for when, x, _ in samples]), expected)
            self.assertEqual(triggers(config, [(when, x, x) for when, x, _ in samples]), expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
