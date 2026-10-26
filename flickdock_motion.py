"""Small, interruptible physical feedback primitives for FlickDock's Qt UI."""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap, QTextLayout, QTextOption, QTransform
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsEffect, QPushButton, QWidget

from flickdock_icons import document_pixmap, draw_glyph
from surface_morph import paint_surface_shadow, surface_clip
import ui_theme


class Spring(QObject):
    """Retargetable spring channels. Reversals retain their current velocity.

    ``response`` is the natural period in seconds, not an artificial wait.
    ``animate`` replaces the completion callback and updates only named targets.
    """
    settled=Signal()

    def __init__(self, parent: QObject | None, values: dict[str, float],
                 on_update: Callable[[dict[str, float]], None], motion: bool = True):
        super().__init__(parent)
        self.values = {k: float(v) for k, v in values.items()}
        self._targets = dict(self.values)
        self._velocities = {k: 0.0 for k in values}
        self._on_update = on_update
        self.motion = motion
        self.response = .24
        self.damping = .70
        self._finished: Callable[[], None] | None = None
        self._last_tick = time.perf_counter()
        self._started_at = self._last_tick
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def animate(self, target: dict[str, float], finished: Callable[[], None] | None = None,
                response: float = .24, damping: float = .70) -> None:
        for key, value in target.items():
            self.values.setdefault(key, float(value))
            self._velocities.setdefault(key, 0.0)
            self._targets[key] = float(value)
        self.response = max(.06, float(response)) if self.motion else .13
        self.damping = max(.25, float(damping)) if self.motion else 1.0
        self._finished = finished
        self._started_at = time.perf_counter()
        self._start()

    def jump(self, values: dict[str, float]) -> None:
        self.stop()
        self.values.update({k: float(v) for k, v in values.items()})
        self._targets = dict(self.values)
        self._velocities = {k: 0.0 for k in self.values}
        self._on_update(dict(self.values))

    def kick(self, velocities: dict[str, float]) -> None:
        if self.motion:
            for key, velocity in velocities.items():
                self.values.setdefault(key, 0.0)
                self._targets.setdefault(key, self.values[key])
                self._velocities[key] = self._velocities.get(key, 0.0) + float(velocity)
        self._start()

    def stop(self) -> None:
        self._timer.stop()
        self._finished = None

    def _start(self) -> None:
        if not self._timer.isActive():
            self._last_tick = time.perf_counter()
            self._timer.start()

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(.048, max(.001, now - self._last_tick))
        self._last_tick = now
        omega = 2 * math.pi / self.response
        # Substeps keep a delayed paint or a busy disk from destabilising motion.
        substeps = max(1, math.ceil(dt / .004))
        h = dt / substeps
        for _ in range(substeps):
            for key, target in self._targets.items():
                value = self.values[key]
                velocity = self._velocities[key]
                acceleration = omega * omega * (target - value) - 2 * self.damping * omega * velocity
                velocity += acceleration * h
                self.values[key] = value + velocity * h
                self._velocities[key] = velocity
        pixels = {"x", "y", "h", "w", "dx", "dy", "width", "height"}
        settled = all(
            abs(self.values[k] - target) < (.35 if k in pixels else .0008)
            and abs(self._velocities[k]) < (3.0 if k in pixels else .014)
            for k, target in self._targets.items()
        ) or now - self._started_at >= .65
        if settled:
            self.values.update(self._targets)
            self._velocities = {k: 0.0 for k in self.values}
            self._timer.stop()
        callback=None
        if settled:callback,self._finished=self._finished,None
        self._on_update(dict(self.values))
        if settled:self.settled.emit()
        if callback:callback()


class MotionShadow(QGraphicsDropShadowEffect):
    """One persistent soft shadow that also transforms its live widget source.

    The widget's hit bounds stay stable. Only its painted presentation moves,
    so pointer interaction and layout don't jitter as the surface springs.
    """
    def __init__(self, widget: QWidget, alpha: int = 45):
        super().__init__(widget)
        self.sx = self.sy = 1.0
        self.dx = self.dy = 0.0
        self._shadow_cache=OrderedDict()
        self._surface_snapshot=None
        self.setBlurRadius(28)
        self.setOffset(0, 7)
        self.setColor(QColor(18, 31, 53, alpha))

    def set_motion(self, sx: float = 1, sy: float = 1, dx: float = 0, dy: float = 0) -> None:
        self.sx = max(.05, min(1.3, float(sx)))
        self.sy = max(.05, min(1.3, float(sy)))
        self.dx, self.dy = float(dx), float(dy)
        self.updateBoundingRect()
        self.update()

    def begin_surface_morph(self,snapshot) -> None:
        """Keep this effect as the sole compositor throughout the resize."""
        self._surface_snapshot=snapshot
        self.updateBoundingRect()
        self.update()

    def end_surface_morph(self) -> None:
        self._surface_snapshot=None
        self.updateBoundingRect()
        self.update()

    def refresh_surface_morph(self) -> None:
        if self._surface_snapshot is not None:
            self.update()

    def _surface_rect(self,snapshot):
        widget=self.parent()
        return snapshot.content_rect().translated(-widget.x(),-widget.y())

    def boundingRectFor(self, rect: QRectF) -> QRectF:
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is not None and snapshot.active:
            # A fixed dirty region for the entire transition avoids backing
            # store reallocations while the visual rectangle changes inside it.
            widget=self.parent()
            region=snapshot.content_rect(snapshot.transition_bounds).translated(-widget.x(),-widget.y())
            rect=rect.united(region)
        bounds = super().boundingRectFor(rect)
        if not hasattr(self, "sx"):
            return bounds
        center = rect.center()
        t = QTransform()
        t.translate(center.x() + self.dx, center.y() + self.dy)
        t.scale(self.sx, self.sy)
        t.translate(-center.x(), -center.y())
        return bounds.united(t.mapRect(bounds)).adjusted(-2, -2, 2, 2)

    def draw(self, painter: QPainter) -> None:
        bounds = self.sourceBoundingRect(Qt.CoordinateSystem.LogicalCoordinates)
        # QWidget drawSource() can restore its own painter state and bypass
        # the caller's scale/clip. Always composite its cached pixmap ourselves,
        # at rest as well as in motion, so intermediate and final frames share
        # exactly one transform-respecting path.
        offset=QPoint()
        source=self.sourcePixmap(Qt.CoordinateSystem.LogicalCoordinates,offset,
                                 QGraphicsEffect.PixmapPadMode.NoPad)
        def draw_live(target):
            target.drawPixmap(QPointF(offset),source)
        snapshot=self._surface_snapshot
        active=snapshot is not None and snapshot.active
        rect=self._surface_rect(snapshot) if active else bounds
        center = rect.center()
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(center.x() + self.dx, center.y() + self.dy)
        painter.scale(self.sx, self.sy)
        painter.translate(-center.x(), -center.y())
        paint_surface_shadow(painter,rect,self._shadow_cache,self.blurRadius(),self.offset(),self.color())
        if active:
            snapshot.paint_frame(painter,rect,live_draw=draw_live,target_size=bounds.size())
        else:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipPath(surface_clip(rect))
            draw_live(painter)
        painter.restore()


class FeedbackButton(QPushButton):
    """Painted icon button with pointer-down feedback and a soft release."""
    _NAMES = {"more": "更多操作", "close": "移除", "check": "已复制", "plus": "添加文件",
              "pin": "永久区域", "temporary": "临时区域", "trash": "移除",
              "copy": "复制", "chevron": "展开", "shelf": "文件搁架",
              "expand": "展开详情", "collapse": "收起详情",
              "chevron-up": "收起", "chevron-down": "展开",
              "dock": "收起到屏幕边缘", "hide-to-edge": "收起到屏幕边缘"}

    def __init__(self, glyph: str, parent: QWidget | None = None, motion: bool = True):
        super().__init__(parent)
        self.glyph = glyph
        self.motion = motion
        self._hovered = False
        self._pressed = False
        self._keyboard_focus = False
        self._presentation_scale=(1.0,1.0)
        self.setFixedSize(36, 36)
        self.setAutoFillBackground(False)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.setToolTip(self._NAMES.get(glyph, glyph))
        self.setAccessibleName(self._NAMES.get(glyph, glyph))
        self.spring = Spring(self, {"scale": 1, "hover": 0, "down": 0},
                             lambda values: self.update(), motion)

    def set_glyph(self, name: str) -> None:
        self.glyph = name
        self.setToolTip(self._NAMES.get(name, name))
        self.setAccessibleName(self._NAMES.get(name, name))
        self.update()

    def set_presentation_scale(self,sx=1.0,sy=1.0):
        self._presentation_scale=(sx,sy)
        self.update()

    def _retarget(self) -> None:
        self.spring.animate({"scale": .89 if self._pressed and self.motion else 1,
                             "hover": float(self._hovered), "down": float(self._pressed)},
                            response=.16 if self._pressed else .23,
                            damping=1 if self._pressed else .66)

    def enterEvent(self, event):
        self._hovered = True
        self._retarget()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self._retarget()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._keyboard_focus = False
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._pressed = True
            self._retarget()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self._retarget()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._keyboard_focus = True
            self._pressed = True
            self._retarget()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        self._pressed = False
        self._retarget()
        super().keyReleaseEvent(event)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._keyboard_focus = False
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        v = self.spring.values
        c = self.rect().center()
        p.translate(c.x(), c.y())
        p.scale(v["scale"]*self._presentation_scale[0], v["scale"]*self._presentation_scale[1])
        p.translate(-c.x(), -c.y())
        bg = QColor(ui_theme.color("button"))
        bg.setAlphaF(min(1, .72 + .20 * v["hover"] + .08 * v["down"]))
        p.setBrush(bg)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.8, .8, -.8, -.8), self.height() / 2, self.height() / 2)
        if self.hasFocus() and self._keyboard_focus:
            focus = QColor(ui_theme.color("accent"))
            focus.setAlpha(180)
            p.setPen(QPen(focus, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.6, 1.6, -1.6, -1.6), self.height() / 2, self.height() / 2)
        glyph_size = min(24, self.width() - 8)*(.82 if self.glyph=='copy' else .875)
        if not self.isEnabled():
            p.setOpacity(.45)
        draw_glyph(p, self.glyph, QRectF((self.width()-glyph_size)/2, (self.height()-glyph_size)/2,
                                       glyph_size, glyph_size),
                   ui_theme.color("icon") if self.isEnabled() else ui_theme.color("secondary"), 1.8)
        p.end()


class ContentTile(QWidget):
    """Self-contained image/text/file preview with intake and press feedback.

    Content replacement clears both text and pixmap state, preventing old drag
    images from remaining as an orphaned label. No effect objects are replaced.
    """
    def __init__(self, parent: QWidget | None = None, motion: bool = True):
        super().__init__(parent)
        self.motion = motion
        self._kind = "empty"
        self._pixmap = QPixmap()
        self._text = ""
        self._hovered = self._pressed = False
        self._pulse_epoch = 0
        self._pulse_active = False
        self._pulse_completion: Callable[[], None] | None = None
        self._pulse_finished: Callable[[], None] | None = None
        self._pulse_motion_done=False
        self._pulse_success=False
        self._pulse_deadline=QTimer(self)
        self._pulse_deadline.setSingleShot(True)
        self._pulse_deadline.timeout.connect(self._expire_pulse)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.spring = Spring(self, {"sx": 1, "sy": 1, "y": 0, "hover": 0, "check": 0},
                             lambda values: self.update(), motion)
        self.spring.settled.connect(self._motion_settled)

    def set_content(self, kind: str, pixmap: QPixmap | None = None, text: str = "") -> None:
        self._kind = kind
        self._pixmap = QPixmap(pixmap) if pixmap is not None and not pixmap.isNull() else QPixmap()
        self._text = str(text)
        self.update()

    def set_hover(self, value: bool) -> None:
        self._hovered = bool(value)
        self._retarget()

    def set_pressed(self, value: bool) -> None:
        self._pressed = bool(value)
        self._retarget()

    def _retarget(self) -> None:
        scale = .94 if self._pressed and self.motion else 1
        self.spring.animate({"sx": scale, "sy": scale, "y": 1.2 if self._pressed and self.motion else 0,
                             "hover": float(self._hovered)},
                            response=.16 if self._pressed else .24,
                            damping=.95 if self._pressed else .70)

    def _motion_settled(self):
        if self._pulse_active:
            self._pulse_motion_done=True
            self._try_complete_pulse()

    def _pulse_ready(self):
        return True

    def _finish_pulse_visual(self):
        pass

    def _expire_pulse(self):
        if not self._pulse_active:return
        self._finish_pulse_visual()
        self._pulse_motion_done=True
        self._try_complete_pulse()

    def _try_complete_pulse(self):
        if not self._pulse_active or not self._pulse_motion_done or not self._pulse_ready():return
        self._pulse_active=False
        self._pulse_completion=None
        self._pulse_deadline.stop()
        callback,self._pulse_finished=self._pulse_finished,None
        if self._pulse_success:self.spring.animate({'check':0},response=.16,damping=1)
        if callback:callback()

    def cancel_pulse(self):
        """Cancel an accepted intake explicitly, rather than via hover feedback."""
        self._pulse_epoch+=1
        self._pulse_active=False
        self._pulse_motion_done=False
        self._pulse_completion=None
        self._pulse_finished=None
        self._pulse_deadline.stop()
        self._finish_pulse_visual()
        # Clearing a shelf must clear the success artwork as well as its
        # callback; otherwise the cancelled spring can leave a permanent tick
        # floating over an empty tile.
        self.spring.jump({'sx':1,'sy':1,'y':0,'hover':float(self._hovered),'check':0})

    def pulse(self, kind: str = "drop", finished: Callable[[], None] | None = None) -> None:
        self._pulse_epoch += 1
        # A decode/copy refresh may request visual feedback after the accepted
        # intake already registered its tuck. Only an explicit new completion
        # replaces that obligation; callback-free feedback must preserve it.
        if finished is not None or not self._pulse_active:self._pulse_finished=finished
        self._pulse_active = True
        self._pulse_motion_done=False
        success = kind in ("drop", "success", "intake")
        self._pulse_success=success
        self._pulse_completion=self._try_complete_pulse
        # Hover retargets can keep a Spring busy. Intake completion has its own
        # bounded lifetime and does not belong to Spring.animate's callback.
        self._pulse_deadline.start(650 if self.motion else 180)
        if self.motion:
            self.spring.kick({"sx": 2.8 if success else -2.2,
                              "sy": -3.5 if success else 2.7,
                              "y": -92 if success else -56})
        self.spring.animate({"sx": 1, "sy": 1, "y": 0,
                             "check": 1 if success else 0},
                            response=.25 if self.motion else .12, damping=.68)

    @staticmethod
    def _draw_excerpt(p: QPainter, rect: QRectF, text: str) -> None:
        font = p.font()
        font.setPixelSize(13)
        p.setFont(font)
        metrics = QFontMetricsF(font)
        displayed = text.replace("\r", "")
        encoded = displayed.encode("utf-16-le")
        layout = QTextLayout(displayed, font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        max_lines = max(1, int(rect.height() // metrics.lineSpacing()))
        lines = []
        for _ in range(max_lines):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            lines.append(line)
        layout.endLayout()
        y = rect.top() + metrics.ascent()
        for index, line in enumerate(lines):
            start, length = line.textStart(), line.textLength()
            chunk = encoded[start * 2:(start + length) * 2].decode("utf-16-le").replace("\n", " ")
            if index == len(lines) - 1 and (start + length) * 2 < len(encoded):
                remaining = encoded[start * 2:].decode("utf-16-le").replace("\n", " ")
                chunk = metrics.elidedText(remaining, Qt.TextElideMode.ElideRight, rect.width())
            p.drawText(QPointF(rect.left(), y), chunk)
            y += metrics.lineSpacing()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        v = self.spring.values
        center = QRectF(self.rect()).center()
        p.translate(center.x(), center.y() + v["y"])
        p.scale(v["sx"], v["sy"])
        p.translate(-center.x(), -center.y())
        r = QRectF(self.rect()).adjusted(8, 7, -8, -9)
        if self._kind in ("image", "text"):
            p.setBrush(QColor(ui_theme.color("surface")))
            p.setPen(QPen(QColor(ui_theme.color("border")), .8))
            p.drawRoundedRect(r, 10, 10)
        if self._kind == "image" and not self._pixmap.isNull():
            r = r.adjusted(4, 4, -4, -4)
            image = self._pixmap
            scale = min(r.width()/image.width(), r.height()/image.height())
            width, height = image.width()*scale, image.height()*scale
            dest = QRectF(center.x()-width/2, r.center().y()-height/2, width, height)
            clip = QPainterPath()
            clip.addRoundedRect(r, 6, 6)
            p.setClipPath(clip)
            p.drawPixmap(dest, image, QRectF(image.rect()))
            p.setClipping(False)
        elif self._kind == "text":
            p.setPen(QColor(ui_theme.color("text")))
            self._draw_excerpt(p, r.adjusted(8, 8, -8, -8), self._text or "空文本")
        elif self._kind == "file":
            extent = min(128, r.width(), r.height())
            pixmap = self._pixmap if not self._pixmap.isNull() else document_pixmap(self._text, round(extent), dpr=self.devicePixelRatioF())
            logical = pixmap.deviceIndependentSize()
            scale = min(1.0, extent/max(1,logical.width()), extent/max(1,logical.height()))
            width, height = logical.width()*scale, logical.height()*scale
            p.drawPixmap(QRectF(center.x()-width/2, r.center().y()-height/2, width, height),
                         pixmap, QRectF(pixmap.rect()))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            placeholder = QColor(ui_theme.color("button"))
            placeholder.setAlpha(round(150 + 100 * v["hover"]))
            p.setBrush(placeholder)
            circle = QRectF(center.x() - 29, center.y() - 30, 58, 58)
            p.drawEllipse(circle)
            draw_glyph(p, "shelf", QRectF(center.x()-22, center.y()-23, 44, 44), ui_theme.color("secondary"))
        if v["check"] > .005:
            p.setOpacity(max(0, min(1, v["check"])))
            check_rect = QRectF(self.width() - 30, self.height() - 30, 23, 23)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(QColor("#32b777"))
            p.drawEllipse(check_rect)
            draw_glyph(p, "check", QRectF(check_rect.center().x()-7.5, check_rect.center().y()-7.5, 15, 15), "#ffffff", 2)
        p.end()
