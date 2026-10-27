"""Offscreen regressions for drag validation and the screen-edge handoff."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QMimeData, QPoint, QUrl
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class TriggerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix='flickdock-trigger-v1810-')
        self.patches = [
            patch.dict(os.environ, {'APPDATA': str(Path(self.profile.name) / 'profile')}),
            patch.object(QSystemTrayIcon, 'show'),
            patch.object(QSystemTrayIcon, 'hide'),
            patch.object(app_settings, '_read_startup_value'),
            patch.object(app_settings, '_write_startup_value'),
        ]
        for item in self.patches:
            item.start()
        self.manager = flickdock.ShelfManager(APP)
        self.manager.timer.stop()

    def tearDown(self):
        self.manager.quit()
        APP.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.profile.cleanup()

    def test_validated_drag_reveals_after_delayed_native_mime_delivery(self):
        """A confirmed drag and completed shake belong to the held press.

        Recording overlays can delay DragEnter beyond the probe's original
        timeout while sustained reversals keep the MIME probe alive.
        """
        manager = self.manager
        manager.was_down = True
        manager.drag_started_inside = False
        manager._content_gesture_at = 17.5
        payload = Path(self.profile.name) / 'dragged.txt'
        payload.write_text('real content', encoding='utf-8')
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(payload))])
        with patch.object(manager, '_pointer_dragging', return_value=True), \
             patch.object(flickdock, 'native_window_move_active', return_value=False), \
             patch.object(flickdock.time, 'perf_counter', return_value=20.0):
            self.assertTrue(manager.drag_gate.arm(QPoint(320, 240)))
            self.assertTrue(manager.drag_gate._accepts(mime))
            manager.drag_gate._content_entered(QPoint(320, 240))
            APP.processEvents()
        self.assertTrue(manager.shelf.isVisible())
        self.assertEqual(manager.shelf.presentation_kind, 'drag')

    def test_edge_remains_until_shelf_is_shown(self):
        manager = self.manager
        manager.update_handles()
        self.assertTrue(manager.edge.isVisible())
        visible_during_show = []
        original_show = manager.shelf.show

        def show():
            visible_during_show.append(manager.edge.isVisible())
            original_show()

        with patch.object(manager.shelf, 'show', side_effect=show):
            manager.show_docked()
        self.assertEqual(visible_during_show, [True])
        self.assertTrue(manager.shelf.isVisible())
        self.assertFalse(manager.edge.isVisible())


if __name__ == '__main__':
    unittest.main(verbosity=2)
