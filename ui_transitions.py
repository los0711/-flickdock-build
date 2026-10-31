"""Opacity-only utility transitions, separate from the native shelf compositor."""
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QPointF,QRectF,Qt,QTimer,QVariantAnimation,QEasingCurve
from PySide6.QtGui import QGuiApplication,QPainter
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid


class SegmentIndicator(QWidget):
    """One painted moving selection, behind immediately usable buttons."""
    def __init__(self,owner,buttons):
        super().__init__(owner);self.buttons=tuple(buttons);self._rect=QRectF();self._pending=False
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground)
        self.animation=QVariantAnimation(self);self.animation.setDuration(160);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)
        owner.installEventFilter(self)
        for button in self.buttons:button.toggled.connect(lambda checked:self.schedule() if checked else None)
        self.lower();self.show();self.schedule()

    def schedule(self):
        if isValid(self) and not self._pending:self._pending=True;QTimer.singleShot(0,self,self._move)

    def _move(self):
        if not isValid(self):return
        self._pending=False
        if not isValid(self.parentWidget()):return
        self.setGeometry(self.parentWidget().rect());self.lower()
        chosen=next((button for button in self.buttons if button.isChecked()),None)
        if chosen is None:self.animation.stop();self._frame(QRectF());return
        target=QRectF(chosen.geometry())
        if self._rect==target:return
        self.animation.stop()
        if self._rect.isEmpty() or not self.isVisible() or not motion_allowed():self._frame(target);return
        self.animation.setStartValue(self._rect);self.animation.setEndValue(target);self.animation.start()

    def _frame(self,rect):self._rect=QRectF(rect);self.update()
    def set_theme(self,theme):self._theme=theme;self.update()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Hide:
            self.animation.stop();self._rect=QRectF()
        elif event.type() in (QEvent.Show,QEvent.Resize,QEvent.LayoutRequest):self.schedule()
        elif event.type() in (QEvent.StyleChange,QEvent.PaletteChange):self.update()
        return False

    def paintEvent(self,event):
        if self._rect.isEmpty():return
        from PySide6.QtGui import QColor
        import ui_theme
        theme=getattr(self,'_theme',ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen);p.setBrush(QColor(255,255,255,31) if theme=='dark' else QColor('white'))
        p.drawRoundedRect(self._rect,5,5);p.end()


def motion_allowed():
    from native_ui_common import reduced_motion
    return not reduced_motion()


def exit_curve():
    curve=QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    return curve


class DwellHint(QObject):
    """A single two-pixel glyph lift after deliberate hover; never an idle loop."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.value=0.
        self.wait=QTimer(self);self.wait.setSingleShot(True);self.wait.setInterval(1400)
        self.wait.timeout.connect(self._start)
        self.animation=QVariantAnimation(self);self.animation.setDuration(240)
        self.animation.setEasingCurve(exit_curve())
        self.animation.setKeyValueAt(0.,0.);self.animation.setKeyValueAt(.45,1.);self.animation.setKeyValueAt(1.,0.)
        self.animation.valueChanged.connect(self._frame);owner.installEventFilter(self)

    def enter(self):
        self.stop()
        if motion_allowed() and self.owner.isVisible():self.wait.start()

    def _start(self):
        if self.owner.isVisible() and self.owner.isEnabled() and motion_allowed():self.animation.start()

    def _frame(self,value):
        self.value=float(value)
        if isValid(self.owner):self.owner.update()

    def stop(self):
        self.wait.stop();self.animation.stop();self._frame(0.)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.owner) or not isValid(self.animation):return False
        if event.type() in (QEvent.Hide,QEvent.Leave,QEvent.MouseButtonPress,QEvent.DragEnter):self.stop()
        elif event.type()==QEvent.EnabledChange and not self.owner.isEnabled():self.stop()
        return False


class ToolbarEntrance(QObject):
    """One finite clock for all compact header glyphs, with fixed hit regions."""
    def __init__(self,owner,controls):
        super().__init__(owner);self.owner=owner;self.controls=controls;self.progress=1.
        self.animation=QVariantAnimation(self);self.animation.setDuration(240)
        self.animation.valueChanged.connect(self._frame);self.curve=exit_curve()
        owner.installEventFilter(self)

    def reveal(self,*,reset=False):
        if reset:self._frame(0.)
        self._target(1.)

    def conceal(self):self._target(0.)

    def _target(self,target):
        self.animation.stop()
        if not motion_allowed():self._frame(1.);return
        current=self.progress
        self.animation.setDuration(240 if target else 160)
        self.animation.setStartValue(current);self.animation.setEndValue(target);self.animation.start()

    def _frame(self,value):
        self.progress=float(value);controls=self.controls()
        for index,control in enumerate(controls):
            local=max(0.,min(1.,(self.progress-index*.055)/(1-max(0,len(controls)-1)*.055)))
            local=self.curve.valueForProgress(local)
            control._entry_alpha=local;control._entry_dx=6*(1-local);control.update()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Hide:
            self.animation.stop();self._frame(1.)
        return False


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
