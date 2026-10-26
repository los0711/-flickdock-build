"""Right-anchored elastic geometry with a lightweight frozen presentation."""
from collections import OrderedDict
import math
from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import QWidget


def paint_surface_shadow(painter,rect,cache,blur=28.0,offset=None,color=None):
    """Paint the same fixed-width shadow for live and animated surfaces."""
    offset=QPoint(0,7) if offset is None else offset
    color=QColor(18,31,53,40) if color is None else QColor(color)
    dpr=painter.device().devicePixelRatioF()
    radius=min(26.0,min(rect.width(),rect.height())*.10)
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
    path=QPainterPath(); path.addRoundedRect(rect,24,24)
    return path


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
        self.hide()

    def capture_live(self,card,controls):
        visibility=[(button,not button.isHidden()) for button in controls]
        effect=card.graphicsEffect()
        enabled=effect.isEnabled() if effect else False
        for button,_ in visibility: button.hide()
        if effect: effect.setEnabled(False)
        try:
            card.ensurePolished()
            for child in card.findChildren(QWidget):
                if child.layout(): child.layout().activate()
            dpr=card.devicePixelRatioF()
            image=QPixmap(round(card.width()*dpr),round(card.height()*dpr))
            image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
            painter=QPainter(image); card.render(painter,QPoint()); painter.end()
            self.capture_count+=1
            return image
        finally:
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

    def begin(self,source,target,origin,present=True,target_rect=None):
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
        self.source=QPixmap(); self.target=QPixmap()

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

    def paint_frame(self,painter,rect,live_draw=None,target_size=None):
        """Paint inside MotionShadow's existing source coordinate system.

        Target widgets stay at their final layout size and remain visible to
        Qt. The source fades away over that live target. At progress one this
        is already the exact drawSource path used after end_surface_morph().
        No widget visibility, native geometry or raster changes at completion.
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipPath(surface_clip(rect))
        if live_draw is not None:
            painter.save()
            painter.translate(rect.x(),rect.y())
            if target_size is not None:
                painter.scale(rect.width()/max(1,target_size.width()),
                              rect.height()/max(1,target_size.height()))
            live_draw(painter)
            painter.restore()
        elif not self.target.isNull():
            painter.drawPixmap(rect,self.target,QRectF(self.target.rect()))
        if not self.source.isNull() and self.mix<1:
            painter.setOpacity(1-self.mix)
            painter.drawPixmap(rect,self.source,QRectF(self.source.rect()))
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
        # Native bounds and crossfade progress share one monotonic trajectory.
        # The reveal Spring supplies the elastic feedback without a second
        # resize reversal after the target image has already become opaque.
        curve=QEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.setEasingCurve(curve)
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self._finish)
        self._done = None
        self.progress=1.0
        self.active=False

    def stop(self, settle=True):
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

    def animate(self, rect, finished=None, duration=230):
        self.stop(settle=False)
        self._from = QRect(self.current)
        self._to = QRect(rect)
        self._done = finished
        self.active=True; self.progress=0.0
        if not self.motion or self._from == self._to:
            self.current = QRect(self._to)
            self.update(QRect(self.current))
            self._finish()
            return
        if self.prepare:
            self.prepare(QRect(self._from), QRect(self._to))
        self.animation.setDuration(duration)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.start()

    def _frame(self, progress):
        t=float(progress)
        self.progress=t
        a, b = self._from, self._to
        def blend(x, y): return round(x + (y - x) * t)
        width, height = blend(a.width(), b.width()), blend(a.height(), b.height())
        right = blend(a.x() + a.width(), b.x() + b.width())
        self.current = QRect(right - width, blend(a.y(), b.y()), width, height)
        self.update(QRect(self.current))

    def _finish(self):
        if not self.active:return
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
