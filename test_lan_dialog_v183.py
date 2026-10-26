"""Offscreen LAN secondary-window behavior with disposable paths and no sockets."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QObject,QMimeData,QPointF,Qt,QUrl,Signal
from PySide6.QtGui import QDragEnterEvent,QDropEvent
from PySide6.QtWidgets import QApplication

from app_settings import SettingsStore
from lan_panel import LanDialog

APP=QApplication.instance() or QApplication([])


class FakeLan(QObject):
    peers_changed=Signal(list);offer_received=Signal(dict);progress=Signal(dict)
    completed=Signal(dict);status_changed=Signal(str)
    def __init__(self):
        super().__init__();self.sent=[]
    def peers(self):return [{'id':'peer-1','name':'测试设备','host':'127.0.0.1'}]
    def send(self,peer,paths):self.sent.append((peer,paths));return 'send-1'
    def accept(self,identifier,folder):return True
    def cancel(self,identifier):pass


class LanDialogTests(unittest.TestCase):
    def setUp(self):
        self.profile=tempfile.TemporaryDirectory(prefix='flickdock-lan-dialog-')
        self.root=Path(self.profile.name)
        self.settings=SettingsStore(self.root/'profile')
        self.service=FakeLan()
        self.dialog=LanDialog(self.service,self.settings)
        self.dialog.show();APP.processEvents()

    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();APP.processEvents()
        self.profile.cleanup()

    def test_local_file_drop_queues_original_and_sends_to_selected_device(self):
        source=self.root/'原文件.png';source.write_bytes(b'\x89PNG original')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))])
        enter=QDragEnterEvent(self.dialog.panel.drop_zone.rect().center(),Qt.DropAction.CopyAction,mime,
            Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        self.dialog.panel.drop_zone.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        drop=QDropEvent(QPointF(12,12),Qt.DropAction.CopyAction,mime,
            Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        self.dialog.panel.drop_zone.dropEvent(drop)
        self.assertEqual(self.dialog.panel._queued_paths,[str(source)])
        self.dialog.panel.send_button.click()
        self.assertEqual(self.service.sent,[('peer-1',[str(source)])])
        self.assertEqual(self.dialog.panel._queued_paths,[])

    def test_destination_and_recent_transfer_survive_window_reopen(self):
        destination=self.root/'chosen';destination.mkdir()
        self.settings.set_lan_receive_dir(destination)
        self.dialog.panel._progress({'id':'transfer-1','direction':'send','peer':'测试设备',
            'name':'report.pdf','state':'complete'})
        self.dialog.close();self.dialog.deleteLater();APP.processEvents()
        self.dialog=LanDialog(self.service,SettingsStore(self.root/'profile'))
        self.assertEqual(self.dialog.panel.receive_path.text(),str(destination))
        self.assertIn('report.pdf',self.dialog.panel.history_label.text())

    def test_remote_url_is_not_treated_as_a_local_file(self):
        mime=QMimeData();mime.setUrls([QUrl('https://example.invalid/image.png')])
        enter=QDragEnterEvent(self.dialog.panel.drop_zone.rect().center(),Qt.DropAction.CopyAction,mime,
            Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        self.dialog.panel.drop_zone.dragEnterEvent(enter)
        self.assertFalse(enter.isAccepted())

if __name__=='__main__':unittest.main()
