"""Quartz/Carbon fakes; no system hooks, desktop actions or input registration."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QWidget
import platform_native as native

APP = QApplication.instance() or QApplication([])


class CopyActivityTests(unittest.TestCase):
    def test_offscreen_copy_observation_never_queries_os_input(self):
        self.assertIsNone(native.copy_activity())

    def test_mac_uses_coarse_counters_without_creating_an_input_tap(self):
        quartz=SimpleNamespace(kCGEventSourceStateCombinedSessionState=0,kCGEventKeyDown=10,
            kCGEventLeftMouseDown=1,kCGEventRightMouseDown=3,
            CGEventSourceCounterForEventType=lambda state,kind:kind+100,
            CGEventSourceSecondsSinceLastEventType=lambda state,kind:kind/10)
        with patch.object(native,'native_input_available',return_value=True),patch.object(native.sys,'platform','darwin'),patch.dict(sys.modules,Quartz=quartz):
            self.assertEqual(native.copy_activity(),((110,101,103),100.,None))


class Quartz:
    kCGEventSourceStateCombinedSessionState = 0
    kCGWindowListOptionOnScreenOnly, kCGWindowListExcludeDesktopElements = 1, 16
    kCGNullWindowID, kCGWindowListOptionIncludingWindow = 0, 8
    kCGWindowBounds, kCGWindowLayer, kCGWindowAlpha, kCGWindowNumber = 'bounds', 'layer', 'alpha', 'id'
    def __init__(self):
        self.mask = 0
        self.bounds = dict(X=0, Y=0, Width=600, Height=400)
    def CGEventSourceButtonState(self, source, button): return bool(self.mask & (1 << button))
    def CGWindowListCopyWindowInfo(self, flags, identifier):
        return [dict(bounds=dict(self.bounds), layer=0, alpha=1, id=42)]


class Carbon:
    def __init__(self):
        self.calls, self.keys, self.handlers = [], {}, []
        self.kind, self.identifier, self.signature = 6, 0, 0
    def GetApplicationEventTarget(self): return 1
    def InstallEventHandler(self, target, callback, count, types, context, reference):
        self.handlers.append(callback)
        reference._obj.value = 2
        return 0
    def RegisterEventHotKey(self, key, modifiers, identifier, target, options, reference):
        if (key, modifiers) in self.keys.values(): return -9878
        self.keys[identifier.id] = (key, modifiers)
        self.calls.append((key, modifiers))
        reference._obj.value = identifier.id
        return 0
    def UnregisterEventHotKey(self, reference):
        self.keys.pop(reference.value, None)
        return 0
    def RemoveEventHandler(self, reference): self.handlers.clear(); return 0
    def GetEventKind(self, event): return self.kind
    def GetEventParameter(self, event, direct, kind, actual, size, returned, identifier):
        identifier._obj.id, identifier._obj.signature = self.identifier, self.signature
        return 0


class NativeTests(unittest.TestCase):
    def test_mac_tool_window_remains_visible_when_app_deactivates(self):
        widget = QWidget()
        widget.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        try:
            self.assertFalse(native.keep_tool_visible_when_inactive(widget, platform='win32'))
            self.assertFalse(widget.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow))
            self.assertTrue(native.keep_tool_visible_when_inactive(widget, platform='darwin'))
            self.assertTrue(widget.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow))
            self.assertTrue(widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        finally:
            widget.close()

    def test_paths_and_platform_guards(self):
        home = Path('C:/disposable-home')
        self.assertEqual(native.data_directory(platform='win32', home=home, environ={'APPDATA': 'D:/Profile'}), Path('D:/Profile/FlickDock'))
        self.assertEqual(native.data_directory(platform='darwin', home=home, environ={}), home / 'Library/Application Support/FlickDock')
        self.assertTrue(native.native_input_available(platform='darwin', qt_platform='cocoa'))
        self.assertFalse(native.native_input_available(platform='darwin', qt_platform='offscreen'))
        with patch.object(native, '_mac', side_effect=AssertionError('native called')):
            self.assertEqual(native.mouse_buttons_mask(), 0)
            self.assertFalse(native.window_move_active())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'FlickDock.app/Contents/MacOS/FlickDock'
            self.assertEqual(native.app_bundle(path), path.parent.parent.parent.resolve())
            self.assertIsNone(native.app_bundle(Path(directory) / 'python'))

    def test_cocoa_host_shadow_is_disabled_only_on_native_mac(self):
        widget = SimpleNamespace(winId=lambda: 123)
        with patch.object(native.sys, 'platform', 'win32'):
            self.assertFalse(native.remove_host_window_shadow(widget))
        window = SimpleNamespace(setHasShadow_=lambda value: calls.append(value))
        view = SimpleNamespace(window=lambda: window)
        fake_objc = SimpleNamespace(objc_object=lambda **kwargs: view)
        calls = []
        with patch.object(native.sys, 'platform', 'darwin'), \
             patch.object(native, 'native_input_available', return_value=True), \
             patch.dict(sys.modules, {'objc': fake_objc}):
            self.assertTrue(native.remove_host_window_shadow(widget))
        self.assertEqual(calls, [False])

    def test_cosmetic_cocoa_shadow_failure_does_not_abort_startup(self):
        class CocoaError(Exception):
            pass

        widget = SimpleNamespace(winId=lambda: 123)
        window = SimpleNamespace(setHasShadow_=lambda _value: (_ for _ in ()).throw(CocoaError('native unavailable')))
        fake_objc = SimpleNamespace(error=CocoaError,
            objc_object=lambda **_kwargs: SimpleNamespace(window=lambda: window))
        with patch.object(native.sys, 'platform', 'darwin'), \
             patch.object(native, 'native_input_available', return_value=True), \
             patch.dict(sys.modules, {'objc': fake_objc}):
            self.assertFalse(native.remove_host_window_shadow(widget))

    def test_cosmetic_native_fallback_does_not_hide_implementation_errors(self):
        widget = SimpleNamespace(winId=lambda: 123)
        window = SimpleNamespace(setHasShadow_=lambda _value: (_ for _ in ()).throw(KeyError('implementation error')))
        fake_objc = SimpleNamespace(objc_object=lambda **_kwargs: SimpleNamespace(window=lambda: window))
        with patch.object(native.sys, 'platform', 'darwin'), \
             patch.object(native, 'native_input_available', return_value=True), \
             patch.dict(sys.modules, {'objc': fake_objc}):
            with self.assertRaises(KeyError):
                native.remove_host_window_shadow(widget)

    def test_quartz_external_buttons_and_window_move_until_release(self):
        quartz = Quartz()
        monitor = native.MacInput(quartz, lambda: QPoint(100, 100))
        self.assertEqual(monitor.buttons(), 0)
        quartz.mask = 5  # left + middle
        self.assertEqual(monitor.buttons(), 5)
        self.assertFalse(monitor.window_moving())
        self.assertFalse(monitor.window_moving())  # content drag with fixed parent
        quartz.bounds['X'] = 80
        self.assertTrue(monitor.window_moving())
        quartz.bounds['X'] = 0
        self.assertTrue(monitor.window_moving())  # returning to origin is still a move
        quartz.mask = 0
        self.assertFalse(monitor.window_moving())

    def test_carbon_registration_conflict_dispatch_repeat_and_cleanup(self):
        carbon, activated = Carbon(), []
        backend = native.MacHotkeyBackend(activated.append, carbon=carbon)
        self.assertTrue(backend.register(9, 2 | 1 | 0x4000, 0x20))
        self.assertEqual(carbon.calls[-1], (49, (1 << 8) | (1 << 11)))
        self.assertFalse(backend.register(10, 2 | 1, 0x20))
        self.assertEqual(backend.last_error, 1409)
        self.assertTrue(backend.register(11, 8 | 4, ord('A')))
        self.assertEqual(carbon.calls[-1], (0, (1 << 12) | (1 << 9)))
        carbon.identifier, carbon.signature = 9, backend._signature
        backend._native_event(None, None, None)
        backend._native_event(None, None, None)
        APP.processEvents()
        self.assertEqual(activated, [9])
        carbon.kind = 7
        backend._native_event(None, None, None)
        carbon.kind = 6
        backend._native_event(None, None, None)
        APP.processEvents()
        self.assertEqual(activated, [9, 9])
        backend._pressed(11, True)
        backend.close()
        self.assertFalse(carbon.keys)
        self.assertFalse(carbon.handlers)
        backend._pressed(9, True)
        APP.processEvents()
        self.assertEqual(activated, [9, 9])


class NativeClipboardReaderTests(unittest.TestCase):
    def setUp(self):
        import native_clipboard_reader as subject
        from PySide6.QtCore import QTimer
        self.subject=subject;self.temp=tempfile.TemporaryDirectory(prefix='flickdock-reader-')
        self.cache=Path(self.temp.name)/'clipboard-cache';self.cache.mkdir()
        self.sequence=[10];self.reader=subject.NativeClipboardReader(self.cache,lambda:self.sequence[0])
        self.ready=[];self.failed=[];self.reader.ready.connect(lambda *args:self.ready.append(args));self.reader.failed.connect(self.failed.append)
        self.guard=patch.object(subject,'foreign_clipboard_lock',return_value=False);self.guard.start()
        self.beats=[];self.timer=QTimer();self.timer.setInterval(5);self.timer.timeout.connect(lambda:self.beats.append(1));self.timer.start()

    def wait(self,predicate):
        from PySide6.QtTest import QTest
        import time
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline:APP.processEvents();QTest.qWait(5)
        self.assertTrue(predicate())

    def tearDown(self):
        self.reader.close();self.wait(lambda:self.reader.process is None and not self.reader.jobs)
        self.timer.stop();self.reader.deleteLater();APP.processEvents();self.guard.stop();self.temp.cleanup()

    def command(self,job,*,delay=0,sequence=None):
        script="import json,sys,time;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());time.sleep(float(sys.argv[2]));(p.parent/'result.json').write_text(json.dumps(dict(ok=True,sequence=int(sys.argv[3]) if sys.argv[3] else v['sequence'],kind='text',data=dict(text='private fixture text',html=''))))"
        return [sys.executable,'-c',script,str(job),str(delay),str(sequence) if sequence is not None else '']

    def test_blocked_external_producer_times_out_without_blocking_ui_and_next_copy_recovers(self):
        with patch.object(self.subject,'READ_TIMEOUT_MS',150),patch.object(self.reader,'_command',side_effect=lambda job:self.command(job,delay=10)):
            self.assertEqual(self.reader.read(10),(False,None));self.wait(lambda:bool(self.failed))
        self.assertEqual(self.failed[-1],'NATIVE_READ_TIMEOUT');self.assertGreater(len(self.beats),5)
        self.sequence[0]=11
        with patch.object(self.reader,'_command',side_effect=self.command):
            self.reader.read(11);self.wait(lambda:bool(self.ready))
        self.assertEqual(self.reader.read(11),(True,('text',{'text':'private fixture text','html':''})))
        self.assertFalse(list(self.cache.iterdir()))

    def test_foreign_lock_is_only_observed_and_stale_result_never_overwrites_new_copy(self):
        with patch.object(self.subject,'foreign_clipboard_lock',return_value=True),patch.object(self.reader,'_command') as command:
            self.assertEqual(self.reader.read(10),(False,None));command.assert_not_called()
        with patch.object(self.reader,'_command',side_effect=lambda job:self.command(job,delay=.12)):
            self.reader.read(10);self.sequence[0]=11;self.wait(lambda:bool(self.failed))
        self.assertEqual(self.failed[-1],'NATIVE_READ_REPLACED');self.assertFalse(self.ready)
        self.assertFalse(list(self.cache.iterdir()))

    def test_native_encoded_images_and_bitmaps_both_roundtrip_without_clipboard_access(self):
        import json,threading
        from PySide6.QtCore import QBuffer,QIODevice
        from PySide6.QtGui import QImage,QColor
        image=QImage(80,40,QImage.Format_ARGB32);image.fill(QColor('#2468ad'))
        for format in ('PNG','JPEG','bitmap'):
            with self.subTest(format=format),tempfile.TemporaryDirectory(dir=self.cache) as directory:
                root=Path(directory)
                if format=='bitmap':data={'image':image}
                else:
                    buffer=QBuffer();buffer.open(QIODevice.WriteOnly);self.assertTrue(image.save(buffer,format))
                    encoded=bytes(buffer.data());data={'raw':encoded,'mime':'image/'+format.lower()}
                mime=self.subject._store_image(root,data)
                (root/'result.json').write_text(json.dumps(dict(ok=True,sequence=10,kind='image',mime=mime)))
                result=self.subject._read_result(root,threading.Event())['value']
                self.assertEqual(result[0],'image');self.assertEqual(result[1]['image'].size(),image.size())
                if format!='bitmap':self.assertEqual(result[1]['raw'],encoded)

if __name__ == '__main__': unittest.main(verbosity=2)
