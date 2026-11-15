"""Persisted accent drafts, reversible dialogs and existing control recoloring."""
import json,tempfile,unittest
from pathlib import Path
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog
from app_settings import SettingsStore,SettingsDialog
from native_ui_common import colors
from reference_widgets import button,refresh_tree,split_send_button
from shelf_ui_common import FONT_FAMILY
import theme_accent,ui_theme
from test_html_native_workspace import APP,flush,contrast_colors


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

    def test_draft_cancel_save_and_reset_do_not_mutate_the_global_theme(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder));dialog=SettingsDialog(store)
            try:
                dialog._set_accent_color('#0066D6')
                self.assertEqual(dialog.draft()['accent_color'],'#0066D6')
                self.assertEqual(store.accent_color,'#000000');self.assertEqual(theme_accent.current(),'#000000')
                dialog.reject();dialog.load_values();self.assertEqual(dialog._draft_accent,'#000000')
                dialog._set_accent_color('#7C3AED');dialog.save()
                self.assertEqual(store.accent_color,'#7C3AED')
                dialog.load_values();dialog.accent_reset_button.click();dialog.save()
                self.assertEqual(store.accent_color,'#000000')
            finally:dialog.hide();dialog.deleteLater();flush()

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
            finally:dialog.hide();dialog.deleteLater();flush()

    def test_existing_primary_and_split_buttons_refresh_without_stale_tokens(self):
        primary=button('test',variant='primary');frame,send,arrow=split_send_button(None,32)
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme)
                for value in ('#7C3AED','#FFFF00','#FFFFFF','#000000'):
                    theme_accent.set_color(value);ui_theme.apply_application(APP,FONT_FAMILY)
                    tokens=colors(theme)
                    self.assertGreaterEqual(contrast_colors(QColor(tokens['action']),QColor(tokens['on_action'])),4.5)
                    for control in (primary,send,arrow):
                        self.assertIn(tokens['action'],control.styleSheet());self.assertNotIn('@fd_',control.styleSheet())
                    self.assertEqual(tokens['blue'],'#0A84FF')
        finally:
            primary.deleteLater();frame.deleteLater();flush()

if __name__=='__main__':unittest.main()
