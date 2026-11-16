"""The real manager must close cleanly after its clipboard preview opens."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def run_child():
    if os.environ.get('QT_QPA_PLATFORM')!='offscreen' or os.environ.get('FLICKDOCK_ISOLATED_QA')!='1':
        raise RuntimeError('This smoke test requires an isolated offscreen profile.')
    from PySide6.QtCore import QCoreApplication,QEvent
    from PySide6.QtWidgets import QApplication
    from flickdock import ShelfManager
    app=QApplication([]);manager=ShelfManager(app,cocoa_smoke=True)
    try:
        for _ in range(20):app.processEvents();time.sleep(.005)
        for turn in range(12):
            manager.show_clipboard_preview()
            for _ in range(4):app.processEvents();time.sleep(.005)
            dialog=manager.clipboard_preview_dialog
            assert dialog is not None and dialog.isVisible()==(turn%2==0),turn
        manager.show_clipboard_preview()
        for _ in range(20):app.processEvents();time.sleep(.005)
        assert manager.clipboard_preview_dialog.isVisible()
        manager.quit()
    finally:
        manager.cloud.close()
        worker=manager.clipboard_sync.inbox._thread
        worker.join(3)
        assert not worker.is_alive()
    for _ in range(20):
        app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);time.sleep(.005)


class ClipboardShutdownTests(unittest.TestCase):
    def test_open_preview_then_quit_exits_without_qt_callback_crash(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-clipboard-shutdown-') as directory:
            profile=Path(directory)
            for name in ('appdata','localappdata','temp'):(profile/name).mkdir()
            env=dict(os.environ,QT_QPA_PLATFORM='offscreen',FLICKDOCK_ISOLATED_QA='1',
                     APPDATA=str(profile/'appdata'),LOCALAPPDATA=str(profile/'localappdata'),
                     TEMP=str(profile/'temp'),TMP=str(profile/'temp'))
            result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--qa-child'],
                cwd=Path(__file__).parent,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=25)
            self.assertEqual(result.returncode,0,result.stdout.decode('utf8','replace'))


if __name__=='__main__':
    if '--qa-child' in sys.argv:run_child()
    else:unittest.main()
