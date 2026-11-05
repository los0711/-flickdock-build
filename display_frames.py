"""One native update-request clock per QWidget window, idle without animations."""
from weakref import WeakSet

from PySide6.QtCore import QEvent, QMetaObject, QObject, Qt, Signal, Slot
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
