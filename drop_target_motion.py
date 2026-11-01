"""Shared drop-target artwork. One local clock, no layout or payload changes."""
from time import perf_counter
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPointF,QRect,QRectF,Qt,QTimer
from PySide6.QtGui import QColor,QPainterPath,QPen
from shiboken6 import isValid


def target_path(rect,radius=12):
    rect=QRectF(rect);r=max(0,min(radius,rect.width()/2,rect.height()/2))
    path=QPainterPath();path.addRoundedRect(rect,r,r);return path


class DropTargetMotion(QObject):
    duration=.16
    def __init__(self,owner,rect,*,radius=12,pointer_source=None,enabled=None,changed=None):
        super().__init__(owner);self.owner=owner;self.rect=rect;self.radius=radius
        self.pointer_source=pointer_source or owner;self.enabled=enabled or (lambda:True)
        self.changed=changed;self.hovered=False;self.dragged=False;self.value=0.;self.phase=0.
        self._from=0.;self._target=0.;self._started=perf_counter();self._last=self._started;self._point=None;self._reduced=False
        from ui_transitions import exit_curve
        self.curve=exit_curve();self.timer=QTimer(self);self.timer.setInterval(16);self.timer.timeout.connect(self._tick)
        owner.installEventFilter(self)
        if self.pointer_source is not owner:self.pointer_source.installEventFilter(self)
        self.pointer_source.setMouseTracking(True)

    @property
    def active(self):return bool(self.enabled() and (self.hovered or self.dragged))

    @property
    def icon_scale(self):
        return 1. if self._reduced else 1.+.06*self.value

    def _notify(self):
        if not isValid(self.owner):return
        self.owner.update()
        if self.changed:self.changed()

    def _sample(self,now):
        elapsed=max(0.,min(1.,(now-self._started)/self.duration))
        self.value=self._from+(self._target-self._from)*self.curve.valueForProgress(elapsed)
        return elapsed>=1.

    def _retarget(self):
        if not isValid(self.owner):return
        now=perf_counter();self._sample(now)
        target=float(self.active)
        from native_ui_common import reduced_motion
        self._reduced=reduced_motion()
        if not self.owner.isVisible() or self._reduced:
            self._from=self._target=self.value=target if self.owner.isVisible() else 0.
            self.timer.stop();self._notify();return
        if target!=self._target:
            self._from=self.value;self._target=target;self._started=now
        if self.active or abs(self.value-self._target)>.0001:
            if not self.timer.isActive():self._last=now;self.timer.start()
        else:self.timer.stop()
        self._notify()

    def set_drag_active(self,active):
        active=bool(active)
        if active==self.dragged:return
        self.dragged=active;self._retarget()

    def pointer_at(self,point):
        self._point=QPointF(point)
        hovered=bool(self.enabled() and target_path(self.rect(),self.radius).contains(self._point))
        if hovered!=self.hovered:self.hovered=hovered;self._retarget()

    def refresh(self):
        if self._point is not None:self.pointer_at(self._point)
        if not self.enabled():self.stop()
        else:self._retarget()

    def stop(self):
        if isValid(self.timer):self.timer.stop()
        self.hovered=self.dragged=False;self._point=None
        self.value=self._from=self._target=0.;self._notify()

    def _tick(self,now=None):
        if not isValid(self.owner) or not self.owner.isVisible() or not self.enabled():self.stop();return
        now=perf_counter() if now is None else now
        from native_ui_common import reduced_motion
        self._reduced=reduced_motion()
        if self._reduced:
            self.value=self._from=self._target=float(self.active);self.timer.stop();self._notify();return
        if self.active:self.phase=(self.phase+max(0.,now-self._last)*20.)%4096.
        self._last=now;finished=self._sample(now)
        if finished and not self.active:self.timer.stop()
        self._notify()

    def eventFilter(self,watched,event):
        owner=getattr(self,'owner',None)
        if owner is None or not hasattr(self,'timer'):return False
        if QCoreApplication.closingDown() or not isValid(owner):return False
        kind=event.type()
        if kind in (QEvent.Hide,QEvent.Close):self.stop()
        elif watched is self.pointer_source:
            if kind in (QEvent.MouseMove,QEvent.Enter) and hasattr(event,'globalPosition'):
                self.pointer_at(owner.mapFromGlobal(event.globalPosition().toPoint()))
            elif kind==QEvent.Leave:
                if self.hovered:self.hovered=False;self._retarget()
        return False


def _mix(start,end,value):
    return QColor(*(round(a+(b-a)*value) for a,b in zip(start.getRgb(),end.getRgb())))


def paint_target(painter,rect,motion,*,dark=False,radius=12,base_fill=None):
    """Unscaled path and whole dash cycles keep every corner uniform."""
    path=target_path(rect,radius);value=motion.value
    if path.isEmpty():return
    fill=QColor(255,255,255,7) if dark else QColor(0,0,0,5)
    if base_fill is not None:fill=QColor(base_fill)
    painter.fillPath(path,_mix(fill,QColor(10,132,255,25 if dark else 18),value))
    stroke=_mix(QColor(255,255,255,55) if dark else QColor(0,0,0,48),QColor('#0A84FF'),value)
    width=1.2;cycle=path.length()/max(1,round(path.length()/8.))
    pen=QPen(stroke,width);pen.setDashPattern([cycle*.52/width,cycle*.48/width])
    pen.setCapStyle(Qt.FlatCap);pen.setJoinStyle(Qt.RoundJoin);pen.setDashOffset((motion.phase/width)%(cycle/width))
    painter.setPen(pen);painter.setBrush(Qt.NoBrush);painter.drawPath(path)


def draw_drop_glyph(painter,rect,motion,*,dark=False):
    from native_ui_common import icon,colors
    rect=QRectF(rect);center=rect.center();painter.save()
    scale=motion.icon_scale if motion is not None else 1.;value=motion.value if motion is not None else 0.
    painter.translate(center);painter.scale(scale,scale);painter.translate(-center)
    box=QRect(round(rect.x()),round(rect.y()),round(rect.width()),round(rect.height()))
    icon('upload',colors('dark' if dark else 'light')['secondary']).paint(painter,box)
    if value>0:
        painter.setOpacity(painter.opacity()*value);icon('upload','#0A84FF').paint(painter,box)
    painter.restore()
