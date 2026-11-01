"""Input-only motion, temporary settings and Qt offscreen; no native hooks."""
import os,tempfile,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QEvent,Qt,QAbstractAnimation
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLineEdit,QSpinBox,QKeySequenceEdit,QToolButton
from input_focus import install_focus,install_focus_tree,focus_qss
from app_settings import SettingsDialog,SettingsStore,SettingsToggleSwitch
from account_panel import AccountGlyph,RemoveDeviceButton,PasswordEdit
from native_ui_common import UiSwitch

APP=QApplication.instance() or QApplication([])
def flush():
    for _ in range(5):APP.processEvents()


class InputMotionTests(unittest.TestCase):
    def test_pointer_focus_reverses_and_hide_stops_without_editing_content(self):
        host=QWidget();layout=QVBoxLayout(host);field=QLineEdit();other=QLineEdit();baseline=QLineEdit();layout.addWidget(field);layout.addWidget(other);layout.addWidget(baseline)
        field.setText('original input');field.setSelection(1,4);field.setModified(True)
        control=install_focus(field);self.assertIs(control,install_focus(field))
        host.setStyleSheet('QLineEdit{border:1px solid #cccccc;border-radius:8px;}'+focus_qss('', '#cccccc'))
        host.show();flush();field.setSelection(1,4);baseline.setText('original input');baseline.setSelection(1,4);baseline.setModified(True);control._frame(0.)
        with patch('native_ui_common.reduced_motion',return_value=False):
            APP.sendEvent(field,QFocusEvent(QEvent.FocusIn,Qt.MouseFocusReason));control.animation.setCurrentTime(55)
            APP.sendEvent(baseline,QFocusEvent(QEvent.FocusIn,Qt.MouseFocusReason))
            middle=control.value;self.assertGreater(middle,0);self.assertLess(middle,1)
            APP.sendEvent(field,QFocusEvent(QEvent.FocusOut,Qt.MouseFocusReason))
            APP.sendEvent(baseline,QFocusEvent(QEvent.FocusOut,Qt.MouseFocusReason))
            self.assertAlmostEqual(control.value,middle);self.assertEqual(float(control.animation.startValue()),middle)
            self.assertEqual(field.text(),'original input');self.assertEqual(field.selectedText(),baseline.selectedText());self.assertEqual(field.cursorPosition(),baseline.cursorPosition());self.assertTrue(field.isModified())
            APP.sendEvent(field,QFocusEvent(QEvent.FocusIn,Qt.TabFocusReason))
            self.assertEqual(control.value,1);self.assertEqual(control.animation.state(),QAbstractAnimation.Stopped)
            APP.sendEvent(field,QFocusEvent(QEvent.FocusOut,Qt.MouseFocusReason));host.hide();flush()
            self.assertEqual(control.value,0);self.assertEqual(control.animation.state(),QAbstractAnimation.Stopped)
        host.deleteLater();flush()

    def test_error_border_reduced_motion_and_internal_editors_remain_distinct(self):
        host=QWidget();layout=QVBoxLayout(host);line=QLineEdit();spin=QSpinBox();shortcut=QKeySequenceEdit()
        for widget in (line,spin,shortcut):layout.addWidget(widget)
        controls=install_focus_tree(host);self.assertEqual(len(controls),3)
        self.assertIs(install_focus(spin.lineEdit()),spin._input_focus_motion)
        host.show();flush();control=line._input_focus_motion
        with patch('native_ui_common.reduced_motion',return_value=True):
            APP.sendEvent(line,QFocusEvent(QEvent.FocusIn,Qt.MouseFocusReason));self.assertEqual(control.value,1)
            self.assertEqual(control.animation.state(),QAbstractAnimation.Stopped)
        line.setProperty('invalid',True);flush();self.assertFalse(line.property('focusMotion'));self.assertEqual(control.value,0)
        line.setProperty('invalid',False);line.setStyleSheet('border:1px solid #FF453A;');flush()
        self.assertTrue(control.is_error());self.assertFalse(line.property('focusMotion'))
        APP.sendEvent(line,QFocusEvent(QEvent.FocusIn,Qt.MouseFocusReason));self.assertEqual(control.value,0)
        image=line.grab().toImage();red=image.pixelColor(0,image.height()//2);self.assertGreater(red.red(),red.green()+50)
        host.hide();host.deleteLater();flush()

    def test_switches_slide_reverse_and_snap_when_hidden_or_reduced(self):
        host=QWidget();layout=QVBoxLayout(host);switch=SettingsToggleSwitch('开关');native=UiSwitch()
        layout.addWidget(switch);layout.addWidget(native);host.show();flush()
        with patch('native_ui_common.reduced_motion',return_value=False):
            switch.setChecked(True);switch._toggle_motion.animation.setCurrentTime(55)
            value=switch._toggle_motion.value;self.assertGreater(value,0);self.assertLess(value,1)
            switch.setChecked(False);self.assertEqual(float(switch._toggle_motion.animation.startValue()),value)
            native.setChecked(True);native._motion.setCurrentTime(55);self.assertGreater(native._position,0)
            host.hide();flush()
            self.assertEqual(switch._toggle_motion.animation.state(),QAbstractAnimation.Stopped)
            self.assertEqual(native._motion.state(),QAbstractAnimation.Stopped);self.assertEqual(native._position,1)
            switch.setChecked(True);self.assertEqual(switch._toggle_motion.value,1)
        host.show();flush()
        with patch('native_ui_common.reduced_motion',return_value=True):
            switch.setChecked(False);native.setChecked(False)
            self.assertEqual(switch._toggle_motion.value,0);self.assertEqual(native._position,0)
        host.hide();host.deleteLater();flush()

    def test_settings_navigation_keeps_immediate_selection_and_interruptible_plane(self):
        with tempfile.TemporaryDirectory() as root,patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(root)));dialog.show();flush()
            indicator=dialog.settings_navigation_indicator
            with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
                dialog.settings_category_buttons['sync'].click();flush();indicator.animation.setCurrentTime(55)
                current=indicator._rect
                dialog.settings_category_buttons['account'].click();flush()
                self.assertTrue(dialog.settings_category_buttons['account'].isChecked())
                self.assertEqual(indicator.animation.startValue(),current)
                self.assertEqual(sum(b.isChecked() for b in dialog.settings_category_buttons.values()),1)
                dialog.apply_theme('dark');self.assertEqual(indicator._theme,'dark')
                dialog.hide();flush();self.assertEqual(indicator.animation.state(),QAbstractAnimation.Stopped)
            dialog.deleteLater();flush()

    def test_avatar_is_achromatic_delete_red_and_password_stays_native(self):
        host=QWidget();layout=QVBoxLayout(host);avatar=AccountGlyph();delete=RemoveDeviceButton();password=PasswordEdit()
        for widget in (avatar,delete,password):layout.addWidget(widget)
        password.setText('test-value');password.setSelection(1,4);control=install_focus(password,radius=12)
        host.show();flush();image=avatar.grab().toImage()
        pixel=image.pixelColor(image.width()//2,8);self.assertEqual(pixel.red(),pixel.green())
        self.assertEqual(pixel.red(),pixel.blue())
        image=delete.grab().toImage();self.assertTrue(any(image.pixelColor(x,y).red()>image.pixelColor(x,y).green()+50 for x in range(image.width()) for y in range(image.height())))
        cursor=password.cursorPosition();selected=password.selectedText();control._frame(1.)
        eye=password.findChild(QToolButton);QTest.mouseClick(eye,Qt.LeftButton,pos=eye.rect().center())
        self.assertEqual(password.echoMode(),QLineEdit.Normal)
        self.assertEqual(password.text(),'test-value');self.assertEqual(password.cursorPosition(),cursor);self.assertEqual(password.selectedText(),selected)
        self.assertIs(control,install_focus(password));host.hide();host.deleteLater();flush()

    def test_containing_settings_theme_does_not_override_nested_account_focus_geometry(self):
        from account_panel import AccountPanel
        from test_account_settings_v181 import Service
        with tempfile.TemporaryDirectory() as root,patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(root)));panel=AccountPanel(Service())
            dialog.set_account_panel(panel);dialog.show_settings_category('account');dialog.show();flush()
            for theme in ('dark','light'):
                dialog.apply_theme(theme);flush()
                self.assertEqual(panel.password_edit._input_focus_motion.radius,12)
                self.assertEqual(panel.password_edit._input_focus_motion.theme(),theme)
            dialog.hide();dialog.deleteLater();flush()

    def test_focus_border_really_fades_and_installs_no_application_filter(self):
        host=QWidget();layout=QVBoxLayout(host);field=QLineEdit();layout.addWidget(field)
        with patch.object(APP,'installEventFilter',wraps=APP.installEventFilter) as global_filter:
            control=install_focus(field);global_filter.assert_not_called()
        host.setStyleSheet('QLineEdit{background:white;border:1px solid #cccccc;border-radius:8px;} QLineEdit:focus{border-color:#0A84FF;}'+focus_qss('', '#cccccc'))
        host.show();field.setFocus();flush();control.animation.stop();colors=[]
        for value in (0.,.5,1.):
            control._frame(value);image=field.grab().toImage();colors.append(image.pixelColor(1,image.height()//2))
        self.assertEqual(colors[0].red(),colors[0].blue())
        self.assertGreater(colors[1].blue()-colors[1].red(),0)
        self.assertGreater(colors[2].blue()-colors[2].red(),colors[1].blue()-colors[1].red())
        host.hide();host.deleteLater();flush()

    def test_settings_right_items_enter_only_on_user_category_click_and_release_effects(self):
        with tempfile.TemporaryDirectory() as root,patch('app_settings._startup_runtime_allowed',return_value=False),patch('content_entrance.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(root)));dialog.show();flush()
            entrance=dialog._settings_content_entrance
            dialog.show_settings_category('sync');flush();self.assertEqual(entrance.active_count,0)
            dialog.settings_category_buttons['general'].click();flush()
            self.assertGreater(entrance.active_count,1)
            widgets=[entry[0] for entry in entrance.entries.values()]
            dialog.settings_category_buttons['sync'].click();flush()
            self.assertTrue(all(widget.graphicsEffect() is None for widget in widgets))
            self.assertGreater(entrance.active_count,1)
            dialog.hide();flush();self.assertEqual(entrance.active_count,0)
            dialog.deleteLater();flush()

    def test_only_retention_description_has_new_cloud_note_frame_and_wraps(self):
        with tempfile.TemporaryDirectory() as root,patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(root)));dialog.show();dialog.show_settings_category('sync');flush()
            label=dialog.retention_description
            self.assertTrue(label.property('cloudRetentionNote'))
            cloud=dialog._settings_categories['sync'][1][0];self.assertIsNone(cloud.property('cloudSettingsGroup'))
            for theme in ('light','dark'):
                dialog.apply_theme(theme)
                for width,height in ((740,560),(1100,760)):
                    dialog.resize(width,height);flush()
                    self.assertGreaterEqual(label.height(),label.heightForWidth(label.width()))
                    self.assertTrue(cloud.rect().contains(label.geometry()))
            dialog.hide();dialog.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
