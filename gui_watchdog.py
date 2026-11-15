"""Capture Python stacks after a stalled event loop, without inspecting content."""
import faulthandler
import sys
from PySide6.QtCore import QObject,QTimer


def _python_watchdog():
    # Timed C stack walking itself crashed in the Cocoa/PySide comparison.
    # The existing diagnostic worker can inspect frames while holding the GIL.
    return sys.platform=='darwin'


class GuiWatchdog(QObject):
    def __init__(self,app,base,*,timeout=8):
        super().__init__(app);self.stream=None;self.timeout=timeout
        self.python_watchdog=_python_watchdog()
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.beat)
        from crash_diagnostics import CrashDiagnostics
        from app_version import APP_VERSION
        self.diagnostics=CrashDiagnostics(app,base,APP_VERSION)
        app._crash_diagnostics=self.diagnostics
        self.diagnostics.ready.connect(self._diagnostics_ready)
        app.aboutToQuit.connect(self.close)

    def _diagnostics_ready(self):
        self.stream=self.diagnostics.stall_stream
        if self.stream is not None:self.beat();self.timer.start()

    def beat(self):
        if self.stream is None:return
        if self.python_watchdog:
            self.diagnostics.watchdog_heartbeat(self.timeout)
            return
        # CPython's C watchdog also works when a blocking native extension
        # holds the GIL. A Python polling thread would miss that class of hang.
        faulthandler.cancel_dump_traceback_later()
        if self.stream.tell()>1024*1024:
            # Keep one bounded current log and one previous-start archive.
            # Detailed diagnostics must never grow without limit in a long run.
            self.close();return
        faulthandler.dump_traceback_later(self.timeout,repeat=False,file=self.stream)

    def close(self):
        self.timer.stop()
        self.diagnostics.stop_watchdog()
        if self.stream is not None:
            if not self.python_watchdog:faulthandler.cancel_dump_traceback_later()
            self.stream=None
