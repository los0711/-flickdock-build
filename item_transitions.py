"""Short, interruptible item exits; no data ownership or native window resize."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QEasingCurve, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor,QPainter
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

_BACKING_PROPERTY='_flickdock_reflow_backing'


def paint_card_backing(widget,painter,rect,radii):
    """Occlude sibling ink only inside the original antialiased card path."""
    if not widget.property(_BACKING_PROPERTY):return
    import ui_theme
    from surface_morph import draw_rounded_rect
    painter.save();painter.setPen(Qt.NoPen);painter.setBrush(QColor(ui_theme.color('surface')))
    draw_rounded_rect(painter,rect,radii);painter.restore()


def _curve(kind):
    curve=QEasingCurve(QEasingCurve.Type.BezierSpline)
    if kind=='out':curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    else:curve.addCubicBezierSegment(QPointF(.77,0),QPointF(.175,1),QPointF(1,1))
    return curve


class _Exits(QWidget):
    def __init__(self,owner):
        # Remain visible for the final item's exit after the empty state hides
        # its scroll area. This is a transparent, input-pass-through sibling.
        super().__init__(owner.scroll.parentWidget())
        self.owner=owner
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.hide()

    def paintEvent(self,event):
        painter=QPainter(self)
        now=time.perf_counter()
        for pixmap,position,started,alpha in self.owner.ghosts:
            progress=min(1,max(0,(now-started)/self.owner.duration))
            painter.setOpacity(alpha*(1-self.owner.fade.valueForProgress(progress)))
            painter.drawPixmap(position,pixmap)
        painter.end()


class ItemTransitions(QObject):
    duration=.2

    def __init__(self,scroll,body):
        super().__init__(body)
        self.scroll,self.body=scroll,body
        self.fade=_curve('out');self.move=_curve('move')
        self.ghosts=[];self.moves={};self.generation=0
        self._backings={}
        self.layout_pending=False
        self.overlay=_Exits(self)
        body.destroyed.connect(self.overlay.deleteLater)
        from native_ui_common import ScreenAnimationTimer
        self.timer=ScreenAnimationTimer(self);self.timer.timeout.connect(self._tick)
        # Scroll is an immediate user operation. Never drag old pixels along
        # with it, or animate to positions from a previous viewport size.
        scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        scroll.horizontalScrollBar().valueChanged.connect(self._scrolled)
        scroll.viewport().installEventFilter(self)
        self._lifecycle_hosts=tuple(dict.fromkeys((scroll.parentWidget(),scroll.window())))
        for host in self._lifecycle_hosts:
            if host is not None:host.installEventFilter(self)
        scroll.verticalScrollBar().sliderPressed.connect(self.finish)
        scroll.horizontalScrollBar().sliderPressed.connect(self.finish)

    def eventFilter(self,watched,event):
        if (event.type()==QEvent.Wheel or watched in self._lifecycle_hosts
                and event.type() in (QEvent.Hide,QEvent.Close)):self.finish()
        return super().eventFilter(watched,event)

    def _back_card(self,widget):
        """Precompose the transparent card over its unchanged overview surface.

        Moving translucent QWidget siblings otherwise show each other's text.
        Keep normal paint/hover/selection live; no snapshot replaces the card.
        """
        key=id(widget)
        if key in self._backings:return
        self._backings[key]=(widget,widget.property(_BACKING_PROPERTY))
        widget.setProperty(_BACKING_PROPERTY,True);widget.update()

    def _restore_backing(self,key):
        widget,previous=self._backings.pop(key)
        if not isValid(widget):return
        widget.setProperty(_BACKING_PROPERTY,previous);widget.update()

    def _restack(self,widgets):
        ordered=sorted(widgets,key=lambda widget:(getattr(widget,'_reflow_index',0),widget.y(),widget.x()))
        for widget in reversed(ordered):widget.raise_()

    def appended(self,widgets):
        """Late construction joins only a still-running visual transaction."""
        if self.layout_pending or not (self.moves or self.ghosts):return
        from ui_transitions import motion_allowed
        if not self.body.isVisible() or not motion_allowed():return
        for widget in widgets:
            if isValid(widget) and widget.isVisible():self._back_card(widget)
        self._restack([record[0] for record in self._backings.values() if isValid(record[0])])

    def _scrolled(self,*_):
        if not self.layout_pending:self.finish()

    def capture(self,widgets):
        self.timer.stop()
        self.generation+=1
        self.layout_pending=True
        positions={ref:QPoint(widget.pos()) for ref,widget in widgets.items() if isValid(widget)}
        # A second edit starts at each row's CURRENT painted position.
        self.moves.clear()
        return positions

    def remove(self,widgets,*,enabled):
        from ui_transitions import motion_allowed
        enabled=enabled and motion_allowed()
        if not enabled:
            self.finish();return
        viewport=self.scroll.viewport()
        now=time.perf_counter()
        for widget in widgets:
            if not isValid(widget) or not widget.isVisible():continue
            position=widget.mapTo(viewport,QPoint())
            if not viewport.rect().intersects(widget.rect().translated(position)):continue
            self.ghosts.append((widget.grab(),position,now,1.0))
        if self.ghosts:
            self.overlay.setGeometry(viewport.rect().translated(viewport.mapTo(self.overlay.parentWidget(),QPoint())))
            self.overlay.show();self.overlay.raise_()
            self.timer.start()

    def reflow(self,widgets,positions,*,enabled):
        from ui_transitions import motion_allowed
        enabled=enabled and motion_allowed()
        generation=self.generation
        def begin():
            if not isValid(self) or generation!=self.generation:return
            self.layout_pending=False
            if not enabled or not motion_allowed():self.finish();return
            now=time.perf_counter()
            viewport=self.scroll.viewport()
            live=[widget for widget in widgets.values() if isValid(widget)
                  and viewport.rect().intersects(widget.rect().translated(widget.mapTo(viewport,QPoint())))]
            present={id(widget) for widget in live}
            for key in tuple(self._backings):
                if key not in present:self._restore_backing(key)
            # New first-slot cards and old movers may share their current
            # paint rectangle. Use the final reading order consistently.
            for widget in live:self._back_card(widget)
            self._restack(live)
            for ref,widget in widgets.items():
                if ref not in positions or not isValid(widget):continue
                target=QPoint(widget.pos());origin=positions[ref]
                if origin==target:continue
                at=widget.mapTo(viewport,QPoint())
                if not viewport.rect().intersects(widget.rect().translated(at)):continue
                self.moves[ref]=(widget,origin,target,now)
                widget.move(origin)
            if self.moves or self.ghosts:self.timer.start()
            else:self.finish()
        QTimer.singleShot(0,begin)

    def _tick(self):
        # A timeout may already be queued when capture stops the old clock.
        # Only the new begin may finalize its layout epoch or resume ghosts.
        if self.layout_pending:self.timer.stop();return
        from ui_transitions import motion_allowed
        if not motion_allowed():self.finish();return
        now=time.perf_counter()
        moving={id(row[0]) for row in self.moves.values()}
        viewport=self.scroll.viewport()
        for key,record in tuple(self._backings.items()):
            widget=record[0]
            if not isValid(widget) or (key not in moving and not viewport.rect().intersects(
                    widget.rect().translated(widget.mapTo(viewport,QPoint())))):
                self._restore_backing(key)
        for ref,(widget,origin,target,started) in tuple(self.moves.items()):
            if not isValid(widget):self.moves.pop(ref,None);continue
            progress=min(1,max(0,(now-started)/self.duration))
            fraction=self.move.valueForProgress(progress)
            widget.move(QPoint(round(origin.x()+(target.x()-origin.x())*fraction),
                               round(origin.y()+(target.y()-origin.y())*fraction)))
            if progress>=1:self.moves.pop(ref,None)
        self.ghosts=[ghost for ghost in self.ghosts if now-ghost[2]<self.duration]
        if self.ghosts:self.overlay.update()
        else:self.overlay.hide()
        if not self.moves and not self.ghosts:self.finish()

    def finish(self,*_):
        self.generation+=1;self.layout_pending=False
        for widget,origin,target,started in self.moves.values():
            if isValid(widget):widget.move(target)
        self.moves.clear();self.ghosts.clear();self.timer.stop();self.overlay.hide()
        for key in tuple(self._backings):self._restore_backing(key)
