"""Capture Python stacks after a stalled event loop, without inspecting content."""
import faulthandler
from pathlib import Path
from PySide6.QtCore import QObject,QTimer


class GuiWatchdog(QObject):
    def __init__(self,app,base,*,timeout=8):
        super().__init__(app);self.stream=None;self.timeout=timeout
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
        if self.stream is not None:
            faulthandler.cancel_dump_traceback_later();self.stream.close();self.stream=None
