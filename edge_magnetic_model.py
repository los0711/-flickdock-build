"""Small, time-based edge field simulation, independent of Qt and native input.

Coordinates are (inward distance from the edge, global tangent position). They
never mix desktop coordinates with widget coordinates. Only the presentation
adapter may start this model, after native file MIME validation.
"""
from __future__ import annotations

from dataclasses import dataclass,replace
import math
import random


@dataclass(frozen=True)
class MagneticConfig:
    trigger_distance_px: int = 160
    prearm_distance_px: int = 300
    glow_width_px: int = 120
    glow_height_px: int = 300
    glow_intensity: int = 110
    glow_color: tuple[int, int, int] = (120, 148, 255)
    glow_core_color: tuple[int, int, int] = (166, 207, 255)
    particle_count: int = 28
    particle_speed_px_s: float = 78.0
    attraction_strength: float = 2.1
    cursor_speed_gain: float = 1.4
    particle_size_px: float = 2.0
    particle_color: tuple[int, int, int] = (203, 222, 255)
    particle_births_per_second: float = 48.0
    fade_in_ms: float = 160.0
    fade_out_ms: float = 250.0
    drop_ms: float = 140.0
    drop_settle_ms: float = 105.0
    drop_absorb_ms: float = 335.0
    drop_icon_size_px: int = 64
    drop_rest_depth_px: int = 84
    drop_mesh_strips: int = 96
    drop_stack_max_icons: int = 4
    drop_stack_offset_px: int = 22
    drop_stack_angle_degrees: float = 4.0
    drop_min_alpha: float = .6
    drop_min_intensity: float = .6
    center_damping: float = 27.0
    edge_position: str = 'nearest'
    release_grace_ms: int = 180

    def __post_init__(self):
        limits = {'trigger_distance_px': (40, 240), 'prearm_distance_px': (120, 480), 'glow_width_px': (30, 180),
                  'glow_height_px': (80, 400), 'glow_intensity': (0, 130),
                  'particle_count': (0, 40), 'particle_speed_px_s': (10, 240),
                  'attraction_strength': (0, 4), 'cursor_speed_gain': (0,3), 'particle_size_px': (.5, 3),
                  'particle_births_per_second': (1, 60), 'fade_in_ms': (40, 500),
                  'fade_out_ms': (100, 400), 'drop_ms': (50, 250),
                  'center_damping': (5, 80), 'release_grace_ms': (0, 250),
                  'drop_settle_ms': (60, 180), 'drop_absorb_ms': (180, 450),
                  'drop_icon_size_px': (32, 96), 'drop_rest_depth_px': (48, 140),
                  'drop_mesh_strips': (32, 144), 'drop_min_alpha': (0,1), 'drop_min_intensity': (0,1),
                  'drop_stack_max_icons': (1,4), 'drop_stack_offset_px': (4,28), 'drop_stack_angle_degrees': (0,6)}
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Invalid magnetic configuration: ' + name)
        if type(self.particle_count) is not int:
            raise ValueError('particle_count must be an integer')
        if type(self.drop_mesh_strips) is not int:
            raise ValueError('drop_mesh_strips must be an integer')
        if type(self.drop_stack_max_icons) is not int:
            raise ValueError('drop_stack_max_icons must be an integer')
        if self.prearm_distance_px < self.trigger_distance_px or self.drop_rest_depth_px < self.drop_icon_size_px/2+8:
            raise ValueError('Invalid magnetic preparation / resting distance')
        for color in (self.glow_color, self.glow_core_color, self.particle_color):
            if len(color) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in color):
                raise ValueError('Invalid magnetic color')
        if self.edge_position not in ('configured', 'left', 'right', 'top', 'bottom', 'nearest'):
            raise ValueError('Invalid magnetic edge')


DEFAULT_CONFIG = MagneticConfig()
# Desktop art direction is injected at the composition root. The general
# renderer's reusable base geometry remains independent of the input regions.
DESKTOP_MAGNETIC_CONFIG=replace(DEFAULT_CONFIG,glow_width_px=150,glow_height_px=360,glow_intensity=128)


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def smoothstep(value):
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def ease_out(value):
    return 1.0 - (1.0 - clamp(value)) ** 3


@dataclass(slots=True)
class Particle:
    depth: float
    tangent: float
    vx: float
    vy: float
    age: float
    life: float
    size: float
    bend: float
    previous_depth: float
    previous_tangent: float

    @property
    def visibility(self):
        entrance = smoothstep(self.age / .12)
        near_edge = smoothstep(self.depth / 17.0)
        end = smoothstep((self.life - self.age) / .24)
        return entrance * near_edge * end


class MagneticField:
    def __init__(self, config=DEFAULT_CONFIG, *, seed=None):
        self.config = config
        self.random = random.Random(seed)
        self.state = 'idle'
        self.center = self.target_center = 0.0
        self.tangent_min = self.tangent_max = 0.0
        self.distance = float(config.trigger_distance_px)
        self.intensity = self.alpha = 0.0
        self.contraction = 1.0
        self.particles: list[Particle] = []
        self._elapsed = 0.0
        self._from_alpha = 0.0
        self._emission = 0.0
        self._physics_time = 0.0
        self._drop_alpha = 0.0
        self._drop_wait = 0.0

    @property
    def running(self):
        return self.state != 'idle'

    def follow(self, center, distance, tangent_min, tangent_max, *, reset=False):
        if reset or not self.running:
            self.center = float(center)
            self.particles.clear()
            self._emission = self._physics_time = 0.0
        self.target_center = clamp(float(center), tangent_min, tangent_max)
        self.tangent_min, self.tangent_max = float(tangent_min), float(tangent_max)
        self.distance = clamp(float(distance), 0.0, self.config.trigger_distance_px)
        desired = 'active' if self.distance <= self.config.trigger_distance_px * .52 else 'approaching'
        if self.state not in ('active', 'approaching'):
            self._elapsed = 0.0
            self._from_alpha = self.alpha
        self.state = desired
        self.contraction = 1.0

    def retreat(self):
        if self.state in ('idle', 'exiting'):
            return
        self.state = 'exiting'
        self._elapsed = 0.0
        self._from_alpha = self.alpha
        self._emission = 0.0

    def drop(self, hold_ms=0.0):
        if not self.running:
            return
        self.state = 'dropping'
        self._elapsed = 0.0
        self._from_alpha = self.alpha
        self._drop_alpha = max(self.alpha, self.config.drop_min_alpha)
        self._drop_wait = max(0.0, float(hold_ms)) / 1000.0
        self._emission = 0.0

    def clear(self):
        self.state = 'idle'
        self.alpha = self.intensity = 0.0
        self.contraction = 1.0
        self.particles.clear()
        self._elapsed = self._emission = 0.0

    def step(self, dt):
        if not self.running:
            return
        dt = max(0.0, float(dt))
        self._elapsed += dt
        self.center += (self.target_center - self.center) * -math.expm1(-self.config.center_damping * dt)
        if self.state == 'exiting':
            self.alpha = self._from_alpha * (1.0 - smoothstep(self._elapsed * 1000.0 / self.config.fade_out_ms))
            if self._elapsed * 1000.0 >= self.config.fade_out_ms:
                self.clear()
                return
        elif self.state == 'dropping':
            p = ease_out(max(0.0,self._elapsed-self._drop_wait) * 1000.0 / self.config.drop_ms)
            self.contraction = 1.0 - .19 * p
            rise = ease_out(self._elapsed * 1000.0 / self.config.drop_ms)
            self.alpha = self._from_alpha + (self._drop_alpha - self._from_alpha) * rise
            self.intensity += (max(self.intensity, self.config.drop_min_intensity) - self.intensity) * -math.expm1(-18 * dt)
            if (self._elapsed-self._drop_wait) * 1000.0 >= self.config.drop_ms:
                remainder = self._elapsed - self._drop_wait - self.config.drop_ms / 1000.0
                self.retreat()
                self._elapsed = remainder
                self.alpha = self._from_alpha * (1.0 - smoothstep(remainder * 1000 / self.config.fade_out_ms))
                if remainder * 1000 >= self.config.fade_out_ms:
                    self.clear()
                    return
        else:
            self.alpha = self._from_alpha + (1 - self._from_alpha) * ease_out(self._elapsed * 1000.0 / self.config.fade_in_ms)
            proximity = smoothstep(1.0 - self.distance / self.config.trigger_distance_px)
            target = .1 + .9 * proximity
            self.intensity += (target - self.intensity) * -math.expm1(-18 * dt)
        # Fixed small integration steps bound acceleration on a delayed frame.
        # Fade deadlines still use actual elapsed time above, so a busy app
        # never leaves a stranded, invisible overlay after an expired exit.
        remaining = min(dt, .075)
        while remaining > 1e-8:
            step = min(remaining, 1 / 120)
            self._simulate(step)
            remaining -= step

    def _simulate(self, dt):
        cfg = self.config
        self._physics_time += dt
        if self.state in ('approaching', 'active'):
            ratio = 1.0 - self.distance / cfg.trigger_distance_px
            limit = min(cfg.particle_count, max(3, round(cfg.particle_count * (.45 + .55 * ratio))))
            self._emission = min(2.0, self._emission + dt * cfg.particle_births_per_second)
            while len(self.particles) < limit and self._emission >= 1.0:
                self._emission -= 1.0
                self._birth()
        cursor_gain=1+cfg.cursor_speed_gain*smoothstep(1-self.distance/cfg.trigger_distance_px)
        keep = []
        for particle in self.particles:
            particle.previous_depth, particle.previous_tangent = particle.depth, particle.tangent
            dx, dy = -particle.depth, self.center - particle.tangent
            distance = max(.1, math.hypot(dx, dy))
            closeness = 1.0 - clamp(distance / cfg.trigger_distance_px)
            speed = cfg.particle_speed_px_s * (1 + cfg.attraction_strength * closeness)*cursor_gain
            if self.state == 'dropping':
                speed *= 2.2
            bend = particle.bend * .1 * speed * math.sin(self._physics_time * 1.3 + particle.bend)
            desired_x = dx / distance * speed - dy / distance * bend
            desired_y = dy / distance * speed + dx / distance * bend
            blend = -math.expm1(-10.0 * dt)
            particle.vx += (desired_x - particle.vx) * blend
            particle.vy += (desired_y - particle.vy) * blend
            particle.depth += particle.vx * dt
            particle.tangent += particle.vy * dt
            particle.age += dt
            if particle.depth > 1.5 and particle.age < particle.life:
                keep.append(particle)
        self.particles = keep

    def _birth(self):
        cfg, rng = self.config, self.random
        depth = rng.uniform(28.0, cfg.trigger_distance_px * .94)
        tangent = clamp(self.center + rng.gauss(0, cfg.glow_height_px * .24), self.tangent_min, self.tangent_max)
        speed = cfg.particle_speed_px_s * rng.uniform(.22, .45)
        self.particles.append(Particle(depth, tangent, -speed, 0, 0, rng.uniform(1.25, 2.3),
            clamp(cfg.particle_size_px * rng.uniform(.55, 1.3), .7, 3), rng.uniform(-1, 1), depth, tangent))
