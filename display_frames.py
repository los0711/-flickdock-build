"""One native update-request clock per QWidget window, idle without animations."""
from weakref import WeakSet
import time

from PySide6.QtCore import QAbstractAnimation,QEvent,QMetaObject,QObject,Qt,QTimer,Signal,Slot,QVariantAnimation
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid


class WindowFrames(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.pulses = WeakSet()
        self.handle = None
        self.pending = False
        self.dispatching = False
        self.queued = False
        window.installEventFilter(self)

    def add(self, pulse):
        self.pulses.add(pulse)
        self.schedule()

    def discard(self, pulse):
        self.pulses.discard(pulse)

    def schedule(self):
        if self.queued or self.pending or self.dispatching or not self.pulses:
            return
        self.queued = True
        # Run after QWidgetWindow has flushed the preceding frame. Filtering
        # UpdateRequest must never consume Qt's own backing-store processing.
        QMetaObject.invokeMethod(self, 'request_next', Qt.QueuedConnection)

    @Slot()
    def request_next(self):
        self.queued = False
        if not self.pulses or not self.window.isVisible() or self.pending:
            return
        handle = self.window.windowHandle()
        if handle is None:
            return
        if handle is not self.handle:
            if self.handle is not None and isValid(self.handle):
                self.handle.removeEventFilter(self)
            self.handle = handle
            handle.installEventFilter(self)
        self.pending = True
        # Qt follows the native window's display/vsync, including monitor
        # changes. A refreshRate-derived polling timer cannot provide that.
        handle.requestUpdate()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.window:
            if kind in (QEvent.Show, QEvent.WinIdChange):
                self.pending = False
                self.schedule()
            elif kind == QEvent.Hide:
                self.pending = False
        elif watched is self.handle and kind == QEvent.UpdateRequest:
            self.pending = False
            if not self.window.isVisible():
                return False
            self.dispatching = True
            try:
                for pulse in tuple(self.pulses):
                    # QObject lifetime, rather than Python wrapper lifetime,
                    # owns callbacks after a child or its native host closes.
                    if isValid(pulse) and pulse.isActive():
                        pulse.timeout.emit()
                    else:
                        self.pulses.discard(pulse)
            finally:
                self.dispatching = False
                self.schedule()
        return False


class FramePulse(QObject):
    timeout = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        widget = parent
        while widget is not None and not isinstance(widget, QWidget):
            widget = widget.parent()
        if widget is None:
            raise ValueError('A display animation must belong to a QWidget window.')
        self.widget = widget
        self.clock = None
        self.active = False

    def start(self):
        window = self.widget.window()
        clock = getattr(window, '_display_frames', None)
        if clock is None:
            clock = window._display_frames = WindowFrames(window)
        if self.clock is not clock:
            self.stop()
            self.clock = clock
        self.active = True
        clock.add(self)

    def stop(self):
        self.active = False
        if self.clock is not None and isValid(self.clock):
            self.clock.discard(self)

    def isActive(self):
        return self.active


class DisplayAnimation(QObject):
    """Qt value interpolation paced by the owning window's native frame clock.

    QVariantAnimation remains the value/easing engine, but is never started
    on Qt's fixed unified timer. This is the same clock used by shelf springs
    and geometry, including pauses, seeks and interrupted window transitions.
    """
    valueChanged=Signal(object)
    finished=Signal()
    State=QAbstractAnimation.State
    Stopped=State.Stopped
    Running=State.Running
    Paused=State.Paused

    def __init__(self,parent):
        super().__init__(parent)
        self._values=QVariantAnimation(self)
        self._values.valueChanged.connect(self.valueChanged.emit)
        self._frames=FramePulse(self);self._frames.timeout.connect(self._advance)
        # Native paint requests may pause while a window is occluded. A finite
        # transition still owns a completion deadline, independent of painting;
        # this is one endpoint event, never a per-frame polling timer.
        self._completion=QTimer(self);self._completion.setSingleShot(True);self._completion.setTimerType(Qt.PreciseTimer)
        self._completion.timeout.connect(lambda:self._update_time(self.duration()) if self._state==self.Running else None)
        self._state=self.Stopped;self._started_at=0.

    def _configure(self,method,*args):
        previous=self._values.blockSignals(True)
        try:method(*args)
        finally:self._values.blockSignals(previous)

    def setDuration(self,value):self._configure(self._values.setDuration,value)
    def duration(self):return self._values.duration()
    def setEasingCurve(self,value):self._configure(self._values.setEasingCurve,value)
    def easingCurve(self):return self._values.easingCurve()
    def setStartValue(self,value):self._configure(self._values.setStartValue,value)
    def startValue(self):return self._values.startValue()
    def setEndValue(self,value):self._configure(self._values.setEndValue,value)
    def endValue(self):return self._values.endValue()
    def setKeyValueAt(self,step,value):self._configure(self._values.setKeyValueAt,step,value)
    def currentTime(self):return self._values.currentTime()
    def currentValue(self):return self._values.currentValue()
    def state(self):return self._state

    def start(self):
        self.stop();self._state=self.Running
        self._configure(self._values.setCurrentTime,0)
        self._started_at=time.perf_counter()
        self.valueChanged.emit(self.currentValue())
        if self.duration()==0:self._complete()
        elif self._state==self.Running:self._frames.start();self._arm_completion()

    def _arm_completion(self):
        self._completion.start(max(0,self.duration()-self.currentTime()))

    def stop(self):
        self._frames.stop();self._completion.stop();self._state=self.Stopped

    def pause(self):
        if self._state==self.Running:self._frames.stop();self._completion.stop();self._state=self.Paused

    def resume(self):
        if self._state==self.Paused:
            self._state=self.Running;self._started_at=time.perf_counter()-self.currentTime()/1000
            self._frames.start();self._arm_completion()

    def setCurrentTime(self,value):
        if self._state==self.Running:self._started_at=time.perf_counter()-int(value)/1000
        self._update_time(value)
        if self._state==self.Running:self._arm_completion()

    def _update_time(self,value):
        value=max(0,min(self.duration(),int(value)))
        self._values.setCurrentTime(value)
        if value==self.duration() and self._state!=self.Stopped:self._complete()

    def _advance(self):
        if self._state==self.Running:self._update_time(round((time.perf_counter()-self._started_at)*1000))

    def _complete(self):
        self.stop();self.finished.emit()
