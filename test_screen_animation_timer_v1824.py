"""Screen cadence policy only; actual monitor presentation requires native QA."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,Signal,QEvent,SIGNAL
from PySide6.QtWidgets import QWidget
from test_html_native_workspace import APP,flush
from native_ui_common import ScreenAnimationTimer

class Screen(QObject):
    refreshRateChanged=Signal(float)
    def __init__(self,hz):super().__init__();self.hz=hz
    def refreshRate(self):return self.hz

class ScreenTimerTests(unittest.TestCase):
    def test_refresh_rate_changes_are_not_limited_to_sixty_hz(self):
        owner=QWidget();timer=ScreenAnimationTimer(owner);screen=Screen(60)
        try:
            timer._screen=screen
            screen.refreshRateChanged.connect(timer._repace)
            for hz,interval in ((60,16),(120,8),(144,6),(165,6),(240,4),(360,2)):
                screen.hz=hz;screen.refreshRateChanged.emit(hz)
                self.assertEqual(timer.interval(),interval)
                self.assertFalse(timer.isActive(),'Rate changes must not start an idle timer')
            for hz in (0,float('nan'),float('inf'),-20):
                screen.hz=hz;timer._repace();self.assertEqual(timer.interval(),16)
        finally:timer.stop();owner.deleteLater();flush()

    def test_late_native_window_binding_and_destroy_leave_no_active_clock(self):
        owner=QWidget();timer=ScreenAnimationTimer(owner)
        timer.start();self.assertTrue(timer.isActive())
        owner.show();flush()
        self.assertIs(timer._handle,owner.windowHandle())
        self.assertGreaterEqual(timer.interval(),1)
        timer.stop();self.assertFalse(timer.isActive());owner.hide();owner.deleteLater();flush()

    def test_native_handle_replacement_and_screen_reconnection_do_not_duplicate_slots(self):
        class Handle(QObject):
            screenChanged=Signal(object)
            def __init__(self,screen):super().__init__();self.current=screen
            def screen(self):return self.current
        owner=QWidget();timer=ScreenAnimationTimer(owner);first=Screen(60);second=Screen(144)
        handle=Handle(first);replacement=Handle(second)
        try:
            with patch.object(owner,'windowHandle',return_value=handle):
                timer.start();self.assertEqual(timer.interval(),16)
                for _ in range(20):timer._bind()
                self.assertEqual(handle.receivers(SIGNAL('screenChanged(PyObject)')),1)
                handle.current=second;handle.screenChanged.emit(second)
                self.assertEqual(timer.interval(),6)
                first.hz=30;first.refreshRateChanged.emit(30)
                self.assertEqual(timer.interval(),6,'Detached screen must not change cadence')
            with patch.object(owner,'windowHandle',return_value=replacement):
                APP.sendEvent(owner,QEvent(QEvent.WinIdChange));flush()
                self.assertIs(timer._handle,replacement)
                self.assertEqual(handle.receivers(SIGNAL('screenChanged(PyObject)')),0)
                self.assertEqual(replacement.receivers(SIGNAL('screenChanged(PyObject)')),1)
                timer.stop();second.hz=240;second.refreshRateChanged.emit(240)
                self.assertEqual(timer.interval(),4);self.assertFalse(timer.isActive())
        finally:timer.stop();owner.deleteLater();flush()
