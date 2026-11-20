"""Live edge-field renderer; the previous full-screen effect is removed.

An input-transparent, small native tool surface paints analytic gradients and
live particles. No blur filter, image sequence, GL child window or source drag
image is involved. Native OLE/Cocoa file validation lives in edge_file_drag.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import sys
import time
from types import MappingProxyType
import weakref

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from display_frames import FramePulse
from edge_magnetic_model import DEFAULT_CONFIG, MagneticConfig, MagneticField
from platform_native import keep_tool_visible_when_inactive, raise_edge_target, remove_host_window_shadow

CONFIG = MappingProxyType(asdict(DEFAULT_CONFIG))


@dataclass(frozen=True)
class EdgeTarget:
    screen: object
    area: QRect
    edge: str
    point: QPoint
    distance: float
    region: object = None
    trigger_width: float = 0

    def signature(self):
        return self.edge, self.area.getRect(), id(self.screen), self.region.getRect() if self.region is not None else None


def edge_point(area, edge, tangent):
    if edge == 'right': return QPointF(area.right() + .5, tangent)
    if edge == 'left': return QPointF(area.left() + .5, tangent)
    if edge == 'top': return QPointF(tangent, area.top() + .5)
    return QPointF(tangent, area.bottom() + .5)


def target_in_area(position, area, edge, distance, screen=None):
    """Logical desktop coordinates, including negative-origin mixed-DPI screens."""
    area = QRect(area)
    if not area.contains(position):
        return None
    if edge == 'nearest':
        edge = min(('left', 'right', 'top', 'bottom'), key=lambda e: {
            'left': position.x() - area.left(), 'right': area.right() - position.x(),
            'top': position.y() - area.top(), 'bottom': area.bottom() - position.y()}[e])
    depth = {'left': position.x() - area.left(), 'right': area.right() - position.x(),
             'top': position.y() - area.top(), 'bottom': area.bottom() - position.y()}[edge]
    if not 0 <= depth <= distance:
        return None
    tangent = position.y() if edge in ('left', 'right') else position.x()
    point = edge_point(area, edge, tangent).toPoint()
    point.setX(max(area.left(), min(area.right(), point.x())))
    point.setY(max(area.top(), min(area.bottom(), point.y())))
    return EdgeTarget(screen, area, edge, point, float(depth))


def input_band(target, distance):
    area, edge = QRect(target.area), target.edge
    span = min(int(math.ceil(distance)) + 1, area.width() if edge in ('left', 'right') else area.height())
    if edge == 'right': return QRect(area.right() - span + 1, area.top(), span, area.height())
    if edge == 'left': return QRect(area.left(), area.top(), span, area.height())
    if edge == 'top': return QRect(area.left(), area.top(), area.width(), span)
    return QRect(area.left(), area.bottom() - span + 1, area.width(), span)


def canvas_rect(target, tangent, config=DEFAULT_CONFIG, drop_depth=0):
    area = target.area
    depth = math.ceil(max(config.trigger_distance_px, config.glow_width_px, drop_depth)) + 8
    span = math.ceil(config.glow_height_px) + 8
    width = min(area.width(), depth)
    height = min(area.height(), span)
    if target.edge in ('top', 'bottom'):
        width, height = min(area.width(), span), min(area.height(), depth)
        x = min(max(area.left(), round(tangent - width / 2)), area.right() - width + 1)
        y = area.top() if target.edge == 'top' else area.bottom() - height + 1
    else:
        x = area.left() if target.edge == 'left' else area.right() - width + 1
        y = min(max(area.top(), round(tangent - height / 2)), area.bottom() - height + 1)
    return QRect(x, y, width, height).intersected(area)


class _Overlay(QWidget):
    def __init__(self, host):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self._host = weakref.ref(host)
        self.setObjectName('flickdockEdgeMagnet')
        self.setWindowTitle('FlickDock magnetic field')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAcceptDrops(False)
        keep_tool_visible_when_inactive(self)

    def paintEvent(self, event):
        host = self._host()
        if host is not None and not host.closed:
            host.paint(self)

    def hideEvent(self, event):
        host = self._host()
        if host is not None:
            host._surface_hidden()
        super().hideEvent(event)


class EdgeAbsorb(QObject):
    def __init__(self, manager=None, *, config=DEFAULT_CONFIG, seed=None, icon_cache=None):
        super().__init__()
        self._manager = weakref.ref(manager) if manager is not None else lambda: None
        self.config = config
        self.field = MagneticField(config, seed=seed)
        self.closed = False
        self.target = None
        self.point = None
        self.edge = None
        self.position_provider = None
        self.drop_artwork = None
        self._regions = None
        if manager is not None and hasattr(manager.settings,'magnetic_edges'):
            self._regions = manager.settings.magnetic_edges
        self._display_ids = {}
        from edge_native_icons import NativeIconCache
        self.icons = icon_cache or NativeIconCache(self)
        self.icons.ready.connect(self._icon_ready)
        self._pending_drop = None
        self._icon_deadline = QTimer(self);self._icon_deadline.setSingleShot(True)
        self._icon_deadline.timeout.connect(self._icon_timeout)
        self.overlay = _Overlay(self)
        self._timer = FramePulse(self.overlay)
        self._timer.timeout.connect(self._tick)
        self._last = 0.0
        self._screen = None
        self._prepare_brushes()
        app = QGuiApplication.instance()
        self._app = app
        if app is not None:
            app.aboutToQuit.connect(self.close)
            app.screenRemoved.connect(self._screen_removed)

    @property
    def state(self): return self.field.state
    @property
    def intensity(self): return self.field.intensity
    @property
    def _particles(self): return self.field.particles

    def target_at(self, position, *, distance=None):
        screen = QGuiApplication.screenAt(position)
        if screen is None and sys.platform=='darwin':
            # The Cocoa-to-Qt conversion can round the outermost fraction to
            # the first logical pixel beyond QRect's inclusive right/bottom.
            for candidate in QGuiApplication.screens():
                area=candidate.geometry()
                if area.adjusted(-1,-1,1,1).contains(position):
                    screen=candidate
                    position=QPoint(min(area.right(),max(area.left(),position.x())),
                                    min(area.bottom(),max(area.top(),position.y())))
                    break
        if screen is None:
            return None
        if self._regions is not None:
            from dataclasses import replace
            from magnetic_settings import region_rect
            candidates=[];area=screen.geometry()
            for edge,row in self._screen_edges(screen).items():
                if not row['enabled']:continue
                region=region_rect(area,edge,row)
                target=target_in_area(position,area,edge,row['width'] if distance is None else distance,screen)
                if target is None:continue
                if distance is None and not region.contains(position):continue
                tangent=position.y() if edge in ('left','right') else position.x()
                low,high=(region.top(),region.bottom()) if edge in ('left','right') else (region.left(),region.right())
                if low<=tangent<=high:candidates.append(replace(target,region=region,trigger_width=row['width']))
            return min(candidates,key=lambda target:target.distance) if candidates else None
        edge = self.config.edge_position
        manager = self._manager()
        if manager is not None:
            from display_triggers import display_values
            values = display_values(manager.settings, screen)
            if not values['enabled']:
                return None
            if edge == 'configured':edge = values['dock_edge']
        elif edge == 'configured':edge = 'right'
        return target_in_area(position, screen.geometry(), edge,
                              self.config.trigger_distance_px if distance is None else distance, screen)

    def _screen_edges(self,screen):
        from display_triggers import display_id
        from magnetic_settings import for_display
        key=self._display_ids.get(id(screen))
        if key is None:key=self._display_ids.setdefault(id(screen),display_id(screen))
        return for_display(self._regions,key)

    def input_regions(self,target):
        if self._regions is not None:
            from magnetic_settings import region_rect
            return {edge:region_rect(target.area,edge,row) for edge,row in self._screen_edges(target.screen).items() if row['enabled']}
        edges=('left','right','top','bottom') if self.config.edge_position=='nearest' else (target.edge,)
        from dataclasses import replace
        return {edge:input_band(replace(target,edge=edge),self.config.trigger_distance_px) for edge in edges}

    def configure_regions(self,value):
        from magnetic_settings import validate
        self._regions=validate(value);self._display_ids.clear()
        self._pending_drop=None;self._icon_deadline.stop();self.drop_artwork=None
        self.retreat()

    def prefetch_icons(self,urls):
        if not self.closed:
            for url in urls[:self.config.drop_stack_max_icons]:self.icons.request(url.toLocalFile())

    def _drop_images(self,urls):
        return [self.icons.request(url.toLocalFile()) for url in urls[:self.config.drop_stack_max_icons]]

    def band(self, position):
        target = self.target_at(position)
        return None if target is None else (target.edge, QPoint(target.point), target.distance)

    def armed(self, position): return self.target_at(position) is not None

    def follow(self, position):
        if self.closed:
            return None
        target = self.target_at(position)
        if target is None:
            self.retreat()
            return None
        if self.state == 'dropping' or self._pending_drop is not None:
            self.drop_artwork = None;self._pending_drop=None;self._icon_deadline.stop()
        self._follow_target(target)
        return QPoint(self.point)

    def _follow_target(self, target):
        reset = self.target is None or self.target.signature() != target.signature()
        if reset:
            # A new monitor/edge is a new coordinate space, never a flight
            # across the desktop carrying particles from the previous screen.
            self._timer.stop()
            self.field.clear()
            self.drop_artwork = None
        if target.screen is not self._screen:
            self._disconnect_screen()
            self._screen = target.screen
            if self._screen is not None and hasattr(self._screen,'geometryChanged'):
                self._screen.geometryChanged.connect(self._screen_geometry_changed)
        self.target, self.edge = target, target.edge
        side = target.edge in ('left', 'right')
        tangent = target.point.y() if side else target.point.x()
        low, high = (target.area.top(), target.area.bottom()) if side else (target.area.left(), target.area.right())
        normalized=target.distance*self.config.trigger_distance_px/target.trigger_width if target.trigger_width else target.distance
        self.field.follow(tangent, normalized, low, high, reset=reset)
        self._update_geometry()
        self._start()

    def retreat(self):
        if self.closed:
            return
        self.field.retreat()
        if self.field.running:
            self._start()

    def release(self, position=None, *, urls=()):
        if self.closed:
            return
        if position is not None:
            target = self.target_at(position)
            if target is not None:
                self._follow_target(target)
        if self.target is None:
            return
        self._pending_drop=None;self._icon_deadline.stop();self.drop_artwork=None
        images=self._drop_images(urls)
        if urls and any(image is None for image in images):
            self._pending_drop=(self.target,tuple(urls))
            self.field.drop(hold_ms=1500);self._icon_deadline.start(1500);self._start();return
        images=[image for image in images if image is not None and not image.isNull()]
        if urls and not images:
            self.field.drop();self._start();return
        self._begin_artwork(urls,images=images)

    def _begin_artwork(self,urls,image=None,images=None):
        from edge_drop_animation import DropArtwork
        self.drop_artwork = DropArtwork(self.target, urls, config=self.config,
                                       dpr=self.overlay.devicePixelRatioF(),image=image,images=images)
        self.field.target_center = self.drop_artwork.tangent
        self.field.drop(hold_ms=self.config.drop_settle_ms+self.config.drop_absorb_ms-self.config.drop_ms)
        self._update_geometry()
        self._start()

    def _icon_ready(self,ref):
        if self.closed or self._pending_drop is None:return
        target,urls=self._pending_drop
        images=self._drop_images(urls)
        if any(image is None for image in images):return
        self._pending_drop=None;self._icon_deadline.stop()
        images=[image for image in images if not image.isNull()]
        if not images:self.field.drop();return
        if self.target is None or target.signature()!=self.target.signature():return
        self._begin_artwork(urls,images=images)

    def _icon_timeout(self):
        pending=self._pending_drop
        self._pending_drop=None
        if not self.closed:
            if pending and self.target is not None and pending[0].signature()==self.target.signature():
                images=[self.icons.image(url.toLocalFile()) for url in pending[1][:self.config.drop_stack_max_icons]]
                images=[image for image in images if image is not None and not image.isNull()]
                if images:self._begin_artwork(pending[1],images=images);return
            self.field.drop()

    def _start(self):
        if not self.field.running or self.closed:
            return
        if not self.overlay.isVisible():
            self.overlay.show()
            remove_host_window_shadow(self.overlay)
            raise_edge_target(self.overlay)
        if not self._timer.isActive():
            self._last = time.perf_counter()
            self._timer.start()

    def _surface_hidden(self):
        self._timer.stop()
        self.field.clear()
        self.drop_artwork = None
        self._pending_drop=None;self._icon_deadline.stop()
        self.point = None

    def _screen_removed(self, screen):
        self._display_ids.pop(id(screen),None)
        if self.target is not None and self.target.screen is screen:
            self._surface_hidden()
            self.target = None
            self.overlay.hide()

    def _disconnect_screen(self):
        if self._screen is not None and hasattr(self._screen,'geometryChanged'):
            from shiboken6 import isValid
            if isValid(self._screen):
                self._screen.geometryChanged.disconnect(self._screen_geometry_changed)
        self._screen = None

    def _screen_geometry_changed(self, *_):
        # A display reconfiguration invalidates its old coordinate space.
        # Never let an old canvas fade across a newly adjacent monitor.
        self._surface_hidden()
        self.target = None
        self.overlay.hide()

    def _update_geometry(self):
        if self.target is None:
            return
        self.point = edge_point(self.target.area, self.edge, self.field.center).toPoint()
        depth = 0
        if self.drop_artwork is not None:
            # Allocate the complete post-drop envelope once, including the
            # initial intact page at a far-edge release. Never resize a shelf.
            depth = max(self.drop_artwork.start_depth,self.config.drop_rest_depth_px)+self.drop_artwork.config.drop_icon_size_px/2+4
        rect = canvas_rect(self.target, self.field.center, self.config, depth)
        if self.overlay.geometry() != rect:
            # Only this independent small overlay moves. The shelf's native
            # animation host and surface transaction are never touched.
            self.overlay.setGeometry(rect)

    def _tick(self):
        if self.closed or not self.overlay.isVisible():
            self._surface_hidden()
            return
        now = time.perf_counter()
        dt, self._last = max(0, now - self._last), now
        if self.state in ('active', 'approaching') and self.position_provider is not None:
            position = self.position_provider()
            target = self.target_at(position) if position is not None else None
            if target is None:
                self.field.retreat()
            else:
                self._follow_target(target)
        self.field.step(dt)
        if self.drop_artwork is not None:
            self.drop_artwork.step(dt)
            if not self.drop_artwork.running:self.drop_artwork = None
        if not self.field.running:
            self._timer.stop()
            self.overlay.hide()
            return
        self._update_geometry()
        self.overlay.update()

    def _prepare_brushes(self):
        cfg = self.config
        self._glow = QRadialGradient(QPointF(0, 0), 1.0)
        for index in range(13):
            r = index / 12
            alpha = round(cfg.glow_intensity * math.exp(-4.5 * r * r) * (1 - r * r))
            self._glow.setColorAt(r, QColor(*cfg.glow_color, alpha))
        self._core = QRadialGradient(QPointF(0, 0), 1.0)
        self._core.setColorAt(0, QColor(*cfg.glow_core_color, min(255,round(cfg.glow_intensity*1.22))))
        self._core.setColorAt(.35, QColor(*cfg.glow_core_color, round(cfg.glow_intensity*.44)))
        self._core.setColorAt(1, QColor(*cfg.glow_core_color, 0))
        self._dot = QRadialGradient(QPointF(0, 0), 1.0)
        self._dot.setColorAt(0, QColor(*cfg.particle_color, 205))
        self._dot.setColorAt(.2, QColor(*cfg.particle_color, 140))
        self._dot.setColorAt(.6, QColor(*cfg.particle_color, 35))
        self._dot.setColorAt(1, QColor(*cfg.particle_color, 0))
        self._arcs = QPainterPath()
        self._arcs.moveTo(.95, -.86)
        self._arcs.cubicTo(.64, -.8, .32, -.13, .01, 0)
        self._arcs.moveTo(.9, .79)
        self._arcs.cubicTo(.64, .75, .33, .17, .01, 0)
        self._arc_pen = QPen(QColor(*cfg.glow_color, 9), .006)
        self._trail_pen = QPen(QColor(*cfg.particle_color, 35), .65)

    def paint(self, widget):
        painter = QPainter(widget)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(widget.rect(), Qt.transparent)
        if self.target is None or self.field.alpha <= .001 and self.drop_artwork is None:
            painter.end()
            return
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.Antialiasing)
        # Clip to both the local canvas and the exact participating QScreen.
        area = QRectF(self.target.area.translated(-widget.pos()))
        painter.setClipRect(QRectF(widget.rect()).intersected(area))
        center = edge_point(self.target.area, self.edge, self.field.center) - QPointF(widget.pos())
        horizontal = self.edge in ('left', 'right')
        w, h = self.config.glow_width_px, self.config.glow_height_px / 2
        if not horizontal:
            w, h = h, w
        envelope = self.field.alpha * self.field.intensity
        painter.save()
        painter.setOpacity(envelope)
        painter.translate(center)
        painter.scale(w * self.field.contraction, h * self.field.contraction)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._glow)
        painter.drawEllipse(QRectF(-1, -1, 2, 2))
        painter.restore()
        painter.save()
        painter.setOpacity(envelope)
        painter.translate(center)
        painter.scale((9 if horizontal else h * .65) * self.field.contraction,
                      (h * .65 if horizontal else 9) * self.field.contraction)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._core)
        painter.drawEllipse(QRectF(-1, -1, 2, 2))
        painter.restore()
        painter.save()
        painter.setOpacity(envelope * .55)
        painter.translate(center)
        painter.rotate({'left': 0, 'right': 180, 'top': 90, 'bottom': -90}[self.edge])
        painter.scale(self.config.glow_width_px, self.config.glow_height_px / 2)
        painter.setPen(self._arc_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self._arcs)
        painter.restore()
        for particle in self.field.particles:
            visibility = particle.visibility * self.field.alpha
            if visibility < .005:
                continue
            position = self._particle_point(particle.depth, particle.tangent) - QPointF(widget.pos())
            previous = self._particle_point(particle.previous_depth, particle.previous_tangent) - QPointF(widget.pos())
            painter.setOpacity(visibility * .3)
            painter.setPen(self._trail_pen)
            painter.drawLine(previous, position)
            painter.save()
            painter.setOpacity(visibility)
            painter.translate(position)
            radius = particle.size * .9 * max(.15,min(1.0,particle.depth/20.0))
            painter.scale(radius, radius)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._dot)
            painter.drawEllipse(QRectF(-1, -1, 2, 2))
            painter.restore()
        if self.drop_artwork is not None:
            self.drop_artwork.paint(painter,widget.pos())
        painter.end()

    def _particle_point(self, depth, tangent):
        area = self.target.area
        if self.edge == 'right': return QPointF(area.right() + .5 - depth, tangent)
        if self.edge == 'left': return QPointF(area.left() + .5 + depth, tangent)
        if self.edge == 'top': return QPointF(tangent, area.top() + .5 + depth)
        return QPointF(tangent, area.bottom() + .5 - depth)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._icon_deadline.stop();self._pending_drop=None;self.icons.close()
        self._disconnect_screen()
        app, self._app = self._app, None
        if app is not None:
            app.aboutToQuit.disconnect(self.close)
            app.screenRemoved.disconnect(self._screen_removed)
        self.position_provider = None
        self._timer.stop()
        self.field.clear()
        self.drop_artwork = None
        self.overlay.hide()
        self.overlay.deleteLater()
