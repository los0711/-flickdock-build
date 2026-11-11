"""One compositing path, shared safe-work-area placement and persistent toggle."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import os
import subprocess
import sys
import textwrap
from unittest.mock import Mock,patch
from PySide6.QtCore import QObject,QPointF,QRect,Qt,Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget,QLabel,QVBoxLayout
from test_html_native_workspace import APP,flush,until
from ui_transitions import WindowFade,UTILITY_TRANSITION_MS
from intake_notice import ClipboardNotice,NOTICE_MARGIN_PX,NOTICE_PAIR_GAP_PX
from video_prompt import VideoLinkPrompt

def visual(card):return card.capsule_rect().translated(card.pos())

class UtilityFeedbackTests(unittest.TestCase):
    def test_clipboard_success_keeps_both_capsule_sizes_for_three_seconds(self):
        shelf=QWidget();manager=SimpleNamespace(shelf=shelf,settings=SimpleNamespace(theme='dark',clipboard_success_notice=True))
        notice=ClipboardNotice(manager)
        try:
            notice.show_result(None,dict(id='fixture-copy',phase='uploading'))
            notice._expire();self.assertTrue(notice._compact)
            notice.show_result(True,dict(id='fixture-copy',phase='synced',backed_up=True))
            self.assertEqual(notice._phase,'success');self.assertGreaterEqual(notice.timer.interval(),3000)
            notice.toggle_expanded();self.assertFalse(notice._compact)
            self.assertGreaterEqual(notice.timer.interval(),3000)
        finally:notice.supersede();notice.deleteLater();shelf.deleteLater();flush()

    def test_online_installer_handoff_exits_with_an_animated_dialog_open(self):
        # A separate live event loop is essential: a mocked quit() cannot expose
        # Qt rejecting Quit after a dialog defers its Close for animation.
        script=textwrap.dedent('''
            import os,sys,tempfile
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import patch
            with tempfile.TemporaryDirectory(prefix='flickdock-update-exit-') as profile:
                os.environ['APPDATA']=str(Path(profile)/'roaming')
                os.environ['LOCALAPPDATA']=str(Path(profile)/'local')
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QApplication
                import flickdock,ui_transitions
                from update_controller import VerifiedInstaller,launch_installer
                app=QApplication([]);flickdock.load_font(app)
                with patch.object(ui_transitions.WindowFade,'enabled',return_value=True):
                    manager=flickdock.ShelfManager(app,cocoa_smoke=True)
                    manager.show_settings()
                    if sys.argv[1]=='closing':manager.settings_dialog.close()
                    root=Path(profile)/'updates';version='99.0.0'
                    installer=root/version/'setup.exe';installer.parent.mkdir(parents=True)
                    installer.write_bytes(b'MZ disposable QA fixture')
                    executable=Path(profile)/'FlickDock.exe';executable.write_bytes(b'MZ disposable QA target')
                    lease=VerifiedInstaller(installer,dict(version=version),root,installer.open('rb'))
                    spawned=[];exited=[]
                    app.aboutToQuit.connect(lambda:exited.append(True))
                    def handoff():
                        launch_installer(lease,installed_exe=executable,
                            popen=lambda args,**kw:spawned.append(args) or SimpleNamespace(pid=123),
                            quit_callback=manager.quit,quiet=True)
                        manager.show_first()
                        assert not manager.shelf.isVisible()
                    QTimer.singleShot(40,handoff)
                    QTimer.singleShot(2000,lambda:app.exit(7))
                    result=app.exec()
                    assert result==0 and exited==[True] and len(spawned)==1,(result,exited,len(spawned))
                    assert '/VERYSILENT' in spawned[0] and '/SILENT' not in spawned[0]
                    assert manager.manual.closed and manager.cloud.closed
                    manager.quit()
                    assert not (root/version/'install.log').exists()
        ''')
        env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONUTF8='1')
        for state in ('open','closing'):
            with self.subTest(state=state):
                result=subprocess.run([sys.executable,'-c',script,state],cwd=Path(__file__).parent,
                    env=env,capture_output=True,text=True,encoding='utf8',timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

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
            self.assertAlmostEqual(visual(prompt).left()-visual(copy).right(),NOTICE_PAIR_GAP_PX,delta=1)
            self.assertLessEqual(abs((visual(copy).left()+visual(prompt).right())/2-area.center().x()),1)
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
                self.assertAlmostEqual(visual(transfer).left()-visual(copy).right(),NOTICE_PAIR_GAP_PX,delta=1)
                self.assertAlmostEqual(visual(video).left()-visual(transfer).right(),NOTICE_PAIR_GAP_PX,delta=1)
                copy._expire();transfer._expire()
                for card in (copy,transfer,video):
                    card._position_motion.setCurrentTime(card._position_motion.duration());card._morph.setCurrentTime(card._morph.duration())
                self.assertAlmostEqual(visual(transfer).left()-visual(copy).right(),NOTICE_PAIR_GAP_PX,delta=1)
                self.assertAlmostEqual(visual(video).left()-visual(transfer).right(),NOTICE_PAIR_GAP_PX,delta=1)
                video.supersede();self.assertEqual(copy._position_motion.state(),copy._position_motion.Running)
                for card in (copy,transfer):card._position_motion.setCurrentTime(card._position_motion.duration())
                self.assertAlmostEqual(visual(transfer).left()-visual(copy).right(),NOTICE_PAIR_GAP_PX,delta=1)
        finally:
            for card in (copy,video,transfer):card.supersede();card.deleteLater()
            images.close();images.deleteLater();shelf.deleteLater();flush()

    def test_pair_splits_then_merges_without_an_overlay_or_second_recentering(self):
        import notice_cluster
        from PySide6.QtGui import QPainterPath
        shelf=QWidget();manager=SimpleNamespace(shelf=shelf,settings=SimpleNamespace(theme='dark',clipboard_success_notice=True),show_video_dialog=Mock())
        copy=ClipboardNotice(manager);video=VideoLinkPrompt(manager)
        area=QRect(0,0,1920,1000);screen=SimpleNamespace(availableGeometry=lambda:area)
        def finish(card):
            card._position_motion.setCurrentTime(card._position_motion.duration())
            card._morph.setCurrentTime(card._morph.duration());card._fade.setCurrentTime(card._fade.duration())
        try:
            with patch('native_ui_common.reduced_motion',return_value=False),patch('intake_notice.QGuiApplication.screenAt',return_value=screen),patch('video_prompt.QGuiApplication.screenAt',return_value=screen):
                copy.show_result(True,dict(id='copy',phase='synced',backed_up=True));finish(copy)
                video.present('https://www.youtube.com/watch?v=fixture')
                self.assertIs(video._liquid_peer(),copy)
                self.assertEqual(video._position_motion.duration(),420)
                self.assertEqual(video._morph.duration(),420)
                self.assertAlmostEqual(visual(copy).center().x(),visual(video).center().x(),delta=1)
                sizes=[card.size() for card in (copy,video)];bridges=[]
                for at in (60,120,180,240,300,360):
                    for card in (copy,video):
                        card._position_motion.setCurrentTime(at)
                        if card is video:
                            card._morph.setCurrentTime(at);card._fade.setCurrentTime(min(at,card._fade.duration()))
                    bridge=notice_cluster.shape(video,QPainterPath())
                    bridges.append(not bridge.isEmpty())
                    if not bridge.isEmpty():
                        left,right=sorted((visual(copy),visual(video)),key=lambda rect:rect.center().x())
                        middle=QPointF((left.right()+right.left())/2,(left.center().y()+right.center().y())/2)
                        for card in (copy,video):
                            curve=notice_cluster.shape(card,QPainterPath())
                            for offset in (-.75,.75):
                                self.assertTrue(curve.contains(middle-QPointF(card.pos())+QPointF(offset,0)))
                    if at>=300:self.assertTrue(notice_cluster.shape(video,QPainterPath()).isEmpty())
                    self.assertEqual([card.size() for card in (copy,video)],sizes)
                self.assertTrue(any(bridges),bridges)
                for card in (copy,video):finish(card)
                self.assertIsNone(video._liquid_peer)
                copy.dismiss();self.assertIs(copy._liquid_peer(),video)
                target=video._position_motion.endValue()
                video._position_motion.setCurrentTime(180)
                original_start=video._position_motion.startValue()
                finish(copy)
                self.assertEqual(video._position_motion.currentTime(),180)
                self.assertEqual(video._position_motion.startValue(),original_start)
                finish(video)
                self.assertEqual(video.pos(),target)
                self.assertEqual(video._position_motion.state(),video._position_motion.Stopped)
                self.assertAlmostEqual(visual(video).center().x(),area.center().x(),delta=1)
                self.assertFalse(copy.isVisible());self.assertIsNone(copy._liquid_peer)
                self.assertFalse(video._liquid_children)
        finally:
            for card in (copy,video):card.supersede();card.deleteLater()
            shelf.deleteLater();flush()

    def test_repeated_same_native_copy_renews_five_second_prompt_without_duplicate_sync(self):
        from video_prompt import VideoLinkWatcher,PROMPT_DURATION_MS
        from clipboard_sync import ClipboardSync
        from test_clipboard_sync_v1819 import Account,Clipboard,Events,Hub
        class Settings(QObject):
            changed=Signal()
            video_link_prompt=True
            theme='dark'
        self.assertEqual(PROMPT_DURATION_MS,5000)
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
