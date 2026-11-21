"""Magnetic edge glow: appears only for real drags, drops silently, fades out."""
import os, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QMimeData, QPoint, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
import app_settings, flickdock
from edge_absorb import EdgeAbsorb, CONFIG

APP = QApplication.instance() or QApplication([])


def pump(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents(); time.sleep(0.01)


class EdgeAbsorbTests(unittest.TestCase):
    def test_glow_follows_pointer_and_fades_out(self):
        absorb = EdgeAbsorb(None)
        area = QGuiApplication.primaryScreen().availableGeometry()
        point = QPoint(area.right() - 30, area.center().y())
        self.assertTrue(absorb.armed(point))
        absorb.follow(point)
        self.assertIn(absorb.state, ('approaching', 'active'))
        self.assertTrue(absorb.overlay.isVisible())
        pump(0.35)
        self.assertGreater(len(absorb._particles), 0)
        self.assertGreater(absorb.intensity, 0.05)
        absorb.retreat()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and absorb.state != 'idle':
            APP.processEvents(); time.sleep(0.01)
        self.assertEqual(absorb.state, 'idle')
        self.assertFalse(absorb.overlay.isVisible())
        self.assertEqual(absorb._particles, [])
        absorb.close()

    def test_border_drop_shelves_without_opening(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-edge-') as folder:
            base = Path(folder)
            with patch.dict(os.environ, {'APPDATA': str(base / 'profile')}), \
                 patch.object(flickdock, 'BASE', base / 'profile' / 'FlickDock'), \
                 patch.object(flickdock, 'STATE_FILE', base / 'profile' / 'FlickDock' / 'shelves.json'), \
                 patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
                 patch.object(app_settings, '_read_startup_value'), \
                 patch.object(app_settings, '_write_startup_value'):
                manager = flickdock.ShelfManager(APP)
                manager.timer.stop(); manager.update_timer.stop()
                shelf = manager.shelf
                shelf.hide_immediately(); APP.processEvents()
                self.assertFalse(shelf.isVisible())
                try:
                    sample = base / 'edge-drop.txt'
                    sample.write_text('edge payload', encoding='utf-8')
                    area = QGuiApplication.primaryScreen().availableGeometry()
                    point = QPoint(area.right() - 12, area.center().y())
                    mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(sample))])
                    self.assertTrue(manager.edge_absorb.armed(point))
                    self.assertTrue(manager._probe_drop(mime, point))
                    self.assertEqual(manager.edge_absorb.state, 'dropping')
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline and not any(
                            Path(str(ref)).name == 'edge-drop.txt' for ref in shelf.files['permanent']):
                        APP.processEvents(); time.sleep(0.02)
                    self.assertTrue(any(Path(str(ref)).name == 'edge-drop.txt'
                                        for ref in shelf.files['permanent']))
                    self.assertFalse(shelf.isVisible())
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline and manager.edge_absorb.state != 'idle':
                        APP.processEvents(); time.sleep(0.02)
                    self.assertEqual(manager.edge_absorb.state, 'idle')
                    self.assertFalse(manager.edge_absorb.overlay.isVisible())
                finally:
                    manager.quit()


if __name__ == '__main__':
    unittest.main()
