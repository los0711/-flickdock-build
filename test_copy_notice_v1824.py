"""The copy-notice switch suppresses both outcomes, including delayed replies."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import QRect,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit
from test_html_native_workspace import APP,flush
from flickdock import ShelfManager
from intake_notice import ClipboardNotice,NOTICE_MARGIN_PX


class CopyNoticeTests(unittest.TestCase):
    def test_progress_rings_refresh_without_reopening_and_dismissal_suppresses_late_success(self):
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        state=dict(id='progress-copy',phase='uploading',local_copy=True,value=('files',['original.bin']),
                   cloud_transfer=dict(phase='uploading',received=10,total=100),deliveries={'cloud':dict(success=0,total=2)})
        try:
            with patch('native_ui_common.reduced_motion',return_value=True),patch.object(card._window_fade,'reopen',wraps=card._window_fade.reopen) as reopen:
                card.update_delivery(state);QTest.qWait(180);flush()
                self.assertTrue(card.isVisible());self.assertEqual(card.progress.labels['cloud'].text(),'云端 10%')
                self.assertEqual(set(card.progress.labels),{'cloud'});expiry=card.timer.remainingTime()
                card.update_delivery(dict(state,cloud_transfer=dict(phase='verifying',received=100,total=100)))
                self.assertEqual(card.progress.labels['cloud'].text(),'云端确认')
                self.assertLessEqual(card.timer.remainingTime(),expiry);self.assertEqual(reopen.call_count,1)
                card.dismiss();flush()
                card.schedule_result(True,dict(state,phase='synced',backed_up=True));QTest.qWait(180);flush()
                self.assertFalse(card.isVisible());self.assertEqual(reopen.call_count,1)
        finally:card.hide();card.deleteLater();flush()

    def test_success_route_counts_update_in_place_without_reopening_or_extending_expiry(self):
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        counts={'cloud':{'success':0,'total':4}}
        first={'cloud':{'success':1,'total':4}}
        latest={'cloud':{'success':4,'total':4}}
        try:
            with patch('native_ui_common.reduced_motion',return_value=True),patch.object(card._window_fade,'reopen',wraps=card._window_fade.reopen) as reopen:
                card.schedule_result(True,dict(id='latest-copy',value=('text',{'text':'original copy'}),deliveries=counts))
                card.update_delivery(dict(id='latest-copy',deliveries=first))
                QTest.qWait(180);flush();self.assertTrue(card.isVisible())
                self.assertEqual(card.status.text(),'已同步到云端')
                expiry=card.timer.remainingTime();QTest.qWait(20)
                card.update_delivery(dict(id='latest-copy',deliveries=latest));flush()
                self.assertEqual(card.status.text(),'已同步到云端')
                self.assertLessEqual(card.timer.remainingTime(),expiry);self.assertEqual(reopen.call_count,1)
                card.update_delivery(dict(id='older-copy',deliveries=counts))
                self.assertEqual(card.status.text(),'已同步到云端')
                card.dismiss();flush();card.update_delivery(dict(id='latest-copy',deliveries=first))
                self.assertFalse(card.isVisible());self.assertFalse(card.timer.isActive());self.assertEqual(reopen.call_count,1)
        finally:card.hide();card.deleteLater();flush()

    def test_success_counts_never_elide_a_route_on_narrow_displays_in_both_themes(self):
        screen=SimpleNamespace(availableGeometry=lambda:QRect(100,200,320,400))
        counts={route:{'success':10,'total':20} for route in ('cloud',)}
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        try:
            for theme in ('dark','light'):
                card.manager.settings.theme=theme
                with patch('intake_notice.QGuiApplication.screenAt',return_value=screen):
                    card.show_result(True,dict(id='copy',value=('text',{'text':'original'}),deliveries=counts));flush()
                self.assertEqual(card.status.text(),'已同步到云端')
                self.assertTrue(card.card.rect().contains(card.status.geometry()));self.assertTrue(screen.availableGeometry().contains(card.geometry()))
                self.assertGreaterEqual(card.status.height(),card.status.heightForWidth(card.status.width()))
                self.assertLessEqual(card.height(),120)
        finally:card.hide();card.deleteLater();flush()

    def test_disabled_manager_never_constructs_a_card_for_either_result(self):
        manager=SimpleNamespace(_quitting=False,_clipboard_runtime_enabled=lambda:True,
                                settings=SimpleNamespace(clipboard_success_notice=False))
        for ok in (True,False):ShelfManager._clipboard_copy_result(manager,ok,{'id':'copy'})
        self.assertFalse(hasattr(manager,'clipboard_notice'))

    def test_toggle_off_suppresses_queued_and_direct_results_of_both_kinds(self):
        settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)
        card=ClipboardNotice(SimpleNamespace(settings=settings))
        try:
            with patch('native_ui_common.reduced_motion',return_value=True):
                for ok in (True,False):
                    settings.clipboard_success_notice=True
                    card.schedule_result(ok,dict(id='delayed',value=('text',{'text':'copy'})))
                    settings.clipboard_success_notice=False
                    QTest.qWait(180);flush();self.assertFalse(card.isVisible())
                    card.show_result(ok,{});self.assertFalse(card.isVisible())
                settings.clipboard_success_notice=True;card.show_result(False,{})
                self.assertTrue(card.isVisible())
                settings.clipboard_success_notice=False;card.supersede()
                self.assertFalse(card.isVisible());self.assertIsNone(card._pending_result)
                self.assertFalse(card._result_timer.isActive())
        finally:card.hide();card.deleteLater();flush()

    def test_close_button_dismisses_with_nonactivating_window_flags(self):
        editor=QLineEdit();editor.setText('保留正在编辑的内容');editor.show();editor.activateWindow();editor.setFocus();flush()
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        try:
            card.show_result(True,dict(value=('text',{'text':'沿海公路，开往山海之间'}),delivered=True,route='cloud',deliveries={'cloud':{'success':1,'total':1}}));flush()
            self.assertTrue(card.windowFlags()&Qt.WindowDoesNotAcceptFocus)
            self.assertTrue(card.testAttribute(Qt.WA_ShowWithoutActivating));self.assertEqual(card.close_button.focusPolicy(),Qt.NoFocus)
            self.assertEqual(card.status.text(),'已同步到云端')
            QTest.mouseClick(card.close_button,Qt.LeftButton);flush()
            self.assertFalse(card.isVisible());self.assertFalse(card.timer.isActive())
            self.assertEqual(editor.text(),'保留正在编辑的内容')
        finally:card.deleteLater();editor.deleteLater();flush()

    def test_narrow_display_and_both_themes_keep_plain_text_and_controls_inside_card(self):
        screen=SimpleNamespace(availableGeometry=lambda:QRect(100,200,320,400))
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        try:
            for theme in ('dark','light'):
                card.manager.settings.theme=theme
                with patch('intake_notice.QGuiApplication.screenAt',return_value=screen):
                    card.show_result(True,dict(value=('text',{'text':'<b>原始文本</b>\n'+('长内容'*500)}),backed_up=True));flush()
                self.assertTrue(screen.availableGeometry().contains(card.geometry()))
                self.assertEqual(card.preview.textFormat(),Qt.PlainText);self.assertIn('<b>',card.preview.text())
                self.assertEqual(card.preview.toolTip(),'');self.assertNotIn('\n',card.preview.text())
                self.assertEqual(card.status.text(),'已同步到云端')
                for child in (card.logo,card.message,card.preview,card.status,card.close_button):
                    self.assertTrue(card.card.rect().contains(child.geometry()),child.objectName())
                self.assertGreater(card.preview.y(),card.message.geometry().bottom())
                self.assertGreater(card.status.y(),card.preview.geometry().bottom())
                self.assertEqual(card.card.layout().contentsMargins().left(),12)
        finally:card.hide();card.deleteLater();flush()

    def test_size_limit_is_a_compact_blue_notice_centered_above_the_reserved_dock_area(self):
        area=QRect(-1440,24,1440,800)
        screen=SimpleNamespace(availableGeometry=lambda:area)
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        try:
            for theme in ('dark','light'):
                card.manager.settings.theme=theme
                with patch('intake_notice.QGuiApplication.screenAt',return_value=screen):
                    card.show_result(False,dict(phase='size_limit',value=('files',['large-video.mp4'])));flush()
                self.assertEqual(card.message.text(),'此文件不能同步')
                self.assertIn('超过 20 MB',card.status.text());self.assertNotIn('失败',card.status.text())
                from native_ui_common import colors
                self.assertIn('color:'+colors(theme)['link'],card.card.styleSheet())
                self.assertLessEqual(card.width(),320);self.assertLessEqual(card.height(),100)
                self.assertLessEqual(abs(card.geometry().center().x()-area.center().x()),1)
                self.assertEqual(area.bottom()-card.geometry().bottom(),NOTICE_MARGIN_PX)
                self.assertTrue(area.contains(card.geometry()))
        finally:card.hide();card.deleteLater();flush()

    def test_failure_does_not_invent_network_cause_or_recipient_delivery(self):
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        try:
            card.show_result(False,dict(value=('files',['missing-original.bin','other.bin'])));flush()
            self.assertEqual(card.preview.text(),'missing-original.bin · 共 2 项')
            self.assertIn('内容保留在本机',card.status.text());self.assertNotIn('另一台设备',card.status.text())
            card.timer.start(10);QTest.qWait(40);flush();self.assertFalse(card.isVisible())
        finally:card.hide();card.deleteLater();flush()

    def test_superseded_copy_is_neutral_and_never_claims_success_or_upload_failure(self):
        card=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)))
        try:
            card.show_result(False,dict(phase='superseded',value=('text',{'text':'an older copy'})));flush()
            self.assertEqual(card.message.text(),'已有更新的复制');self.assertIn('接替',card.status.text())
            self.assertNotIn('失败',card.message.text());self.assertNotIn('已同步',card.status.text())
            from native_ui_common import colors
            self.assertIn('color:'+colors('dark')['link'],card.card.styleSheet())
        finally:card.hide();card.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
