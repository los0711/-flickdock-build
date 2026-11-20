"""One native update-request clock per QWidget window, idle without animations."""
from weakref import WeakSet
import math
import sys
import time

from PySide6.QtCore import QAbstractAnimation,QAbstractNativeEventFilter,QEvent,QMetaObject,QObject,Qt,QTimer,Signal,Slot,QVariantAnimation
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid
from frame_diagnostics import for_window as frame_trace_for,normalize_refresh_rate


LOCKED_FRAME_RATE_HZ=60.
FRAME_GATE_EPSILON_SECONDS=.0005
# One animation step per presented frame. Native requestUpdate on Windows is a
# 5 ms polling timer, so an ungated clock ticks far faster than the display and
# spends the whole frame budget rendering intermediate states that DWM never
# presents. The request is issued this much before the display period elapses
# to compensate for requestUpdate delivery latency.
FRAME_REQUEST_LEAD_SECONDS=.005
STALL_RECOVERY_MS=140
WM_POWERBROADCAST=0x0218
PBT_APMPOWERSTATUSCHANGE=0x000A
PBT_APMRESUMEAUTOMATIC=0x0012
_on_battery=False
_window_clocks=WeakSet()


class FrameRateGate:
    """Drop native pulses when capped; never synthesize extra display frames."""
    def __init__(self):self.next_due=None;self.last_time=None
    def reset(self):self.next_due=None;self.last_time=None
    def allows(self,now,capped):
        if not capped:self.reset();return True
        if self.last_time is not None and now<self.last_time:self.reset()
        self.last_time=now
        period=1./LOCKED_FRAME_RATE_HZ
        if self.next_due is None:
            self.next_due=now+period;return True
        if now+FRAME_GATE_EPSILON_SECONDS<self.next_due:return False
        steps=max(1,math.floor((now-self.next_due)/period)+1)
        self.next_due+=steps*period
        return True


def _reset_frame_gates():
    for clock in tuple(_window_clocks):
        if isValid(clock):clock.gate.reset();clock.schedule()


def _set_on_battery(value):
    global _on_battery
    value=bool(value)
    if value!=_on_battery:
        _on_battery=value;_reset_frame_gates()


def _query_on_battery():
    if sys.platform!='win32':return False
    import ctypes
    from ctypes import wintypes
    class PowerStatus(ctypes.Structure):
        _fields_=[('ac',ctypes.c_ubyte),('flags',ctypes.c_ubyte),('percent',ctypes.c_ubyte),
                  ('system',ctypes.c_ubyte),('lifetime',wintypes.DWORD),('full_lifetime',wintypes.DWORD)]
    status=PowerStatus()
    try:query=ctypes.WinDLL('kernel32',use_last_error=True).GetSystemPowerStatus
    except (OSError,AttributeError):return False
    query.argtypes=[ctypes.POINTER(PowerStatus)];query.restype=wintypes.BOOL
    return bool(query(ctypes.byref(status)) and status.ac==0)


class _PowerEvents(QAbstractNativeEventFilter):
    def nativeEventFilter(self,event_type,message):
        if bytes(event_type) in (b'windows_generic_MSG',b'windows_dispatcher_MSG'):
            import ctypes
            from ctypes import wintypes
            try:address=int(message)
            except (TypeError,ValueError,OverflowError):return False,0
            if not address:return False,0
            native=wintypes.MSG.from_address(address)
            if native.message==WM_POWERBROADCAST and native.wParam in (PBT_APMPOWERSTATUSCHANGE,PBT_APMRESUMEAUTOMATIC):
                _set_on_battery(_query_on_battery())
        return False,0


def configure_power_monitor(app):
    """Read battery state once and update it on Windows power notifications."""
    if sys.platform!='win32' or app.platformName()!='windows':return
    _set_on_battery(_query_on_battery())
    if not getattr(app,'_flickdock_power_events',None):
        app._flickdock_power_events=_PowerEvents()
        app.installNativeEventFilter(app._flickdock_power_events)


class WindowFrames(QObject):
    """One frame clock per native window, paced to the display it is shown on.

    Windows' native requestUpdate is a 5 ms polling timer that also flushes a
    frame each time, so an ungated clock asks for ~200 frames per second on
    every monitor and spends its budget rendering states DWM never shows.
    After each dispatched step the next native request is scheduled one display
    period later (shortened by the request latency), giving one animated step
    per presented frame on 60, 75, 120, 144 and 240 Hz displays without a fixed
    16 ms timer. Offscreen/minimal backends keep the immediate request loop.
    """
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.pulses = WeakSet()
        self.handle = None
        self.pending = False
        self.dispatching = False
        self.queued = False
        self.gate=FrameRateGate();self._reduced_motion=False
        self._screen=None;self.refresh_hz=None
        # Offscreen/minimal backends have no presentation clock to follow and
        # are used by the deterministic frame tests, so they keep every request.
        self._display_paced=QGuiApplication.platformName() not in ('offscreen','minimal')
        self._last_dispatch=0.0
        self._next_frame_at=0.0
        self._cadence=QTimer(self);self._cadence.setSingleShot(True)
        self._cadence.setTimerType(Qt.TimerType.PreciseTimer)
        self._cadence.timeout.connect(self._cadence_fired)
        self._stall_timer=0
        self.trace = frame_trace_for(window)
        _window_clocks.add(self)
        window.installEventFilter(self)
        window.destroyed.connect(self._release_screen)

    def add(self, pulse):
        if not self.pulses:
            from native_ui_common import reduced_motion
            self._reduced_motion=bool(reduced_motion());self.gate.reset()
            self._last_dispatch=0.0;self._next_frame_at=0.0
        self.pulses.add(pulse)
        self.schedule()

    def discard(self, pulse):
        self.pulses.discard(pulse)
        if not self.pulses:
            self._cancel_stall_timer();self._cancel_cadence_timer()
            if self.trace is not None:self.trace.end_phase()

    def _trace_phase(self):
        morph=getattr(self.window,'geometry_morph',None)
        if morph is not None and morph.active:
            before,after=morph._from,morph._to
            first=before.width()*before.height();last=after.width()*after.height()
            return 'expand' if last>first else 'collapse' if last<first else 'other'
        for pulse in self.pulses:
            owner=pulse.parent()
            while owner is not None and owner is not self.window:
                name=type(owner).__name__
                if 'Scroll' in name or name=='ScrollBoundaryBounce':return 'scroll'
                if name=='SnapshotFade':return 'theme'
                owner=owner.parent()
        return 'other'

    @Slot()
    def _release_screen(self):
        if not hasattr(self,'_stall_timer') or not hasattr(self,'_cadence'):return
        if self._screen is not None and isValid(self._screen) and hasattr(self._screen,'refreshRateChanged'):
            try:self._screen.refreshRateChanged.disconnect(self._screen_rate_changed)
            except (RuntimeError,TypeError):pass
        self._screen=None
        self._cancel_stall_timer();self._cancel_cadence_timer()

    def _screen_changed(self,screen):
        if screen is not self._screen:
            self._release_screen();self._screen=screen
            # refreshRateChanged exists from Qt 6.12; older bindings only have
            # the property, and display_period re-reads it every frame anyway.
            if screen is not None and hasattr(screen,'refreshRateChanged'):
                try:screen.refreshRateChanged.connect(self._screen_rate_changed)
                except (RuntimeError,TypeError):pass
        self._screen_rate_changed()

    def _screen_rate_changed(self,*_):
        self.refresh_hz=normalize_refresh_rate(self._screen.refreshRate()) if self._screen is not None and isValid(self._screen) and hasattr(self._screen,'refreshRate') else None
        self.gate.reset();self._last_dispatch=0.0;self._next_frame_at=0.0;self._cancel_cadence_timer()
        # The old cadence was cancelled above; keep an active animation alive
        # when the window moves to a screen with a different refresh rate.
        if self._display_paced and self.window.isVisible() and not self.window.isMinimized():self.schedule()

    def display_period(self):
        """The presented-frame interval in seconds, or 0 when unpaced."""
        if not self._display_paced:return 0.0
        if _on_battery or self._reduced_motion:return 1.0/LOCKED_FRAME_RATE_HZ
        hz=self.refresh_hz or 60.0
        return 1.0/max(30.0,float(hz))

    def schedule(self):
        if self.queued or self.pending or self.dispatching or not self.pulses:
            return
        if self.display_period():
            # Paced backends dispatch from the cadence timer below. The native
            # requestUpdate timer is coalesced to ~16 ms once the process is
            # idle or not foreground, which would halve the intended cadence.
            if not self._cadence.isActive():self._arm_cadence_timer(.001)
            return
        self.queued = True
        # Run after QWidgetWindow has flushed the preceding frame. Filtering
        # UpdateRequest must never consume Qt's own backing-store processing.
        QMetaObject.invokeMethod(self, 'request_next', Qt.QueuedConnection)

    def _cancel_stall_timer(self):
        if self._stall_timer:
            self.killTimer(self._stall_timer);self._stall_timer=0

    def _cancel_cadence_timer(self):
        if self._cadence.isActive():self._cadence.stop()

    def _arm_cadence_timer(self,seconds=0.0):
        self._cancel_cadence_timer()
        if seconds>0:
            self._next_frame_at=time.perf_counter()+seconds
        if not self._next_frame_at:
            return
        # Windows quantises one-shot timers above ~13 ms to the 15.6 ms tick.
        # Split a longer wait into a <=13 ms chunk plus the remainder so 60 Hz
        # (16.7 ms) stays at its real period instead of drifting to ~15.4 ms.
        remaining=self._next_frame_at-time.perf_counter()
        ms=min(13,max(1,round(remaining*1000)))
        self._cadence.start(ms)

    def _arm_stall_timer(self):
        self._cancel_stall_timer()
        self._stall_timer=self.startTimer(STALL_RECOVERY_MS)

    def _dispatch_pulses(self):
        self.dispatching = True
        try:
            for pulse in tuple(self.pulses):
                # QObject lifetime, rather than Python wrapper lifetime, owns
                # callbacks after a child or its native host closes.
                if isValid(pulse) and pulse.isActive():
                    pulse.timeout.emit()
                else:
                    self.pulses.discard(pulse)
        finally:
            self.dispatching = False

    def _cadence_fired(self):
        now=time.perf_counter()
        period=self.display_period()
        if not self.pulses or not period or not self.window.isVisible() or self.window.isMinimized():
            self._next_frame_at=0.0
            return
        if self._next_frame_at and now<self._next_frame_at-.0005:
            # Long waits are split into <=13 ms chunks above; wait out the
            # remainder without advancing the animation.
            self._arm_cadence_timer()
            return
        handle=self._bind_handle()
        if handle is None or not handle.isExposed():
            # Wait for the Expose event, which resets the cadence.
            return
        if self.trace is not None:
            self.trace.update_request(self._trace_phase(),capped=_on_battery or self._reduced_motion)
            self.trace.dispatched()
        deadline=self._next_frame_at
        self._last_dispatch=now
        self._dispatch_pulses()
        if not self.pulses:return
        if self._next_frame_at!=deadline or self.display_period()!=period:
            self.schedule()
            return
        due=deadline or now
        # Keep the next deadline on the display cadence. Adding a full period
        # to the actual callback time accumulates the timer's rounding error.
        completed=time.perf_counter()
        self._next_frame_at=due+max(1,math.floor((completed-due)/period)+1)*period
        self._arm_cadence_timer()

    def timerEvent(self, event):
        if event.timerId()==self._stall_timer:
            self._stall_timer=0
            if not self.pending:return
            # A requested native frame never arrived (an occluded window, a
            # dropped UpdateRequest or a paused compositor). Leaving pending
            # set would freeze every spring on this window for the rest of the
            # session, so retry and let each animation finish from wall time.
            self.pending=False
            if self.trace is not None:self.trace.skipped()
            if self.window.isVisible() and not self.window.isMinimized():
                self._dispatch_pulses();self._last_dispatch=time.perf_counter()
            self.schedule()
            return
        super().timerEvent(event)

    def _bind_handle(self):
        handle = self.window.windowHandle()
        if handle is None:
            return None
        if handle is not self.handle:
            if self.handle is not None and isValid(self.handle):
                self.handle.removeEventFilter(self)
                self.handle.screenChanged.disconnect(self._screen_changed)
            self.handle = handle
            handle.installEventFilter(self)
            handle.screenChanged.connect(self._screen_changed)
            from native_ui_common import screen_for_widget
            self._screen_changed(screen_for_widget(self.window))
            if self.trace is not None:self.trace.bind_handle(handle)
        return handle

    @Slot()
    def request_next(self):
        self.queued = False
        if not self.pulses or not self.window.isVisible() or self.window.isMinimized() or self.pending:
            return
        handle = self._bind_handle()
        if handle is None:
            return
        if not handle.isExposed():return
        # Hold the native request until the next display period is due. This
        # is what stops requestUpdate's 5 ms polling from flushing and
        # animating ~200 frames per second on a 60-144 Hz monitor. The small
        # lead compensates for the requestUpdate delivery latency.
        period=self.display_period()
        if period and self._last_dispatch:
            remaining=period-FRAME_REQUEST_LEAD_SECONDS-(time.perf_counter()-self._last_dispatch)
            if remaining>0.0015:
                self._arm_cadence_timer(remaining)
                return
        self.pending = True
        self._arm_stall_timer()
        handle.requestUpdate()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.window:
            if kind in (QEvent.Show, QEvent.WinIdChange, QEvent.WindowStateChange):
                self.pending = False;self._cancel_stall_timer();self._cancel_cadence_timer()
                self._last_dispatch=0.0;self._next_frame_at=0.0
                if not self.window.isMinimized():self.schedule()
                elif self.trace is not None:self.trace.end_phase()
            elif kind == QEvent.Hide:
                self.pending = False;self._cancel_stall_timer();self._cancel_cadence_timer()
                self._next_frame_at=0.0
                if self.trace is not None:self.trace.end_phase()
        elif watched is self.handle and kind == QEvent.Expose:
            self.pending=False;self._cancel_stall_timer();self._cancel_cadence_timer()
            self._last_dispatch=0.0;self._next_frame_at=0.0
            if self.handle.isExposed() and not self.window.isMinimized():self.schedule()
            elif self.trace is not None:self.trace.end_phase()
        elif watched is self.handle and kind == QEvent.UpdateRequest:
            self.pending = False;self._cancel_stall_timer()
            if self.display_period():
                # Every animated widget update also produces an UpdateRequest.
                # Dispatching here would recreate the ~200 Hz feedback loop
                # this clock exists to remove; let Qt paint and wait for the
                # cadence timer instead.
                return False
            if not self.window.isVisible() or self.window.isMinimized() or not self.handle.isExposed():
                return False
            now=time.perf_counter()
            capped=_on_battery or self._reduced_motion
            if self.trace is not None:self.trace.update_request(self._trace_phase(),capped=capped)
            if capped and not self.gate.allows(now,True):
                if self.trace is not None:self.trace.skipped()
                self.schedule();return False
            if not capped and self.gate.next_due is not None:self.gate.reset()
            if self.trace is not None:self.trace.dispatched()
            self._last_dispatch=now
            self._dispatch_pulses()
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
        value=self.currentValue()
        self.valueChanged.emit(self.startValue() if value is None else value)
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
