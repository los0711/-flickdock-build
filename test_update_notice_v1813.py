"""The automatic update offer stays inside FlickDock and uses the live version."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import unittest
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from flickdock import ShelfManager


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class FakeUpdates:
    def __init__(self):
        self.state = 'available'
        self.state_payload = {'version': '1.8.13'}
        self.activations = 0

    def activate(self):
        self.activations += 1


class FakeManager:
    _show_update_notice = ShelfManager._show_update_notice
    _start_notified_update = ShelfManager._start_notified_update
    _update_notice_state_changed = ShelfManager._update_notice_state_changed

    def __init__(self):
        self._quitting = False
        self.settings = SimpleNamespace(theme='light')
        self.updates = FakeUpdates()
        self.update_notice = None
        self.settings_opens = 0

    def show_settings(self):
        self.settings_opens += 1


class UpdateNoticeTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()

    def tearDown(self):
        if self.manager.update_notice is not None:
            self.manager.update_notice.close()
            self.manager.update_notice.deleteLater()
            APP.processEvents()

    def test_new_version_offers_inline_choice_without_activating_update(self):
        self.manager._show_update_notice('1.8.12')
        self.assertIsNone(self.manager.update_notice)
        self.manager._show_update_notice('1.8.13')
        notice = self.manager.update_notice
        self.assertTrue(notice.isVisible())
        self.assertIn('1.8.13', notice.version_label.text())
        self.assertEqual(self.manager.updates.activations, 0)
        QTest.mouseClick(notice.later_button, Qt.MouseButton.LeftButton)
        APP.processEvents()
        self.assertFalse(notice.isVisible())
        self.assertEqual(self.manager.settings_opens, 0)
        self.manager._show_update_notice('1.8.13')
        QTest.mouseClick(notice.update_button, Qt.MouseButton.LeftButton)
        APP.processEvents()
        self.assertFalse(notice.isVisible())
        self.assertEqual(self.manager.settings_opens, 1)
        self.assertEqual(self.manager.updates.activations, 1)

    def test_completed_or_quitting_update_does_not_show_stale_notice(self):
        self.manager._quitting = True
        self.manager._show_update_notice('1.8.13')
        self.assertIsNone(self.manager.update_notice)
        self.manager._quitting = False
        self.manager.updates.state = 'latest'
        self.manager._show_update_notice('1.8.13')
        self.assertIsNone(self.manager.update_notice)
        self.manager.updates.state = 'available'
        self.manager._show_update_notice('1.8.13')
        self.manager._update_notice_state_changed('downloading', {})
        self.assertFalse(self.manager.update_notice.isVisible())


if __name__ == '__main__':
    unittest.main(verbosity=2)
