"""Diagnostic persistence, bounded history and safe lifetime guards."""
import json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QObject,Signal,QTimer,qWarning,QCoreApplication,QEvent
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
    def test_watchdog_observes_diagnostics_that_became_ready_during_construction(self):
        import io
        from gui_watchdog import GuiWatchdog
        class ReadyDiagnostics(QObject):
            ready=Signal()
            def __init__(self,app,base,version):
                super().__init__(app);self.stall_stream=io.BytesIO();self.beats=0
                self.ready.emit()
            def watchdog_heartbeat(self,timeout):self.beats+=1
            def stop_watchdog(self):pass
        previous=getattr(APP,'_crash_diagnostics',None)
        with patch('crash_diagnostics.CrashDiagnostics',ReadyDiagnostics),patch('gui_watchdog._python_watchdog',return_value=True):
            watchdog=GuiWatchdog(APP,'unused-fixture')
            try:
                APP.processEvents()
                self.assertIs(watchdog.stream,watchdog.diagnostics.stall_stream)
                self.assertTrue(watchdog.timer.isActive())
                self.assertGreater(watchdog.diagnostics.beats,0)
            finally:
                watchdog.close();watchdog.deleteLater();APP.processEvents()
                if previous is None:delattr(APP,'_crash_diagnostics')
                else:APP._crash_diagnostics=previous

    def test_cocoa_watchdog_records_stall_without_the_native_faulthandler_timer(self):
        import time
        from gui_watchdog import GuiWatchdog
        with tempfile.TemporaryDirectory() as directory, \
                patch('gui_watchdog._python_watchdog',return_value=True), \
                patch('gui_watchdog.faulthandler.dump_traceback_later',side_effect=AssertionError('unsafe native timer')):
            watchdog=GuiWatchdog(APP,directory,timeout=.2)
            try:
                until(lambda:watchdog.stream is not None)
                time.sleep(.7)
                stack=Path(directory)/'diagnostics'/'ui-stall.log'
                self.assertIn('Timeout: GUI heartbeat',stack.read_text(encoding='utf8'))
                self.assertIn('_check_watchdog',stack.read_text(encoding='utf8'))
            finally:
                watchdog.close();watchdog.diagnostics.wait_closed()
                if getattr(APP,'_crash_diagnostics',None) is watchdog.diagnostics:delattr(APP,'_crash_diagnostics')
                watchdog.deleteLater();APP.processEvents()

    def test_video_breadcrumb_contains_only_fixed_event_name(self):
        from file_preview import _note_preview_event
        class Recorder:
            def __init__(self):self.events=[]
            def note(self,name):self.events.append(name)
        recorder=Recorder();previous=getattr(APP,'_crash_diagnostics',None)
        APP._crash_diagnostics=recorder
        try:
            _note_preview_event('preview.video.select')
            self.assertEqual(recorder.events,['preview.video.select'])
        finally:
            if previous is None:delattr(APP,'_crash_diagnostics')
            else:APP._crash_diagnostics=previous

    def test_macos_home_is_redacted_from_diagnostic_text(self):
        from crash_diagnostics import _redact
        with patch.dict(os.environ,{'HOME':'/Users/private-fixture'}):
            value=_redact('file /Users/private-fixture/secret.txt')
        self.assertNotIn('/Users/private-fixture',value);self.assertIn('<home>',value)

    def test_watchdog_writes_a_stalled_ui_stack_without_gui_thread_cooperation(self):
        import time
        from gui_watchdog import GuiWatchdog
        with tempfile.TemporaryDirectory() as directory:
            watchdog=GuiWatchdog(APP,directory,timeout=.2)
            try:
                until(lambda:watchdog.stream is not None)
                time.sleep(.45)
                stack=Path(directory)/'diagnostics'/'ui-stall.log'
                self.assertIn('Timeout',stack.read_text(encoding='utf8'))
            finally:
                watchdog.close();watchdog.diagnostics.wait_closed()
                if getattr(APP,'_crash_diagnostics',None) is watchdog.diagnostics:delattr(APP,'_crash_diagnostics')
                watchdog.deleteLater();APP.processEvents()

    def test_stall_is_bundled_without_a_ui_export_action(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            diagnostics=CrashDiagnostics(APP,directory,'1.8.37')
            try:
                until(lambda:diagnostics.stall_stream is not None)
                # Let the worker initialize its baseline, then model a watchdog
                # write without delivering any GUI event or clicking export.
                time.sleep(.3)
                diagnostics.stall_stream.write(b'Timeout: synthetic stack\n')
                target=Path(directory)/'diagnostics'/'latest-incident.zip'
                deadline=time.monotonic()+5
                while not target.exists() and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue(target.exists())
                with zipfile.ZipFile(target) as archive:
                    self.assertIn(b'synthetic stack',archive.read('ui-stall.log'))
            finally:diagnostics.wait_closed()

    def test_restart_preserves_previous_fatal_and_stall_in_automatic_and_exported_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            first=CrashDiagnostics(APP,directory,'1.8.37')
            try:
                until(lambda:first._installed)
                first.stack_stream.write(b'Fatal Python error: test native crash\n')
                first.stall_stream.write(b'Timeout: test UI stall\n')
            finally:first.wait_closed();first.deleteLater();APP.processEvents()
            second=CrashDiagnostics(APP,directory,'1.8.37');done=[]
            second.exported.connect(lambda path,error:done.append((path,error)))
            try:
                until(lambda:second._installed)
                automatic=Path(directory)/'diagnostics'/'latest-incident.zip'
                until(automatic.is_file)
                target=Path(directory)/'export.zip';second.export(target);until(lambda:bool(done))
                self.assertEqual(done,[(str(target),'')])
                for bundle in (automatic,target):
                    with zipfile.ZipFile(bundle) as archive:
                        names=archive.namelist()
                        previous=[name for name in names if name.startswith('previous/') and name.endswith('/python-stack.txt')]
                        self.assertTrue(previous,names)
                        self.assertIn(b'Fatal Python error',archive.read(previous[-1]))
                        self.assertIn(b'test UI stall',archive.read('ui-stall.log'))
            finally:second.wait_closed();second.deleteLater();APP.processEvents()

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
                    self.assertEqual(set(archive.namelist()),{'python-stack.txt','qt-messages.txt','breadcrumbs.jsonl','env.json','last-paint.json','ui-stall.log'})
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

    def test_directory_result_after_drop_zone_retirement_does_not_touch_widgets(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from PySide6.QtCore import QObject,Signal
        from shelf_window import ShelfWindow
        class Signals(QObject):
            done=Signal(object,object)
        signals=Signals();job=SimpleNamespace(signals=signals)
        owner=QWidget();zone=QWidget();zone.refresh=Mock()
        owner._directory_sizes={};owner._directory_jobs={}
        owner.files={'permanent':['fixture']};owner.zones={'permanent':zone}
        owner.content=SimpleNamespace(metadata={});owner.popup=None
        owner.content_transition_active=lambda:False
        errors=[]
        try:
            with patch('shelf_window.DiskWork',return_value=job),patch('shelf_window.QThreadPool') as pool:
                ShelfWindow.request_directory_size(owner,'fixture')
                pool.globalInstance.return_value.start.assert_called_once_with(job)
            zone.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            with patch.object(sys,'excepthook',side_effect=lambda *args:errors.append(args)):
                signals.done.emit({'logical_size':10},None);APP.processEvents()
            self.assertFalse(errors)
            zone.refresh.assert_not_called()
        finally:owner.deleteLater();APP.processEvents()

    def test_queued_callback_ignores_deleted_dependency_and_preserves_live_errors(self):
        owner=QWidget();child=QWidget();seen=[]
        later(0,owner,lambda host,control:seen.append(control),child)
        child.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        APP.processEvents();self.assertFalse(seen)
        with self.assertRaisesRegex(RuntimeError,'live callback'):
            guard(owner,lambda host:(_ for _ in ()).throw(RuntimeError('live callback')))()
        owner.deleteLater()

if __name__=='__main__':unittest.main(verbosity=2)
