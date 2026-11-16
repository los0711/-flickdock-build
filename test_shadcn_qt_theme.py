"""Offscreen checks for the shared neutral theme and interactive states."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['FLICKDOCK_REDUCE_MOTION']='1'

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from app_settings import SettingsDialog, SettingsStore, SettingsToggleSwitch
from native_ui_common import colors, configure_reference_font
from reference_widgets import button
import ui_theme

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
configure_reference_font(APP)


def contrast(first,second):
    def luminance(value):
        channels=[int(value[i:i+2],16)/255 for i in (1,3,5)]
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
        return sum(v*w for v,w in zip(linear,(.2126,.7152,.0722)))
    a,b=sorted((luminance(first),luminance(second)))
    return (b+.05)/(a+.05)


class NeutralThemeTests(unittest.TestCase):
    def test_semantic_pairs_are_readable_on_their_actual_surfaces(self):
        for theme in ('light','dark'):
            c=colors(theme)
            for foreground,background in (('text','surface'),('secondary','control'),
                    ('on_action','action'),('on_action','action_hover'),
                    ('danger_on_action','danger_action')):
                self.assertGreaterEqual(contrast(c[foreground],c[background]),4.5,
                    (theme,foreground,background))
            ui_theme.set_theme(theme)
            self.assertEqual(ui_theme.color('surface').lower(),c['surface'].lower())
            self.assertEqual(ui_theme.color('accent').lower(),c['action'].lower())

    def test_primary_button_and_switch_have_distinct_real_states(self):
        host=QWidget();layout=QHBoxLayout(host)
        action=button('保存设置',variant='primary',parent=host);action.setFixedWidth(120)
        switch=SettingsToggleSwitch('',host);switch.setFixedSize(44,32)
        layout.addWidget(action);layout.addWidget(switch);host.show();APP.processEvents()
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme);switch.dark=theme=='dark'
                from reference_widgets import refresh_tree
                refresh_tree(host);APP.processEvents()
                action.setEnabled(True);action.setDown(False);APP.processEvents()
                normal=action.grab().toImage().pixelColor(8,16)
                action.setDown(True);APP.processEvents()
                pressed=action.grab().toImage().pixelColor(8,16)
                action.setDown(False);action.setEnabled(False);APP.processEvents()
                disabled=action.grab().toImage().pixelColor(8,16)
                self.assertNotEqual(normal,pressed,theme)
                self.assertNotEqual(normal,disabled,theme)
                switch.setChecked(False);APP.processEvents();off=switch.grab().toImage()
                switch.setChecked(True);APP.processEvents();on=switch.grab().toImage()
                self.assertNotEqual(off.pixelColor(4,16),on.pixelColor(4,16),theme)
                self.assertGreater(abs(on.pixelColor(4,16).lightness()-on.pixelColor(27,16).lightness()),100,theme)
        finally:
            host.close();host.deleteLater();APP.processEvents()

    def test_translation_dropdown_tracks_the_active_theme(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-neutral-theme-') as folder, \
             patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(folder)))
            dialog.show();APP.processEvents()
            try:
                for theme in ('dark','light'):
                    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData(theme))
                    dialog.apply_theme(theme);APP.processEvents()
                    self.assertEqual(dialog.translation_language.dark,theme=='dark')
                    dialog.translation_language.showPopup();APP.processEvents()
                    popup=dialog.translation_language.view().window();image=popup.grab().toImage()
                    self.assertEqual(popup._settings_backdrop.fill.lower(),colors(theme)['popup_surface'].lower())
                    self.assertTrue(popup.mask().isEmpty());self.assertEqual(image.pixelColor(0,0).alpha(),0)
                    self.assertGreater(image.pixelColor(image.width()//2,image.height()//2).alpha(),200)
                    dialog.translation_language.hidePopup()
            finally:
                dialog.close();dialog._preference_writer.close();dialog.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
