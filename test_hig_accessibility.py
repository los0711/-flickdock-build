"""Check usable keyboard, contrast and narrow-form behavior without OS changes."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from app_settings import SettingsDialog, SettingsStore
from native_ui_common import colors, configure_reference_font, icon

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
configure_reference_font(APP)


def contrast(a, b):
    def luminance(value):
        channels=[int(value[i:i+2],16)/255 for i in (1,3,5)]
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
        return sum(v*w for v,w in zip(linear,(.2126,.7152,.0722)))
    x,y=sorted((luminance(a),luminance(b)))
    return (y+.05)/(x+.05)


class HigAccessibilityTests(unittest.TestCase):
    def test_text_and_primary_actions_remain_readable_in_both_appearances(self):
        for theme in ('light','dark'):
            c=colors(theme)
            for role in ('text','secondary','muted','link','green','red'):
                self.assertGreaterEqual(contrast(c[role],c['surface']),4.5,(theme,role))
            for role in ('action','action_hover','danger_action'):
                self.assertGreaterEqual(contrast(c['on_action'],c[role]),4.5,(theme,role))

    def test_pointer_and_keyboard_focus_have_distinct_feedback(self):
        host=QWidget();layout=QVBoxLayout(host)
        first=QPushButton('first');second=QPushButton('second')
        layout.addWidget(first);layout.addWidget(second);host.show();APP.processEvents()
        second.setFocus(Qt.FocusReason.OtherFocusReason);APP.processEvents()
        first.setFocus(Qt.FocusReason.TabFocusReason);APP.processEvents()
        self.assertTrue(first.property('keyboardFocus'))
        QTest.mouseClick(second,Qt.MouseButton.LeftButton);APP.processEvents()
        self.assertFalse(second.property('keyboardFocus'))
        QTest.keyClick(second,Qt.Key.Key_Tab);APP.processEvents()
        self.assertTrue(APP.focusWidget().property('keyboardFocus'))
        host.close();host.deleteLater();APP.processEvents()

    def test_settings_reflows_without_losing_fields_or_committing_a_draft(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-hig-form-') as folder, \
             patch('app_settings._startup_runtime_allowed',return_value=False):
            store=SettingsStore(Path(folder));dialog=SettingsDialog(store)
            dialog.show();APP.processEvents()
            for width in (892,632,572,892):
                dialog.resize(width,632);QTest.qWait(30)
                self.assertLessEqual(dialog.body.minimumSizeHint().width(),dialog.scroll.viewport().width())
                self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(),0)
                self.assertIsNotNone(dialog.dock_edge_combo.parentWidget())
            original=store.dock_position
            dialog.dock_position_spin.setValue((original+23)%100)
            dialog.reject();APP.processEvents()
            self.assertEqual(store.dock_position,original)
            dialog.deleteLater();APP.processEvents()

    def test_all_shared_action_icons_render_as_vectors_at_fractional_scale(self):
        for name in ('check','close','trash','copy','user','pause','desktop','laptop','sun','moon','shelf'):
            pm=icon(name,'#515154').pixmap(21,21)
            self.assertFalse(pm.isNull(),name)
            image=pm.toImage()
            self.assertTrue(any(image.pixelColor(x,y).alpha() for x in range(21) for y in range(21)),name)


if __name__=='__main__':unittest.main(verbosity=2)
