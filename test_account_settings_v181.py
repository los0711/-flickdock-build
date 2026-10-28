"""Account actions and settings scroll/update feedback, entirely Qt offscreen."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject,QPoint,QPointF,Qt,Signal
from PySide6.QtGui import QFont,QFontDatabase,QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QComboBox,QLabel,QLineEdit,QSlider,QSpinBox,QWidget

import app_settings
from account_panel import AccountPanel

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
QFontDatabase.addApplicationFont(str(Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf'))
APP.setFont(QFont('HarmonyOS Sans SC',11))


class Service(QObject):
    account_changed=Signal(object);status_changed=Signal(str);busy_changed=Signal(bool)
    devices_changed=Signal(list);email_challenge=Signal(str,str);password_changed=Signal()
    def __init__(self):
        super().__init__();self.busy=False;self.account={'id':'fixture','username':'Roger'}
        self.signed_in=True;self.device_id='current';self.calls=[]
        self.devices=[dict(id='current',name='我的电脑',platform='windows',current=True),
                      dict(id='other',name='MacBook',platform='macos',current=False)]
    def list_devices(self):self.devices_changed.emit(copy.deepcopy(self.devices))
    def logout(self):self.signed_in=False;self.account=None;self.account_changed.emit(None)
    def change_password(self,*args):self.calls.append(('password',args))
    def rename_device(self,*args):self.calls.append(('rename',args))


class SettingsAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-account-ui-')
        self.runtime=patch.object(app_settings,'_startup_runtime_allowed',return_value=False);self.runtime.start()
        self.store=app_settings.SettingsStore(Path(self.temp.name))
        self.dialog=app_settings.SettingsDialog(self.store)
        self.service=Service();self.panel=AccountPanel(self.service,email_binding=False)
        self.dialog.set_account_panel(self.panel);self.dialog.resize(892,632);self.dialog.show();APP.processEvents()
    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();APP.processEvents()
        self.runtime.stop();self.temp.cleanup()
    def click(self,button):QTest.mouseClick(button,Qt.MouseButton.LeftButton);APP.processEvents()
    def fill_password(self,old='original-secret-123',new='new-private-secret-456',confirm=None):
        if not self.panel.password_toggle_button.isChecked():self.click(self.panel.password_toggle_button)
        self.panel.old_password_edit.setText(old);self.panel.new_password_edit.setText(new)
        self.panel.confirm_password_edit.setText(new if confirm is None else confirm)
    def wheel(self,widget,delta=-120):
        local=QPointF(widget.rect().center());global_pos=QPointF(widget.mapToGlobal(widget.rect().center()))
        event=QWheelEvent(local,global_pos,QPoint(),QPoint(0,delta),Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
        QApplication.sendEvent(widget,event);APP.processEvents()
    def test_password_confirmation_success_and_private_values(self):
        self.fill_password(confirm='different-new-secret')
        self.click(self.panel.password_change_button)
        self.assertFalse(self.service.calls);self.assertIn('不一致',self.panel.status_label.text())
        self.panel.confirm_password_edit.setText(self.panel.new_password_edit.text())
        QTest.keyClick(self.panel.confirm_password_edit,Qt.Key.Key_Return);APP.processEvents()
        self.assertEqual(self.service.calls,[('password',('original-secret-123','new-private-secret-456','new-private-secret-456'))])
        self.assertFalse(self.store.path.exists())
        self.service.status_changed.emit('error original-secret-123 new-private-secret-456')
        self.assertNotIn('secret',self.panel.status_label.text())
        self.service.password_changed.emit()
        self.assertFalse(self.panel.password_form.isVisible())
        for edit in (self.panel.old_password_edit,self.panel.new_password_edit,self.panel.confirm_password_edit):
            self.assertEqual(edit.text(),'')
        self.assertIn('其他设备',self.panel.status_label.text())
        self.fill_password();self.dialog.hide();APP.processEvents()
        self.assertEqual(self.panel.old_password_edit.text(),'');self.assertEqual(self.panel.new_password_edit.text(),'')
    def test_current_and_other_devices_can_rename_inline_enter_does_not_save(self):
        for key in ('current','other'):
            self.click(self.panel.device_rename_buttons[key])
            self.assertTrue(self.panel._rename_forms[key].isVisible())
            edit=self.panel.device_name_edits[key];edit.setText('新名称 '+key)
            QTest.keyClick(edit,Qt.Key.Key_Return);APP.processEvents()
            self.assertEqual(self.service.calls[-1],('rename',(key,'新名称 '+key)))
            self.assertFalse(self.store.path.exists());self.assertTrue(self.dialog.isVisible())
            self.service.devices_changed.emit(copy.deepcopy(self.service.devices))
            self.assertEqual(self.panel._editing_device,'')
        self.assertNotIn('current',self.panel.device_buttons)
    def test_wheel_cannot_change_any_settings_control_but_scrolls_parent(self):
        controls=[*self.dialog.findChildren(QComboBox),*self.dialog.findChildren(QSpinBox),*self.dialog.findChildren(QSlider)]
        self.assertGreaterEqual(len(controls),13)
        for focused in (False,True):
            for widget in controls:
                self.dialog.scroll.ensureWidgetVisible(widget);APP.processEvents()
                if focused:widget.setFocus()
                else:self.dialog.save_button.setFocus()
                before=widget.currentIndex() if isinstance(widget,QComboBox) else widget.value()
                self.wheel(widget)
                after=widget.currentIndex() if isinstance(widget,QComboBox) else widget.value()
                self.assertEqual(before,after,(widget.accessibleName(),focused))
        self.dialog.scroll.verticalScrollBar().setValue(0)
        self.wheel(self.dialog.theme_combo)
        self.assertGreater(self.dialog.scroll.verticalScrollBar().value(),0)
        combo=self.dialog.theme_combo;before=combo.currentIndex();combo.setFocus()
        QTest.keyClick(combo,Qt.Key.Key_Down if before==0 else Qt.Key.Key_Up)
        self.assertNotEqual(combo.currentIndex(),before)
    def test_fresh_empty_trigger_enabled_saved_opt_out_preserved(self):
        self.assertTrue(self.store.keep_empty_handle)
        self.store.update(keep_empty_handle=False)
        self.assertFalse(app_settings.SettingsStore(self.store.base).keep_empty_handle)
    def test_trigger_switch_clicks_at_both_ends_and_preserves_cancel(self):
        switch=self.dialog.trigger_visible_checkbox
        self.assertIsInstance(switch,app_settings.SettingsToggleSwitch)
        self.dialog.scroll.ensureWidgetVisible(switch);APP.processEvents()
        before=switch.isChecked()
        for x in (switch.width()-12,12):
            QTest.mouseClick(switch,Qt.MouseButton.LeftButton,pos=QPoint(x,switch.height()//2))
            APP.processEvents()
            before=not before
            self.assertEqual(switch.isChecked(),before)
        switch.setFocus(Qt.FocusReason.TabFocusReason);APP.processEvents()
        self.assertTrue(switch.hasFocus())
        self.dialog.reject();APP.processEvents()
        self.assertEqual(self.store.trigger_visible,before)
        self.dialog.show();APP.processEvents()
        self.assertEqual(switch.isChecked(),before)
        self.dialog.scroll.ensureWidgetVisible(self.dialog.startup_checkbox)
        APP.processEvents()
        startup_bottom=self.dialog.startup_checkbox.mapTo(
            self.dialog,QPoint(0,self.dialog.startup_checkbox.height())).y()
        footer_top=self.dialog.save_button.mapTo(self.dialog,QPoint()).y()
        self.assertLess(startup_bottom,footer_top)
    def test_inline_update_animation_and_real_download_progress(self):
        self.dialog.set_update_state('installing',version='1.8.1');APP.processEvents()
        bar=self.dialog.update_activity
        self.assertTrue(bar.active);self.assertIsNone(bar.fraction);self.assertTrue(bar._timer.isActive())
        initial=bar.grab().toImage();QTest.qWait(100);following=bar.grab().toImage()
        self.assertNotEqual(initial,following)
        self.dialog.set_update_state('downloading',received=25,total=100)
        self.assertEqual(bar.fraction,.25);self.assertFalse(bar._timer.isActive())
        self.dialog.set_update_state('available',version='1.8.1')
        self.assertFalse(bar.active);self.assertFalse(bar.isVisible());self.assertEqual(self.dialog.update_button.text(),'立即更新')
        self.dialog.set_update_state('verifying');self.dialog.hide();APP.processEvents()
        self.assertFalse(bar._timer.isActive())
    def test_account_and_lan_slot_layout_order_and_theme(self):
        class Lan(QWidget):
            def apply_theme(self,theme):self.theme=theme
        lan=Lan();self.dialog.set_lan_panel(lan)
        self.assertIs(self.dialog.account_slot_layout.itemAt(1).widget(),self.panel)
        self.assertIs(self.dialog.body_layout.itemAt(0).widget(),self.dialog._reference_top)
        self.assertIs(self.dialog.body_layout.itemAt(1).widget(),lan)
        self.dialog.apply_theme('dark');self.assertEqual(lan.theme,'dark')
        self.assertIs(self.dialog.set_lan_panel(None),lan);self.assertIsNone(lan.parent())
        lan.deleteLater()
    def test_password_and_rename_layouts_fit_reference_light_dark_860(self):
        self.fill_password();self.click(self.panel.device_rename_buttons['other'])
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);APP.processEvents()
            self.assertEqual(self.dialog.scroll.horizontalScrollBar().maximum(),0)
            self.assertEqual(self.panel.devices_body.objectName(),'accountDevices')
            self.assertEqual(len(self.panel.devices_body.findChildren(QWidget,'accountDeviceSeparator')),0)
            rows=list(self.panel._rows_by_id.values())
            self.assertLess(rows[0].geometry().bottom(),rows[1].geometry().top())
            for row in self.panel._rows_by_id.values():
                self.assertTrue(self.panel.devices_body.rect().contains(row.geometry()))
            for widget in self.panel.findChildren(QWidget):
                if widget.isVisible():
                    container=self.panel.password_dialog if widget.window() is self.panel.password_dialog else self.panel
                    point=widget.mapTo(container,QPoint())
                    self.assertGreaterEqual(point.x(),0)
                    self.assertLessEqual(point.x()+widget.width(),container.width())
            render=os.environ.get('FLICKDOCK_ACCOUNT_RENDER_DIR')
            if render:
                target=Path(render);target.mkdir(parents=True,exist_ok=True)
                self.panel.grab().save(str(target/f'account-v181-{theme}.png'))

    def test_reference_secondary_surfaces_alpha_owner_and_real_auth_entry(self):
        def verify_surface(dialog,width):
            APP.processEvents()
            self.assertEqual(dialog.surface.width(),width)
            self.assertTrue(dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            self.assertFalse(dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.assertIs(dialog.windowHandle().transientParent(),self.dialog.windowHandle())
            image=dialog.grab().toImage()
            self.assertEqual(image.pixelColor(0,0).alpha(),0)
            self.assertGreater(image.pixelColor(10,image.height()//2).alpha(),0)
            # The readable surface is opaque; rounded corners remain transparent.
            self.assertLess(image.pixelColor(20,20).alpha(),100)
            self.assertEqual(image.pixelColor(25,image.height()//2).alpha(),255)
        self.click(self.panel.password_toggle_button)
        verify_surface(self.panel.password_dialog,390)
        self.panel._reset_password()
        self.dialog.show_author();APP.processEvents()
        verify_surface(self.dialog.author_dialog,400)
        self.dialog.author_dialog.close()
        self.service.logout();APP.processEvents()
        self.assertTrue(self.panel.unsigned_profile_button.isVisible())
        self.assertFalse(self.panel.auth_dialog.isVisible())
        self.click(self.panel.unsigned_profile_button)
        verify_surface(self.panel.auth_dialog,390)
        self.assertTrue(self.panel.auth_page.isVisible())
        self.assertEqual(set(self.panel.auth_page.findChildren(QLineEdit)),{self.panel.username_edit,self.panel.password_edit})
        self.panel.password_edit.setText('temporary-secret')
        self.click(self.panel.auth_cancel_button)
        self.assertFalse(self.panel.auth_dialog.isVisible());self.assertEqual(self.panel.password_edit.text(),'')
        self.assertFalse(self.store.path.exists())


if __name__=='__main__':unittest.main(verbosity=2)
