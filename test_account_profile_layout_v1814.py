"""Account identity and device geometry, using Qt offscreen and fake service IO."""
import os
import sys
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent,QPoint,QPointF,Qt
from PySide6.QtGui import QColor,QEnterEvent,QFontInfo,QTextLayout
from PySide6.QtTest import QSignalSpy,QTest
from PySide6.QtWidgets import QLabel,QWidget

import app_settings
from account_panel import AccountGlyph, AccountNameLabel, AccountPanel
from native_ui_common import configure_reference_font
from test_account_settings_v181 import APP, Service


class AccountProfileLayoutTests(unittest.TestCase):
    def test_account_avatar_uses_the_same_solid_blue_in_both_themes(self):
        from native_ui_common import colors
        for theme in ('dark','light'):
            self.panel.apply_theme(theme);self.settle()
            image=self.panel.account_glyph.grab().toImage()
            expected=QColor(colors(theme)['action'])
            self.assertEqual(image.pixelColor(10,13),expected)
            self.assertEqual(image.pixelColor(30,13),expected)

    def test_signed_in_identity_is_centered_without_transfer_subtitle(self):
        self.panel._show_sync_status=False
        for status in ('正在上传 fixture.zip · 1/1','搁架已同步。','同步失败'):
            self.panel.set_sync_status(status);self.settle()
            self.assertTrue(self.panel.sync_status_label.isHidden())
        panel=self.panel
        self.assertTrue(panel.account_state_label.isVisible())
        centers=[widget.mapTo(panel.profile_frame,widget.rect().center()).y()
                 for widget in (panel.account_glyph,panel.username_label,panel.account_state_label,
                                panel.password_toggle_button,panel.logout_button)]
        self.assertLessEqual(max(centers)-min(centers),2,centers)

    def test_settings_heading_reflects_sync_directions_and_connection(self):
        panel=self.panel;panel._show_sync_status=False
        for upload,receive,connected,expected in (
                (True,True,True,'云端同步已开启'),
                (True,True,False,'云端同步未连接'),
                (False,False,False,'云端同步已关闭'),
                (True,False,True,'仅云端上传已开启'),
                (False,True,True,'仅云端接收已开启')):
            panel.set_cloud_sync_heading(upload,receive,connected)
            self.assertEqual(panel.account_heading_state.text(),expected)
        self.service.signed_in=False
        panel.set_cloud_sync_heading(True,True,True)
        self.assertEqual(panel.account_heading_state.text(),'未登录')

    def test_device_card_fits_content_and_large_device_list_scrolls(self):
        self.dialog.resize(892,820);self.settle()
        viewport=self.dialog.scroll.viewport()
        self.assertLess(self.panel.height(),viewport.height())
        self.assertGreater(self.panel.devices_scroll.height(),40)
        self.service.devices.extend(dict(id=f'fill-{index}',name=f'设备 {index}',platform='windows') for index in range(20))
        self.service.list_devices();self.settle()
        self.assertGreater(self.panel.devices_scroll.verticalScrollBar().maximum(),0)
        self.assertLessEqual(self.panel.height(),viewport.height()+8)
        self.dialog.resize(892,632);self.settle()
        self.assertLessEqual(self.panel.height(),viewport.height()+8)

    def test_idle_and_hidden_account_layouts_quiesce_and_refresh_on_next_show(self):
        self.settle();spy=QSignalSpy(self.panel.height_changed)
        QTest.qWait(150);self.settle()
        self.assertLessEqual(spy.count(),1)
        self.assertFalse(self.panel._fit_timer.isActive())
        self.dialog.hide();self.settle();before=spy.count()
        self.service.devices[1]['name']='离屏更新后的 MacBook Air'
        self.service.list_devices();QTest.qWait(150);self.settle()
        self.assertEqual(spy.count(),before)
        self.assertFalse(self.panel._fit_timer.isActive())
        self.dialog.show();self.settle()
        self.assertEqual(self.panel._rows_by_id['other'].findChild(AccountNameLabel,'accountDeviceName').text(),'离屏更新后的 MacBook Air')
        self.assertTrue(self.panel.devices_scroll.isVisible())

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
        self.dialog.select_category('账号与设备')
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

    def test_account_and_device_names_use_platform_font_without_added_spacing(self):
        self.service.devices[1]['name']='MacBook Air';self.service.list_devices();self.settle()
        device=self.panel._rows_by_id['other'].findChild(AccountNameLabel,'accountDeviceName')
        for label in (self.panel.username_label,device):
            font=label.font()
            self.assertEqual(font.letterSpacing(),0)
            self.assertEqual(font.wordSpacing(),0)
            self.assertIn(font.stretch(),(0,100))
            layout=QTextLayout(label.text(),font);layout.beginLayout();layout.createLine();layout.endLayout()
            self.assertTrue(layout.glyphRuns())
            if sys.platform=='win32':
                self.assertEqual(QFontInfo(font).family(),'Inter')
                self.assertTrue({run.rawFont().familyName() for run in layout.glyphRuns()} <= {'Inter','Microsoft YaHei UI','Microsoft YaHei'})

    def test_identical_device_push_keeps_rows_and_avoids_rebuilding(self):
        self.settle();rows=dict(self.panel._rows_by_id)
        self.service.list_devices();self.settle()
        self.assertEqual(rows,self.panel._rows_by_id)
        self.service.devices[1]['name']='MacBook Air'
        self.service.list_devices();self.settle()
        self.assertEqual(self.panel._devices['other']['name'],'MacBook Air')
        self.assertIs(rows['other'],self.panel._rows_by_id['other'])
        self.assertEqual(self.panel._rows_by_id['other'].findChild(AccountNameLabel,'accountDeviceName').text(),'MacBook Air')

    def test_device_names_are_centered_in_two_scroll_columns_without_presence_labels(self):
        self.service.devices.extend(dict(id=f'peer-{index}',name=f'设备 {index}',platform='windows') for index in range(8))
        self.service.list_devices();self.settle();rows=dict(self.panel._rows_by_id)
        self.settle()
        self.assertEqual(rows,self.panel._rows_by_id)
        for row in rows.values():
            name=row.findChild(AccountNameLabel,'accountDeviceName')
            self.assertLessEqual(abs(name.mapTo(row,name.rect().center()).y()-row.rect().center().y()),1)
            self.assertFalse(row.findChildren(AccountNameLabel,'accountDeviceMeta'))
        for width in (740,892,1024):
            self.dialog.resize(width,632);self.settle()
            self.assertEqual(len({row.x() for row in rows.values()}),2)
            self.assertEqual(len({row.y() for row in rows.values()}),(len(rows)+1)//2)
            for index,row in enumerate(rows.values()):
                grid=self.panel.devices_layout.getItemPosition(self.panel.devices_layout.indexOf(row))
                self.assertEqual(grid[:2],(index//2,index%2))
        self.assertGreater(self.panel.devices_scroll.verticalScrollBar().maximum(),0)
        self.assertEqual(rows,self.panel._rows_by_id)

    def test_rename_is_floating_without_resizing_rows_and_actions_stay_at_right_edge(self):
        for width in (740,892,1024):
            self.dialog.resize(width,632);self.settle()
            for key in ('current','other'):
                row=self.panel._rows_by_id[key]
                point=QPointF(row.rect().center());APP.sendEvent(row,QEnterEvent(point,point,point));self.settle()
                previous=row.size()
                self.panel.device_rename_buttons[key].click();self.settle()
                form=self.panel._rename_forms[key];row=self.panel._rows_by_id[key]
                self.assertTrue(form.isVisible())
                self.assertTrue(form.isWindow());self.assertEqual(row.size(),previous)
                self.assertTrue(form.screen().availableGeometry().contains(form.frameGeometry()))
                rename=self.panel.device_rename_buttons[key]
                last=self.panel.device_buttons.get(key,rename)
                self.assertLessEqual(row.width()-last.mapTo(row,last.rect().bottomRight()).x(),14)
                if key=='other':
                    gap=last.mapTo(row,QPoint(0,0)).x()-rename.mapTo(row,rename.rect().topRight()).x()
                    self.assertLessEqual(gap,6)
                self.panel._cancel_rename();APP.sendEvent(row,QEvent(QEvent.Leave));self.settle()

    def test_device_actions_reveal_on_hover_or_keyboard_focus_without_layout_shift(self):
        self.dialog.close_button.setFocus(Qt.TabFocusReason);self.settle()
        row=self.panel._rows_by_id['other']
        actions=row.findChild(QWidget,'accountDeviceActions')
        name=row.findChild(AccountNameLabel,'accountDeviceName')
        self.assertTrue(actions.isHidden())
        before=(row.size(),name.geometry())
        point=QPointF(row.rect().center())
        APP.sendEvent(row,QEnterEvent(point,point,point));self.settle()
        self.assertTrue(actions.isVisible())
        self.assertEqual(before,(row.size(),name.geometry()))
        APP.sendEvent(row,QEvent(QEvent.Leave));self.settle()
        self.assertTrue(actions.isHidden())
        row.setFocus(Qt.TabFocusReason);self.settle()
        self.assertTrue(actions.isVisible())
        QTest.keyClick(row,Qt.Key_Tab);self.settle()
        self.assertIs(APP.focusWidget(),self.panel.device_rename_buttons['other'])
        self.dialog.close_button.setFocus(Qt.TabFocusReason);self.settle()
        self.assertTrue(actions.isHidden())
        self.assertLess(self.panel.sync_status_label.font().pixelSize(),self.panel.username_label.font().pixelSize())


if __name__=='__main__':unittest.main(verbosity=2)
