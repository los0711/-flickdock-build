"""Account identity and device geometry, using Qt offscreen and fake service IO."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QLabel

import app_settings
from account_panel import AccountGlyph, AccountPanel
from native_ui_common import configure_reference_font
from test_account_settings_v181 import APP, Service


class AccountProfileLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-account-layout-')
        self.runtime=patch.object(app_settings,'_startup_runtime_allowed',return_value=False)
        self.runtime.start()
        configure_reference_font(APP)
        self.dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(self.temp.name)))
        self.service=Service()
        self.service.account=dict(id='fixture',username='Roger',login_alias='id:legacy-account-identity')
        self.panel=AccountPanel(self.service)
        self.dialog.set_account_panel(self.panel)
        self.panel.set_sync_status('可同步内容已同步；1 项暂留本机，请检查文件大小或原文件所在位置。')
        self.dialog.show()

    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();APP.processEvents()
        self.runtime.stop();self.temp.cleanup()

    def settle(self):
        for _ in range(4):APP.processEvents()

    def test_profile_feedback_and_legacy_alias_never_overlap_or_clip(self):
        for width in (892,740,1024,892):
            with self.subTest(width=width):
                self.dialog.resize(width,632);self.settle()
                panel=self.panel
                for widget in (panel.profile_frame,panel.sync_status_label,panel.login_alias_edit):
                    self.assertTrue(widget.isVisible())
                    bottom=widget.mapTo(panel.signed_in_page,widget.rect().bottomRight())
                    self.assertTrue(panel.signed_in_page.rect().contains(bottom),(widget.objectName(),bottom))
                profile_bottom=panel.profile_frame.mapTo(panel.signed_in_page,panel.profile_frame.rect().bottomRight()).y()
                sync_top=panel.sync_status_label.mapTo(panel.signed_in_page,QPoint(0,0)).y()
                alias_top=panel.login_alias_edit.mapTo(panel.signed_in_page,QPoint(0,0)).y()
                username_bottom=panel.username_label.mapTo(panel.signed_in_page,panel.username_label.rect().bottomRight()).y()
                self.assertGreater(sync_top,username_bottom)
                self.assertGreater(alias_top,profile_bottom)
                self.assertGreaterEqual(panel.sync_status_label.height(),panel.sync_status_label.heightForWidth(panel.sync_status_label.width()))
                self.assertEqual(panel.login_alias_edit.text(),'id:legacy-account-identity')

    def test_devices_show_name_platform_and_complete_vector_icon(self):
        self.settle()
        for key,platform in (('current','Windows'),('other','Mac')):
            row=self.panel._rows_by_id[key]
            self.assertGreaterEqual(row.height(),44)
            visible_labels=[label for label in row.findChildren(QLabel) if label.isVisible()]
            self.assertIn(platform,row.accessibleName())
            self.assertTrue(any(self.panel._devices[key]['name'] in label.text() for label in visible_labels))
            for label in visible_labels:
                self.assertTrue(row.rect().contains(label.mapTo(row,label.rect().bottomRight())))
            glyph=row.findChild(AccountGlyph)
            self.assertIsNotNone(glyph);self.assertGreaterEqual(glyph.width(),24)
            pixmap=glyph.grab().toImage()
            opaque=[(x,y) for x in range(pixmap.width()) for y in range(pixmap.height()) if pixmap.pixelColor(x,y).alpha()]
            self.assertTrue(opaque)

    def test_identical_device_push_keeps_rows_and_avoids_rebuilding(self):
        self.settle();rows=dict(self.panel._rows_by_id)
        self.service.list_devices();self.settle()
        self.assertEqual(rows,self.panel._rows_by_id)
        self.service.devices[1]['name']='MacBook Air'
        self.service.list_devices();self.settle()
        self.assertEqual(self.panel._devices['other']['name'],'MacBook Air')
        self.assertIsNot(rows['other'],self.panel._rows_by_id['other'])


if __name__=='__main__':unittest.main(verbosity=2)
