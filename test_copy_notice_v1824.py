"""Real capsule lifecycle, acknowledgement fences and nonactivating native host."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,Qt
from PySide6.QtGui import QImage,QPainter,QColor,QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit,QLabel,QWidget
from test_html_native_workspace import APP,flush
from flickdock import ShelfManager
from intake_notice import ClipboardNotice,NOTICE_MARGIN_PX,COMPACT_SIDE_PX


def uploading(identity='current-copy'):
    return dict(id=identity,phase='uploading',local_copy=True,value=('files',['private-name.png']),
        cloud_transfer=dict(phase='uploading',received=10,total=100),deliveries={'cloud':dict(success=0,total=2)})


class CopyNoticeTests(unittest.TestCase):
    def setUp(self):
        self.reduced=patch('native_ui_common.reduced_motion',return_value=True);self.reduced.start()
        self.card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
    def tearDown(self):
        self.card.supersede();self.card.deleteLater();flush();self.reduced.stop()

    def test_progress_updates_do_not_restart_entry_or_extend_the_full_capsule_timeout(self):
        card=self.card;state=uploading()
        with patch.object(card,'_animate_shape',wraps=card._animate_shape) as shape:
            card.update_delivery(state);QTest.qWait(180);flush()
            self.assertTrue(card.isVisible());self.assertEqual(card.accessibleName(),'剪贴板同步中')
            self.assertEqual(card._progress['received'],10);expiry=card.timer.remainingTime()
            card.update_delivery(dict(state,cloud_transfer=dict(phase='verifying',received=100,total=100)))
            self.assertIn('云端确认',card.accessibleDescription())
            self.assertLessEqual(card.timer.remainingTime(),expiry);self.assertEqual(shape.call_count,1)
            self.assertFalse(card.findChildren(QLabel))
            self.assertNotIn('private-name',card.accessibleName()+card.accessibleDescription())

    def test_cloud_commit_finishes_the_compact_notice_without_waiting_for_device_receipts(self):
        card=self.card;state=uploading();card.show_result(None,state);bounds=card.geometry()
        card.timer.start(10);QTest.qWait(40);flush()
        self.assertTrue(card.isVisible());self.assertTrue(card._compact)
        self.assertEqual(card.capsule_rect().width(),COMPACT_SIDE_PX);self.assertEqual(card.geometry(),bounds)
        self.assertFalse(card.timer.isActive())
        card.update_delivery(dict(state,phase='synced',backed_up=True,cloud_transfer=dict(phase='complete',received=1,total=1),deliveries={'cloud':dict(success=0,total=2)}))
        self.assertEqual(card._phase,'success');self.assertTrue(card._compact);self.assertTrue(card.timer.isActive())
        self.assertFalse(card._ring_clock.isActive());self.assertIn('已同步到云端',card.accessibleDescription())
        self.assertNotIn('设备已接收',card.accessibleDescription())
        expiry=card.timer.remainingTime()
        card.update_delivery(dict(id=state['id'],deliveries={'cloud':dict(success=1,total=2)}))
        self.assertEqual(card._phase,'success');self.assertLessEqual(card.timer.remainingTime(),expiry)
        self.assertEqual(card.capsule_rect().width(),COMPACT_SIDE_PX)
        card.timer.start(10);QTest.qWait(40);flush();self.assertFalse(card.isVisible())
        self.assertFalse(card._ring_clock.isActive());self.assertEqual(card.geometry(),bounds)

    def test_uploaded_bytes_alone_do_not_claim_cloud_commit(self):
        card=self.card;state=uploading();card.show_result(None,state)
        card.update_delivery(dict(state,cloud_transfer=dict(phase='verifying',received=100,total=100)))
        self.assertEqual(card._phase,'syncing');self.assertIn('等待云端确认',card.accessibleDescription())
        card.update_delivery(dict(state,backed_up=True,cloud_transfer=dict(phase='complete',received=1,total=1)))
        self.assertEqual(card._phase,'success');self.assertFalse(card._ring_clock.isActive())

    def test_real_progress_interpolates_without_spinner_or_unconfirmed_byte_growth(self):
        self.reduced.stop();self.reduced=patch('native_ui_common.reduced_motion',return_value=False);self.reduced.start()
        card=self.card;state=uploading();card.show_result(None,state)
        card.update_delivery(dict(state,cloud_transfer=dict(phase='uploading',received=80,total=100)))
        self.assertFalse(card._ring_clock.isActive());self.assertEqual(card._progress['received'],80)
        card._progress_motion.setCurrentTime(card._progress_motion.duration()//2)
        shown=card._painted_progress()['received'];self.assertGreater(shown,10);self.assertLess(shown,80)
        card._progress_motion.setCurrentTime(card._progress_motion.duration())
        self.assertAlmostEqual(card._painted_progress()['received'],80)
        card.update_delivery(dict(state,cloud_transfer=dict(phase='verifying',received=100,total=100)))
        card._progress_motion.setCurrentTime(card._progress_motion.duration())
        self.assertAlmostEqual(card._painted_progress()['received'],100)
        self.assertEqual(card._phase,'syncing');self.assertFalse(card._ring_clock.isActive())
        card.update_delivery(dict(state,phase='synced',backed_up=True))
        self.assertEqual(card._phase,'success');self.assertEqual(card._progress_motion.state(),card._progress_motion.Stopped)

    def test_settled_native_mask_preserves_all_antialiased_pixels_at_display_scales(self):
        card=self.card;card.show_result(None,uploading())
        for compact in (False,True):
            if compact:card._expire()
            for scale in (1.,1.25,1.5,2.):
                with self.subTest(compact=compact,scale=scale):
                    images=[]
                    for ignore_mask in (False,True):
                        image=QImage(round(card.width()*scale),round(card.height()*scale),QImage.Format_ARGB32_Premultiplied)
                        image.setDevicePixelRatio(scale);image.fill(Qt.transparent)
                        painter=QPainter(image);flags=QWidget.DrawChildren
                        if ignore_mask:flags|=QWidget.IgnoreMask
                        origin=QPoint() if ignore_mask else card.mask().boundingRect().topLeft()
                        card.render(painter,origin,QRegion(card.rect()),flags);painter.end();images.append(image)
                    self.assertEqual(images[0],images[1])
                    self.assertTrue(any(0<images[0].pixelColor(x,y).alpha()<255
                        for y in range(images[0].height()) for x in range(images[0].width())))

    def test_backup_without_online_targets_finishes_and_late_state_cannot_resurrect_a_dismissed_copy(self):
        card=self.card;state=uploading();card.show_result(None,state)
        card.update_delivery(dict(state,phase='synced',backed_up=True,deliveries={'cloud':dict(success=0,total=0)}))
        self.assertEqual(card._phase,'success')
        card.dismiss();flush();card.schedule_result(True,state);QTest.qWait(180);flush()
        self.assertFalse(card.isVisible());self.assertFalse(card.timer.isActive())

    def test_disabled_manager_never_constructs_a_notice_for_either_result(self):
        manager=SimpleNamespace(_quitting=False,_clipboard_runtime_enabled=lambda:True,settings=SimpleNamespace(clipboard_success_notice=False))
        for ok in (True,False):ShelfManager._clipboard_copy_result(manager,ok,{'id':'copy'})
        self.assertFalse(hasattr(manager,'clipboard_notice'))

    def test_toggle_off_suppresses_pending_success_failure_and_all_display_clocks(self):
        card=self.card;settings=card.manager.settings
        for ok in (True,False):
            settings.clipboard_success_notice=True;card.schedule_result(ok,dict(id='delayed'))
            settings.clipboard_success_notice=False;QTest.qWait(180);flush()
            self.assertFalse(card.isVisible());card.show_result(ok,{});self.assertFalse(card.isVisible())
        settings.clipboard_success_notice=True;card.show_result(None,uploading());card._expire()
        settings.clipboard_success_notice=False;card.update_delivery(uploading())
        self.assertFalse(card.isVisible());self.assertIsNone(card._pending_result)
        for clock in (card.timer,card._result_timer,card._ring_clock):self.assertFalse(clock.isActive())

    def test_capsule_click_toggles_without_dismissing_or_activating_the_editing_window(self):
        editor=QLineEdit();editor.setText('保留正在编辑的内容');editor.show();editor.activateWindow();editor.setFocus();flush()
        try:
            card=self.card;card.show_result(None,uploading());flush();bounds=card.geometry()
            self.assertTrue(card.windowFlags()&Qt.WindowDoesNotAcceptFocus)
            self.assertTrue(card.testAttribute(Qt.WA_ShowWithoutActivating));self.assertEqual(card.focusPolicy(),Qt.NoFocus)
            QTest.mouseClick(card,Qt.LeftButton,pos=card.capsule_rect().center().toPoint());flush()
            self.assertTrue(card.isVisible());self.assertTrue(card._compact);self.assertFalse(card._hiding)
            self.assertEqual(card.capsule_rect().width(),COMPACT_SIDE_PX);self.assertFalse(card.timer.isActive())
            self.assertIn('点击展开',card.accessibleDescription())
            QTest.mouseClick(card,Qt.LeftButton,pos=card.capsule_rect().center().toPoint());flush()
            self.assertTrue(card.isVisible());self.assertFalse(card._compact);self.assertFalse(card._hiding)
            self.assertGreater(card.capsule_rect().width(),COMPACT_SIDE_PX);self.assertTrue(card.timer.isActive())
            self.assertIn('点击收起',card.accessibleDescription());self.assertEqual(card.geometry(),bounds)
            self.assertEqual(editor.text(),'保留正在编辑的内容')
        finally:editor.hide();editor.deleteLater();flush()

    def test_both_themes_keep_one_plain_line_and_safe_screen_placement_without_file_names(self):
        card=self.card;area=QRect(-300,200,300,400);screen=SimpleNamespace(availableGeometry=lambda:area)
        for theme in ('dark','light'):
            card.manager.settings.theme=theme
            with patch('intake_notice.QGuiApplication.screenAt',return_value=screen):card.show_result(None,uploading(theme))
            self.assertTrue(area.contains(card.geometry()));self.assertEqual(area.bottom()-card.geometry().bottom(),NOTICE_MARGIN_PX)
            self.assertLessEqual(abs(card.geometry().center().x()-area.center().x()),1)
            self.assertLessEqual(card.width(),244);self.assertEqual(card.capsule_rect().height(),44)
            self.assertNotIn('private-name',card.accessibleName()+card.accessibleDescription())
            self.assertFalse(card.findChildren(QLabel))
            self.assertFalse(card.mask().contains(QPoint(0,0)))

    def test_limit_failure_and_superseded_states_explain_outcomes_without_inventing_success(self):
        card=self.card
        for phase,ok,wanted in (('size_limit',False,'limited'),('error',False,'error'),('superseded',False,'superseded')):
            card.show_result(ok,dict(id=phase,phase=phase,value=('files',['private-name.bin'])))
            self.assertEqual(card._phase,wanted);self.assertNotIn('private-name',card.accessibleName()+card.accessibleDescription())
            self.assertNotIn('复制已同步',card.accessibleName());self.assertFalse(card._ring_clock.isActive())
            if phase=='size_limit':self.assertIn('20 MB',card.accessibleDescription());self.assertNotIn('失败',card.accessibleName())
        card.timer.start(10);QTest.qWait(40);flush();self.assertFalse(card.isVisible())

    def test_interruptible_shape_animation_never_resizes_the_native_host_or_accepts_stale_results(self):
        self.reduced.stop();self.reduced=patch('native_ui_common.reduced_motion',return_value=False);self.reduced.start()
        card=self.card;card.show_result(None,uploading());native=card.winId();bounds=card.geometry()
        widths=[]
        for at in (0,80,160,260,420):
            card._morph.setCurrentTime(at);card._fade.setCurrentTime(min(at,160));widths.append(card.capsule_rect().width())
            card.grab();self.assertEqual(card.geometry(),bounds);self.assertEqual(card.winId(),native)
        self.assertGreater(max(widths),min(widths)+100)
        card._expire();card._morph.setCurrentTime(150);middle=card.capsule_rect().width()
        card.show_result(None,uploading('new-copy'))
        self.assertAlmostEqual(card.capsule_rect().width(),middle,places=3)
        card.update_delivery(dict(id='current-copy',phase='synced',backed_up=True))
        self.assertEqual(card._current_result[1]['id'],'new-copy');self.assertEqual(card._phase,'syncing')
        card.supersede();self.assertFalse(card._morph._frames.isActive());self.assertFalse(card._fade._frames.isActive())

    def test_contracting_surface_erases_previous_pixels_from_the_translucent_backing_store(self):
        card=self.card;card.show_result(None,uploading());card._expire();card.clearMask()
        image=QImage(card.size(),QImage.Format_ARGB32_Premultiplied);image.fill(QColor('magenta'))
        painter=QPainter(image);card.render(painter,QPoint(),QRegion(),QWidget.DrawChildren);painter.end()
        self.assertEqual(image.pixelColor(0,0).alpha(),0)
        self.assertEqual(image.pixelColor(image.width()//2,image.height()//2).alpha(),255)


if __name__=='__main__':unittest.main(verbosity=2)
