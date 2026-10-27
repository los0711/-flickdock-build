"""Offscreen check that native socket state reaches the sync fallback policy."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class EventStreamWiringTests(unittest.TestCase):
    def test_connected_and_disconnected_change_sync_poll_policy(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-events-') as folder, \
             patch.dict(os.environ, {'APPDATA': str(Path(folder) / 'profile')}), \
             patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
             patch.object(app_settings, '_read_startup_value'), \
             patch.object(app_settings, '_write_startup_value'):
            manager = flickdock.ShelfManager(APP)
            manager.timer.stop()
            try:
                self.assertFalse(manager.sync._event_stream_online)
                manager.events.socket.connected.emit()
                self.assertTrue(manager.sync._event_stream_online)
                manager.events.socket.disconnected.emit()
                self.assertFalse(manager.sync._event_stream_online)
            finally:
                manager.quit()
                APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
