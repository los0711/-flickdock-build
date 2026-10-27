"""Offscreen regression for shelf z-order at each reveal entry point."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class ShelfRaiseTests(unittest.TestCase):
    def test_reveal_paths_order_shelf_without_making_it_permanently_topmost(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-shelf-raise-') as folder, \
             patch.dict(os.environ, {'APPDATA': str(Path(folder) / 'profile')}), \
             patch.object(flickdock, 'motion_enabled', return_value=False), \
             patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
             patch.object(app_settings, '_read_startup_value'), \
             patch.object(app_settings, '_write_startup_value'), \
             patch.object(flickdock, 'raise_shelf_for_reveal') as native_raise, \
             patch.object(flickdock.QCursor, 'pos', return_value=QPoint(300, 260)):
            manager = flickdock.ShelfManager(APP)
            manager.timer.stop()
            manager.update_timer.stop()
            shelf = manager.shelf
            try:
                self.assertFalse(shelf.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

                # Direct shortcut and shake share _show_at, including an
                # already-visible re-reveal after another app came forward.
                shelf.show_transient(QPoint(250, 200), reason='shortcut')
                self.assertEqual(native_raise.call_count, 1)
                self.assertIs(native_raise.call_args.args[0], shelf)
                shelf.show_transient(QPoint(250, 200), reason='drag')
                self.assertEqual(native_raise.call_count, 2)

                # An expanded shelf can be summoned again without calling
                # _show_at; both shortcut and overview must raise it then.
                shelf.show_contents()
                after_expand = native_raise.call_count
                self.assertGreater(after_expand, 2)
                with patch.object(manager, '_pointer_dragging', return_value=False), \
                     patch.object(shelf, 'activateWindow'), patch.object(shelf, 'setFocus'):
                    manager.show_shortcut()
                    self.assertGreater(native_raise.call_count, after_expand)
                    after_shortcut = native_raise.call_count
                    manager.show_overview()
                    self.assertGreater(native_raise.call_count, after_shortcut)

                shelf.hide_immediately()
                before_dock = native_raise.call_count
                manager.show_docked()
                self.assertTrue(shelf.isVisible())
                self.assertGreater(native_raise.call_count, before_dock)
                self.assertFalse(shelf.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            finally:
                manager.quit()
                APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
