"""Quartz/Carbon fakes; no system hooks, desktop actions or input registration."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
import platform_native as native

APP = QApplication.instance() or QApplication([])


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


if __name__ == '__main__': unittest.main(verbosity=2)
