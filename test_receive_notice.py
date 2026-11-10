"""Authorized automatic reception notices follow actual transfer completion."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import QObject,Signal,QCoreApplication,QEvent,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget
from app_settings import SettingsStore
from receive_notice import FileTransferNotices

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

class Account(QObject):
    account_changed=Signal(object)
    signed_in=True
    account={'id':'qa-account'}
    device_id='qa-device'
    api=SimpleNamespace(token='qa-token')

class Service(QObject):
    offer_received=Signal(dict)
    progress=Signal(dict)
    completed=Signal(dict)
    status_changed=Signal(str)

class ReceiveNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.owner=QWidget()
        self.account=Account();self.wan=Service();self.wan.accept=Mock(return_value=True);self.wan.reject=Mock();self.wan.cancel=Mock()
        self.wan._bound_identity=(self.account.device_id,self.account.api.token)
        settings=SettingsStore(self.root/'profile');settings.update(theme='dark');settings.set_lan_receive_dir(self.root)
        self.panel=SimpleNamespace(focus_transfer=Mock(),show_offer=Mock())
        self.manager=SimpleNamespace(app=APP,shelf=self.owner,settings=settings,account=self.account,wan=self.wan,
            show_wan_dialog=Mock(),wan_dialog=SimpleNamespace(panel=self.panel))
        self.notice=FileTransferNotices(self.manager,self.wan);self.manager.receive_notice=self.notice
        self.offer=dict(id='qa-transfer',peer='另一台电脑',names=['原始文件.zip'],count=1,total=1024*1024,
            account_device=True,account_id=self.account.account['id'])
        self.motion=patch('native_ui_common.reduced_motion',return_value=True);self.motion.start()

    def tearDown(self):
        self.notice.close();self.notice.deleteLater();self.owner.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents();self.motion.stop();self.temp.cleanup()

    def progress(self,state='transferring',received=123):
        self.wan.progress.emit(dict(id=self.offer['id'],direction='receive',state=state,received=received,
            total=self.offer['total'],name=self.offer['names'][0],route='p2p'))

    def complete(self):
        path=self.root/'原始文件.zip';path.write_bytes(b'notification test file')
        value=dict(id=self.offer['id'],direction='receive',paths=[str(path)],total=self.offer['total'])
        self.wan.completed.emit(value);return path,value

    def test_manager_only_auto_accepts_and_notifies_the_authenticated_same_account(self):
        from flickdock import ShelfManager
        ShelfManager._wan_offer(self.manager,self.offer)
        self.wan.accept.assert_called_once_with(self.offer['id'],self.root.resolve())
        self.manager.show_wan_dialog.assert_not_called();self.assertTrue(self.notice.toast.isVisible())
        self.assertTrue(self.notice.toast.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertEqual(self.notice.toast.width(),320)
        foreign=dict(self.offer,id='foreign',account_id='other-account')
        ShelfManager._wan_offer(self.manager,foreign)
        self.assertNotIn('foreign',self.notice.records);self.panel.show_offer.assert_called_once_with(foreign)

    def test_auto_compact_then_click_expands_and_next_click_opens_transfer(self):
        self.notice.begin(self.offer);toast=self.notice.toast
        toast._expire();self.assertTrue(toast._compact);self.assertTrue(toast.isVisible())
        self.progress(received=400000);self.assertTrue(toast._compact)
        QTest.mouseClick(toast,Qt.LeftButton,pos=toast.capsule_rect().center().toPoint())
        self.assertFalse(toast._compact);self.assertTrue(toast.isVisible());self.manager.show_wan_dialog.assert_not_called()
        QTest.mouseClick(toast,Qt.LeftButton,pos=toast.capsule_rect().center().toPoint())
        self.manager.show_wan_dialog.assert_called_once();self.assertFalse(toast._compact)
        self.assertEqual(self.panel.focus_transfer.call_args.args[0],self.offer['id'])
        self.wan.cancel.assert_not_called();self.wan.reject.assert_not_called()

    def test_completion_requires_saved_result_and_never_replays_a_duplicate(self):
        self.notice.begin(self.offer);toast=self.notice.toast;toast._expire()
        self.progress('complete',self.offer['total']);self.assertEqual(self.notice.records[self.offer['id']]['state'],'verifying')
        self.assertEqual(toast._phase,'syncing')
        path,value=self.complete();self.assertEqual(toast._phase,'success');self.assertFalse(toast._compact)
        self.assertTrue(toast.timer.isActive());self.assertFalse(toast._ring_clock.isActive())
        toast._expire();self.assertFalse(toast.isVisible());self.wan.completed.emit(value)
        self.assertFalse(toast.isVisible());self.assertTrue(path.exists())

    def test_send_and_receive_colors_follow_real_progress_and_interruption_can_resume(self):
        from receive_notice import SEND_RING_COLOR,RECEIVE_RING_COLOR
        from PySide6.QtGui import QColor,QImage,QPainter
        from PySide6.QtCore import QRectF
        self.notice.begin(self.offer);toast=self.notice.toast;self.progress('interrupted',300000)
        self.assertEqual(toast._phase,'error');self.progress(received=800000);self.assertEqual(toast._phase,'syncing')
        for direction,color in [('receive',RECEIVE_RING_COLOR),('send',SEND_RING_COLOR)]:
            if direction=='send':
                offer=dict(self.offer,id='send',direction='send');self.notice.begin(offer)
                self.wan.progress.emit(dict(id='send',direction='send',state='transferring',received=400000,total=self.offer['total']))
            image=QImage(40,40,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
            painter=QPainter(image);toast._paint_indicator(painter,QRectF(4,4,30,30),{'red':'#ff0000'});painter.end()
            wanted=QColor(color)
            self.assertTrue(any(image.pixelColor(x,y)==wanted for y in range(40) for x in range(40)))
        self.assertEqual(toast._text,self.offer['names'][0])

    def test_account_reset_suppresses_late_progress_and_completion(self):
        self.notice.begin(self.offer);self.account.account_changed.emit(None)
        self.assertFalse(self.notice.toast.isVisible());self.assertFalse(self.notice.records)
        self.progress();self.complete();self.assertFalse(self.notice.toast.isVisible())
        self.assertFalse(self.notice.toast.timer.isActive())

    def test_terminal_result_expands_a_compact_notice_before_animated_close(self):
        from intake_notice import COMPACT_SIDE_PX
        for state in ('complete','interrupted','error','cancelled'):
            with self.subTest(state=state):
                self.notice.reset();self.notice.begin(self.offer);toast=self.notice.toast
                toast._expire();self.assertTrue(toast._compact)
                if state=='complete':self.complete()
                else:self.progress(state)
                self.assertFalse(toast._compact);self.assertTrue(toast.isVisible())
                self.assertGreater(toast.capsule_rect().width(),COMPACT_SIDE_PX)
                self.assertTrue(toast.timer.isActive())
                toast._expire();self.assertFalse(toast.isVisible())

    def test_corner_status_marks_are_filled_at_small_sizes(self):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage,QPainter
        from transfer_visuals import draw_cloud_status_badge
        for status in ('synced','local'):
            image=QImage(24,24,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
            painter=QPainter(image);draw_cloud_status_badge(painter,QRectF(2,2,20,20),status);painter.end()
            center=image.pixelColor(12,12)
            self.assertGreater(min(center.red(),center.green(),center.blue()),245)

if __name__=='__main__':unittest.main()
