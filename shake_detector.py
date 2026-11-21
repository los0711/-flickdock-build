"""Timestamped pointer intent detection; this module never creates UI/hooks.

A shake is only a *request to inspect* a content drag.  It must still pass the
OLE MIME gate before any shelf appears.  Coordinates are logical screen pixels,
timestamps are monotonic seconds, and all input can be supplied by unit tests.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math


@dataclass(frozen=True)
class ShakeConfig:
    reversals: int = 3
    minimum_leg: float = 22.0
    minimum_peak_speed: float = 750.0
    minimum_average_speed: float = 450.0
    minimum_acceleration: float = 6500.0
    gesture_window: float = 0.85
    maximum_turn_gap: float = 0.32
    maximum_sample_gap: float = 0.24
    stationary_reset: float = 0.24
    jitter: float = 0.6
    retrigger_delay: float = 0.6


def config_for_sensitivity(value=50):
    """Map sensitivity to required turns and the acceleration of each turn.

    Low sensitivity requires six forceful reversals; high sensitivity needs
    two lighter reversals. Speed/distance floors still reject ordinary slow
    drags and pointer noise. The default keeps the established three turns.
    """
    value=max(1,min(100,int(value)))
    reversals=6 if value<=15 else 5 if value<=35 else 4 if value<50 else 3 if value<80 else 2
    if value<=50:
        effort=(50-value)/49.0
        leg=22.0+8.0*effort
        peak=750.0+300.0*effort
        average=450.0+150.0*effort
        acceleration=6500.0+13500.0*effort
    else:
        effort=(value-50)/50.0
        leg=22.0-6.0*effort
        peak=750.0-300.0*effort
        average=450.0-180.0*effort
        acceleration=6500.0-4300.0*effort
    turn_gap=.32+.02*max(0,reversals-3)
    # Six valid turns can span more than the old .85 seconds. Derive the
    # history window from the count so a low setting remains attainable.
    window=max(.85,(reversals-1)*turn_gap+.08)
    return replace(ShakeConfig(),reversals=reversals,minimum_leg=leg,
                   minimum_peak_speed=peak,minimum_average_speed=average,
                   minimum_acceleration=acceleration,maximum_turn_gap=turn_gap,
                   gesture_window=window)


class _AxisShake:
    def __init__(self, config: ShakeConfig):
        self.config = config
        self.turns = deque()
        self.reset()

    def reset(self, position: float | None = None, when: float | None = None):
        self.turns.clear()
        self.position = position
        self.when = when
        self.previous_speed = 0.0
        self.direction = 0
        self.start = self.extreme = position
        self.start_time = self.extreme_time = when
        self.peak_speed = 0.0
        self.opposite_peak = 0.0
        self.opposite_acceleration = 0.0
        self.last_motion = when

    def update(self, position: float, when: float) -> bool:
        cfg = self.config
        if self.when is None:
            self.reset(position, when)
            return False
        dt = when - self.when
        if dt <= 0.0:
            return False
        if dt > cfg.maximum_sample_gap:
            self.reset(position, when)
            return False
        previous_position, previous_time = self.position, self.when
        step = position - previous_position
        speed = step / dt
        acceleration = abs(speed - self.previous_speed) / dt
        self.position, self.when, self.previous_speed = position, when, speed
        if abs(step) < cfg.jitter:
            if when - self.last_motion >= cfg.stationary_reset:
                self.reset(position, when)
            return False
        self.last_motion = when
        direction = 1 if step > 0 else -1
        if not self.direction:
            self.direction = direction
            self.start, self.start_time = previous_position, previous_time
            self.extreme, self.extreme_time = position, when
            self.peak_speed = abs(speed)
            return False

        # Direction is confirmed only after travelling a meaningful distance
        # from the last extremum. Tiny pointer noise cannot create a reversal.
        if self.direction * (position - self.extreme) >= 0:
            self.extreme, self.extreme_time = position, when
            self.peak_speed = max(self.peak_speed, abs(speed))
            self.opposite_peak = self.opposite_acceleration = 0.0
            return False

        if direction != self.direction:
            self.opposite_peak = max(self.opposite_peak, abs(speed))
            self.opposite_acceleration = max(self.opposite_acceleration, acceleration)
        distance = abs(position - self.extreme)
        if distance < cfg.minimum_leg:
            return False

        old_distance = abs(self.extreme - self.start)
        old_duration = max(self.extreme_time - self.start_time, 0.001)
        new_duration = max(when - self.extreme_time, 0.001)
        fast_turn = (
            old_distance >= cfg.minimum_leg
            and old_distance / old_duration >= cfg.minimum_average_speed
            and self.peak_speed >= cfg.minimum_peak_speed
            and distance / new_duration >= cfg.minimum_average_speed
            and self.opposite_peak >= cfg.minimum_peak_speed
            and self.opposite_acceleration >= cfg.minimum_acceleration
        )
        if fast_turn:
            if self.turns and when - self.turns[-1] > cfg.maximum_turn_gap:
                self.turns.clear()
            self.turns.append(when)
            while self.turns and when - self.turns[0] > cfg.gesture_window:
                self.turns.popleft()
        else:
            self.turns.clear()

        self.start, self.start_time = self.extreme, self.extreme_time
        self.extreme, self.extreme_time = position, when
        self.direction *= -1
        self.peak_speed = self.opposite_peak
        self.opposite_peak = self.opposite_acceleration = 0.0
        return len(self.turns) >= cfg.reversals


class ShakeDetector:
    """Recognise several quick wrist reversals while the left button is held.

    update() returning True authorises ContentDragGate.arm(), never opening UI.
    Window moving/internal drags/presses begun inside a shelf can be blocked by
    the caller without depending on cursor icons, capture software or hooks.
    Both horizontal and vertical shakes work; diagonal shakes need no special
    angle mode. Speed uses actual timestamps, so timer jitter is not treated as
    a constant frame interval.
    """
    def __init__(self, config: ShakeConfig | None = None):
        self.config = config or ShakeConfig()
        self._x, self._y = _AxisShake(self.config), _AxisShake(self.config)
        self._button_down = False
        self._last_time = None
        self._last_trigger = -math.inf
        self._previous_point=None
        self.trigger_velocity=(0.0,0.0)

    def reset(self):
        self._x.reset()
        self._y.reset()
        self._button_down = False
        self._last_time = None
        self._last_trigger = -math.inf
        self._previous_point=None
        self.trigger_velocity=(0.0,0.0)

    def update(self, x: float, y: float, when: float, *, button_down: bool,
               window_moving: bool = False, blocked: bool = False) -> bool:
        if not all(math.isfinite(value) for value in (x, y, when)):
            self.reset()
            return False
        if not button_down:
            self.reset()
            return False
        velocity=(0.0,0.0)
        if self._previous_point is not None:
            px,py,pt=self._previous_point
            if 0<when-pt<=self.config.maximum_sample_gap:
                velocity=((x-px)/(when-pt),(y-py)/(when-pt))
        self._previous_point=(x,y,when)
        if window_moving or blocked:
            self._x.reset(x, when)
            self._y.reset(y, when)
            self._button_down = True
            self._last_time = when
            return False
        if not self._button_down:
            self._x.reset(x, when)
            self._y.reset(y, when)
            self._button_down = True
            self._last_time = when
            return False
        if self._last_time is not None and when <= self._last_time:
            return False
        self._last_time = when
        detected_x = self._x.update(x, when)
        detected_y = self._y.update(y, when)
        if ((detected_x or detected_y)
                and when - self._last_trigger >= self.config.retrigger_delay):
            self._last_trigger = when
            self.trigger_velocity=velocity
            self._x.reset(x, when)
            self._y.reset(y, when)
            return True
        return False

    def intent_recent(self, when: float, maximum_age: float = 1.2) -> bool:
        return self._button_down and 0.0 <= when - self._last_trigger <= maximum_age


@dataclass(frozen=True)
class PressTransition:
    pressed: bool
    released: bool
    outside_press: bool
    foreground_changed: bool
    foreground_outside: bool


class MousePressTracker:
    """Pure edge detector for promptly dismissing a shelf on an outside click.

    Sample GetAsyncKeyState(VK_LBUTTON) and GetForegroundWindow in the main
    poll, then pass their values here. The geometry result stays authoritative:
    a nonactivating shelf may remain over another app's foreground window.
    Foreground changes are supplied separately for optional dismissal policy.
    """
    def __init__(self):
        self.down = False
        self.foreground = 0

    def reset(self, button_down: bool = False, foreground_window: int = 0):
        self.down = bool(button_down)
        self.foreground = int(foreground_window or 0)

    def update(self, button_down: bool, foreground_window: int, *,
               inside_surface: bool, owned_windows=()) -> PressTransition:
        foreground = int(foreground_window or 0)
        down = bool(button_down)
        result = PressTransition(
            pressed=down and not self.down,
            released=not down and self.down,
            outside_press=down and not self.down and not inside_surface,
            foreground_changed=bool(foreground and self.foreground and foreground != self.foreground),
            foreground_outside=bool(foreground and foreground not in owned_windows),
        )
        self.down, self.foreground = down, foreground
        return result
