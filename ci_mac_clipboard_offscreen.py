"""Run independent clipboard GUI fixtures in separate Qt app lifetimes."""
import os
import subprocess
import sys
import unittest


def run(check):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    QCursor.setPos(QPoint(1200, 900))
    suite = unittest.defaultTestLoader.loadTestsFromName(check)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('QT_QPA_PLATFORM') != 'offscreen':
        raise SystemExit('Run only in an isolated offscreen CI session')
    if len(sys.argv) == 2:
        raise SystemExit(0 if run(sys.argv[1]) else 1)
    from test_local_clipboard_history import ClipboardHistoryRuntimeTests
    prefix = 'test_local_clipboard_history.ClipboardHistoryRuntimeTests'
    checks = ['test_local_clipboard_history.ClipboardHistoryPanelTests']
    checks.extend(prefix + '.' + name for name in unittest.defaultTestLoader.getTestCaseNames(ClipboardHistoryRuntimeTests))
    failures = [name for name in checks if subprocess.run([sys.executable, __file__, name], timeout=25).returncode]
    if failures:
        raise SystemExit('Failed clipboard cases: ' + ', '.join(failures))


if __name__ == '__main__':
    main()
