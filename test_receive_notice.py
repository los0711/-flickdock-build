"""Authorized automatic reception notices follow actual transfer completion."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import QObject,Signal,QCoreApplication,QEvent,Qt
from PySide6.QtWidgets import QApplication,QWidget
from app_settings import SettingsStore
from receive_notice import SameAccountReceiveNotice

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

class Account(QObject):
    account_changed=Signal(object)
    signed_in=True
    account={'id':'qa-account'}
    device_id='qa-device'
    api=SimpleNamespace(token='qa-token')

class Service(QObject):
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
        self.notice=SameAccountReceiveNotice(self.manager,self.wan);self.manager.receive_notice=self.notice
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
        self.wan.accept.assert_called_once_with(self.offer['id'],self.root)
        self.manager.show_wan_dialog.assert_not_called();self.assertTrue(self.notice.toast.isVisible())
        self.assertTrue(self.notice.toast.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertEqual(self.notice.toast.width(),390)
        foreign=dict(self.offer,id='foreign',account_id='other-account')
        ShelfManager._wan_offer(self.manager,foreign)
        self.assertNotIn('foreign',self.notice.records);self.panel.show_offer.assert_called_once_with(foreign)

    def test_hide_preserves_transfer_and_completion_requires_saved_result_signal(self):
        self.notice.begin(self.offer);toast=self.notice.toast
        toast.dismiss_button.click();self.assertFalse(toast.isVisible());self.wan.cancel.assert_not_called();self.wan.reject.assert_not_called()
        self.progress(received=400000);self.assertFalse(toast.isVisible())
        self.progress('complete',self.offer['total']);self.assertEqual(self.notice.records[self.offer['id']]['state'],'verifying')
        self.assertFalse(toast.isVisible())
        path,value=self.complete();self.assertTrue(toast.isVisible());self.assertEqual(toast.heading.text(),'接收完成')
        self.assertEqual(toast.action_button.text(),'打开文件夹');self.assertTrue(toast.timer.isActive())
        self.notice.dismiss();self.wan.completed.emit(value);self.assertFalse(toast.isVisible());self.assertTrue(path.exists())

    def test_view_transfer_focuses_the_task_even_before_its_first_byte(self):
        self.notice.begin(self.offer);self.notice.toast.action_button.click()
        self.manager.show_wan_dialog.assert_called_once();identifier,snapshot=self.panel.focus_transfer.call_args.args
        self.assertEqual(identifier,self.offer['id']);self.assertEqual(snapshot['state'],'connecting')
        self.assertEqual(snapshot['total'],self.offer['total'])

    def test_interruption_can_resume_and_final_folder_action_uses_actual_saved_file(self):
        self.notice.begin(self.offer);self.progress('interrupted',300000)
        self.assertEqual(self.notice.toast.heading.text(),'接收已中断')
        self.progress(received=800000);self.assertIn('正在下载',self.notice.toast.heading.text())
        path,_=self.complete()
        with patch('receive_notice.reveal_file') as reveal:
            self.notice.toast.action_button.click();reveal.assert_called_once_with(str(path))
        self.assertEqual(path.read_bytes(),b'notification test file')

    def test_account_reset_suppresses_late_progress_and_completion(self):
        self.notice.begin(self.offer);self.account.account_changed.emit(None)
        self.assertFalse(self.notice.toast.isVisible());self.assertFalse(self.notice.records)
        self.progress();self.complete();self.assertFalse(self.notice.toast.isVisible())
        self.assertFalse(self.notice.toast.timer.isActive())

if __name__=='__main__':unittest.main()
