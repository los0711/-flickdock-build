"""One compositing path, shared safe-work-area placement and persistent toggle."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import QPointF,QRect,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget,QLabel,QVBoxLayout
from test_html_native_workspace import APP,flush,until
from ui_transitions import WindowFade,UTILITY_TRANSITION_MS
from intake_notice import ClipboardNotice,NOTICE_MARGIN_PX
from video_prompt import VideoLinkPrompt

class UtilityFeedbackTests(unittest.TestCase):
    def test_zoom_frames_blend_without_native_opacity_changes_and_restore_live_content(self):
        window=QWidget();window.setAttribute(Qt.WA_TranslucentBackground);window.resize(640,420)
        layout=QVBoxLayout(window);label=QLabel('Original',window);label.setStyleSheet('background:#2a2a2a;color:white');layout.addWidget(label)
        fade=WindowFade(window)
        try:
            with patch.object(fade,'enabled',return_value=True):
                window.show();window.grab();until(lambda:not fade._awaiting_capture);fade.animation.stop();bounds=window.geometry()
                with patch.object(window,'setWindowOpacity',wraps=window.setWindowOpacity) as native:
                    for step in range(20):
                        fade._frame(QPointF(.78+.01*step,.96+.002*step));window.grab()
                    native.assert_not_called()
                self.assertEqual(window.geometry(),bounds);self.assertEqual(window.windowOpacity(),1.)
                self.assertAlmostEqual(fade.zoom.opacity,.97);self.assertAlmostEqual(fade.zoom.scale,.998)
                label.setText('Updated during opening');fade._finished();flush()
                self.assertFalse(fade.zoom.isEnabled());self.assertTrue(fade.zoom.snapshot.isNull());self.assertEqual(label.text(),'Updated during opening')
        finally:window.hide();window.deleteLater();flush()

    def test_video_prompt_and_copy_notice_share_bottom_center_and_dock_clearance(self):
        shelf=QWidget();shelf.resize(390,420)
        manager=SimpleNamespace(shelf=shelf,settings=SimpleNamespace(theme='dark'),show_video_dialog=Mock())
        prompt=VideoLinkPrompt(manager);copy=ClipboardNotice(manager);area=QRect(-1920,40,1920,1000)
        try:
            screen=SimpleNamespace(availableGeometry=lambda:area)
            with patch('video_prompt.QGuiApplication.screenAt',return_value=screen):prompt.present('https://www.youtube.com/watch?v=fixture')
            copy.resize(320,120);copy._place(area)
            for card in (prompt,copy):
                self.assertLessEqual(abs(card.geometry().center().x()-area.center().x()),1)
                self.assertEqual(area.bottom()-card.geometry().bottom(),NOTICE_MARGIN_PX)
                self.assertTrue(area.contains(card.geometry()))
            prompt._accept();manager.show_video_dialog.assert_called_once_with('https://www.youtube.com/watch?v=fixture')
        finally:prompt.hide();prompt.deleteLater();copy.hide();copy.deleteLater();shelf.deleteLater();flush()

    def test_video_prompt_switch_is_larger_clear_in_both_themes_and_persists(self):
        from app_settings import SettingsStore,SettingsToggleSwitch
        from video_controller import VideoController
        from video_panel import VideoDialog
        with TemporaryDirectory() as directory:
            settings=SettingsStore(Path(directory)/'profile');shelf=QWidget()
            manager=SimpleNamespace(settings=settings,shelf=shelf,app=APP)
            controller=VideoController(manager);window=VideoDialog(manager,controller)
            try:
                window.show();flush();self.assertIsInstance(window.prompt,SettingsToggleSwitch)
                self.assertGreaterEqual(window.prompt.height(),38);self.assertGreaterEqual(window.prompt.width(),240)
                for theme in ('dark','light'):
                    settings.update(theme=theme);window.refresh();flush();self.assertEqual(window.prompt.dark,theme=='dark')
                    window.prompt.setChecked(True)
                    position=window.prompt.rect().center();position.setX(window.prompt.width()-25)
                    QTest.mouseClick(window.prompt,Qt.LeftButton,pos=position)
                    self.assertFalse(settings.video_link_prompt)
                    self.assertFalse(SettingsStore(Path(directory)/'profile').video_link_prompt)
            finally:window.hide();window.deleteLater();controller.close();shelf.deleteLater();flush()

if __name__=='__main__':unittest.main()
