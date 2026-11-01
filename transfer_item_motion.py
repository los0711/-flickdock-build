"""Paint-only removal/reflow for transfer history and two-column payload queues.

Capture before the immediate data/layout mutation, then reflow the surviving
widgets. One bounded clock owns cached ghosts and temporary survivor offsets;
no timer or callback changes files, records, selection or layout geometry.
"""
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QPointF,QRect,QRectF,Qt,QTimer,QVariantAnimation,QEasingCurve
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGraphicsEffect,QWidget
from shiboken6 import isValid
from ui_transitions import exit_curve,motion_allowed


class _MoveEffect(QGraphicsEffect):
    def __init__(self,widget,offset):
        super().__init__(widget);self.motion_offset=QPointF(offset);self.extent=QPointF(abs(offset.x()),abs(offset.y()))
    def boundingRectFor(self,rect):return rect.adjusted(-self.extent.x(),-self.extent.y(),self.extent.x(),self.extent.y())
    def set_motion_offset(self,offset):self.motion_offset=QPointF(offset);self.update()
    def draw(self,painter):
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.NoPad)
        painter.save();painter.translate(self.motion_offset);painter.drawPixmap(offset,source);painter.restore()


class _GhostLayer(QWidget):
    def __init__(self,owner,controller):
        super().__init__(owner);self.controller=controller
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground);self.hide()
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.SmoothPixmapTransform)
        for ghost in self.controller.ghosts:
            rect,alpha,scale=self.controller._ghost_frame(ghost)
            painter.save();painter.setClipRect(ghost['clip']);painter.setOpacity(alpha)
            painter.translate(rect.center());painter.scale(scale,scale)
            painter.drawPixmap(QRectF(-rect.width()/2,-rect.height()/2,rect.width(),rect.height()),ghost['pixmap'],QRectF(ghost['pixmap'].rect()))
            painter.restore()


class TransferItemMotion(QObject):
    duration=200
    max_ghosts=32
    max_survivors=24
    def __init__(self,owner,viewport):
        super().__init__(owner);self.owner=owner;self.viewport=viewport;self.ghosts=[];self.moves={};self.value=1.;self.generation=0
        self.layer=_GhostLayer(owner,self);self.animation=QVariantAnimation(self)
        self.curve=exit_curve();self._staggered=False
        self.animation.setDuration(self.duration);self.animation.setEasingCurve(exit_curve())
        self.animation.setStartValue(0.);self.animation.setEndValue(1.)
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self.cancel)
        self.layout_timer=QTimer(self);self.layout_timer.setSingleShot(True);self.layout_timer.timeout.connect(self._start_pending);self.pending=None
        self._scrollbars=[];self._scroll_before={}
        owner.installEventFilter(self);viewport.installEventFilter(self)
        area=viewport.parentWidget()
        for name in ('verticalScrollBar','horizontalScrollBar'):
            getter=getattr(area,name,None)
            if callable(getter):
                bar=getter();self._scrollbars.append(bar)
                bar.valueChanged.connect(lambda value,bar=bar:self._scroll_changed(bar,value))
                bar.actionTriggered.connect(self.cancel);bar.sliderPressed.connect(self.cancel)

    def _scroll_changed(self,bar,value):
        previous=self._scroll_before.get(bar)
        if previous is not None and ((previous>bar.maximum() and value==bar.maximum()) or (previous<bar.minimum() and value==bar.minimum())):
            # Removing rows shrinks the range and clamps it automatically.
            # This is part of the same layout transaction, not user scrolling.
            return
        self.cancel()

    def _allowed(self):
        return bool(isValid(self.owner) and self.owner.isVisible() and motion_allowed())

    def _rect(self,widget):return QRectF(QPointF(widget.mapTo(self.owner,QPoint())),widget.size())

    def _clip(self):return QRectF(QPointF(self.viewport.mapTo(self.owner,QPoint())),self.viewport.size()).intersected(QRectF(self.owner.rect()))

    def _ghost_frame(self,ghost):
        rect=QRectF(ghost['rect']);t=self.value
        if self._staggered:
            elapsed=self.value*self.animation.duration()
            t=self.curve.valueForProgress(max(0.,min(1.,(elapsed-ghost.get('delay',0))/self.duration)))
        if ghost['style']=='slide':rect.translate(-min(88.,rect.width()*.25)*t,0)
        return rect,ghost['alpha']*(1-t),ghost['scale']*(1-.04*t if ghost['style']=='shrink' else 1)

    def _release_moves(self):
        for widget,effect,_,borrowed in tuple(self.moves.values()):
            if not isValid(widget):continue
            if widget.graphicsEffect() is not effect:continue
            if borrowed:effect.cancel()
            else:widget.setGraphicsEffect(None)
        self.moves.clear()

    def capture(self,widgets):
        """Keyed old positions, including an interrupted paint offset."""
        positions={}
        if self._allowed() and self.viewport.isVisible():
            clip=self._clip()
            for key,widget in widgets.items():
                if not isValid(widget) or not widget.isVisible():continue
                rect=self._rect(widget);effect=widget.graphicsEffect()
                if effect is not None and effect.isEnabled():rect.translate(getattr(effect,'motion_offset',QPointF()))
                if rect.intersects(clip):positions[key]=rect
        # Retain the current ghost pixels/position during rapid successive edits.
        for ghost in self.ghosts:
            rect,alpha,scale=self._ghost_frame(ghost);ghost.update(rect=rect,alpha=alpha,scale=scale,delay=0)
        self.animation.stop();self.layout_timer.stop();self.pending=None;self._release_moves();self.value=0.;self.generation+=1
        self._staggered=False
        self._scroll_before={bar:bar.value() for bar in self._scrollbars if isValid(bar)}
        if not self._allowed():self.cancel()
        return positions

    def remove(self,widgets,positions,*,style,stagger=False):
        if not self._allowed() or not self.viewport.isVisible():return
        clip=self._clip()
        for key,widget in widgets.items():
            rect=positions.get(key)
            if rect is None or not isValid(widget) or not widget.isVisible():continue
            delay=sum(1 for ghost in self.ghosts if ghost.get('batch')==self.generation)*24 if stagger else 0
            self.ghosts.append(dict(pixmap=widget.grab(),rect=QRectF(rect),clip=QRectF(clip),style=style,alpha=1.,scale=1.,delay=delay,batch=self.generation))
        self.ghosts=self.ghosts[-self.max_ghosts:]

    def reflow(self,widgets,positions):
        if not self._allowed():self.cancel();return
        self.pending=(dict(widgets),positions);self.layout_timer.start(0)

    def _start_pending(self):
        pending=self.pending;self.pending=None
        if pending is None or not self._allowed():self.cancel();return
        widgets,positions=pending;clip=self._clip()
        for key,widget in widgets.items():
            if len(self.moves)>=self.max_survivors:break
            if key not in positions or not isValid(widget) or not widget.isVisible():continue
            rect=self._rect(widget)
            if not rect.intersects(clip):continue
            offset=positions[key].topLeft()-rect.topLeft()
            if abs(offset.x())+abs(offset.y())<.5:continue
            entrance_owner=getattr(widget,'_content_entrance_owner',None)
            if entrance_owner is not None:entrance_owner.cancel_item(widget)
            effect=widget.graphicsEffect();borrowed=False
            if effect is not None:
                # Only our queue entrance can be borrowed; preserve shadows/media.
                if effect is not getattr(widget,'entrance',None) or not hasattr(effect,'set_motion_offset'):continue
                effect.cancel();borrowed=True
            else:effect=_MoveEffect(widget,offset);widget.setGraphicsEffect(effect)
            effect.set_motion_offset(offset);self.moves[id(widget)]=(widget,effect,offset,borrowed)
        if not self.moves and not self.ghosts:return
        delay=max((ghost.get('delay',0) for ghost in self.ghosts),default=0)
        self._staggered=bool(delay)
        self.animation.setDuration(self.duration+delay)
        self.animation.setEasingCurve(QEasingCurve.Linear if self._staggered else self.curve)
        self.layer.setGeometry(self.owner.rect());self.layer.setVisible(bool(self.ghosts));self.layer.raise_()
        self.animation.start()

    def _frame(self,value):
        if not self._allowed():self.cancel();return
        self.value=float(value)
        movement=self.curve.valueForProgress(self.value) if self._staggered else self.value
        for widget,effect,offset,_ in tuple(self.moves.values()):
            if isValid(widget) and isValid(effect) and widget.graphicsEffect() is effect:effect.set_motion_offset(offset*(1-movement))
        self.layer.update()

    def cancel(self,*_):
        self.generation+=1;self.pending=None
        self._scroll_before={}
        if isValid(self.animation):self.animation.stop()
        if isValid(self.layout_timer):self.layout_timer.stop()
        self._release_moves();self.ghosts.clear();self.value=1.
        if isValid(self.layer):self.layer.hide()

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.animation):return False
        kind=event.type()
        # Hiding just the empty queue viewport must not erase its final ghost.
        if (watched is self.owner and kind in (QEvent.Hide,QEvent.Close,QEvent.Resize)) or kind==QEvent.Wheel:self.cancel()
        return False
