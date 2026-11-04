"""Reversible utility zoom without native resize or lingering snapshots."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QWidget
from test_html_native_workspace import APP,flush
from ui_transitions import WindowFade,WINDOW_MIN_SCALE,UTILITY_TRANSITION_MS


class WindowZoomTests(unittest.TestCase):
    def test_actual_settings_transfer_and_clipboard_dialogs_complete_zoom_close(self):
        from app_settings import SettingsDialog
        from lan_panel import LanDialog
        from test_html_native_lan import FakeLan
        from test_clipboard_preview_v1821 import PreviewTests
        fixture=PreviewTests();fixture.setUp();windows=[]
        try:
            fixture.manager.show_clipboard_preview();preview=fixture.manager.clipboard_preview_dialog;preview.hide()
            windows=[SettingsDialog(fixture.settings),LanDialog(FakeLan(),fixture.settings),preview]
            with patch.object(WindowFade,'enabled',return_value=True):
                for window in windows:
                    window.show();flush();fade=window._window_fade;bounds=window.geometry()
                    fade.animation.setCurrentTime(70);window.grab()
                    self.assertFalse(fade.zoom.snapshot.isNull());self.assertLess(fade.scale,1)
                    self.assertEqual(window.geometry(),bounds)
                    fade.animation.setCurrentTime(UTILITY_TRANSITION_MS)
                    window.close();self.assertIsNotNone(fade.pending)
                    fade.animation.setCurrentTime(UTILITY_TRANSITION_MS);flush()
                    self.assertFalse(window.isVisible());self.assertFalse(fade.zoom.isEnabled())
        finally:
            for window in windows[:2]:window.hide();window.deleteLater()
            fixture.tearDown()

    def test_entry_exit_and_reopen_preserve_native_bounds_and_intermediate_frame(self):
        window=QWidget();window.resize(640,420);fade=WindowFade(window)
        try:
            with patch.object(fade,'enabled',return_value=True):
                window.show();flush();bounds=window.geometry()
                self.assertEqual(fade.animation.endValue(),QPointF(1,1))
                fade.animation.setCurrentTime(60);window.grab()
                self.assertTrue(WINDOW_MIN_SCALE<fade.scale<1)
                self.assertEqual(window.geometry(),bounds)
                completions=[];current=QPointF(window.windowOpacity(),fade.scale)
                self.assertTrue(fade.defer_done(0,completions.append))
                self.assertEqual(fade.animation.startValue(),current)
                fade.animation.setCurrentTime(60);current=QPointF(window.windowOpacity(),fade.scale)
                fade.reopen();self.assertIsNone(fade.pending)
                self.assertEqual(fade.animation.startValue(),current)
                fade.animation.setCurrentTime(UTILITY_TRANSITION_MS);flush()
                self.assertEqual(completions,[]);self.assertFalse(fade.zoom.isEnabled())
                self.assertEqual(window.geometry(),bounds)
                fade.defer_done(0,lambda _:window.hide());fade.animation.setCurrentTime(UTILITY_TRANSITION_MS);flush()
                self.assertFalse(window.isVisible());self.assertTrue(fade.zoom.snapshot.isNull())
                self.assertFalse(fade.zoom.isEnabled());self.assertIsNone(fade.pending)
        finally:window.hide();window.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
