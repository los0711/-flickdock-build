"""Reference dialogs exercise the original UI signals without network or install."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import unittest

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLineEdit, QWidget

from confirm_clear import ClearConfirmOverlay, confirmation_active
from update_dialog import UpdateDialog
from update_notice import UpdateNotice

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
font_path=Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf'
QFontDatabase.addApplicationFont(str(font_path))
APP.setFont(QFont('HarmonyOS Sans SC',10))


class FakeClient(QObject):
    checking=Signal(bool);update_available=Signal(dict,bool);up_to_date=Signal(bool)
    failed=Signal(str,bool);progress=Signal(int,int);verified_file=Signal(str,dict,bool);cancelled=Signal(bool)
    def __init__(self):super().__init__();self.calls=[]
    def check(self,manual=False):self.calls.append(('check',manual));self.checking.emit(manual);return True
    def download(self,manifest=None,manual=True):self.calls.append(('download',manifest,manual));return True
    def cancel(self):self.calls.append(('cancel',));self.cancelled.emit(True)


class HtmlNativeDialogs(unittest.TestCase):
    def test_clear_buttons_scope_and_keyboard_cancel_still_match_original_flow(self):
        host=QWidget();host.resize(1040,600)
        card=QFrame(host);card.setGeometry(host.rect())
        editor=QLineEdit('keep',card);editor.setGeometry(20,20,100,28)
        host.show();APP.processEvents()
        for theme in ('dark','light'):
            card.setStyleSheet('background:#161616;' if theme=='dark' else 'background:#EAEEF5;')
            overlay=ClearConfirmOverlay(card,8,font_family='HarmonyOS Sans SC',theme=theme)
            results=[];overlay.finished.connect(results.append)
            self.assertTrue(overlay.open());APP.processEvents()
            self.assertEqual(overlay.panel.width(),340)
            self.assertEqual(overlay.cancel_button.width(),overlay.delete_button.width())
            self.assertTrue(overlay.cancel_button.isDefault());self.assertFalse(overlay.delete_button.isDefault())
            self.assertTrue(confirmation_active(host))
            self.assertFalse(overlay.isWindow())
            for button in (overlay.cancel_button,overlay.delete_button):
                self.assertTrue(overlay.panel.rect().contains(button.mapTo(overlay.panel,button.rect().bottomRight())))
                self.assertGreaterEqual(button.height(),button.fontMetrics().height()+10)
            self.assertEqual(overlay.description.text(),overlay.description.full_text)
            if os.environ.get('FLICKDOCK_CAPTURE_DIALOGS'):
                output=Path(__file__).resolve().parents[2]/'outputs'
                host.grab().save(str(output/f'html-native-clear-{theme}.png'))
            QTest.keyClick(overlay.delete_button,Qt.Key.Key_Return);APP.processEvents()
            self.assertEqual(results,[False]);self.assertFalse(confirmation_active(host))
            self.assertTrue(overlay.open());overlay.delete_button.click()
            self.assertEqual(results,[False,True]);self.assertFalse(overlay.isVisible())
            self.assertEqual(editor.text(),'keep')
            overlay.deleteLater();APP.processEvents()
        host.close();host.deleteLater();APP.processEvents()

    def test_updates_keep_real_check_download_cancel_install_and_skip_bindings(self):
        dialog=UpdateDialog('1.8.14');client=FakeClient();dialog.bind_client(client)
        dialog.show();APP.processEvents();installed=[];skipped=[]
        dialog.request_install.connect(lambda:installed.append(dialog.verified_path))
        dialog.skipped.connect(skipped.append)
        manifest={'version':'1.8.15','notes':'• 优化小搁架叠放动画与边缘悬停防抖体验\n• 提升局域网大文件直连传输与分块校验稳定性',
                  'installer':{'sha256':'a'*64,'size':4096,'url':'https://example.invalid/no-request'}}
        dialog.primary_button.click();self.assertEqual(client.calls[-1],('check',True))
        client.update_available.emit(manifest,True);APP.processEvents()
        self.assertEqual(dialog.surface.width(),420)
        # Changelog is intentionally centralized in the overview notice.
        self.assertFalse(dialog.notes_card.isVisible())
        self.assertEqual(dialog._notes,manifest['notes'])
        self.assertFalse(dialog.isModal())
        for theme in ('dark','light'):
            dialog.apply_theme(theme);APP.processEvents()
            if os.environ.get('FLICKDOCK_CAPTURE_DIALOGS'):
                output=Path(__file__).resolve().parents[2]/'outputs'
                dialog.grab().save(str(output/f'html-native-update-{theme}.png'))
        dialog.primary_button.click();self.assertEqual(client.calls[-1],('download',manifest,True))
        client.progress.emit(1024,4096);APP.processEvents()
        self.assertEqual(dialog.progress_bar.value(),25)
        self.assertIn('25%',dialog.progress_label.text())
        self.assertTrue(dialog.sha_label.isVisible())
        dialog.secondary_button.click();self.assertEqual(dialog.state,'cancelled')
        client.verified_file.emit('/never-executed-installer',manifest,True)
        self.assertFalse(installed);self.assertEqual(dialog.state,'cancelled')
        client.checking.emit(True);client.update_available.emit(manifest,True)
        client.verified_file.emit('/never-executed-installer',manifest,True)
        self.assertEqual(dialog.state,'ready');self.assertFalse(installed)
        dialog.primary_button.click();self.assertEqual(installed,['/never-executed-installer'])
        client.checking.emit(True);client.update_available.emit(manifest,True)
        dialog.skip_button.click();self.assertEqual(skipped,['1.8.15'])
        dialog.bind_client(None);dialog.close();dialog.deleteLater();APP.processEvents()

    def test_notice_keeps_offer_nonactivating_and_actions_explicit(self):
        notice=UpdateNotice(theme='dark');calls=[]
        notice.update_requested.connect(lambda:calls.append('update'))
        notice.show_version('1.8.15');APP.processEvents()
        self.assertIn('1.8.15',notice.version_label.text());self.assertFalse(calls)
        self.assertTrue(notice.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        notice.later_button.click();self.assertFalse(notice.isVisible())
        notice.show_version('1.8.15');notice.update_button.click();self.assertEqual(calls,['update'])
        if os.environ.get('FLICKDOCK_CAPTURE_DIALOGS'):
            output=Path(__file__).resolve().parents[2]/'outputs'
            notice.grab().save(str(output/'html-native-update-notice.png'))
        notice.close();notice.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
