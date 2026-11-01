"""Viewport-bounded category entrances. No data mutation, layout move or per-item clock.

Use reveal(items, viewport) after a category's new layout is ready. Incremental
builders may token=arm(viewport), then batch(new_widgets, token=token) after each
layout slice. Normal metadata refreshes must not arm a category entrance.
"""
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QRect,Qt,QVariantAnimation,QEasingCurve
from PySide6.QtWidgets import QWidget,QGraphicsEffect,QAbstractScrollArea
from shiboken6 import isValid
from ui_transitions import exit_curve,motion_allowed


class _EntranceEffect(QGraphicsEffect):
    def __init__(self,widget):
        super().__init__(widget);self.alpha=0.;self.dy=6.

    def boundingRectFor(self,rect):return rect.adjusted(0,-6,0,6)

    def draw(self,painter):
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.NoPad)
        if source.isNull():return
        painter.save();painter.setOpacity(self.alpha);painter.translate(0,self.dy)
        painter.drawPixmap(offset,source);painter.restore()


class ContentEntrance(QObject):
    item_duration=160
    stagger=24
    delay_limit=96
    max_items=128

    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.viewport=None;self.generation=0
        self.entries={};self.seen=set();self.skipped_effects=0;self._armed=False;self._started=False;self._slots=0;self._line_delays={};self._bars=[]
        self.curve=exit_curve()
        self.animation=QVariantAnimation(self);self.animation.setDuration(self.item_duration+self.delay_limit)
        self.animation.setEasingCurve(QEasingCurve.Linear);self.animation.setStartValue(0.);self.animation.setEndValue(float(self.animation.duration()))
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self.cancel)
        owner.installEventFilter(self)

    @property
    def active_count(self):return len(self.entries)

    def _bind(self,viewport):
        if self.viewport is viewport:return
        if self.viewport is not None and isValid(self.viewport):self.viewport.removeEventFilter(self)
        for bar in self._bars:
            if isValid(bar):
                try:bar.valueChanged.disconnect(self.cancel)
                except RuntimeError:pass
        self._bars=[];self.viewport=viewport
        if viewport is not None:
            viewport.installEventFilter(self)
            area=viewport.parentWidget()
            if isinstance(area,QAbstractScrollArea):
                self._bars=[area.verticalScrollBar(),area.horizontalScrollBar()]
                for bar in self._bars:bar.valueChanged.connect(self.cancel)

    def arm(self,viewport):
        self.cancel();self._bind(viewport);self.seen.clear();self.skipped_effects=0;self._slots=0;self._line_delays={};self._started=False
        self._armed=bool(isValid(self.owner) and self.owner.isVisible() and viewport is not None and isValid(viewport) and viewport.isVisible() and motion_allowed())
        return self.generation

    def reveal(self,items,viewport):
        token=self.arm(viewport);self.batch(items,token=token);return token

    def batch(self,items,*,token=None):
        if token is not None and token!=self.generation:return ()
        if not self._armed or not motion_allowed():
            if self._armed:self.cancel()
            return ()
        viewport=self.viewport
        if not isValid(viewport) or not viewport.isVisible():self.cancel();return ()
        current=float(self.animation.currentTime()) if self._started else 0.
        if current>=self.animation.duration():return ()
        widgets=[w for w in items if isinstance(w,QWidget) and isValid(w)]
        candidates={id(w) for w in widgets};added=[]
        for widget in widgets:
            key=id(widget)
            if key in self.seen or len(self.seen)>=self.max_items:continue
            if not widget.isVisible() or widget.window() is not viewport.window():continue
            rect=QRect(widget.mapTo(viewport,QPoint()),widget.size())
            if not viewport.rect().intersects(rect):continue
            ancestor=widget.parentWidget();nested=False
            while ancestor is not None:
                if id(ancestor) in candidates:nested=True;break
                ancestor=ancestor.parentWidget()
            if nested:continue
            self.seen.add(key)
            if widget.graphicsEffect() is not None:self.skipped_effects+=1;continue
            # Separate label/editor widgets on one visual line move together.
            y=rect.center().y();line=next((at for at in self._line_delays if abs(at-y)<=3),y)
            if line not in self._line_delays:
                self._line_delays[line]=min(self._slots*self.stagger,self.delay_limit);self._slots+=1
            delay=self._line_delays[line]
            if current>=delay+self.item_duration:continue
            effect=_EntranceEffect(widget);widget.setGraphicsEffect(effect);widget.installEventFilter(self)
            self.entries[key]=(widget,effect,delay);widget._content_entrance_owner=self;added.append(widget)
        if added:
            if not self._started:self._started=True;self.animation.start()
            self._frame(float(self.animation.currentTime()))
        return tuple(added)

    def _release(self,key):
        entry=self.entries.pop(key,None)
        if entry is None:return
        widget,effect,_=entry
        if isValid(widget):
            widget.removeEventFilter(self)
            if getattr(widget,'_content_entrance_owner',None) is self:del widget._content_entrance_owner
            if widget.graphicsEffect() is effect:widget.setGraphicsEffect(None)

    def cancel_item(self,widget):self._release(id(widget))

    def _frame(self,elapsed):
        if not motion_allowed():self.cancel();return
        for key,(widget,effect,delay) in tuple(self.entries.items()):
            if not isValid(widget) or not isValid(effect) or not widget.isVisible():self._release(key);continue
            fraction=max(0.,min(1.,(float(elapsed)-delay)/self.item_duration))
            if fraction>=1:self._release(key);continue
            value=self.curve.valueForProgress(fraction);effect.alpha=value;effect.dy=6*(1-value);effect.update()

    def cancel(self,*_):
        self.generation+=1;self._armed=False
        if isValid(self.animation):self.animation.stop()
        for key in tuple(self.entries):self._release(key)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.animation):return False
        kind=event.type()
        if watched is self.owner or watched is self.viewport:
            if kind in (QEvent.Hide,QEvent.Close,QEvent.Resize,QEvent.Wheel):self.cancel()
        elif kind in (QEvent.Hide,QEvent.Close):self.cancel_item(watched)
        return False
