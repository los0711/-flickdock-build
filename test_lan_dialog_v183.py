"""Offscreen LAN secondary-window behavior with disposable paths and no sockets."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QByteArray,QEvent,QObject,QMimeData,QPointF,Qt,QUrl,Signal
from PySide6.QtGui import QDragEnterEvent,QDropEvent,QMouseEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication,QPushButton,QScrollArea
from unittest.mock import patch

from app_settings import SettingsStore
from flickdock_icons import glyph_svg
from lan_panel import LanDialog,_motion_allowed

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

    def test_transfer_mode_button_keeps_native_dialog_visible_and_emits_navigation(self):
        navigated=[];self.dialog.mode_selected.connect(lambda mode:navigated.append(mode))
        self.assertEqual(self.dialog.mode_buttons['wan'].text(),'远程传输')
        self.dialog.mode_buttons['wan'].click();APP.processEvents()
        self.assertEqual(navigated,['wan']);self.assertTrue(self.dialog.isVisible())

    def test_file_drop_anywhere_in_transfer_dialog_queues_once_and_rejects_web_urls(self):
        for index,target in enumerate((self.dialog,self.dialog.title_bar,self.dialog.panel,
                                       self.dialog.panel.devices,self.dialog.panel.receive_path)):
            source=self.root/f'whole-window-{index}.txt';source.write_text(str(index))
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))])
            enter=QDragEnterEvent(target.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            APP.sendEvent(target,enter);self.assertTrue(enter.isAccepted())
            self.assertTrue(self.dialog.panel.drop_zone._active)
            drop=QDropEvent(QPointF(12,12),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            APP.sendEvent(target,drop);self.assertTrue(drop.isAccepted())
            self.assertEqual(self.dialog.panel._queued_paths.count(str(source)),1)
            self.assertFalse(self.dialog.panel.drop_zone._active)
        mime=QMimeData();mime.setUrls([QUrl('https://example.invalid/file.zip')])
        enter=QDragEnterEvent(self.dialog.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
        APP.sendEvent(self.dialog,enter);self.assertFalse(enter.isAccepted())
        self.assertEqual(len(self.dialog.panel._queued_paths),5)

    def test_wan_recent_devices_have_no_placeholder_and_code_has_title(self):
        from wan_transfer import WanTransferService
        service=WanTransferService(self.root/'wan',enabled=False)
        dialog=LanDialog(service,self.settings);dialog.show();APP.processEvents()
        try:
            self.assertEqual(dialog.mode_buttons['lan'].text(),'局域网传输')
            self.assertEqual(dialog.panel.peer_code_label.text(),'对方接收码')
            self.assertTrue(dialog.panel.peer_code_label.isVisible())
            self.assertEqual(dialog.panel.devices.count(),0)
            peers=[dict(id='a'*64,name='最近设备',online=True,platform='macos'),dict(id='b'*64,name='第二设备',online=False,platform='windows')]
            dialog.panel._peers_changed(peers)
            self.assertEqual(dialog.panel.devices.count(),2)
            self.assertEqual(dialog.panel.devices.currentData(),'a'*64)
            self.assertTrue(dialog.panel.devices.itemData(0,Qt.ItemDataRole.UserRole+2))
            self.assertFalse(dialog.panel.devices.itemData(1,Qt.ItemDataRole.UserRole+2))
            dialog.panel.devices.setCurrentIndex(1);dialog.panel._peers_changed(peers)
            self.assertEqual(dialog.panel.devices.currentData(),'b'*64)
            dialog.panel._peers_changed([]);self.assertEqual(dialog.panel.devices.currentIndex(),-1)
        finally:dialog.close();dialog.deleteLater();service.close();service.deleteLater();APP.processEvents()

    def test_codes_copy_without_disappearing_and_send_clears_only_valid_request(self):
        panel=self.dialog.panel
        panel.send_code_value.setText('1 2 3 4 5 6');panel.send_code_value.click()
        self.assertEqual(APP.clipboard().text(),'123456')
        self.assertEqual(panel.send_code_value.text(),'1 2 3 4 5 6')
        panel._wan=True;panel.peer_code_input.setText('ABCD2345')
        file=self.root/'queued.dat';file.write_bytes(b'content');panel.queue_paths([str(file)])
        panel.send_button.click()
        self.assertEqual(panel.peer_code_input.text(),'')
        self.assertEqual(self.service.sent[-1],('ABCD2345',[str(file)]))

    def test_remote_offer_displays_sender_device_code_and_copies_it(self):
        from PySide6.QtWidgets import QLabel
        panel=self.dialog.panel;panel._wan=True
        panel.show_offer(dict(id='device-code',peer='远程电脑',names=['file.txt'],count=1,total=12,code='',device_code='ABCD2345'))
        APP.processEvents();card=panel._rows['device-code']['extra']
        caption=card.findChild(QLabel,'lanOfferCodeCaption')
        self.assertEqual(caption.text(),'设备码');self.assertTrue(caption.isVisible())
        value=card.findChild(QPushButton,'lanOfferCodeValue');self.assertEqual(value.text(),'ABCD2345')
        value.click();self.assertEqual(APP.clipboard().text(),'ABCD2345')

    def test_remote_eta_and_turn_usage_survive_terminal_history(self):
        panel=self.dialog.panel;panel._wan=True
        event=dict(id='turn-qa',direction='send',peer='Mac',state='transferring',total=20_000_000,received=2_000_000,route='relay',relay_bytes=2_050_000)
        with patch('lan_panel.time.monotonic',return_value=10):panel._progress(event)
        event.update(received=4_000_000,relay_bytes=4_100_000)
        with patch('lan_panel.time.monotonic',return_value=11):panel._progress(event)
        detail=panel._rows['turn-qa']['detail'].text()
        self.assertIn('剩余约 8 秒',detail);self.assertIn('TURN:4.1MB',detail)
        event.update(state='cancelled',relay_bytes=4_200_000);panel._progress(event)
        self.assertEqual(panel._history[0]['relay_bytes'],4_200_000)
        self.assertIn('TURN:4.2MB',panel.history_rows[0].title.accessibleName())

    def test_destination_and_recent_transfer_survive_window_reopen(self):
        destination=self.root/'chosen';destination.mkdir()
        self.settings.set_lan_receive_dir(destination)
        self.dialog.panel._progress({'id':'transfer-1','direction':'send','peer':'测试设备',
            'name':'report.pdf','state':'complete'})
        self.dialog.close();self.dialog.deleteLater();APP.processEvents()
        self.dialog=LanDialog(self.service,SettingsStore(self.root/'profile'))
        self.assertEqual(Path(self.dialog.panel.receive_path.text()).resolve(),destination.resolve())
        self.assertEqual(len(self.dialog.panel.history_rows),1)
        record=self.dialog.panel.history_rows[0]
        self.assertIn('report.pdf',record.detail.accessibleName())
        self.assertEqual(record.state.text(),'已完成')
        self.assertIn('测试设备',record.title.accessibleName())

    def test_recent_records_keep_status_time_and_long_names_in_separate_rows(self):
        panel=self.dialog.panel
        panel._history=[
            {'when':'10-27 10:25','name':'很长的文档名称-'*12+'.pdf','peer':'Roger · DESKTOP-01',
             'state':'完成','direction':'send','count':3},
            {'when':'10-27 10:24','name':'photo.png','peer':'Mac mini',
             'state':'已取消','direction':'send'},
            {'when':'10-27 08:26','name':'movie.mp4','peer':'工作电脑',
             'state':'失败','direction':'receive'}]
        panel._render_history();APP.processEvents();APP.processEvents()
        self.assertEqual(len(panel.history_rows),3)
        self.assertEqual(len(panel.history_separators),2)
        self.assertEqual(panel.history_body.objectName(),'lanHistoryGroup')
        self.assertFalse(panel.history_label.isVisible())
        self.assertEqual([row.state.text() for row in panel.history_rows],
                         ['已完成','已取消','失败'])
        self.assertEqual([row.when.text() for row in panel.history_rows],
                         ['10-27 10:25','10-27 10:24','10-27 08:26'])
        self.assertIn('3 项',panel.history_rows[0].detail.accessibleName())
        self.assertIn('…',panel.history_rows[0].detail.text())
        self.assertTrue(all(row.geometry().bottom() <= panel.history_body.height()
                            for row in panel.history_rows))

    def test_delete_one_history_record_persists_and_leaves_live_transfer_and_files(self):
        panel=self.dialog.panel
        original=self.root/'kept.pdf';original.write_bytes(b'keep original')
        panel._history=[
            {'when':'10-27 10:25','name':'newest.pdf','peer':'Mac mini',
             'state':'完成','direction':'send'},
            {'when':'10-27 10:24','name':original.name,'peer':'Windows',
             'state':'已取消','direction':'send'},
            {'when':'10-27 10:23','name':'older.png','peer':'Mac mini',
             'state':'失败','direction':'receive'},
            {'when':'10-27 10:22','name':'oldest.zip','peer':'Windows',
             'state':'完成','direction':'receive'}]
        panel._save_history();panel._render_history()
        panel._progress({'id':'still-active','direction':'send','peer':'Windows',
            'state':'waiting','code':'123456'})
        APP.processEvents()
        self.assertEqual(panel.history_rows[1].remove_button.accessibleName(),
                         '删除这条传输记录')
        panel.history_rows[1].remove_button.click();APP.processEvents()
        self.assertEqual([row.detail.accessibleName() for row in panel.history_rows],
                         ['newest.pdf','older.png','oldest.zip'])
        self.assertTrue(original.exists())
        self.assertIn('still-active',panel._rows)
        self.assertFalse(panel._rows['still-active'].get('finished',False))
        self.dialog.close();self.dialog.deleteLater();APP.processEvents()
        self.dialog=LanDialog(self.service,SettingsStore(self.root/'profile'))
        self.assertEqual([row['name'] for row in self.dialog.panel._history],
                         ['newest.pdf','older.png','oldest.zip'])

    def test_history_delete_write_failure_keeps_record_visible(self):
        panel=self.dialog.panel
        panel._history=[{'when':'10-27 10:25','name':'report.pdf','peer':'Mac mini',
                         'state':'完成','direction':'send'}]
        panel._render_history();APP.processEvents()
        with patch.object(panel,'_save_history',side_effect=OSError('read only')):
            panel.history_rows[0].remove_button.click()
        self.assertEqual(len(panel._history),1)
        self.assertEqual(len(panel.history_rows),1)
        self.assertIn('无法删除传输记录',panel.status_label.text())

    def test_remote_url_is_not_treated_as_a_local_file(self):
        mime=QMimeData();mime.setUrls([QUrl('https://example.invalid/image.png')])
        enter=QDragEnterEvent(self.dialog.panel.drop_zone.rect().center(),Qt.DropAction.CopyAction,mime,
            Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        self.dialog.panel.drop_zone.dragEnterEvent(enter)
        self.assertFalse(enter.isAccepted())

    def test_two_columns_keep_send_and_transfer_visible_without_window_scroll(self):
        panel=self.dialog.panel
        self.assertLess(panel.send_scroll.geometry().right(),panel.activity_scroll.geometry().left())
        # Columns stay independent; the send form also scrolls when the
        # full WAN code controls cannot fit a short viewport.
        self.assertEqual(set(self.dialog.findChildren(QScrollArea)),
                         {panel.send_scroll,panel.activity_scroll,panel.queue_scroll,panel.history_scroll})
        self.assertIs(panel.queue_scroll.widget(),panel.queue_body)
        self.assertIs(panel.history_scroll.widget(),panel.history_body)
        self.assertIs(panel.send_scroll.widget(),panel.send_column)
        self.assertEqual(panel.send_scroll.verticalScrollBar().maximum(),0)
        self.assertIs(panel.activity_scroll.widget(),panel.activity_column)
        self.assertTrue(panel.drop_zone.isVisible())
        self.assertFalse(any(button.text() in ('添加文件','添加文件夹')
                             for button in panel.findChildren(QPushButton)))
        self.assertEqual(panel.drop_zone.focusPolicy(),Qt.FocusPolicy.StrongFocus)
        self.assertTrue(panel.receive_path.isVisible())
        self.assertTrue(panel.history_label.isVisible())
        self.assertTrue(QSvgRenderer(QByteArray(glyph_svg('nearby','#1876db').encode())).isValid())
        self.assertTrue(QSvgRenderer(QByteArray(glyph_svg('file-up','#1876db').encode())).isValid())
        self.assertTrue(QSvgRenderer(QByteArray(glyph_svg('cloud','#1876db').encode())).isValid())

    def test_no_device_state_disables_destination_and_send_until_discovery(self):
        panel=self.dialog.panel
        panel._peers_changed([]);APP.processEvents()
        self.assertFalse(panel.devices.isEnabled())
        self.assertFalse(panel.send_button.isEnabled())
        self.assertEqual(panel.devices.currentText(),'附近暂无设备')
        self.assertIn('正在搜索',panel.peer_count.text())
        self.assertEqual(panel.scan_indicator.width(),16)
        panel._peers_changed(self.service.peers());APP.processEvents()
        self.assertTrue(panel.devices.isEnabled())
        self.assertTrue(panel.send_button.isEnabled())

    def test_scanner_animates_only_while_visible_and_respects_reduced_motion(self):
        panel=self.dialog.panel
        with patch('lan_panel._motion_allowed',return_value=True):
            panel._peers_changed([]);APP.processEvents()
            self.assertTrue(panel.scan_indicator._timer.isActive())
            before=panel.scan_indicator.geometry()
            panel.scan_indicator._advance();APP.processEvents()
            self.assertEqual(panel.scan_indicator.geometry(),before)
            panel._peers_changed(self.service.peers());APP.processEvents()
            self.assertFalse(panel.scan_indicator._timer.isActive())
            panel._peers_changed([]);APP.processEvents()
            self.dialog.hide();APP.processEvents()
            self.assertFalse(panel.scan_indicator._timer.isActive())
            self.dialog.show();APP.processEvents()
            self.assertTrue(panel.scan_indicator._timer.isActive())
        with patch('lan_panel._motion_allowed',return_value=False):
            panel.scan_indicator._refresh_timer()
            self.assertFalse(panel.scan_indicator._timer.isActive())
            self.assertTrue(panel.scan_indicator._scanning)
        with patch.dict(os.environ,{'FLICKDOCK_REDUCE_MOTION':'1'}):
            self.assertFalse(_motion_allowed())
        with patch.dict(os.environ,{'FLICKDOCK_LOW_PERFORMANCE':'true'}):
            self.assertFalse(_motion_allowed())

    def test_multiple_transfers_are_reachable_with_buttons_including_incoming_offer(self):
        panel=self.dialog.panel
        for number in range(4):
            panel._progress({'id':f'send-{number}','direction':'send','peer':'测试设备',
                'state':'waiting','code':'123456'})
        APP.processEvents()
        self.assertEqual(panel.transfer_count.text(),'4 项进行中')
        self.assertEqual(panel.page_label.text(),'1 / 2')
        self.assertGreaterEqual(panel.page_label.width(),40)
        self.assertLess(panel.previous_page.geometry().right(),panel.page_label.geometry().left())
        self.assertLess(panel.page_label.geometry().right(),panel.next_page.geometry().left())
        self.assertEqual(sum(row['frame'].isVisible() for row in panel._rows.values()),2)
        panel.next_page.click();APP.processEvents()
        self.assertEqual(panel.page_label.text(),'2 / 2')
        self.assertEqual(sum(row['frame'].isVisible() for row in panel._rows.values()),2)
        panel.show_offer({'id':'receive-1','peer':'另一台设备','count':1,
            'names':['report.pdf'],'total':512,'code':'909090'})
        APP.processEvents()
        self.assertEqual(panel.page_label.text(),'1 / 3')
        self.assertTrue(panel._rows['receive-1']['frame'].isVisible())

    def test_title_drag_moves_dialog_and_native_host_shadow_is_disabled(self):
        self.assertTrue(self.dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(self.dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        bar=self.dialog.title_bar
        start=self.dialog.pos(); point=QPointF(18,18)
        global_start=QPointF(bar.mapToGlobal(point.toPoint()))
        press=QMouseEvent(QEvent.Type.MouseButtonPress,point,global_start,
            Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        bar.mousePressEvent(press)
        travel=QPointF(global_start.x()+65,global_start.y()+38)
        move=QMouseEvent(QEvent.Type.MouseMove,point,travel,
            Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        bar.mouseMoveEvent(move)
        self.assertEqual(self.dialog.pos(),start+travel.toPoint()-global_start.toPoint())
        release=QMouseEvent(QEvent.Type.MouseButtonRelease,point,travel,
            Qt.MouseButton.LeftButton,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier)
        bar.mouseReleaseEvent(release)
        with patch('lan_panel.remove_host_window_shadow') as native:
            self.dialog.hide();self.dialog.show();APP.processEvents()
            native.assert_called_with(self.dialog)

if __name__=='__main__':unittest.main()
