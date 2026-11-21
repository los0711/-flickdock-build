"""Offscreen window-layer contract; no desktop windows or native input hooks."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import platform_native
from test_lan_dialog_v183 import FakeLan


APP = QApplication.instance() or QApplication([])


def window_type(widget):
    return widget.windowFlags() & Qt.WindowType.WindowType_Mask


class WindowLayerTests(unittest.TestCase):
    def test_normal_shelf_level_on_both_platforms(self):
        self.assertEqual(platform_native.shelf_window_flags(platform='win32') & Qt.WindowType.WindowType_Mask,
                         Qt.WindowType.Tool)
        self.assertEqual(platform_native.shelf_window_flags(platform='darwin') & Qt.WindowType.WindowType_Mask,
                         Qt.WindowType.Window)
        for platform in ('win32', 'darwin'):
            self.assertFalse(platform_native.shelf_window_flags(platform=platform)
                             & Qt.WindowType.WindowStaysOnTopHint)

    def test_settings_and_transfer_yield_to_other_apps(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-z-order-') as folder, \
             patch.object(app_settings, '_read_startup_value'), \
             patch.object(app_settings, '_write_startup_value'):
            settings = app_settings.SettingsStore(Path(folder) / 'profile')
            dialog = app_settings.SettingsDialog(settings)
            transfer = __import__('lan_panel').LanDialog(FakeLan(), settings)
            try:
                self.assertEqual(window_type(dialog), Qt.WindowType.Dialog)
                self.assertEqual(window_type(transfer), Qt.WindowType.Dialog)
                self.assertFalse(dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
                self.assertFalse(transfer.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            finally:
                transfer.close()
                dialog.close()

    def test_shelf_returns_to_edge_and_can_reveal_again(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-z-order-') as folder, \
             patch.dict(os.environ, {'APPDATA': str(Path(folder) / 'profile')}), \
             patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
             patch.object(app_settings, '_read_startup_value'), \
             patch.object(app_settings, '_write_startup_value'):
            import flickdock
            manager = flickdock.ShelfManager(APP)
            manager.timer.stop()
            try:
                shelf = manager.shelf
                self.assertFalse(shelf.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
                self.assertTrue(manager.edge.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
                shelf.show_transient(QPoint(200, 200))
                self.assertTrue(shelf.isVisible())
                shelf.show_contents()
                self.assertTrue(shelf.expanded)
                shelf.hide_immediately()
                manager.update_handles()
                self.assertTrue(manager.edge.isVisible())
                manager.show_docked()
                self.assertTrue(shelf.isVisible())
            finally:
                manager.quit()
                APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
