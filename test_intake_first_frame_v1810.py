"""An accepted drop lands locally before cloud journaling starts (offscreen)."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QEventLoop, QMimeData, QPoint, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class IntakeFirstFrameTests(unittest.TestCase):
    def test_local_item_and_drop_pulse_precede_cloud_work(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-intake-first-') as folder:
            base = Path(folder)
            with patch.object(flickdock, 'BASE', base / 'profile'), \
                 patch.object(flickdock, 'STATE_FILE', base / 'profile' / 'shelves.json'), \
                 patch.object(flickdock, 'motion_enabled', return_value=False), \
                 patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
                 patch.object(app_settings, '_read_startup_value'), \
                 patch.object(app_settings, '_write_startup_value'):
                manager = flickdock.ShelfManager(APP)
                manager.timer.stop()
                manager.update_timer.stop()
                shelf = manager.shelf
                payload = base / 'video.mp4'
                payload.write_bytes(b'original bytes')
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(str(payload))])
                second = base / 'photo.png'
                second.write_bytes(b'second original')
                second_mime = QMimeData()
                second_mime.setUrls([QUrl.fromLocalFile(str(second))])
                events = []
                tile = shelf.zones['permanent'].icon

                def pulse(kind, finished=None):
                    events.append(('pulse', kind))

                try:
                    manager.settings.update(capture_mode='reference')
                    shelf.show_transient(QPoint(200, 200), reason='drag')
                    with patch.object(manager.sync, 'local_changed', side_effect=lambda: events.append(('cloud', 'journal'))), \
                         patch.object(tile, 'pulse', side_effect=pulse):
                        self.assertTrue(shelf.receive_mime('permanent', mime))
                        self.assertIn(str(payload), shelf.display_refs())
                        self.assertEqual(events, [('pulse', 'drop')])
                        self.assertTrue(flickdock.STATE_FILE.exists())
                        self.assertTrue(shelf.receive_mime('permanent', second_mime))
                        self.assertIn(str(second), shelf.display_refs())
                        self.assertEqual(events, [('pulse', 'drop'), ('pulse', 'drop')])

                        loop = QEventLoop()
                        QTimer.singleShot(90, loop.quit)
                        loop.exec()
                        self.assertEqual(events, [('pulse', 'drop'), ('pulse', 'drop'),
                                                  ('cloud', 'journal')])
                finally:
                    manager.quit()
                    APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
