"""Child-only transfer feedback; data submission never waits for these clocks."""
from pathlib import Path
from PySide6.QtCore import QEvent,QPoint,QPointF,QRect,QRectF,Qt,QTimer,QVariantAnimation
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget,QGraphicsEffect
from shiboken6 import isValid
from ui_transitions import exit_curve,motion_allowed


class DropFeedback(QWidget):
    def __init__(self,owner):
        super().__init__(owner);self.value=1.;self.pixmap=None;self.callback=None
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground)
        self.animation=QVariantAnimation(self);self.animation.setDuration(220);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self._finish)
        owner.installEventFilter(self);self.hide()

    def play(self,paths,origin,target,callback=None):
        self.cancel()
        owner=self.parentWidget()
        if not owner.isVisible() or not motion_allowed():
            if callable(callback):callback()
            return
        # Lexical suffix only: no native provider/stat/thumbnail on the drop path.
        from flickdock_icons import document_pixmap
        self.type_label=Path(paths[0]).suffix.lstrip('.').upper() if paths else ''
        self.pixmap=document_pixmap(self.type_label,44,min(3,len(paths)),owner.devicePixelRatioF())
        self.target=QPointF(target);delta=QPointF(origin)-self.target
        distance=(delta.x()**2+delta.y()**2)**.5
        if distance>64:delta*=64/distance
        if distance<16:delta=QPointF(-26,-30)
        self.origin=self.target+delta;self.callback=callback
        self.setGeometry(owner.rect());self.show();self.raise_()
        self.animation.setStartValue(0.);self.animation.setEndValue(1.);self.animation.start()

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():self._finish();return
        self.value=float(value);self.update()

    def point(self):
        t=self.value
        return self.origin*(1-t)+self.target*t-QPointF(0,72*t*(1-t))

    def cancel(self):
        self.animation.stop();self.callback=None;self.pixmap=None;self.hide()

    def _finish(self):
        callback=self.callback;self.cancel()
        if callable(callback):callback()

    def eventFilter(self,watched,event):
        if not isValid(self.animation):return False
        if event.type()==QEvent.Resize:
            callback=self.callback;self.cancel()
            if callable(callback):QTimer.singleShot(0,self.parentWidget(),callback)
        elif event.type() in (QEvent.Hide,QEvent.Close):self.cancel()
        return False

    def paintEvent(self,event):
        if self.pixmap is None:return
        p=QPainter(self);p.setRenderHint(QPainter.SmoothPixmapTransform)
        t=self.value;point=self.point();scale=1-.25*t
        p.setOpacity(min(1.,max(0.,(1-t)/.18)))
        p.translate(point);p.scale(scale,scale)
        p.drawPixmap(QRectF(-22,-22,44,44),self.pixmap,QRectF(self.pixmap.rect()));p.end()


class ScaleFade(QGraphicsEffect):
    """Paint transform at the widget's true layout position, with no geometry tween."""
    def __init__(self,widget,*,scale=.96,opacity=0.,viewport=None):
        super().__init__(widget);self.widget=widget;self.viewport=viewport;self.initial_scale=scale;self.initial_opacity=opacity
        self.value=1.;self.pending=False;self.motion_offset=QPointF();self._motion_extent=QPointF()
        self.animation=QVariantAnimation(self);self.animation.setDuration(180);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self.cancel)
        widget.setGraphicsEffect(self);widget.installEventFilter(self);self.setEnabled(False)

    def arm(self):
        self.pending=True
        if self.widget.isVisible():self.start()

    def start(self):
        self.pending=False;self.animation.stop()
        if not self.widget.isVisible() or not motion_allowed():self.cancel();return
        if self.viewport is not None:
            if not isValid(self.viewport) or not self.viewport.rect().intersects(QRect(self.widget.mapTo(self.viewport,QPoint()),self.widget.size())):
                self.cancel();return
        self.setEnabled(True);self._frame(0.)
        self.animation.setStartValue(0.);self.animation.setEndValue(1.);self.animation.start()

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():self.cancel();return
        self.value=float(value);self.update()

    def cancel(self):
        self.pending=False;self.animation.stop();self.value=1.;self.motion_offset=QPointF();self._motion_extent=QPointF();self.setEnabled(False)

    def set_motion_offset(self,offset):
        if not motion_allowed():self.cancel();return
        self.animation.stop();self.pending=False;self.value=1.;self.motion_offset=QPointF(offset)
        self._motion_extent=QPointF(max(abs(offset.x()),self._motion_extent.x()),max(abs(offset.y()),self._motion_extent.y()))
        self.setEnabled(True);self.updateBoundingRect();self.update()

    def boundingRectFor(self,rect):
        return rect.adjusted(-self._motion_extent.x(),-self._motion_extent.y(),self._motion_extent.x(),self._motion_extent.y())

    def eventFilter(self,watched,event):
        if not isValid(self.widget) or not isValid(self.animation):return False
        if event.type() in (QEvent.Hide,QEvent.Close):self.cancel()
        elif event.type()==QEvent.Show and self.pending:self.start()
        return False

    def draw(self,painter):
        # Qt's drawSource fast path can bypass the painter transform/opacity.
        # Composite the cached source explicitly so the feedback affects pixels.
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.NoPad)
        center=self.widget.rect().center();scale=self.initial_scale+(1-self.initial_scale)*self.value
        painter.save();painter.setOpacity(self.initial_opacity+(1-self.initial_opacity)*self.value)
        painter.translate(self.motion_offset)
        painter.translate(center);painter.scale(scale,scale);painter.translate(-center)
        painter.drawPixmap(offset,source);painter.restore()
