"""Chrome ignores irrelevant events before widget lookup; no OS input."""
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QDialog,QWidget,QHBoxLayout
from test_html_native_workspace import APP,flush
from window_chrome import WindowChrome


class Probe(QWidget):
    def __init__(self):
        super().__init__();self.chrome_lookups=0
    def window(self):
        caller=inspect.currentframe().f_back.f_locals.get('self')
        if isinstance(caller,WindowChrome):self.chrome_lookups+=1
        return super().window()


class ChromeEventScopeTests(unittest.TestCase):
    def test_irrelevant_events_never_query_foreign_widget_window(self):
        with tempfile.TemporaryDirectory(prefix='fd-chrome-scope-') as directory:
            owners=[]
            try:
                for index in range(12):
                    dialog=QDialog();title=QWidget(dialog);QHBoxLayout(title)
                    chrome=WindowChrome(dialog,title,SimpleNamespace(base=Path(directory)),'transfer')
                    owners.append((dialog,chrome))
                probe=Probe()
                try:
                    for unused in range(50):APP.sendEvent(probe,QEvent(QEvent.Type.User))
                    self.assertEqual(probe.chrome_lookups,0)
                finally:probe.deleteLater()
            finally:
                for dialog,chrome in owners:dialog.hide();dialog.deleteLater()
                flush()


if __name__=='__main__':unittest.main()
