"""Real Qt controls and disposable settings; no native hooks or registration."""
import os,tempfile,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit
import app_settings
from test_html_native_workspace import APP,flush
from account_panel import AccountPanel
from test_account_settings_v181 import Service

class SettingsCategoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.runtime=patch.object(app_settings,'_startup_runtime_allowed',return_value=False);self.runtime.start()
        self.store=app_settings.SettingsStore(Path(self.temp.name));self.dialog=app_settings.SettingsDialog(self.store)
        self.dialog.show();flush()
    def tearDown(self):
        self.dialog.hide();self.dialog.deleteLater();flush();self.runtime.stop();self.temp.cleanup()

    def test_sidebar_keeps_same_window_and_only_selected_cards_visible(self):
        native=self.dialog.winId();navigation=self.dialog.settings_navigation
        for key in ('sync','account','displays','general','general'):
            QTest.mouseClick(self.dialog.settings_category_buttons[key],Qt.LeftButton);flush()
            self.assertEqual(self.dialog.winId(),native);self.assertIs(navigation,self.dialog.settings_navigation)
            self.assertEqual(sum(button.isChecked() for button in self.dialog.settings_category_buttons.values()),1)
            for name,(_,cards) in self.dialog._settings_categories.items():
                self.assertTrue(all(card.isVisible()==(name==key) for card in cards),(key,name))

    def test_category_and_theme_changes_preserve_unsaved_controls(self):
        d=self.dialog;d.show_settings_category('sync');d.folder_edit.setText('fixture-unsaved-folder')
        d.clipboard_success_checkbox.setChecked(False);d.show_settings_category('general')
        d.shelf_shortcut_edit.setKeySequence('Ctrl+Alt+J');d.apply_theme('dark');d.show_settings_category('sync');flush()
        self.assertEqual(d.folder_edit.text(),'fixture-unsaved-folder');self.assertFalse(d.clipboard_success_checkbox.isChecked())
        self.assertEqual(d.shelf_shortcut_edit.keySequence().toString(),'Ctrl+Alt+J')
        self.assertNotEqual(str(self.store.capture_dir),'fixture-unsaved-folder')

    def test_every_field_is_retained_once_and_reachable_in_its_category(self):
        d=self.dialog
        expected={'general':['startup_checkbox','shelf_shortcut_edit','overview_shortcut_edit'],
                  'sync':['sync_upload_checkbox','sync_receive_checkbox','clipboard_sync_checkbox','clipboard_success_checkbox','folder_edit'],
                  'displays':['trigger_visible_checkbox','keep_empty_handle_checkbox','trigger_view_combo','display_trigger_editor','shake_sensitivity_spin'],
                  'updates':['automatic_updates_checkbox','reference_about_button']}
        for key,names in expected.items():
            d.show_settings_category(key);flush()
            for name in names:
                control=getattr(d,name);self.assertTrue(control.isVisible(),(key,name))
                self.assertTrue(d.body.isAncestorOf(control),(key,name))
                d.scroll.ensureWidgetVisible(control,4,4);flush()
                rectangle=QRect(control.mapTo(d.scroll.viewport(),QPoint()),control.size())
                self.assertTrue(d.scroll.viewport().rect().intersects(rectangle),(key,name,rectangle))

    def test_account_panel_added_later_lives_in_account_category(self):
        d=self.dialog;service=Service();service.account={'id':'fixture','username':'Roger','login_alias':'fixture'}
        panel=AccountPanel(service);d.set_account_panel(panel);flush();self.assertFalse(panel.isVisible())
        d.show_settings_category('account');flush();self.assertTrue(panel.isVisible());self.assertTrue(d.account_slot.isAncestorOf(panel))
        self.assertGreaterEqual(d.account_slot.height(),panel.minimumSizeHint().height())
        d.show_settings_category('sync');flush();self.assertFalse(panel.isVisible())

    def test_resize_keeps_navigation_left_and_footer_accessible(self):
        d=self.dialog
        for size in ((740,560),(1040,780),(820,640)):
            d.resize(*size)
            for key in d._settings_categories:
                d.show_settings_category(key);flush()
                nav=d.settings_navigation;self.assertTrue(nav.isVisible())
                self.assertLess(nav.mapTo(d,QPoint(nav.width(),0)).x(),d.scroll.mapTo(d,QPoint()).x())
                self.assertTrue(d.surface.rect().contains(QRect(d.save_button.mapTo(d.surface,QPoint()),d.save_button.size())))
                self.assertGreater(d.scroll.viewport().width(),360)

if __name__=='__main__':unittest.main()
