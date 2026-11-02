"""Actual Qt subscriptions; disposable widgets, no system clipboard or hooks."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QWidget, QFrame
import test_html_native_workspace as ws
import test_clipboard_popup_motion_v1823 as popup_tests
from update_notice import UpdateNotice


class ClipboardSubscriptionTests(unittest.TestCase):
    setUp=popup_tests.ClipboardPopupTests.setUp
    tearDown=popup_tests.ClipboardPopupTests.tearDown
    open=popup_tests.ClipboardPopupTests.open
    land=popup_tests.ClipboardPopupTests.land

    def test_one_native_hide_has_one_cleanup_callback(self):
        source, dialog, motion=self.open()
        self.land(motion)
        with patch.object(motion, 'hidden', wraps=motion.hidden) as hidden:
            dialog.hide()
            ws.flush()
            self.assertEqual(hidden.call_count, 1)
        self.assertFalse(motion.presenting)
        self.assertFalse(motion._app_filter)
        source.click();self.land(motion)
        self.assertTrue(dialog.isVisible())


class UpdateSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.owner=QWidget();self.owner.resize(700,500)
        self.owner.card=QFrame(self.owner)
        self.owner.card.setGeometry(12,12,676,476)
        self.owner.show();ws.flush()
        self.notice=UpdateNotice(self.owner)

    def tearDown(self):
        self.notice.hide();self.owner.hide();self.owner.deleteLater();ws.flush()

    def test_hidden_notice_does_not_process_owner_geometry(self):
        with patch.object(self.notice, '_fit_surface', wraps=self.notice._fit_surface) as fit:
            self.owner.card.resize(650,450);ws.flush()
            self.assertEqual(fit.call_count,0)

    def test_visible_notice_ignores_unrelated_geometry_and_fits_owner_once(self):
        self.notice.show_version('1.8.25');ws.flush()
        unrelated=QWidget();unrelated.show();ws.flush()
        try:
            with patch.object(self.notice, '_fit_surface', wraps=self.notice._fit_surface) as fit:
                unrelated.resize(340,250);ws.flush()
                self.assertEqual(fit.call_count,0)
                self.owner.card.resize(650,450);ws.flush()
                self.assertEqual(fit.call_count,1)
                self.assertEqual(self.notice.geometry(),self.owner.card.geometry())
        finally:
            unrelated.hide();unrelated.deleteLater();ws.flush()
        self.owner.hide();ws.flush()
        self.assertFalse(self.notice.isVisible())
        self.assertFalse(self.owner.property('flickdockConfirmationActive'))
        self.owner.show();self.notice.show_version('1.8.25');ws.flush()
        self.assertTrue(self.notice.isVisible())


if __name__=='__main__':unittest.main()
