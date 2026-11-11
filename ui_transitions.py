"""Utility-window transitions, separate from the native shelf compositor."""
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QPointF,QRectF,Qt,QTimer,QEasingCurve,Signal,Slot
from PySide6.QtGui import QGuiApplication,QPainter,QPixmap
from PySide6.QtWidgets import QWidget,QGraphicsEffect,QGraphicsOpacityEffect
from shiboken6 import isValid
from display_frames import DisplayAnimation

UTILITY_TRANSITION_MS=180
COPY_NOTICE_MORPH_MS=420
COPY_NOTICE_FADE_MS=160
COPY_NOTICE_OVERSHOOT=1.15
NOTICE_SPLIT_MS=260
NOTICE_SPLIT_BRIDGE_END=.70
WINDOW_MIN_SCALE=.96
WINDOW_OPEN_OPACITY=.78
SHELF_REVEAL_RESPONSE_SECONDS=.34
SHELF_REVEAL_DAMPING=.60
OVERVIEW_OPEN_DURATION_MS=360
OVERVIEW_OPEN_OVERSHOOT=.55
SURFACE_SOURCE_FADE_END=.45
SURFACE_TARGET_FADE_END=.72
STACK_SPREAD_RESPONSE_SECONDS=.72
STACK_SPREAD_DAMPING=.88
STACK_LANDING_DURATION_MS=440
STACK_PULSE_TIMEOUT_MS=950
SHARED_ELEMENT_MAX_TRAVEL_PX=600
EMPTY_STATE_ENTRY_MS=180
EMPTY_STATE_ENTRY_OFFSET_PX=6


def motion_allowed():
    from native_ui_common import reduced_motion
    return not reduced_motion()


def exit_curve():
    curve=QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    return curve


class EmptyStateEntrance(QObject):
    """One reusable, interruptible entrance for a small native empty target."""
    def __init__(self,widget,motion):
        super().__init__(widget);self.widget=widget;self.motion=motion
        assert widget.graphicsEffect() is None
        self.effect=QGraphicsOpacityEffect(widget);self.effect.setEnabled(False);widget.setGraphicsEffect(self.effect)
        self.animation=DisplayAnimation(self);self.animation.setDuration(EMPTY_STATE_ENTRY_MS)
        self.animation.setEasingCurve(exit_curve());self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.finish);self.active=False;self.origin=QPoint()
        widget.installEventFilter(self)

    def begin(self):
        self.finish()
        if not self.motion() or not motion_allowed() or not self.widget.isVisible():return
        self.origin=QPoint(self.widget.pos());self.active=True;self.effect.setEnabled(True)
        self.animation.setStartValue(0.);self.animation.setEndValue(1.);self._frame(0.)
        self.animation.start()

    def _frame(self,value):
        if not self.active:return
        progress=max(0.,min(1.,float(value)))
        self.effect.setOpacity(progress)
        self.widget.move(self.origin+QPoint(0,round(EMPTY_STATE_ENTRY_OFFSET_PX*(1-progress))))

    def finish(self):
        if not self.active:return
        self.active=False;self.animation.stop();self.effect.setOpacity(1.);self.effect.setEnabled(False)
        self.widget.move(self.origin);self.widget.update()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.Resize):self.finish()
        return False


class DwellHint(QObject):
    """A single two-pixel glyph lift after deliberate hover; never an idle loop."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.value=0.
        self.wait=QTimer(self);self.wait.setSingleShot(True);self.wait.setInterval(1400)
        self.wait.timeout.connect(self._start)
        self.animation=DisplayAnimation(self);self.animation.setDuration(240)
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
        self.animation=DisplayAnimation(self);self.animation.setDuration(240)
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


class _WindowZoom(QGraphicsEffect):
    """Transform one captured surface without resizing its native window."""
    captured=Signal()
    def __init__(self,parent):
        super().__init__(parent);self.scale=1.;self.opacity=1.;self.snapshot=QPixmap();self.notify_capture=False;self.setEnabled(False)

    def reset(self):self.snapshot=QPixmap();self.scale=1.;self.opacity=1.;self.setEnabled(False)

    def sourceChanged(self,flags):
        self.snapshot=QPixmap();super().sourceChanged(flags)

    def draw(self,painter):
        if self.snapshot.isNull():
            self.offset=QPoint()
            self.snapshot=self.sourcePixmap(Qt.LogicalCoordinates,self.offset,QGraphicsEffect.NoPad)
        bounds=self.sourceBoundingRect(Qt.LogicalCoordinates);center=bounds.center()
        painter.save();painter.setRenderHint(QPainter.SmoothPixmapTransform);painter.setOpacity(self.opacity)
        painter.translate(center);painter.scale(self.scale,self.scale);painter.translate(-center)
        painter.drawPixmap(QPointF(self.offset),self.snapshot);painter.restore()
        if self.notify_capture:
            self.notify_capture=False;self.captured.emit()


class WindowFade(QObject):
    """Keep native focus, shadow and hit testing; never animate geometry."""
    def __init__(self,window):
        super().__init__(window);self.window=window;self.pending=None;self.completing=False
        self.scale=1.;self.opacity=1.;self.zoom=None;self._awaiting_capture=False
        self.animation=DisplayAnimation(self);self.animation.setDuration(UTILITY_TRANSITION_MS)
        self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self._finished);window.installEventFilter(self)

    def enabled(self):
        return QGuiApplication.platformName() not in ('offscreen','minimal') and motion_allowed()

    def _frame(self,value):
        # Blend and scale the same captured surface. Changing native window
        # alpha on every frame adds a second platform composition/update path.
        self.opacity=value.x();self.scale=value.y()
        if self.zoom is not None:
            self.zoom.scale=self.scale;self.zoom.opacity=self.opacity;self.zoom.update()

    def _reset_zoom(self):
        self._awaiting_capture=False
        self.scale=1.;self.opacity=1.
        if self.zoom is not None:self.zoom.reset()

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.window):return False
        if event.type()==QEvent.Show and not event.spontaneous() and not self.window.isMinimized():
            self.pending=None;self.animation.stop()
            if self.enabled():
                self.scale=WINDOW_MIN_SCALE;self.opacity=WINDOW_OPEN_OPACITY;self._start(1.0)
            else:self._reset_zoom();self.window.setWindowOpacity(1.0)
        elif event.type()==QEvent.Hide:
            self.animation.stop();self.pending=None;self._reset_zoom();self.window.setWindowOpacity(1.0)
        elif event.type()==QEvent.WindowStateChange and self.window.isMinimized():
            self.animation.stop();self.pending=None;self._reset_zoom();self.window.setWindowOpacity(1.0)
        return super().eventFilter(watched,event)

    def _start(self,target):
        self.animation.stop()
        if self.zoom is None:
            self.zoom=_WindowZoom(self.window);self.window.setGraphicsEffect(self.zoom)
            self.zoom.captured.connect(self._captured,Qt.QueuedConnection)
        self.zoom.setEnabled(True);self.zoom.scale=self.scale;self.zoom.opacity=self.opacity
        self.animation.setStartValue(QPointF(self.opacity,self.scale))
        self.animation.setEndValue(QPointF(target,1. if target else WINDOW_MIN_SCALE))
        self._awaiting_capture=target!=0 and self.zoom.snapshot.isNull()
        self.zoom.notify_capture=self._awaiting_capture
        if self._awaiting_capture:
            # Layout/font rendering can cost tens of milliseconds on first
            # open. Start time only after that first image has been painted.
            self.zoom.update()
        else:self.animation.start()

    @Slot()
    def _captured(self):
        if (self._awaiting_capture and isValid(self.window) and self.window.isVisible()
                and not self.zoom.snapshot.isNull()):
            self._awaiting_capture=False;self.animation.start()

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
            else:self._reset_zoom();self.window.setWindowOpacity(1.0)

    def _finished(self):
        callback,self.pending=self.pending,None
        if callback:
            self.completing=True
            try:callback()
            finally:self.completing=False
        else:self._reset_zoom()


class SnapshotFade(QWidget):
    """Fade the old page over immediately usable new contents, with no effects replacement."""
    def __init__(self,parent):
        super().__init__(parent);self.snapshot=None;self.alpha=0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground);self.hide()
        self.animation=DisplayAnimation(self);self.animation.setDuration(180)
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
