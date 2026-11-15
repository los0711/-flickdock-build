"""Diagnostic persistence, bounded history and safe lifetime guards."""
import json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QTimer,qWarning,QCoreApplication,QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget
from crash_diagnostics import CrashDiagnostics
from qt_lifetime import guard,later

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)
def until(predicate,timeout=3000):
    for _ in range(timeout//5):
        APP.processEvents()
        if predicate():return
        QTest.qWait(5)
    raise AssertionError('Diagnostic work did not finish')

class DiagnosticsTests(unittest.TestCase):
    def test_unchanged_idle_snapshot_does_not_rewrite_and_stack_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics=CrashDiagnostics(APP,directory,'1.8.37');done=[]
            diagnostics.exported.connect(lambda *result:done.append(result))
            try:
                until(lambda:diagnostics._installed)
                diagnostics.export(Path(directory)/'first.zip');until(lambda:len(done)==1)
                path=diagnostics.directory/'env.json';modified=path.stat().st_mtime_ns
                QTest.qWait(650);self.assertEqual(path.stat().st_mtime_ns,modified)
                diagnostics._send('stack','fixture-stack\n'*180000)
                diagnostics.export(Path(directory)/'bounded.zip');until(lambda:len(done)==2)
                self.assertLess((diagnostics.directory/'python-stack.txt').stat().st_size,2*1024*1024)
            finally:diagnostics.wait_closed();diagnostics.deleteLater();APP.processEvents()

    def test_export_has_stacks_qt_messages_bounded_actions_and_no_clipboard(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics=CrashDiagnostics(APP,directory,'1.8.37');done=[]
            diagnostics.exported.connect(lambda path,error:done.append((path,error)))
            try:
                until(lambda:diagnostics._installed)
                diagnostics.configure(theme='dark',motion=True)
                for _ in range(250):diagnostics.note('test.action')
                qWarning('diagnostic token=private-fixture-value https://example.invalid/private')
                with patch.object(diagnostics,'_previous_exception'):
                    try:raise RuntimeError('diagnostic test exception')
                    except RuntimeError:diagnostics._exception(*sys.exc_info())
                target=Path(directory)/'export.zip';diagnostics.export(target)
                until(lambda:bool(done));self.assertEqual(done,[(str(target),'')])
                with zipfile.ZipFile(target) as archive:
                    self.assertEqual(set(archive.namelist()),{'python-stack.txt','qt-messages.txt','breadcrumbs.jsonl','env.json','last-paint.json'})
                    self.assertIn('Traceback',archive.read('python-stack.txt').decode())
                    qt=archive.read('qt-messages.txt').decode();self.assertIn('diagnostic',qt)
                    self.assertNotIn('private-fixture-value',qt);self.assertNotIn('/private',qt)
                    events=archive.read('breadcrumbs.jsonl').decode().splitlines();self.assertLessEqual(len(events),200)
                    for row in events:self.assertEqual(set(json.loads(row)),{'event','timestamp'})
                    env=json.loads(archive.read('env.json'));self.assertEqual(env['theme'],'dark');self.assertTrue(env['animations'])
            finally:diagnostics.wait_closed();diagnostics.deleteLater();APP.processEvents()

    def test_guard_does_not_call_an_object_after_cpp_deletion(self):
        target=QWidget();seen=[];callback=guard(target,lambda widget:seen.append(widget))
        target.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        callback();self.assertFalse(seen)

    def test_queued_callback_ignores_deleted_dependency_and_preserves_live_errors(self):
        owner=QWidget();child=QWidget();seen=[]
        later(0,owner,lambda host,control:seen.append(control),child)
        child.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        APP.processEvents();self.assertFalse(seen)
        with self.assertRaisesRegex(RuntimeError,'live callback'):
            guard(owner,lambda host:(_ for _ in ()).throw(RuntimeError('live callback')))()
        owner.deleteLater()

if __name__=='__main__':unittest.main(verbosity=2)
