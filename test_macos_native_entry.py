"""Real production hotkey/gate entry with mocked Cocoa/Carbon/Quartz only."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

import importlib
import unittest
from unittest.mock import patch

from PySide6.QtCore import QMimeData,QPoint,Qt
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtWidgets import QApplication
import global_hotkeys as hotkeys
import drag_probe as probe
import platform_native
from test_platform_native import Carbon

APP=QApplication.instance() or QApplication([])


class MacNativeEntry(unittest.TestCase):
    def test_manager_uses_carbon_dispatch_and_meta_on_cocoa(self):
        carbon,events=Carbon(),[]
        factory=platform_native.MacHotkeyBackend
        with patch.object(hotkeys.sys,'platform','darwin'), patch.object(hotkeys.QApplication,'platformName',return_value='cocoa'), \
             patch.object(platform_native,'MacHotkeyBackend',side_effect=lambda callback:factory(callback,carbon=carbon)):
            manager=hotkeys.HotkeyManager(APP,lambda:events.append('shelf'),lambda:events.append('overview'))
            self.assertIsInstance(manager.backend,factory)
            self.assertIsNone(manager.apply('Ctrl+Alt+Space','Meta+Shift+A'))
            self.assertTrue(hotkeys.parse_shortcut('Meta+Shift+A').modifiers & hotkeys.MOD_META)
            identifier=next(key for key,value in manager._registrations.items() if value[1]=='overview')
            manager.backend._pressed(identifier,True);APP.processEvents()
            self.assertEqual(events,['overview'])
            manager.close()
            self.assertFalse(carbon.keys);self.assertFalse(carbon.handlers)

    def test_cocoa_probe_follows_but_requires_real_content_mime(self):
        calls=[]
        with patch.object(probe.sys,'platform','darwin'), patch.object(probe,'native_input_available',return_value=True), \
             patch.object(probe,'native_window_move_active',return_value=False), patch.object(probe.QCursor,'pos',return_value=QPoint(120,120)):
            gate=probe.ContentDragGate(calls.append,probe_timeout_ms=80)
            self.assertTrue(gate.arm(QPoint(120,120)))
            self.assertTrue(gate._follow.isActive())
            empty=QMimeData()
            event=QDragEnterEvent(QPoint(48,48),Qt.DropAction.CopyAction,empty,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(gate.probe,event)
            self.assertFalse(event.isAccepted());self.assertEqual(calls,[])
            text=QMimeData();text.setText('native Cocoa drag fixture')
            event=QDragEnterEvent(QPoint(48,48),Qt.DropAction.CopyAction,text,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(gate.probe,event);APP.processEvents()
            self.assertTrue(event.isAccepted());self.assertEqual(len(calls),1)
            gate.reset_session()
            with patch.object(probe,'native_window_move_active',return_value=True):
                self.assertFalse(gate.arm(QPoint(120,120)))
            gate.close()

    def test_media_runtime_hook_selects_native_backend_per_platform(self):
        import runtime_media
        with patch.dict(os.environ,{},clear=False):
            with patch.object(runtime_media.sys,'platform','darwin'):importlib.reload(runtime_media)
            self.assertEqual(os.environ['QT_MEDIA_BACKEND'],'darwin')
            with patch.object(runtime_media.sys,'platform','win32'):importlib.reload(runtime_media)
            self.assertEqual(os.environ['QT_MEDIA_BACKEND'],'windows')


if __name__=='__main__':unittest.main(verbosity=2)
