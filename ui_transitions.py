"""Opacity-only utility transitions, separate from the native shelf compositor."""
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QPointF,Qt,QVariantAnimation,QEasingCurve
from PySide6.QtGui import QGuiApplication,QPainter
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid


def motion_allowed():
    from native_ui_common import reduced_motion
    return not reduced_motion()


def exit_curve():
    curve=QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    return curve


def fade_hide(window):
    """Hide without emitting dialog rejection or changing business state."""
    fade=getattr(window,'_window_fade',None)
    if fade is None or not fade.defer_done(None,lambda _:window.hide()):window.hide()


class WindowFade(QObject):
    """Keep native focus, shadow and hit testing; never animate geometry."""
    def __init__(self,window):
        super().__init__(window);self.window=window;self.pending=None;self.completing=False
        self.animation=QVariantAnimation(self);self.animation.setDuration(180)
        self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(lambda value:window.setWindowOpacity(float(value)))
        self.animation.finished.connect(self._finished);window.installEventFilter(self)

    def enabled(self):
        return QGuiApplication.platformName() not in ('offscreen','minimal') and motion_allowed()

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.window):return False
        if event.type()==QEvent.Show and not event.spontaneous() and not self.window.isMinimized():
            self.pending=None;self.animation.stop()
            if self.enabled():
                self.window.setWindowOpacity(.78);self._start(1.0)
            else:self.window.setWindowOpacity(1.0)
        elif event.type()==QEvent.Hide:
            self.animation.stop();self.pending=None;self.window.setWindowOpacity(1.0)
        elif event.type()==QEvent.WindowStateChange and self.window.isMinimized():
            self.animation.stop();self.pending=None;self.window.setWindowOpacity(1.0)
        return super().eventFilter(watched,event)

    def _start(self,target):
        self.animation.stop()
        self.animation.setStartValue(self.window.windowOpacity())
        self.animation.setEndValue(target);self.animation.start()

    def defer_done(self,result,callback):
        if self.completing or not self.window.isVisible() or not self.enabled():return False
        if self.pending is None:
            self.pending=lambda:callback(result);self._start(0.0)
        return True

    def reopen(self):
        # QWidget.show() on an already visible fading window emits no Show.
        # Explicit open intent must reverse the pending close from this frame.
        if self.pending is not None:
            self.pending=None;self.animation.stop()
            if self.enabled():self._start(1.0)
            else:self.window.setWindowOpacity(1.0)

    def _finished(self):
        callback,self.pending=self.pending,None
        if callback:
            self.completing=True
            try:callback()
            finally:self.completing=False


class SnapshotFade(QWidget):
    """Fade the old page over immediately usable new contents, with no effects replacement."""
    def __init__(self,parent):
        super().__init__(parent);self.snapshot=None;self.alpha=0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground);self.hide()
        self.animation=QVariantAnimation(self);self.animation.setDuration(180)
        self.animation.setEasingCurve(exit_curve());self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.finish)
        parent.installEventFilter(self)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.animation):return False
        if event.type() in (QEvent.Hide,QEvent.Resize):self.finish()
        return super().eventFilter(watched,event)

    def change(self,mutation):
        owner=self.parentWidget()
        if not owner.isVisible() or not motion_allowed():
            self.finish();mutation();return
        # grab includes any existing partial transition, so quick reversals
        # continue from the visible frame rather than a stale original page.
        snapshot=owner.grab()
        self.animation.stop();mutation()
        self.snapshot=snapshot;self.alpha=1.0
        self.setGeometry(owner.rect());self.show();self.raise_()
        self.animation.setStartValue(1.0);self.animation.setEndValue(0.0);self.animation.start()

    def _frame(self,value):self.alpha=float(value);self.update()

    def finish(self):
        self.animation.stop();self.hide();self.snapshot=None;self.alpha=0.0

    def paintEvent(self,event):
        if self.snapshot is None:return
        painter=QPainter(self);painter.setOpacity(self.alpha)
        painter.drawPixmap(QPoint(),self.snapshot);painter.end()
