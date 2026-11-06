"""Right-anchored elastic geometry with a lightweight frozen presentation."""
from collections import OrderedDict
import math
import sys
import time
from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTransform, QRegion
from PySide6.QtWidgets import QWidget
from display_frames import FramePulse


def paint_surface_shadow(painter,rect,cache,blur=28.0,offset=None,color=None,*,radius=None):
    """Paint the same fixed-width shadow for live and animated surfaces."""
    offset=QPoint(0,7) if offset is None else offset
    color=QColor(18,31,53,40) if color is None else QColor(color)
    if color.alpha() == 0:
        return
    dpr=painter.device().devicePixelRatioF()
    if radius is None:radius=16.0 if sys.platform=='darwin' else 10.0
    spread=blur*.65
    margin=round(blur+max(abs(offset.x()),abs(offset.y()))+3)
    inset_x=math.ceil(radius+spread+abs(offset.x())+2)
    inset_y=math.ceil(radius+spread+abs(offset.y())+2)
    key=(radius,blur,offset.x(),offset.y(),color.rgba(),dpr)
    texture=cache.get(key)
    if texture is None:
        body=2*max(inset_x,inset_y)+16
        image=QPixmap(round((body+2*margin)*dpr),round((body+2*margin)*dpr))
        image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
        p=QPainter(image); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        bounds=QRectF(margin+offset.x(),margin+offset.y(),
                      image.width()/dpr-2*margin,image.height()/dpr-2*margin)
        for i in range(18,0,-1):
            pad=spread*i/18; ink=QColor(color)
            ink.setAlpha(round(color.alpha()*.11*math.exp(-2.4*(i/18)**2)))
            p.setBrush(ink); p.drawRoundedRect(bounds.adjusted(-pad,-pad,pad,pad),radius+pad,radius+pad)
        p.end()
        texture=(image,margin,round((margin+inset_x)*dpr),round((margin+inset_y)*dpr))
        cache[key]=texture
        while len(cache)>3:cache.popitem(last=False)
    else:
        cache.move_to_end(key)
    image,margin,cut_x,cut_y=texture
    outer=rect.adjusted(-margin,-margin,margin,margin)
    edge_x=min(cut_x/dpr,outer.width()/2)
    edge_y=min(cut_y/dpr,outer.height()/2)
    sx=(0,cut_x,image.width()-cut_x,image.width())
    sy=(0,cut_y,image.height()-cut_y,image.height())
    dx=(outer.left(),outer.left()+edge_x,outer.right()-edge_x,outer.right())
    dy=(outer.top(),outer.top()+edge_y,outer.bottom()-edge_y,outer.bottom())
    painter.save(); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    for row in range(3):
        for column in range(3):
            painter.drawPixmap(QRectF(dx[column],dy[row],dx[column+1]-dx[column],dy[row+1]-dy[row]),
                               image,QRectF(sx[column],sy[row],sx[column+1]-sx[column],sy[row+1]-sy[row]))
    painter.restore()


def surface_clip(rect):
    radius=16 if sys.platform=='darwin' else 10
    path=QPainterPath(); path.addRoundedRect(rect,radius,radius)
    return path


def visible_render_widgets(root):
    """Walk only the clipped widget branches that QWidget.render can paint.

    Hidden overview rows retain thousands of children after the first visit.
    Lifting their inherited update flags emits events even though none of those
    widgets contributes pixels to this endpoint.
    """
    pending=[(root,root.rect())]
    while pending:
        widget,clip=pending.pop()
        if widget.testAttribute(Qt.WA_ForceUpdatesDisabled):continue
        yield widget
        for child in widget.findChildren(QWidget,options=Qt.FindDirectChildrenOnly):
            if child.isHidden() or child.isWindow():continue
            visible=clip.intersected(child.geometry())
            if not visible.isEmpty():
                pending.append((child,visible.translated(-child.x(),-child.y())))


class SurfaceSnapshot(QWidget):
    """Two endpoint renders, one cached shadow, no descendant layout per frame.

    Header controls are siblings on the owner during a transition, so their
    pointer/keyboard feedback and rapid reversals remain live above the image.
    The source tree is restored at the endpoint for full-resolution SVG/text.
    """
    def __init__(self,owner):
        super().__init__(owner)
        self.owner=owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.active=False
        self.source=QPixmap(); self.target=QPixmap()
        self.rect_global=QRect(); self.transition_bounds=QRect(); self.mix=0
        self._shadows=OrderedDict()
        self.capture_count=0
        self.target_sections=[]
        self.elements=None
        self.hide()

    def capture_live(self,card,controls):
        visibility=[(button,not button.isHidden()) for button in controls if card.isAncestorOf(button)]
        effect=card.graphicsEffect()
        enabled=effect.isEnabled() if effect else False
        for button,_ in visibility: button.hide()
        if effect: effect.setEnabled(False)
        # The native owner stays update-blocked during a surface transaction.
        # QWidget.render skips descendants with inherited WA_UpdatesDisabled,
        # even when rendering to a pixmap: that captured an empty endpoint and
        # left opaque child holes black. Lift only inherited subtree flags for
        # this offscreen render; preserve explicitly blocked widgets and never
        # enable the native owner or publish an intermediate layout.
        blocked=[widget for widget in visible_render_widgets(card)
                 if widget.testAttribute(Qt.WA_UpdatesDisabled) and not widget.testAttribute(Qt.WA_ForceUpdatesDisabled)]
        for widget in blocked:widget.setAttribute(Qt.WA_UpdatesDisabled,False)
        try:
            card.ensurePolished()
            # QWidget.render prepares dirty descendant layouts in Qt. Walking
            # thousands of row children again in Python delays the first frame.
            dpr=card.devicePixelRatioF()
            image=QPixmap(round(card.width()*dpr),round(card.height()*dpr))
            image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
            painter=QPainter(image)
            # Styled translucent windows already paint their background in
            # DrawChildren. DrawWindowBackground would apply it a second time.
            card.render(painter,QPoint(),QRegion(),QWidget.RenderFlag.DrawChildren)
            painter.end()
            self.capture_count+=1
            return image
        finally:
            for widget in blocked:widget.setAttribute(Qt.WA_UpdatesDisabled,True)
            if effect: effect.setEnabled(enabled)
            for button,visible in visibility: button.setVisible(visible)

    def capture_current(self,card,controls):
        if not self.active: return self.capture_live(card,controls)
        width=max(1,self.rect_global.width()-28); height=max(1,self.rect_global.height()-27)
        dpr=self.owner.devicePixelRatioF()
        image=QPixmap(round(width*dpr),round(height*dpr))
        image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image)
        try:
            # An interrupted transition is composed from its two already
            # captured endpoints. QWidget.render has the same state-reset
            # hazard as drawSource when called under a scaling transform.
            self.paint_frame(painter,QRectF(0,0,width,height))
        finally:
            painter.end()
        return image

    def begin(self,source,target,origin,present=True,target_rect=None,*,source_elements=(),target_elements=()):
        from surface_elements import SharedElements
        self.elements=SharedElements(source_elements,target_elements,source_size=source.deviceIndependentSize(),target_size=target.deviceIndependentSize())
        self.target_sections=[]
        self.source,self.target=source,target
        self.active=True; self.mix=0; self.rect_global=QRect(origin)
        self.transition_bounds=origin.united(target_rect) if target_rect is not None else QRect(origin)
        self.setGeometry(self.owner.rect())
        if present:self.show(); self.raise_()
        else:self.hide()

    def set_frame(self,rect,progress):
        self.rect_global=QRect(rect); self.mix=max(0,min(1,progress)); self.update()
        effect=self._effect()
        if effect is not None and getattr(effect,'_surface_snapshot',None) is self:
            effect.refresh_surface_morph()

    def finish(self):
        self.active=False; self.hide()
        self.target_sections=[]
        self.source=QPixmap(); self.target=QPixmap()
        self.elements=None

    def current_elements(self):
        if not self.active or self.elements is None:return []
        rect=QRectF(0,0,self.rect_global.width()-28,self.rect_global.height()-27)
        first,last=self._element_offsets(rect)
        return self.elements.current(self.mix,first,last,self.owner.devicePixelRatioF())

    def _element_offsets(self,rect):
        first=self.source.deviceIndependentSize();last=self.target.deviceIndependentSize()
        from PySide6.QtCore import QPointF
        return (QPointF(rect.right()-first.width(),rect.top()),
                QPointF(rect.right()-last.width(),rect.top()))

    def _clip_endpoint(self,painter,rect,source,offset):
        if self.elements is None:return
        path=QPainterPath();path.addRect(rect)
        painter.setClipPath(path.subtracted(self.elements.mask(source,offset)),Qt.ClipOperation.IntersectClip)

    def _effect(self):
        return getattr(self.owner,'surface_effect',None)

    def content_rect(self,rect=None):
        rect=self.rect_global if rect is None else rect
        return QRectF(rect.x()-self.owner.x()+14,rect.y()-self.owner.y()+7,
                      rect.width()-28,rect.height()-27)

    def presentation_transform(self,rect=None):
        """The live card's transform, in owner coordinates, for header handoff.

        The owner can use this same transform for live header controls. Its
        presentation callback should call refresh_presentation() while active,
        since a hidden card's effect no longer schedules our repaint itself.
        """
        effect=self._effect()
        center=self.content_rect(rect).center()
        transform=QTransform()
        if effect is not None:
            transform.translate(center.x()+effect.dx,center.y()+effect.dy)
            transform.scale(effect.sx,effect.sy)
            transform.translate(-center.x(),-center.y())
        return transform

    def refresh_presentation(self):
        if self.active:
            self.update()
            effect=self._effect()
            if effect is not None and getattr(effect,'_surface_snapshot',None) is self:
                effect.refresh_surface_morph()

    def _paint_shadow(self,painter,rect):
        effect=self._effect()
        paint_surface_shadow(painter,rect,self._shadows,
                             effect.blurRadius() if effect else 28.0,
                             effect.offset() if effect else None,
                             effect.color() if effect else None)

    def draw_target(self,painter,rect):
        if not self.target_sections:
            painter.drawPixmap(rect,self.target,QRectF(self.target.rect()));return
        size=self.target.deviceIndependentSize();ratio=self.target.devicePixelRatioF()
        painter.save();painter.translate(rect.topLeft())
        painter.scale(rect.width()/size.width(),rect.height()/size.height())
        for section,offset in self.target_sections:
            painter.save();painter.setClipRect(section,Qt.ClipOperation.IntersectClip)
            destination=section.translated(offset.x()*(1-self.mix),offset.y()*(1-self.mix))
            source=QRectF(section.x()*ratio,section.y()*ratio,section.width()*ratio,section.height()*ratio)
            painter.drawPixmap(destination,self.target,source);painter.restore()
        painter.restore()

    def paint_frame(self,painter,rect,live_draw=None,target_size=None):
        """Move identity-matched layers above the two natural-size layouts.

        Endpoint regions occupied by shared elements are excluded, so each
        file is drawn once. Unmatched layout chrome fades at natural size;
        the native host and the entire live widget tree remain fixed.
        """
        from ui_transitions import SURFACE_SOURCE_FADE_END,SURFACE_TARGET_FADE_END
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipPath(surface_clip(rect))
        # One stationary-radius backing owns opacity for the entire transition.
        import ui_theme
        painter.fillRect(rect,QColor(ui_theme.color('surface')))
        source_offset,target_offset=self._element_offsets(rect)
        if self.mix>0:
            logical=target_size if target_size is not None else self.target.deviceIndependentSize()
            target_rect=QRectF(rect.right()-logical.width(),rect.top(),logical.width(),logical.height())
            painter.save()
            self._clip_endpoint(painter,rect,False,target_offset)
            painter.setOpacity(min(1,self.mix/SURFACE_TARGET_FADE_END))
            if self.mix>=1 and live_draw is not None:
                painter.translate(target_rect.topLeft());live_draw(painter)
            elif not self.target.isNull():
                if self.elements and self.elements.keys:
                    painter.drawPixmap(target_rect,self.target,QRectF(self.target.rect()))
                else:self.draw_target(painter,target_rect)
            painter.restore()
        if self.mix<SURFACE_SOURCE_FADE_END and not self.source.isNull():
            logical=self.source.deviceIndependentSize()
            source_rect=QRectF(rect.right()-logical.width(),rect.top(),logical.width(),logical.height())
            painter.save();painter.setOpacity(1-self.mix/SURFACE_SOURCE_FADE_END)
            self._clip_endpoint(painter,rect,True,source_offset)
            painter.drawPixmap(source_rect,self.source,QRectF(self.source.rect()))
            painter.restore()
        if self.elements is not None:
            self.elements.paint(painter,self.mix,source_offset,target_offset)
        painter.restore()

    def _paint_content(self,painter,rect):
        self.paint_frame(painter,rect)

    def paintEvent(self,event):
        if not self.active:return
        p=QPainter(self)
        rect=self.content_rect()
        p.setTransform(self.presentation_transform())
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._paint_shadow(p,rect)
        self._paint_content(p,rect)
        p.end()

class SurfaceMorph(QObject):
    def __init__(self, parent, rect, update, motion=True, prepare=None, settle=None):
        super().__init__(parent)
        self.current = QRect(rect)
        self.update = update
        self.motion = motion
        self.prepare = prepare
        self.settle = settle
        self.animation = QVariantAnimation(self)
        self._frames = FramePulse(self)
        self._frames.timeout.connect(self._advance)
        self._started_at = 0.0
        # One shared clock drives geometry and content. Only an explicit
        # overview open opts into a small, preallocated elastic overshoot.
        self._geometry_curve=QEasingCurve(QEasingCurve.Type.OutCubic)
        self._content_curve=QEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.setEasingCurve(QEasingCurve.Linear)
        self.animation.valueChanged.connect(self._frame)
        self._done = None
        self.progress=1.0
        self.active=False

    def stop(self, settle=True):
        self._frames.stop()
        self.animation.stop()
        self._done = None
        self.active=False
        if settle and self.settle:
            self.settle(QRect(self.current))

    def jump(self, rect):
        self.stop(settle=False)
        self.current = QRect(rect)
        if self.settle:
            self.settle(QRect(self.current))
        self.update(QRect(self.current))

    def animate(self, rect, finished=None, duration=230, *, elastic=False):
        self.stop(settle=False)
        self._from = QRect(self.current)
        self._to = QRect(rect)
        self._done = finished
        self.active=True; self.progress=0.0
        from native_ui_common import reduced_motion
        if not self.motion or reduced_motion() or self._from == self._to:
            self.current = QRect(self._to)
            self.update(QRect(self.current))
            self._finish()
            return
        if self.prepare:
            self.prepare(QRect(self._from), QRect(self._to))
        curve=QEasingCurve(QEasingCurve.OutBack if elastic else QEasingCurve.OutCubic)
        if elastic:
            from ui_transitions import OVERVIEW_OPEN_OVERSHOOT
            curve.setOvershoot(OVERVIEW_OPEN_OVERSHOOT)
        self._geometry_curve=curve
        self.animation.setDuration(duration)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        # QVariantAnimation supplies Qt's interpolation, but its default
        # unified 16 ms timer must not pace a high-refresh native surface.
        self.animation.setCurrentTime(0)
        self._frame(0.0)
        self._started_at = time.perf_counter()
        self._frames.start()

    def pause(self):
        self._frames.stop()

    def resume(self):
        if self.active:
            self._started_at = time.perf_counter() - self.animation.currentTime() / 1000
            self._frames.start()

    def _advance(self):
        elapsed = round((time.perf_counter() - self._started_at) * 1000)
        self.animation.setCurrentTime(min(elapsed, self.animation.duration()))
        if elapsed >= self.animation.duration():
            self._finish()

    def _frame(self, progress):
        clock=max(0.,min(1.,float(progress)))
        t=self._geometry_curve.valueForProgress(clock)
        self.progress=self._content_curve.valueForProgress(clock)
        a, b = self._from, self._to
        def blend(x, y): return round(x + (y - x) * t)
        width, height = blend(a.width(), b.width()), blend(a.height(), b.height())
        right = blend(a.x() + a.width(), b.x() + b.width())
        self.current = QRect(right - width, blend(a.y(), b.y()), width, height)
        self.update(QRect(self.current))

    def _finish(self):
        if not self.active:return
        self._frames.stop()
        self.animation.stop()
        self.active=False; self.progress=1.0
        self.current = QRect(self._to)
        self.update(QRect(self.current))
        if self.settle:
            self.settle(QRect(self.current))
        callback, self._done = self._done, None
        if callback:
            callback()

    def complete(self):
        """Land at the current target before opening a live child overlay."""
        if self.active:self._finish()

    finish=complete
