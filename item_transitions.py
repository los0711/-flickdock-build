"""Short, interruptible item exits; no data ownership or native window resize."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QEasingCurve, QObject, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGraphicsEffect,QWidget
from shiboken6 import isValid


def _curve(kind):
    curve=QEasingCurve(QEasingCurve.Type.BezierSpline)
    if kind=='out':curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    else:curve.addCubicBezierSegment(QPointF(.77,0),QPointF(.175,1),QPointF(1,1))
    return curve


class _EntryEffect(QGraphicsEffect):
    """Move the entire visible file viewport together, without changing hit bounds."""
    def __init__(self,parent):
        super().__init__(parent);self.progress=1.

    def set_progress(self,value):
        self.progress=max(0.,min(1.,float(value)));self.update()

    def boundingRectFor(self,rect):return rect.adjusted(0,0,0,4)

    def draw(self,painter):
        # drawSource() may restore painter state for QWidget sources; paint a
        # logical-coordinate pixmap so translation and opacity actually apply.
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.PixmapPadMode.NoPad)
        painter.save();painter.setOpacity(.45+.55*self.progress)
        painter.drawPixmap(QPointF(offset)+QPointF(0,4*(1-self.progress)),source);painter.restore()


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
    settled=Signal()

    def __init__(self,scroll,body):
        super().__init__(body)
        self.scroll,self.body=scroll,body
        self.fade=_curve('out');self.move=_curve('move')
        self.ghosts=[];self.moves={};self.generation=0
        self.entry_effect=None;self.entry_started=None;self.entry_pending=False
        self.layout_pending=False
        self.overlay=_Exits(self)
        body.destroyed.connect(self.overlay.deleteLater)
        from display_frames import FramePulse
        self.timer=FramePulse(self);self.timer.timeout.connect(self._tick)
        # The app-level wheel router can consume a wheel event before the
        # viewport filter sees it. Stop entrance effects before that first step.
        self.scroll._flickdock_before_scroll=self.finish
        # Scroll is an immediate user operation. Never drag old pixels along
        # with it, or animate to positions from a previous viewport size.
        scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        scroll.horizontalScrollBar().valueChanged.connect(self._scrolled)
        scroll.viewport().installEventFilter(self)
        scroll.verticalScrollBar().sliderPressed.connect(self.finish)
        scroll.horizontalScrollBar().sliderPressed.connect(self.finish)

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Wheel,QEvent.KeyPress,QEvent.TouchBegin):self.finish()
        return super().eventFilter(watched,event)

    def _scrolled(self,*_):
        if not self.layout_pending and not self.entry_pending:self.finish()

    def capture(self,widgets):
        self.generation+=1
        self.layout_pending=True
        positions={ref:QPoint(widget.pos()) for ref,widget in widgets.items() if isValid(widget)}
        # A second edit starts at each row's CURRENT painted position.
        self._finish_entrances()
        self.moves.clear()
        return positions

    def prepare_entrance(self,*,enabled):
        from ui_transitions import motion_allowed
        if not enabled or not motion_allowed():return False
        viewport=self.scroll.viewport()
        if self.entry_effect is None:
            if viewport.graphicsEffect() is not None:return False
            self.entry_effect=_EntryEffect(viewport);viewport.setGraphicsEffect(self.entry_effect)
        self.entry_effect.set_progress(0);self.entry_effect.setEnabled(True)
        self.entry_pending=True;self.entry_started=None
        return True

    def enter(self):
        if not self.entry_pending:return
        self.entry_pending=False;self.entry_started=time.perf_counter();self.timer.start()

    def _finish_entrances(self):
        self.entry_started=None;self.entry_pending=False
        if self.entry_effect is not None and isValid(self.entry_effect):
            self.entry_effect.set_progress(1);self.entry_effect.setEnabled(False)

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
            if not enabled:self.finish();return
            now=time.perf_counter()
            viewport=self.scroll.viewport()
            for ref,widget in widgets.items():
                if ref not in positions or not isValid(widget):continue
                target=QPoint(widget.pos());origin=positions[ref]
                if origin==target:continue
                at=widget.mapTo(viewport,QPoint())
                if not viewport.rect().intersects(widget.rect().translated(at)):continue
                self.moves[ref]=(widget,origin,target,now)
                widget.move(origin)
            if self.moves or self.ghosts:self.timer.start()
        QTimer.singleShot(0,begin)

    def _tick(self):
        now=time.perf_counter()
        if self.entry_started is not None and self.entry_effect is not None and isValid(self.entry_effect):
            progress=min(1,max(0,(now-self.entry_started)/self.duration))
            self.entry_effect.set_progress(self.fade.valueForProgress(progress))
            if progress>=1:self._finish_entrances()
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
        if not self.moves and not self.ghosts and self.entry_started is None:
            self.timer.stop();self.settled.emit()

    def finish(self,*_):
        running=self.timer.isActive() or self.entry_pending or self.entry_started is not None or bool(self.moves or self.ghosts)
        for widget,origin,target,started in self.moves.values():
            if isValid(widget):widget.move(target)
        self.moves.clear();self.ghosts.clear();self._finish_entrances();self.timer.stop();self.overlay.hide()
        if running:self.settled.emit()
