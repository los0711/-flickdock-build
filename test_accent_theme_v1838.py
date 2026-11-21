"""Persisted accent drafts, reversible dialogs and existing control recoloring."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog,QToolTip
from app_settings import SettingsStore,SettingsDialog
from native_ui_common import colors
from reference_widgets import button,refresh_tree,split_send_button
from shelf_ui_common import FONT_FAMILY
import theme_accent,ui_theme
from test_html_native_workspace import APP,flush,contrast_colors
from test_live_settings_v1838 import settle_preferences


class AccentTests(unittest.TestCase):
    def setUp(self):
        self.previous=theme_accent.current();self.theme=ui_theme.current_theme()
        theme_accent.set_color(theme_accent.DEFAULT)

    def tearDown(self):
        theme_accent.set_color(self.previous);ui_theme.set_theme(self.theme);flush()

    def test_old_profiles_default_black_and_custom_color_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);path.joinpath('settings.json').write_text(json.dumps({'theme':'dark'}))
            store=SettingsStore(path);self.assertEqual(store.accent_color,'#000000')
            store.update(accent_color='#7c3aed');self.assertEqual(SettingsStore(path).accent_color,'#7C3AED')
            for invalid in ('red','#123','#12345678',';background:url(secret)',None):
                if invalid is None:continue
                with self.assertRaises(ValueError):store.update(accent_color=invalid)
            self.assertEqual(store.accent_color,'#7C3AED')

    def test_live_color_persists_on_close_and_reset_needs_no_save(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder));dialog=SettingsDialog(store)
            try:
                dialog._set_accent_color('#0066D6')
                self.assertEqual(dialog.draft()['accent_color'],'#0066D6')
                settle_preferences(dialog)
                self.assertEqual(store.accent_color,'#0066D6')
                dialog.reject();dialog.load_values();self.assertEqual(dialog._draft_accent,'#0066D6')
                dialog._set_accent_color('#7C3AED');settle_preferences(dialog)
                self.assertEqual(store.accent_color,'#7C3AED')
                dialog.load_values();dialog.accent_reset_button.click();settle_preferences(dialog)
                self.assertEqual(store.accent_color,'#000000')
            finally:dialog._preference_writer.close();dialog.hide();dialog.deleteLater();flush()

    def test_picker_is_single_nonblocking_and_applies_only_on_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog=SettingsDialog(SettingsStore(Path(folder)))
            try:
                dialog.choose_accent_color();picker=dialog._accent_picker
                dialog.choose_accent_color();self.assertIs(dialog._accent_picker,picker)
                picker.setCurrentColor(QColor('#22C55E'));picker.reject();flush()
                self.assertIsNone(dialog._accent_picker);self.assertEqual(dialog._draft_accent,'#000000')
                dialog.choose_accent_color();picker=dialog._accent_picker
                picker.setCurrentColor(QColor('#22C55E'));picker.accept();flush()
                self.assertEqual(dialog._draft_accent,'#22C55E')
            finally:dialog._preference_writer.close();dialog.hide();dialog.deleteLater();flush()

    def test_existing_primary_and_split_buttons_refresh_without_stale_tokens(self):
        primary=button('test',variant='primary');frame,send,arrow=split_send_button(None,32)
        primary.resize(120,32);frame.resize(160,32);primary.show();frame.show();flush()
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme)
                for value in ('#7C3AED','#FFFF00','#FFFFFF','#000000'):
                    theme_accent.set_color(value);ui_theme.apply_application(APP,FONT_FAMILY)
                    tokens=colors(theme)
                    for state in ('action','action_hover','action_pressed'):
                        self.assertGreaterEqual(contrast_colors(QColor(tokens[state]),QColor(tokens['on_action'])),4.5,(value,theme,state))
                    for control in (primary,send,arrow):
                        self.assertNotIn('@fd_',control.styleSheet())
                        with patch.object(control,'underMouse',return_value=False),patch.object(control,'isDown',return_value=False):
                            image=control.grab().toImage();scale=image.devicePixelRatio()
                        pixel=image.pixelColor(round(5*scale),round(control.height()/2*scale))
                        self.assertEqual(pixel.name().upper(),tokens['action'].upper(),(theme,value,control.text()))
                    self.assertEqual(tokens['blue'],'#0A84FF')
        finally:
            primary.deleteLater();frame.deleteLater();flush()

    def test_tooltip_changes_colors_without_replacing_global_stylesheet(self):
        sheets=[]
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme);ui_theme.apply_application(APP,FONT_FAMILY)
                sheets.append(APP.styleSheet())
                QToolTip.hideText();QToolTip.showText(QPoint(30,30),'测试提示');flush()
                tip=next(w for w in APP.topLevelWidgets() if w.metaObject().className()=='QTipLabel' and w.isVisible())
                c=colors(theme)
                self.assertIn(c['popup_surface'].lower(),tip.styleSheet().lower())
                self.assertIn(c['text'].lower(),tip.styleSheet().lower())
            self.assertEqual(sheets[0],sheets[1])
        finally:QToolTip.hideText();flush()

if __name__=='__main__':unittest.main()
