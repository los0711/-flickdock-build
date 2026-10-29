"""The automatic update offer stays inside FlickDock and uses the live version."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_html_native_workspace as workspace

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
        self.manifest={'version':'1.8.13','notes':'新增跨设备剪贴板\n修复总览与更新提示'}
        self.activations = 0

    def activate(self):
        self.activations += 1


class FakeManager:
    def _present_clipboard_intro(self):return False
    _show_update_notice = ShelfManager._show_update_notice
    _start_notified_update = ShelfManager._start_notified_update
    _update_notice_state_changed = ShelfManager._update_notice_state_changed
    _present_update_notice=ShelfManager._present_update_notice
    _snooze_update_notice=ShelfManager._snooze_update_notice

    def __init__(self,fixture):
        self._quitting = False
        self.settings=fixture.settings;self.shelf=fixture.shelf
        self.updates = FakeUpdates()
        self.update_notice = None
        self.settings_opens = 0

    def settings_open(self):return False

    def show_settings(self):
        self.settings_opens += 1


class UpdateNoticeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp()
        self.manager = FakeManager(self.fixture)

    def tearDown(self):
        if self.manager.update_notice is not None:
            self.manager.update_notice.close()
            self.manager.update_notice.deleteLater()
            APP.processEvents()
        self.fixture.tearDown()

    def test_new_version_offers_inline_choice_without_activating_update(self):
        self.manager._show_update_notice('1.8.12')
        self.assertIsNone(self.manager.update_notice)
        self.manager._show_update_notice('1.8.13')
        self.assertIsNone(self.manager.update_notice) # A preview never prompts.
        self.fixture.overview()
        workspace.until(lambda:not self.fixture.shelf.presentation._timer.isActive())
        self.manager._show_update_notice('1.8.13')
        notice = self.manager.update_notice
        self.assertTrue(notice.isVisible())
        self.assertIn('1.8.13', notice.version_label.text())
        self.assertEqual(self.manager.updates.activations, 0)
        self.assertIs(notice.parentWidget(),self.fixture.shelf)
        self.assertFalse(notice.isWindow());self.assertIn('新增跨设备剪贴板',notice.notes_label.text())
        self.assertLess((notice.surface.geometry().center()-notice.rect().center()).manhattanLength(),3)
        QTest.mouseClick(notice.later_button, Qt.MouseButton.LeftButton)
        APP.processEvents()
        self.assertFalse(notice.isVisible())
        self.assertEqual(self.manager.settings_opens, 0)
        self.manager._show_update_notice('1.8.13')
        self.assertFalse(notice.isVisible())
        with patch('flickdock.time.strftime',return_value='2099-01-01'):
            self.manager._show_update_notice('1.8.13')
        self.assertTrue(notice.isVisible())
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
        self.fixture.overview()
        workspace.until(lambda:not self.fixture.shelf.presentation._timer.isActive())
        self.manager._show_update_notice('1.8.13')
        self.manager._update_notice_state_changed('downloading', {})
        self.assertFalse(self.manager.update_notice.isVisible())

    def test_mask_blocks_toolbar_and_underlying_keyboard_and_never_follows_into_preview(self):
        popup=self.fixture.overview();workspace.until(lambda:not self.fixture.shelf.presentation._timer.isActive())
        self.manager._show_update_notice('1.8.13');notice=self.manager.update_notice
        point=notice.mapFrom(self.fixture.shelf,self.fixture.shelf.close_button.geometry().center())
        QTest.mouseClick(notice,Qt.LeftButton,pos=point);APP.processEvents()
        self.assertNotEqual(self.fixture.shelf.visible_state,'hiding')
        before=popup.search_edit.text();QTest.keyClick(popup.search_edit,Qt.Key_X);self.assertEqual(popup.search_edit.text(),before)
        QTest.keyClick(popup.search_edit,Qt.Key_Escape);APP.processEvents();self.assertFalse(notice.isVisible())
        self.fixture.shelf.hide_immediately();self.fixture.shelf.show_transient();APP.processEvents()
        self.assertFalse(notice.isVisible());self.assertFalse(self.fixture.shelf.expanded)


if __name__ == '__main__':
    unittest.main(verbosity=2)
