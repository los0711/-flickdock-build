"""Capture Python stacks after a stalled event loop, without inspecting content."""
import faulthandler
import sys
import traceback
from pathlib import Path
from PySide6.QtCore import QObject,QTimer


class GuiWatchdog(QObject):
    def __init__(self,app,base,*,timeout=8):
        super().__init__(app);self.stream=None;self.timeout=timeout;self.base=base
        self._previous_hook=sys.excepthook
        self._exception_hook=self._report_exception
        sys.excepthook=self._exception_hook
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.beat)
        try:
            directory=Path(base)/'diagnostics';directory.mkdir(parents=True,exist_ok=True)
            path=directory/'ui-stall.log'
            if path.exists() and path.stat().st_size>1024*1024:
                path.replace(directory/'ui-stall-previous.log')
            self.stream=path.open('ab',buffering=0)
            self.stream.write(b'\nFlickDock event loop watchdog; stack frames only, no clipboard content\n')
            self.beat();self.timer.start();app.aboutToQuit.connect(self.close)
        except (OSError,RuntimeError):self.close()

    def _report_exception(self,kind,error,trace):
        # Windowed executables have no stderr. Preserve a diagnostic frame,
        # without exception text, locals or private absolute filesystem paths.
        # Delegate unchanged so an invariant failure is never converted to OK.
        from network_diagnostics import record_event
        frames=traceback.extract_tb(trace)
        details=dict(cause=kind.__name__)
        if frames:details['location']=Path(frames[-1].filename).name+':'+str(frames[-1].lineno)
        record_event(self.base,'ui','callback','CALLBACK_EXCEPTION',details=details)
        self._previous_hook(kind,error,trace)

    def beat(self):
        if self.stream is None:return
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
        if sys.excepthook is self._exception_hook:sys.excepthook=self._previous_hook
        if self.stream is not None:
            faulthandler.cancel_dump_traceback_later();self.stream.close();self.stream=None
