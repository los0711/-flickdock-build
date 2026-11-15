"""Automatic preferences, first-frame contrast and themed color chooser."""
import json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QThread,QTimer
from PySide6.QtTest import QTest
from PySide6.QtGui import QColor,QPalette
from PySide6.QtWidgets import QLabel,QPushButton,QWidget,QProgressBar
from app_settings import SettingsStore,SettingsDialog
from native_ui_common import colors
from reference_widgets import button,css
from shelf_ui_common import FONT_FAMILY
import theme_accent,ui_theme
from test_html_native_workspace import APP,flush,until,contrast_colors


def settle_preferences(dialog):
    dialog._preference_writer.flush()
    until(lambda:not dialog._preference_writer.busy)
    flush()


class LiveSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old_theme=ui_theme.current_theme();self.old_accent=theme_accent.current()
        self.store=SettingsStore(Path(self.temp.name));self.dialog=SettingsDialog(self.store)
        self.threads=[]
        def apply():
            self.threads.append(QThread.currentThread())
            theme_accent.set_color(self.store.accent_color);ui_theme.set_theme(self.store.theme)
            ui_theme.apply_application(APP,FONT_FAMILY);self.dialog.apply_theme(self.store.theme)
        self.apply=apply;self.store.changed.connect(apply)

    def tearDown(self):
        settle_preferences(self.dialog);self.dialog._preference_writer.close()
        self.dialog.hide();self.dialog.deleteLater();flush();self.temp.cleanup()
        theme_accent.set_color(self.old_accent);ui_theme.set_theme(self.old_theme);ui_theme.apply_application(APP,FONT_FAMILY)

    def test_all_preference_groups_apply_without_save_and_survive_close(self):
        self.dialog.show();flush();dialog=self.dialog
        self.assertFalse(any(b.text() in ('取消','保存设置') for b in dialog.footer_frame.findChildren(QPushButton)))
        dialog.theme_combo.setCurrentIndex(1);dialog._set_accent_color('#FFFF00')
        dialog.view_combo.setCurrentIndex(1);dialog.capture_mode_combo.setCurrentIndex(1)
        dialog.keep_empty_handle_checkbox.setChecked(False);dialog.startup_checkbox.setChecked(False)
        dialog.shake_sensitivity_spin.setValue(79);dialog.dock_position_spin.setValue(22)
        dialog.translation_switch.setChecked(False);dialog.clipboard_sync_checkbox.setChecked(False)
        settle_preferences(dialog)
        loaded=SettingsStore(self.store.base)
        for key,value in dict(theme='dark',accent_color='#FFFF00',view_mode='icons',capture_mode='copy',
                keep_empty_handle=False,autostart=False,shake_sensitivity=79,dock_position=22,
                translation_enabled=False,clipboard_sync_enabled=False).items():
            self.assertEqual(getattr(loaded,key),value,key)
        self.assertTrue(self.threads);self.assertTrue(all(t==APP.thread() for t in self.threads))
        dialog.close();flush();self.assertEqual(SettingsStore(self.store.base).accent_color,'#FFFF00')

    def test_slow_disk_does_not_block_ui_and_rapid_reversal_keeps_latest_value(self):
        entered=threading.Event();release=threading.Event();beats=[]
        original=self.store._commit
        def commit(*args,**kwargs):
            self.assertNotEqual(QThread.currentThread(),APP.thread())
            entered.set();release.wait(3);return original(*args,**kwargs)
        timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:beats.append(True));timer.start()
        try:
            with patch.object(self.store,'_commit',side_effect=commit):
                self.dialog._set_accent_color('#7C3AED');until(entered.is_set)
                self.dialog._set_accent_color('#0066D6');self.dialog._set_accent_color('#000000')
                until(lambda:len(beats)>=4)
                self.assertEqual(self.store.accent_color,'#000000')
                release.set();settle_preferences(self.dialog)
            self.assertEqual(SettingsStore(self.store.base).accent_color,'#000000')
        finally:release.set();timer.stop()

    def test_theme_button_finishes_feedback_before_crossfade_and_rapid_reversal(self):
        self.dialog.show();flush();dialog=self.dialog
        buttons=dialog._theme_segment._selection_buttons
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            buttons[1].click()
            self.assertTrue(buttons[1].isChecked());self.assertEqual(dialog.theme_combo.currentData(),'light')
            buttons[0].click();QTest.qWait(700);flush()
            self.assertEqual(dialog.theme_combo.currentData(),'light');self.assertIsNone(dialog._pending_theme_index)
            with patch.object(dialog._theme_transition,'change',wraps=dialog._theme_transition.change) as fade:
                buttons[1].click()
                self.assertEqual(dialog.theme_combo.currentData(),'light')
                until(lambda:dialog.theme_combo.currentData()=='dark')
                self.assertTrue(fade.called)
            settle_preferences(dialog)
            self.assertEqual(SettingsStore(self.store.base).theme,'dark')

    def test_closing_settings_commits_pending_theme_selection(self):
        self.dialog.show();flush();dialog=self.dialog
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            dialog._theme_segment._selection_buttons[1].click();dialog.close()
            settle_preferences(dialog)
        self.assertEqual(SettingsStore(self.store.base).theme,'dark')

    def test_theme_crossfade_has_intermediate_frame_without_resizing_window(self):
        self.dialog.show();flush();dialog=self.dialog;geometry=dialog.geometry()
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            dialog._theme_segment._selection_buttons[1].click()
            until(lambda:dialog.theme_combo.currentData()=='dark')
            fade=dialog._theme_transition
            self.assertIsNotNone(fade.snapshot);self.assertTrue(fade.isVisible())
            fade.animation.setCurrentTime(fade.animation.duration()//2)
            self.assertGreater(fade.alpha,0.);self.assertLess(fade.alpha,1.)
            self.assertEqual(dialog.geometry(),geometry)
            fade.finish()
        settle_preferences(dialog)

    def test_chooser_surface_labels_and_first_change_button_and_progress_contrast(self):
        primary=button('测试',variant='primary');progress=QProgressBar()
        css(progress,'QProgressBar::chunk{background:@fd_action@;}')
        try:
            for theme in ('dark','light'):
                self.dialog.theme_combo.setCurrentIndex(1 if theme=='dark' else 0);settle_preferences(self.dialog)
                for color in ('#7C3AED','#FFFF00','#FFFFFF','#000000'):
                    self.dialog._set_accent_color(color);settle_preferences(self.dialog)
                    c=colors(theme)
                    self.assertIn(c['action'],primary.styleSheet());self.assertIn(c['on_action'],primary.styleSheet())
                    self.assertIn(c['action'],progress.styleSheet())
                    self.assertEqual(c['transfer_progress_fill'],c['action'])
                    self.assertGreaterEqual(contrast_colors(QColor(c['action']),QColor(c['on_action'])),4.5)
                self.dialog.choose_accent_color();picker=self.dialog._accent_picker;flush()
                pixel=picker.grab().toImage().pixelColor(8,8)
                self.assertEqual(pixel.name().upper(),c['card'].upper())
                labels=[label for label in picker.findChildren(QLabel) if label.isVisible() and label.text()]
                self.assertTrue(labels)
                for label in labels:
                    self.assertGreaterEqual(contrast_colors(label.palette().color(QPalette.WindowText),QColor(c['card'])),4.5,label.text())
                picker.reject();flush()
        finally:primary.deleteLater();progress.deleteLater();flush()

    def test_dark_capsule_progress_stays_visible_in_light_application(self):
        from types import SimpleNamespace
        from PySide6.QtGui import QImage,QPainter
        from PySide6.QtCore import QRectF,Qt
        from intake_notice import CapsuleNotice
        ui_theme.set_theme('light')
        state=SimpleNamespace(_phase='syncing',_painted_progress=lambda:dict(received=50,total=100))
        for accent in ('#000000','#7C3AED'):
            theme_accent.set_color(accent);c=colors('dark')
            image=QImage(40,40,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
            painter=QPainter(image);CapsuleNotice._paint_indicator(state,painter,QRectF(0,0,40,40),c);painter.end()
            self.assertTrue(any(image.pixelColor(x,y)==QColor(c['action']) for x in range(40) for y in range(40)))

if __name__=='__main__':unittest.main()
