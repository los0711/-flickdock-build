"""Actual notification pixels and sender intent; no native clipboard/network/input."""
import tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import Qt,QVariantAnimation,QTimer
from PySide6.QtGui import QColor,QImage,QPixmap
from test_html_native_workspace import APP,flush,until
from intake_notice import ClipboardNotice


class ClipboardNoticeTests(unittest.TestCase):
    def setUp(self):
        self.manager=SimpleNamespace(settings=SimpleNamespace(theme='light',clipboard_success_notice=True),_quitting=False,_clipboard_runtime_enabled=lambda:True)
        self.notice=ClipboardNotice(self.manager)
        self.motion=patch('native_ui_common.reduced_motion',return_value=False);self.motion.start()
    def tearDown(self):
        self.notice.supersede();self.notice.deleteLater();flush();self.motion.stop()
    def show(self,text='latest',ok=True):
        self.notice.show_result(ok,dict(id=text,value=('text',{'text':text}),point=(10,10)))
        self.notice.animation.pause();return self.notice.motion.active[-1]
    def finish(self):self.notice.animation.setCurrentTime(200);flush()
    def pixel(self,x,y):
        pixmap=self.notice.grab();ratio=pixmap.devicePixelRatioF()
        return pixmap.toImage().pixelColor(round(x*ratio),round(y*ratio))

    def test_enter_really_fades_and_rises_only_six_pixels_with_fixed_native_geometry(self):
        entry=self.show();geometry=self.notice.geometry();native=self.notice.winId()
        self.assertEqual(self.notice.width(),296);self.assertEqual(self.notice.height(),106)
        self.assertEqual(self.pixel(148,30).alpha(),0)
        self.notice.animation.setCurrentTime(30)
        self.assertGreater(self.pixel(148,30).alpha(),0);self.assertLess(self.pixel(148,30).alpha(),255)
        self.assertGreater(entry['y'],0);self.assertLess(entry['y'],6)
        first_top=next(y for y in range(15) if self.pixel(148,y).alpha()>0)
        self.finish();self.assertEqual(entry['y'],0);self.assertEqual(self.pixel(148,30).alpha(),255)
        self.assertGreater(first_top,0);self.assertGreater(self.pixel(148,0).alpha(),0)
        self.assertEqual(self.notice.geometry(),geometry);self.assertEqual(self.notice.winId(),native)
        self.assertIsNone(self.notice.graphicsEffect());self.assertTrue(self.notice.windowFlags()&Qt.WindowTransparentForInput)
        self.assertTrue(self.notice.windowFlags()&Qt.WindowDoesNotAcceptFocus);self.assertTrue(self.notice.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_exit_really_fades_at_exact_current_position_without_slide_or_scale(self):
        self.show();self.finish();geometry=self.notice.geometry();self.notice.dismiss();entry=self.notice.motion.ghosts[-1]
        y=entry['y'];self.notice.animation.pause();self.notice.animation.setCurrentTime(75)
        self.assertEqual(entry['y'],y);self.assertGreater(self.pixel(148,30).alpha(),0);self.assertLess(self.pixel(148,30).alpha(),255)
        self.assertEqual(self.notice.geometry(),geometry);self.finish();self.assertFalse(self.notice.isVisible())

    def test_successes_overlap_sixteen_pixels_latest_in_front_and_compact_smoothly(self):
        for value in ('first','second','latest'):self.show(value);self.finish()
        active=self.notice.motion.active;self.assertEqual([entry['y'] for entry in active],[0,16,32])
        self.assertEqual([entry['card'].preview.text() for entry in active],['first','second','latest'])
        for entry,color in zip(active,('red','green','blue')):
            pixmap=QPixmap(296,68);pixmap.fill(QColor(color));entry['cache']=pixmap
        self.assertEqual(self.pixel(148,5).name(),'#ff0000');self.assertEqual(self.pixel(148,20).name(),'#008000');self.assertEqual(self.pixel(148,40).name(),'#0000ff')
        old=active[0];old['deadline']=time.monotonic()-1;self.notice._expire();self.notice.animation.pause()
        self.assertEqual(len(self.notice.motion.active),2);self.assertEqual(self.notice.motion.ghosts[0]['y'],0)
        second=self.notice.motion.active[0];self.assertEqual(second['y'],16)
        self.notice.animation.setCurrentTime(60);self.assertGreater(second['y'],0);self.assertLess(second['y'],16)
        self.assertEqual(self.pixel(148,65).name(),'#0000ff','Outgoing artwork must not obscure the newest card')
        self.finish();self.assertEqual([entry['y'] for entry in self.notice.motion.active],[0,16]);self.assertFalse(self.notice.motion.ghosts)

    def test_hundred_rapid_commits_use_one_native_window_four_cards_one_clock_and_two_timers(self):
        before=set(APP.topLevelWidgets())
        for index in range(100):self.show(str(index))
        self.assertLessEqual(len(self.notice.motion.active),3);self.assertLessEqual(len(self.notice.motion.ghosts),1)
        self.assertLessEqual(len(self.notice._cards),4);self.assertEqual(len(self.notice.findChildren(QVariantAnimation)),1)
        self.assertEqual(len(self.notice.findChildren(QTimer)),2);self.assertEqual(self.notice.preview.text(),'99')
        self.assertFalse([widget for widget in set(APP.topLevelWidgets())-before if widget is not self.notice])
        self.finish();self.assertFalse(self.notice.motion.ghosts);self.assertEqual([e['card'].preview.text() for e in self.notice.motion.active],['97','98','99'])

    def test_supersede_cancels_pending_result_without_dismissing_completed_success(self):
        self.show('committed');self.finish()
        self.notice.schedule_result(False,dict(value=('text',{'text':'stale failure'})))
        self.notice.supersede(clear_visible=False)
        self.assertIsNone(self.notice._pending_result);self.assertFalse(self.notice._result_timer.isActive())
        self.assertTrue(self.notice.isVisible());self.assertEqual(self.notice.preview.text(),'committed')
        self.notice.schedule_result(True,dict(value=('text',{'text':'new commit'})))
        self.notice._result_timer.timeout.emit();self.notice._result_timer.stop();self.finish()
        self.assertEqual([e['ok'] for e in self.notice.motion.active],[True,True]);self.assertEqual(self.notice.preview.text(),'new commit')

    def test_thumbnail_queue_is_bounded_and_canceled_completion_never_overwrites_latest(self):
        start=Mock();image=QImage(8,8,QImage.Format_RGB32);image.fill(QColor('blue'))
        with patch('intake_notice.QThreadPool.globalInstance',return_value=SimpleNamespace(start=start)):
            for index in range(50):
                self.notice.show_result(True,dict(value=('image',{'image':image}),id=str(index),point=(10,10)))
            self.assertEqual(len(self.notice.jobs),1);self.assertEqual(start.call_count,1);old=self.notice._job;self.assertTrue(old.cancel.is_set())
            old.signals.done.emit((image,None),None);flush()
            self.assertEqual(len(self.notice.jobs),1);self.assertEqual(start.call_count,2);latest=self.notice._job;self.assertFalse(latest.cancel.is_set())
            latest.signals.done.emit((image,None),None);flush()
            self.assertFalse(self.notice.jobs);self.assertEqual(self.notice.symbol.pixmap().toImage().pixelColor(4,4).name(),'#0000ff')

    def test_success_switch_immediately_removes_success_artwork_and_keeps_failures(self):
        self.show('success');self.finish();self.show('failure',False);self.finish();self.show('last success');self.finish()
        self.notice.schedule_result(True,dict(value=('text',{'text':'pending'})))
        self.manager.settings.clipboard_success_notice=False;self.notice.disable_success();flush()
        self.assertEqual(len(self.notice.motion.active),1);self.assertFalse(self.notice.motion.active[0]['ok'])
        self.assertFalse(self.notice.motion.ghosts);self.assertIsNone(self.notice._pending_result);self.assertFalse(self.notice._result_timer.isActive())
        self.assertTrue(self.notice.isVisible());self.assertTrue(self.manager._clipboard_runtime_enabled())
        self.notice.disable_success();self.assertTrue(self.notice.isVisible())

    def test_old_expiry_or_success_switch_does_not_cancel_a_new_pending_failure(self):
        for disabling in (False,True):
            self.show('old success');self.finish()
            self.notice.schedule_result(False,dict(value=('text',{'text':'new failure'})))
            if disabling:self.notice.disable_success()
            else:self.notice.dismiss();self.finish()
            self.assertFalse(self.notice.isVisible());self.assertIsNotNone(self.notice._pending_result)
            self.assertTrue(self.notice._result_timer.isActive())
            self.notice._result_timer.timeout.emit();self.notice._result_timer.stop();self.finish()
            self.assertEqual(self.notice.message.text(),'复制同步失败');self.assertEqual(self.notice.preview.text(),'new failure')
            self.notice.supersede()

    def test_hide_cancels_pending_thumbnail_and_all_clocks_then_ignores_late_result(self):
        with patch('intake_notice.QThreadPool.globalInstance',return_value=SimpleNamespace(start=Mock())):
            self.notice.show_result(True,dict(value=('files',['/not-mounted/original.png']),point=(10,10)))
            job=self.notice._job;self.notice.hide();flush()
            self.assertTrue(job.cancel.is_set());self.assertFalse(self.notice.timer.isActive());self.assertFalse(self.notice._result_timer.isActive())
            self.assertFalse(self.notice.animation.state()==self.notice.animation.State.Running);self.assertFalse(self.notice.motion.active)
            job.signals.done.emit((QImage(),None),None);flush();self.assertFalse(self.notice.jobs);self.assertFalse(self.notice.isVisible())

    def test_no_gui_file_stat_reduced_motion_and_original_bytes_stay_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'original.txt';path.write_bytes(b'original bytes')
            with patch('native_ui_common.reduced_motion',return_value=True),patch.object(Path,'stat',side_effect=AssertionError('GUI stat')),patch('intake_notice.QThreadPool.globalInstance',return_value=SimpleNamespace(start=Mock())):
                self.notice.show_result(True,dict(value=('files',[str(path)]),point=(10,10)))
                self.assertEqual(self.notice.motion.active[-1]['alpha'],1);self.assertEqual(self.notice.motion.active[-1]['y'],0)
                self.assertFalse(self.notice.animation.state()==self.notice.animation.State.Running);self.assertIn(path.name,self.notice.preview.text())
                job=self.notice._job;job.signals.done.emit((QImage(),None),None);flush();self.notice.dismiss();self.assertFalse(self.notice.isVisible())
            self.assertEqual(path.read_bytes(),b'original bytes')

    def test_real_manager_start_preserves_commits_and_result_still_waits_for_success(self):
        from flickdock import ShelfManager
        self.manager.clipboard_notice=self.notice
        ShelfManager._clipboard_copy_started(self.manager)
        self.assertFalse(self.notice.isVisible());self.assertIsNone(self.notice._pending_result)
        ShelfManager._clipboard_copy_result(self.manager,True,dict(value=('text',{'text':'committed'})))
        self.assertFalse(self.notice.isVisible());self.assertIsNotNone(self.notice._pending_result)
        self.notice._result_timer.timeout.emit();self.notice._result_timer.stop();self.finish()
        ShelfManager._clipboard_copy_started(self.manager)
        self.assertTrue(self.notice.isVisible());self.assertEqual(self.notice.preview.text(),'committed')
        self.manager.settings.clipboard_success_notice=False
        ShelfManager._clipboard_copy_result(self.manager,True,dict(value=('text',{'text':'silent commit'})))
        self.assertIsNone(self.notice._pending_result)
        self.manager._quitting=True
        ShelfManager._clipboard_copy_result(self.manager,False,dict(value=('text',{'text':'late failure'})))
        self.assertIsNone(self.notice._pending_result)

    def test_nonactivating_code_never_requests_focus_or_window_activation(self):
        # Offscreen itself ignores WindowDoesNotAcceptFocus even on a plain
        # QWidget. Test the real code contract, not unsupported native policy.
        with patch.object(self.notice,'activateWindow',side_effect=AssertionError('Activation requested')),patch.object(self.notice,'setFocus',side_effect=AssertionError('Focus requested')):
            self.show('background success');self.finish()
            self.assertTrue(self.notice.windowFlags()&Qt.WindowDoesNotAcceptFocus)
            self.assertTrue(self.notice.testAttribute(Qt.WA_ShowWithoutActivating))


if __name__=='__main__':unittest.main()
