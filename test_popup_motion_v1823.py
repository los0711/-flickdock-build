"""Finite popup visuals with live data, stable geometry and immediate intent."""
import unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QPushButton,QFrame,QGraphicsDropShadowEffect
from shiboken6 import isValid
from test_html_native_workspace import APP,flush,until,WorkspaceTests
from ui_transitions import WindowFade,_ClosingSnapshot


def image(widget):
    # Keep QImage alive while consuming its non-owning constBits memoryview.
    frame=widget.grab().toImage();return bytes(frame.constBits())


class PopupMotionTests(unittest.TestCase):
    def setUp(self):
        self.motion=patch('ui_transitions.WindowFade.enabled',return_value=True);self.motion.start()
        self.reduced=patch('native_ui_common.reduced_motion',return_value=False);self.reduced.start()
        self.window=QDialog();self.window.setAttribute(Qt.WA_TranslucentBackground)
        self.window.setGeometry(100,100,360,220);layout=QVBoxLayout(self.window)
        self.body=QFrame();self.body.setStyleSheet('background:#242426;border-radius:10px;');layout.addWidget(self.body)
        inner=QVBoxLayout(self.body);self.label=QLabel('live data');inner.addWidget(self.label)
        self.button=QPushButton('Action');inner.addWidget(self.button)
        self.shadow=QGraphicsDropShadowEffect(self.body);self.body.setGraphicsEffect(self.shadow)
        self.fade=WindowFade(self.window);self.window.show();self.fade.animation.pause()

    def tearDown(self):
        self.window.hide();self.window.deleteLater();flush();self.reduced.stop();self.motion.stop()

    def test_live_source_scales_without_native_resize_and_effect_is_removed(self):
        geometry=self.window.geometry();self.assertAlmostEqual(self.fade.scale,.97)
        self.assertIsNotNone(self.window.graphicsEffect());self.assertIs(self.body.graphicsEffect(),self.shadow)
        self.fade.animation.setCurrentTime(70);old=image(self.window)
        self.label.setText('Different live contents while the animation is paused');new=image(self.window)
        self.assertNotEqual(old,new,'Transition must not freeze a snapshot over changed data')
        self.assertGreater(self.fade.scale,.97);self.assertLess(self.fade.scale,1)
        seen=[];self.button.clicked.connect(lambda:seen.append(True))
        QTest.mouseClick(self.button,Qt.LeftButton,pos=QPoint(1,1));self.assertEqual(seen,[True])
        self.assertEqual(self.window.geometry(),geometry)
        self.fade.animation.setCurrentTime(180);flush()
        self.assertIsNone(self.window.graphicsEffect());self.assertIs(self.body.graphicsEffect(),self.shadow)
        self.assertEqual(self.fade.scale,1);self.assertEqual(self.window.windowOpacity(),1)
        self.label.setText('latest endpoint');self.assertNotEqual(image(self.window),new)

    def test_close_reopen_keeps_current_pose_and_discards_pending_callback(self):
        self.fade.animation.setCurrentTime(180);seen=[]
        self.assertTrue(self.fade.defer_done('close',seen.append));self.fade.animation.pause();self.fade.animation.setCurrentTime(65)
        current=(self.fade.alpha,self.fade.scale);self.fade.reopen()
        self.assertEqual((self.fade.alpha,self.fade.scale),current)
        self.fade.animation.setCurrentTime(180);flush()
        self.assertEqual(seen,[]);self.assertTrue(self.window.isVisible());self.assertIsNone(self.window.graphicsEffect())
        self.assertTrue(self.fade.defer_done('once',seen.append));self.fade.animation.setCurrentTime(180)
        self.assertEqual(seen,['once']);self.assertIsNone(self.window.graphicsEffect())

    def test_hidden_and_minimized_cancel_the_visual_without_closing(self):
        self.fade.animation.setCurrentTime(70);self.window.hide();flush()
        self.assertIsNone(self.window.graphicsEffect());self.assertEqual(self.fade.scale,1)
        self.window.show();self.fade.animation.setCurrentTime(180);seen=[]
        self.fade.defer_done('old close',seen.append);self.fade.animation.setCurrentTime(65)
        self.window.showMinimized();flush()
        self.assertTrue(self.window.isMinimized());self.assertEqual(seen,[]);self.assertIsNone(self.fade.pending)
        self.assertIsNone(self.window.graphicsEffect());self.window.showNormal();flush()
        self.assertTrue(self.window.isVisible());self.assertEqual(seen,[])

    def test_existing_root_effect_is_never_replaced(self):
        self.fade.animation.setCurrentTime(180)
        effect=QGraphicsDropShadowEffect(self.window);self.window.setGraphicsEffect(effect)
        self.fade.defer_done(None,lambda _:None);self.fade.animation.setCurrentTime(70)
        self.assertIs(self.window.graphicsEffect(),effect)
        self.fade.animation.setCurrentTime(180);self.assertIs(self.window.graphicsEffect(),effect)

    def test_reduced_motion_does_not_leave_a_tree_effect_or_pending_close(self):
        self.window.hide()
        with patch.object(self.fade,'enabled',return_value=False):
            self.window.show();self.assertEqual(self.fade.scale,1);self.assertIsNone(self.window.graphicsEffect())
            self.assertFalse(self.fade.defer_done(None,lambda _:None));self.assertIsNone(self.fade.pending)

    def test_clear_confirmation_commits_immediately_and_exit_is_visual_only(self):
        from confirm_clear import ClearConfirmOverlay,confirmation_active
        self.fade.animation.setCurrentTime(180);overlay=ClearConfirmOverlay(self.body,3);seen=[]
        overlay.finished.connect(seen.append);overlay.open();overlay._window_fade.animation.setCurrentTime(180)
        shadow=overlay.panel.graphicsEffect();overlay.delete_button.click()
        self.assertEqual(seen,[True]);self.assertFalse(overlay.active);self.assertFalse(overlay.isVisible())
        self.assertFalse(confirmation_active(self.window));self.assertFalse(confirmation_active(self.body))
        ghost=overlay._closing_snapshot;self.assertIsNotNone(ghost);self.assertTrue(ghost.isVisible())
        self.assertTrue(ghost.testAttribute(Qt.WA_TransparentForMouseEvents));self.assertIs(overlay.panel.graphicsEffect(),shadow)
        overlay.deleteLater();flush();self.assertTrue(isValid(ghost),'Immediate dialog deletion cannot remove the exit artwork')
        ghost.animation.setCurrentTime(160);flush();self.assertFalse(isValid(ghost))


class ActualPopupTests(unittest.TestCase):
    def setUp(self):
        # These UI tests never make a request; avoid probing host TLS plugin
        # DLLs while creating the disposable resolver in the shared fixture.
        self._tls=patch('content_resolver.QSslSocket.availableBackends',return_value=[]);self._tls.start()
        try:WorkspaceTests.setUp(self)
        except BaseException:self._tls.stop();raise

    def tearDown(self):
        try:WorkspaceTests.tearDown(self)
        finally:self._tls.stop()

    def test_update_notice_reopen_cancels_ghost_without_delaying_dismiss(self):
        from update_notice import UpdateNotice
        notice=UpdateNotice(self.shelf,theme='dark');seen=[];notice.dismissed.connect(lambda:seen.append(True))
        try:
            with patch('ui_transitions.WindowFade.enabled',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
                shadow=notice.surface.graphicsEffect();notice.show_version('1.8.24','first');notice._window_fade.animation.setCurrentTime(180)
                notice.dismiss();self.assertEqual(seen,[True]);self.assertFalse(notice.isVisible())
                self.assertFalse(self.shelf.property('flickdockConfirmationActive'))
                ghost=notice._closing_snapshot;notice.show_version('1.8.25','latest');flush()
                self.assertFalse(isValid(ghost));self.assertIn('1.8.25',notice.version_label.text())
                notice._window_fade.animation.setCurrentTime(180)
                self.assertIsNone(notice.graphicsEffect());self.assertIs(notice.surface.graphicsEffect(),shadow)
        finally:notice.hide();notice.deleteLater();flush()

    def test_real_settings_update_stripe_stays_live_and_has_no_persistent_effect(self):
        from app_settings import SettingsDialog
        dialog=SettingsDialog(self.settings)
        try:
            with patch.object(dialog._window_fade,'enabled',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
                dialog.show();dialog._window_fade.animation.pause();geometry=dialog.geometry()
                dialog.set_update_state('checking');flush();self.assertFalse(dialog.update_activity.isHidden())
                bar=dialog.update_activity;top=bar.mapTo(dialog,QPoint()).y()
                mapped=round((top-dialog.height()/2)*dialog._window_fade.scale+dialog.height()/2)
                def stripe_pixels():
                    frame=dialog.grab().toImage();ratio=frame.devicePixelRatio();points=0
                    for y in range(max(0,round((mapped-3)*ratio)),min(frame.height(),round((mapped+bar.height()+3)*ratio))):
                        # Exclude the fixed outer blue focus/shadow pixels;
                        # only the horizontal update stripe is under test.
                        for x in range(round(40*ratio),frame.width()-round(40*ratio)):
                            c=frame.pixelColor(x,y)
                            if c.blue()>c.red()+35 and c.blue()>c.green()+15:points+=1
                    return points
                self.assertGreater(stripe_pixels(),0)
                dialog.set_update_state('latest');flush();self.assertTrue(dialog.update_activity.isHidden())
                self.assertEqual(stripe_pixels(),0,'Live effect cache must repaint the removed update stripe')
                dialog._window_fade.animation.setCurrentTime(180);flush()
                self.assertIsNone(dialog.graphicsEffect());self.assertIsNone(dialog.surface.graphicsEffect())
                self.assertEqual(dialog.geometry(),geometry);self.assertTrue(dialog.update_activity.isHidden())
                dialog.set_update_state('checking');dialog.set_update_state('latest');flush()
                self.assertTrue(dialog.update_activity.isHidden());self.assertFalse(dialog.update_activity._clock.isValid())
        finally:dialog.hide();dialog.deleteLater();flush()

    def test_license_reader_keeps_native_controls_readonly_text_and_close_semantics(self):
        from app_settings import ThirdPartyDialog
        dialog=ThirdPartyDialog(theme='dark');flags=dialog.windowFlags()
        try:
            with patch.object(dialog._window_fade,'enabled',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
                dialog.show();self.assertFalse(dialog.isModal());self.assertTrue(dialog.license_text.isReadOnly())
                self.assertTrue(flags&Qt.WindowTitleHint);self.assertTrue(flags&Qt.WindowCloseButtonHint)
                self.assertIsNotNone(getattr(dialog.license_text,'_input_focus_motion',None))
                dialog._window_fade.animation.setCurrentTime(180);self.assertIsNone(dialog.graphicsEffect())
                dialog.close();self.assertIsNotNone(dialog._window_fade.pending)
                dialog._window_fade.animation.setCurrentTime(180);self.assertFalse(dialog.isVisible())
                self.assertEqual(dialog.windowFlags(),flags);self.assertIsNone(dialog.graphicsEffect())
        finally:dialog.hide();dialog.deleteLater();flush()

    def test_sender_toast_uses_one_eased_clock_and_cleans_up_on_supersede(self):
        from intake_notice import ClipboardNotice
        notice=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark')))
        try:
            with patch('native_ui_common.reduced_motion',return_value=False):
                notice.show_result(True,dict(id='one',point=(10,10),value=('text',{'text':'first'})))
                notice.animation.pause();notice.animation.setCurrentTime(70)
                entry=notice.motion.active[-1];self.assertGreater(entry['y'],0);self.assertLess(entry['y'],6)
                self.assertGreater(entry['alpha'],0);self.assertLess(entry['alpha'],1)
                self.assertTrue(notice.windowFlags()&Qt.WindowDoesNotAcceptFocus)
                notice.show_result(False,dict(id='two',point=(10,10),value=('text',{'text':'latest'})))
                notice.animation.setCurrentTime(200);flush()
                self.assertEqual(notice.preview.text(),'latest');self.assertEqual(notice.message.text(),'复制同步失败')
                self.assertIsNone(notice.graphicsEffect());notice.dismiss();outgoing=notice.motion.ghosts[-1];position=outgoing['y'];notice.animation.setCurrentTime(90)
                self.assertEqual(outgoing['y'],position);self.assertLess(outgoing['alpha'],1);notice.supersede();flush()
                self.assertFalse(notice.isVisible());self.assertIsNone(notice.graphicsEffect())
                self.assertFalse(notice.animation.state()==notice.animation.State.Running)
                self.assertFalse(notice.timer.isActive());self.assertFalse(notice._result_timer.isActive())
        finally:notice.hide();notice.deleteLater();flush()


if __name__=='__main__':unittest.main()
