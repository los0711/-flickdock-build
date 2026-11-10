"""One compositing path, shared safe-work-area placement and persistent toggle."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import QObject,QPointF,QRect,Qt,Signal
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

    def test_video_prompt_and_copy_notice_share_a_safe_row_and_recenter_after_hide(self):
        shelf=QWidget();shelf.resize(390,420)
        manager=SimpleNamespace(shelf=shelf,settings=SimpleNamespace(theme='dark',clipboard_success_notice=True),show_video_dialog=Mock())
        prompt=VideoLinkPrompt(manager);copy=ClipboardNotice(manager);area=QRect(-1920,40,1920,1000)
        try:
            screen=SimpleNamespace(availableGeometry=lambda:area)
            with patch('native_ui_common.reduced_motion',return_value=True):
                with patch('video_prompt.QGuiApplication.screenAt',return_value=screen):prompt.present('https://www.youtube.com/watch?v=fixture')
                with patch('intake_notice.QGuiApplication.screenAt',return_value=screen):copy.show_result(None,dict(id='copy',phase='uploading'))
            self.assertLess(copy.geometry().right(),prompt.geometry().left())
            self.assertLessEqual(abs((copy.geometry().left()+prompt.geometry().right())/2-area.center().x()),1)
            for card in (prompt,copy):
                self.assertEqual(area.bottom()-card.geometry().bottom(),NOTICE_MARGIN_PX)
                self.assertTrue(area.contains(card.geometry()))
            with patch('native_ui_common.reduced_motion',return_value=True):prompt._accept()
            manager.show_video_dialog.assert_called_once_with('https://www.youtube.com/watch?v=fixture')
            self.assertLessEqual(abs(copy.geometry().center().x()-area.center().x()),1)
        finally:prompt.hide();prompt.deleteLater();copy.hide();copy.deleteLater();shelf.deleteLater();flush()

    def test_video_capsule_click_anywhere_opens_and_close_contracts_before_hiding(self):
        shelf=QWidget();manager=SimpleNamespace(shelf=shelf,settings=SimpleNamespace(theme='dark'),show_video_dialog=Mock())
        prompt=VideoLinkPrompt(manager)
        try:
            with patch('native_ui_common.reduced_motion',return_value=False):
                prompt.present('https://www.youtube.com/watch?v=fixture')
                prompt._morph.setCurrentTime(prompt._morph.duration());prompt._fade.setCurrentTime(prompt._fade.duration())
                bounds=prompt.geometry();width=prompt.capsule_rect().width()
                QTest.mouseClick(prompt,Qt.LeftButton,pos=prompt.capsule_rect().center().toPoint());flush()
                manager.show_video_dialog.assert_called_once_with('https://www.youtube.com/watch?v=fixture')
                self.assertTrue(prompt.isVisible());self.assertTrue(prompt._hiding)
                prompt._morph.setCurrentTime(prompt._morph.duration()//2)
                self.assertLess(prompt.capsule_rect().width(),width);self.assertEqual(prompt.geometry(),bounds)
                prompt._fade.setCurrentTime(prompt._fade.duration());self.assertFalse(prompt.isVisible())
        finally:prompt.hide();prompt.deleteLater();shelf.deleteLater();flush()

    def test_all_three_capsules_push_aside_with_rebound_and_return_after_one_leaves(self):
        from async_images import ImageCache
        from receive_notice import TransferCapsule
        shelf=QWidget();manager=SimpleNamespace(shelf=shelf,settings=SimpleNamespace(theme='dark',clipboard_success_notice=True),show_video_dialog=Mock())
        copy=ClipboardNotice(manager);video=VideoLinkPrompt(manager);images=ImageCache()
        transfer=TransferCapsule(SimpleNamespace(manager=manager,images=images,activate=Mock()))
        record=dict(id='transfer',direction='send',name='设计稿.zip',state='transferring',received=30,total=100,paths=[])
        area=QRect(0,0,1920,1000);screen=SimpleNamespace(availableGeometry=lambda:area)
        try:
            with patch('native_ui_common.reduced_motion',return_value=False),patch('intake_notice.QGuiApplication.screenAt',return_value=screen),patch('video_prompt.QGuiApplication.screenAt',return_value=screen),patch('receive_notice.QGuiApplication.screenAt',return_value=screen):
                copy.show_result(None,dict(id='copy',phase='uploading'))
                video.present('https://www.youtube.com/watch?v=fixture');transfer.present(record)
                samples=[];target=copy._position_motion.endValue().x()
                for at in (80,180,280,360,420):copy._position_motion.setCurrentTime(at);samples.append(copy.x())
                self.assertLess(min(samples),target)
                for card in (copy,transfer,video):
                    card._position_motion.setCurrentTime(card._position_motion.duration())
                    card._morph.setCurrentTime(card._morph.duration());card._fade.setCurrentTime(card._fade.duration())
                self.assertLess(copy.geometry().right(),transfer.geometry().left());self.assertLess(transfer.geometry().right(),video.geometry().left())
                video.supersede();self.assertEqual(copy._position_motion.state(),copy._position_motion.Running)
                for card in (copy,transfer):card._position_motion.setCurrentTime(card._position_motion.duration())
                self.assertLess(copy.geometry().right(),transfer.geometry().left())
        finally:
            for card in (copy,video,transfer):card.supersede();card.deleteLater()
            images.close();images.deleteLater();shelf.deleteLater();flush()

    def test_repeated_same_native_copy_renews_three_second_prompt_without_duplicate_sync(self):
        from video_prompt import VideoLinkWatcher,PROMPT_DURATION_MS
        from clipboard_sync import ClipboardSync
        from test_clipboard_sync_v1819 import Account,Clipboard,Events,Hub
        class Settings(QObject):
            changed=Signal()
            video_link_prompt=True
            theme='dark'
        self.assertEqual(PROMPT_DURATION_MS,3000)
        with TemporaryDirectory() as directory:
            clipboard=Clipboard();hub=Hub();client=ClipboardSync(Account(hub.api('fixture'),'fixture'),Events(),Path(directory),clipboard=clipboard,enabled=False)
            shelf=QWidget();manager=SimpleNamespace(app=APP,shelf=shelf,settings=Settings(),clipboard_sync=client,show_video_dialog=Mock())
            watcher=VideoLinkWatcher(manager);url='https://www.youtube.com/watch?v=fixture'
            try:
                with patch('native_ui_common.reduced_motion',return_value=True),patch('video_prompt.PROMPT_DURATION_MS',240),patch('video_prompt.recognized_video',return_value=True) as recognize:
                    clipboard.text(url);until(lambda:watcher.prompt is not None and watcher.prompt.isVisible())
                    prompt=watcher.prompt;QTest.qWait(110);clipboard.text(url);QTest.qWait(150)
                    self.assertIs(watcher.prompt,prompt);self.assertTrue(prompt.isVisible())
                    QTest.qWait(120);self.assertFalse(prompt.isVisible())
                    clipboard.text(url);flush();self.assertTrue(prompt.isVisible());self.assertEqual(recognize.call_count,1)
                    self.assertEqual(hub.calls,[])
                    manager.settings.video_link_prompt=False;manager.settings.changed.emit();flush()
                    self.assertFalse(prompt.isVisible());clipboard.text(url);flush();self.assertFalse(prompt.isVisible())
            finally:
                watcher.close();until(lambda:not watcher.jobs);client.close()
                if watcher.prompt:watcher.prompt.hide();watcher.prompt.deleteLater()
                watcher.deleteLater();shelf.deleteLater();flush()

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
