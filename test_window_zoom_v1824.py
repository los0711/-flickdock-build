"""Reversible utility zoom without native resize or lingering snapshots."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QWidget
from test_html_native_workspace import APP,flush,until
from ui_transitions import WindowFade,WINDOW_MIN_SCALE,UTILITY_TRANSITION_MS


class WindowZoomTests(unittest.TestCase):
    def test_close_finishes_without_waiting_for_an_occluded_windows_first_paint(self):
        from app_settings import SettingsDialog
        import test_html_native_workspace as workspace
        fixture=workspace.WorkspaceTests();fixture.setUp();window=SettingsDialog(fixture.settings)
        try:
            with patch.object(WindowFade,'enabled',return_value=True):
                window.show();fade=window._window_fade
                window.setUpdatesEnabled(False);fade.zoom.snapshot=__import__('PySide6.QtGui',fromlist=['QPixmap']).QPixmap()
                window.reject()
                self.assertFalse(fade._awaiting_capture)
                # No native animation frames can be delivered in this case.
                fade.animation._frames.stop()
                until(lambda:not window.isVisible(),seconds=1)
                self.assertIsNone(fade.pending);self.assertTrue(fade.zoom.snapshot.isNull())
        finally:window.setUpdatesEnabled(True);window.hide();window.deleteLater();fixture.tearDown()

    def test_first_paint_prepares_pixels_before_the_opening_clock_and_late_capture_cannot_reopen(self):
        from PySide6.QtCore import QAbstractAnimation
        window=QWidget();window.resize(640,420);fade=WindowFade(window)
        try:
            with patch.object(fade,'enabled',return_value=True),patch.object(fade.animation,'start',wraps=fade.animation.start) as start:
                window.show()
                self.assertTrue(fade._awaiting_capture)
                self.assertEqual(fade.animation.state(),QAbstractAnimation.Stopped);start.assert_not_called()
                window.grab();until(lambda:not fade._awaiting_capture)
                start.assert_called_once();self.assertFalse(fade.zoom.snapshot.isNull())
                window.hide();start.reset_mock()
                window.show();window.grab();window.hide();flush()
                self.assertFalse(fade._awaiting_capture);self.assertFalse(fade.animation._frames.isActive())
                self.assertFalse(window.isVisible())
        finally:window.hide();window.deleteLater();flush()

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
                    window.show();flush();fade=window._window_fade;until(lambda:not fade._awaiting_capture);bounds=window.geometry()
                    fade.animation.setCurrentTime(70);window.grab()
                    self.assertFalse(fade.zoom.snapshot.isNull());self.assertLess(fade.scale,1)
                    self.assertEqual(window.geometry(),bounds)
                    fade.animation.setCurrentTime(UTILITY_TRANSITION_MS)
                    window.close();self.assertIsNotNone(fade.pending)
                    until(lambda:not fade._awaiting_capture)
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
                until(lambda:not fade._awaiting_capture)
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
                fade.defer_done(0,lambda _:window.hide());until(lambda:not fade._awaiting_capture);fade.animation.setCurrentTime(UTILITY_TRANSITION_MS);flush()
                self.assertFalse(window.isVisible());self.assertTrue(fade.zoom.snapshot.isNull())
                self.assertFalse(fade.zoom.isEnabled());self.assertIsNone(fade.pending)
        finally:window.hide();window.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
