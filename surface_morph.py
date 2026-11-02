"""Right-anchored elastic geometry with a lightweight frozen presentation."""
from collections import OrderedDict
import math
import sys
from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QEasingCurve
from frame_animation import FrameAnimation as QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTransform, QRegion
from PySide6.QtWidgets import QWidget


def rounded_rect_path(rect,radius):
    """The original Qt circular-arc corners, without custom curve geometry."""
    rx,ry=radius if isinstance(radius,(tuple,list)) else (radius,radius)
    path=QPainterPath();path.addRoundedRect(QRectF(rect),rx,ry);return path


def draw_rounded_rect(painter,rect,radius):
    rx,ry=radius if isinstance(radius,(tuple,list)) else (radius,radius)
    painter.drawRoundedRect(QRectF(rect),rx,ry)


def paint_surface_shadow(painter,rect,cache,blur=28.0,offset=None,color=None):
    """Paint the same fixed-width shadow for live and animated surfaces."""
    offset=QPoint(0,7) if offset is None else offset
    color=QColor(18,31,53,40) if color is None else QColor(color)
    if color.alpha() == 0:
        return
    dpr=painter.device().devicePixelRatioF()
    radius=16.0 if sys.platform=='darwin' else 10.0
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
            p.setBrush(ink); draw_rounded_rect(p,bounds.adjusted(-pad,-pad,pad,pad),radius+pad)
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
    return rounded_rect_path(rect,radius)


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
        self._last_paint=None
        self.hide()

    def capture_live(self,card,controls):
        visibility=[(button,not button.isHidden()) for button in controls]
        effect=card.graphicsEffect()
        enabled=effect.isEnabled() if effect else False
        host=card.window();updates_enabled=host.updatesEnabled()
        for button,_ in visibility: button.hide()
        if effect: effect.setEnabled(False)
        try:
            # A surface transaction suppresses native repaints while preparing
            # both endpoint trees. Qt also suppresses disabled descendants in
            # QWidget.render(), yielding a background-only snapshot. Permit
            # this synchronous offscreen render, then restore the barrier
            # before returning; no event loop or native paint is dispatched.
            if not updates_enabled:host.setUpdatesEnabled(True)
            card.ensurePolished()
            for child in card.findChildren(QWidget):
                if child.layout(): child.layout().activate()
            dpr=card.devicePixelRatioF()
            image=QPixmap(round(card.width()*dpr),round(card.height()*dpr))
            image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
            painter=QPainter(image)
            # Styled translucent windows already paint their background in
            # DrawChildren. DrawWindowBackground would apply it a second time.
            try:card.render(painter,QPoint(),QRegion(),QWidget.RenderFlag.DrawChildren)
            finally:painter.end()
            self.capture_count+=1
            return image
        finally:
            if not updates_enabled:host.setUpdatesEnabled(False)
            if effect: effect.setEnabled(enabled)
            for button,visible in visibility: button.setVisible(visible)

    def capture_current(self,card,controls):
        if not self.active:return self.capture_live(card,controls)
        width=max(1,self.rect_global.width()-28); height=max(1,self.rect_global.height()-27)
        dpr=self.owner.devicePixelRatioF()
        image=QPixmap(round(width*dpr),round(height*dpr))
        image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image)
        try:
            # An interrupted transition is composed from its two already
            # captured endpoints. QWidget.render has the same state-reset
            # hazard as drawSource when called under a scaling transform.
            rect=QRectF(0,0,width,height)
            # Keep the last painted recipe, including Qt's already acquired
            # live source pixmap, instead of mixing two full-size scratch
            # pixmaps on every frame. Only a reversal needs this raster copy.
            recipe=self._last_paint
            if recipe is None:self.paint_frame(painter,rect)
            else:self._compose(painter,rect,*recipe)
        finally:
            painter.end()
        return image

    def begin(self,source,target,origin,present=True,target_rect=None):
        self.source,self.target=source,target
        self._last_paint=None
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
        self._last_paint=None

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
        live_mix=max(0.,min(1.,(self.mix-.75)/.25)) if live_draw is not None else 0.
        if self.target.isNull() and live_draw is not None:live_mix=1.
        # draw_live acquires the effect's cached source pixmap when used. Its
        # closure then retains that exact painted frame for a rapid reverse;
        # do not call an unused early-phase closure after the live tree changes.
        painted_live=live_draw if live_mix else None
        self._compose(painter,rect,self.mix,painted_live,target_size)
        self._last_paint=(self.mix,painted_live,target_size)

    @staticmethod
    def _fitted_rect(rect,size):
        """Fit without enlarging text; grow the surface, not its source title.

        Both endpoints keep their aspect ratio. The fixed right/top anchor is
        shared with the native morph and leaves newly revealed space to the
        target tree rather than stretching the compact card over it.
        """
        width=max(1.,size.width());height=max(1.,size.height())
        scale=min(1.,rect.width()/width,rect.height()/height)
        return QRectF(rect.right()-width*scale,rect.top(),width*scale,height*scale)

    def _compose(self,painter,rect,progress,live_draw,target_size):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipPath(surface_clip(rect))
        import ui_theme
        painter.fillRect(rect,QColor(ui_theme.color('surface')))
        if progress<=0 and not self.source.isNull():
            painter.drawPixmap(self._fitted_rect(rect,self.source.deviceIndependentSize()),self.source,QRectF(self.source.rect()))
        else:
            # The application surfaces are opaque. SourceOver on their common
            # opaque material gives the same endpoint crossfade without the
            # old per-frame full-DPR blend/weighted fills, copies and Plus pass.
            # Content newly exposed outside the compact endpoint belongs to
            # the target; it must not contain an enlarged compact title.
            live_mix=max(0.,min(1.,(progress-.75)/.25)) if live_draw is not None else 0.
            if self.target.isNull() and live_draw is not None:live_mix=1.
            if live_mix:self._draw_live(painter,rect,live_draw,target_size)
            if not self.target.isNull() and live_mix<1.:
                painter.setOpacity(1.-live_mix)
                painter.drawPixmap(self._fitted_rect(rect,self.target.deviceIndependentSize()),self.target,QRectF(self.target.rect()))
                painter.setOpacity(1.)
            if progress<1. and not self.source.isNull():
                painter.setOpacity(1.-progress)
                painter.drawPixmap(self._fitted_rect(rect,self.source.deviceIndependentSize()),self.source,QRectF(self.source.rect()))
        painter.restore()

    def _draw_live(self,painter,rect,live_draw,target_size):
        logical=target_size if target_size is not None else self.target.deviceIndependentSize()
        destination=self._fitted_rect(rect,logical)
        painter.save()
        painter.translate(destination.x(),destination.y())
        painter.scale(destination.width()/max(1,logical.width()),destination.height()/max(1,logical.height()))
        live_draw(painter)
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
        self._intent=0
        self.progress=1.0
        self.active=False

    def stop(self, settle=True):
        self._intent+=1
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
        from native_ui_common import reduced_motion
        if not self.motion or reduced_motion() or self._from == self._to:
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
        from native_ui_common import reduced_motion
        if self.active and self.animation.state()==self.animation.State.Running and (not self.motion or reduced_motion()):
            self._finish();return
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
        intent=self._intent;callback,self._done=self._done,None
        self.animation.stop()
        self.active=False; self.progress=1.0
        self.current = QRect(self._to)
        self.update(QRect(self.current))
        if intent!=self._intent:return
        if self.settle:
            self.settle(QRect(self.current))
        if callback and intent==self._intent:
            callback()

    def complete(self):
        """Land at the current target before opening a live child overlay."""
        if self.active:self._finish()

    finish=complete
