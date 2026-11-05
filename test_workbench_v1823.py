"""Approved transfer layout, original file ownership and settings navigation."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint,QRect,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QLabel
from app_settings import SettingsStore,SettingsDialog
from lan_panel import LanDialog,_CodeExpiryBar,_CopyCodeButton
from test_lan_dialog_v183 import FakeLan
from wan_transfer import WanTransferService

APP=QApplication.instance() or QApplication([])


class WorkbenchTests(unittest.TestCase):
    def test_same_account_has_blue_badge_and_nearby_recommendation_is_green(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);wan=WanTransferService(base/'wan',enabled=False)
            window=LanDialog(wan,SettingsStore(base/'profile'));window.apply_theme('light');window.show();QTest.qWait(30)
            panel=window.panel
            try:
                panel._peers_changed([dict(id='peer',name='MacBook Air',platform='macos',online=False,account_device=True),dict(id='online',name='Windows',platform='windows',online=True,account_device=True)])
                self.assertEqual(panel.devices.currentText(),'MacBook Air')
                self.assertTrue(panel.devices.currentData(Qt.UserRole+3))
                self.assertIn('同账号',panel.devices.currentData(Qt.AccessibleTextRole))
                panel.devices.showPopup();QTest.qWait(20)
                image=panel.devices.view().grab().toImage();ratio=image.devicePixelRatio()
                blue=sum(image.pixelColor(x,y).name()=='#0062cc' for x in range(round(49*ratio),round(94*ratio)) for y in range(round(6*ratio),round(29*ratio)))
                self.assertGreater(blue,10)
                second=panel.devices.view().visualRect(panel.devices.model().index(1,0))
                pixels=[image.pixelColor(x,y) for x in range(round(49*ratio),round(180*ratio)) for y in range(round((second.top()+29)*ratio),round((second.top()+47)*ratio))]
                green=sum(pixel.green()>pixel.red()+12 and pixel.green()>pixel.blue()+8 for pixel in pixels)
                self.assertGreater(green,5)
                first=panel.devices.view().visualRect(panel.devices.model().index(0,0))
                with patch.object(wan,'remove_peer') as remove:
                    QTest.mouseClick(panel.devices.view().viewport(),Qt.LeftButton,pos=QPoint(first.right()-26,first.center().y()))
                    remove.assert_called_once_with('peer')
                panel.devices.hidePopup()
                with patch.object(wan,'nearby_peer',return_value={'id':'lan-peer'}):panel._target_changed()
                QTest.qWait(20);self.assertTrue(panel.nearby_hint.isVisible())
                label=panel.nearby_hint.findChild(QLabel)
                self.assertEqual(label.palette().color(label.foregroundRole()).name(),'#187a43')
                image=panel.nearby_hint.grab().toImage();ratio=image.devicePixelRatio()
                pixel=image.pixelColor(round(100*ratio),round((panel.nearby_hint.height()-3)*ratio))
                self.assertEqual(pixel.name(),'#e3f5ea')
            finally:window.hide();window.deleteLater();wan.close();wan.deleteLater();QTest.qWait(20)

    def test_code_progress_uses_server_deadline_and_requests_one_refresh_without_countdown_text(self):
        with tempfile.TemporaryDirectory() as directory:
            wan=WanTransferService(Path(directory)/'wan',enabled=False);button=_CopyCodeButton();bar=_CodeExpiryBar(wan,button)
            try:
                wan._connected=True;wan.code='AB23';wan._code_duration=20;wan._code_refresh_at=1010;wan._code_expires_at=1100
                with patch('wan_transfer.time.monotonic',return_value=1000):bar._tick()
                self.assertEqual(bar.value(),500);self.assertFalse(bar.isTextVisible());self.assertEqual(button.text(),'AB23')
                with patch('wan_transfer.time.monotonic',return_value=1010),patch.object(wan,'_send_signal',return_value=True) as send:
                    for _ in range(20):bar._tick()
                    send.assert_called_once_with({'type':'refresh'});self.assertEqual(bar.value(),0)
                wan.code='EF67';wan._code_refresh_at=1031;wan._refresh_sent_at=0
                with patch('wan_transfer.time.monotonic',return_value=1011):bar._tick()
                self.assertEqual(bar.value(),1000);self.assertEqual(button.text(),'EF67')
                bar.show();QTest.qWait(5);bar.hide();self.assertFalse(bar._timer.isActive())
            finally:bar.deleteLater();button.deleteLater();wan.close();wan.deleteLater();QTest.qWait(10)

    def test_send_button_stays_visible_with_long_queues_short_screens_and_both_modes(self):
        with tempfile.TemporaryDirectory() as directory,patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,853,480)):
            base=Path(directory);service=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            window=LanDialog(service,SettingsStore(base/'profile'));window.show();QTest.qWait(30)
            paths=[]
            for number in range(12):
                path=base/f'{number}.txt';path.write_text('original');paths.append(str(path))
            try:
                for backend in (service,wan):
                    geometry=window.geometry();panel=window.set_service(backend);QTest.qWait(30)
                    self.assertEqual(window.geometry(),geometry)
                    for count in (0,1,12,0):
                        panel.queue_paths(paths[:count],replace=True);QTest.qWait(20)
                        self.assertTrue(panel.send_button.isVisible())
                        if backend is wan:self.assertFalse(hasattr(panel,'custom_input'));self.assertIsNotNone(panel.code_progress)
                        self.assertFalse(panel.send_scroll.isAncestorOf(panel.send_button))
                        bounds=QRect(panel.send_button.mapTo(window,QPoint()),panel.send_button.size())
                        self.assertTrue(window.card.geometry().contains(bounds))
                        self.assertFalse(bounds.intersects(QRect(panel.destination_bar.mapTo(window,QPoint()),panel.destination_bar.size())))
                        scroll=panel.send_scroll.verticalScrollBar()
                        for position in (0,scroll.maximum()):
                            scroll.setValue(position);QTest.qWait(10)
                            self.assertEqual(bounds.topLeft(),panel.send_button.mapTo(window,QPoint()))
            finally:window.hide();window.deleteLater();wan.close();wan.deleteLater();QTest.qWait(20)

    def test_equal_columns_both_modes_and_completed_records_reveal_original_files(self):
        with tempfile.TemporaryDirectory() as directory,patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1920,1080)):
            base=Path(directory);store=SettingsStore(base/'profile');service=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            window=LanDialog(service,store);window.show();QTest.qWait(30)
            try:
                self.assertEqual(window.title_label.text(),'文件互传');self.assertFalse(hasattr(window,'title_icon'))
                for backend in (service,wan):
                    panel=window.set_service(backend)
                    for width in (824,980,1100,1400):
                        window.resize(width,760);QTest.qWait(20)
                        self.assertLessEqual(abs(panel.left_column.width()-panel.activity_scroll.width()),1)
                    self.assertTrue(panel.open_destination.isVisible())
                panel=window.set_service(service);path=base/'原始文件.txt';path.write_text('original bytes',encoding='utf-8')
                panel.queue_paths([str(path)]);panel.send_button.click();QTest.qWait(10)
                service.progress.emit(dict(id='send-1',direction='send',peer='peer',state='preparing'))
                self.assertEqual(panel._rows['send-1']['icon'].ref,str(path))
                service.progress.emit(dict(id='send-1',direction='send',peer='peer',state='complete'))
                service.completed.emit(dict(id='send-1',direction='send',paths=[str(path)]));QTest.qWait(10)
                self.assertEqual(panel.history_rows[0].icon.ref,str(path))
                with patch('lan_panel.reveal_file') as reveal:
                    panel.history_rows[0].reveal_button.click();reveal.assert_called_once_with(str(path))
                panel.history_rows[0].remove_button.click();self.assertEqual(path.read_text(encoding='utf-8'),'original bytes')
            finally:window.hide();window.deleteLater();wan.close();wan.deleteLater();QTest.qWait(20)

    def test_categories_keep_unsaved_values_and_account_injection_in_its_category(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SettingsStore(Path(directory)/'profile');window=SettingsDialog(store);window.show();QTest.qWait(20)
            try:
                draft=str(Path(directory)/'changed');window.folder_edit.setText(draft)
                for index,(_,sections) in enumerate(window.setting_categories):
                    window.category_list.setCurrentRow(index);QTest.qWait(10)
                    for number,(_,other) in enumerate(window.setting_categories):
                        for card in other:self.assertEqual(card.isVisible(),number==index)
                    self.assertEqual(window.folder_edit.text(),draft)
                account=QLabel('Isolated account view');window.set_account_panel(account);QTest.qWait(10)
                self.assertFalse(account.isVisible());window.select_category('账号与设备');QTest.qWait(10);self.assertTrue(account.isVisible())
                window.select_category('文件与保存');self.assertEqual(window.folder_edit.text(),draft)
                window.reject();self.assertNotEqual(store.capture_dir,Path(draft))
            finally:window.hide();window.deleteLater();QTest.qWait(20)


if __name__=='__main__':unittest.main()
