"""Native ordering reproductions via real Qt events, no desktop automation."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QCursor
from unittest.mock import patch
import test_clipboard_popup_motion_v1823 as popup_tests
import test_html_native_workspace as ws


class NativePopupOrdering(popup_tests.ClipboardPopupTests):
    def test_secondary_source_click_cannot_reverse_close_or_fall_through(self):
        for button in (Qt.RightButton, Qt.MiddleButton):
            with self.subTest(button=button):
                source, dialog, motion = self.open(overview=True)
                self.land(motion)
                clicks=[]; connection=source.clicked.connect(lambda:clicks.append(True))
                point=dialog.mapFromGlobal(motion.source_rect.center().toPoint())
                QTest.mouseClick(dialog, button, pos=point)
                self.assertEqual(motion.target,0.)
                motion.animation.pause()
                motion.animation.setCurrentTime(max(1,motion.animation.duration()//2))
                pose=motion.current_rect()
                QTest.mouseClick(dialog, button, pos=point)
                self.assertEqual(motion.target,0.,'Secondary button reopened the closing popup')
                self.assertEqual(motion.current_rect(),pose)
                self.land(motion)
                self.assertFalse(dialog.isVisible())
                self.assertEqual(clicks,[],'Consumed click reached the source button')
                ws.until(lambda:not motion._app_filter)
                self.assertEqual(motion._swallow_button,Qt.NoButton)
                self.assertFalse(motion._swallow_context)
                source.clicked.disconnect(connection)

    def test_left_source_click_reverses_from_same_pose_without_forwarding(self):
        source, dialog, motion = self.open(overview=True)
        self.land(motion)
        clicks=[]; source.clicked.connect(lambda:clicks.append(True))
        point=dialog.mapFromGlobal(motion.source_rect.center().toPoint())
        QTest.mouseClick(dialog,Qt.LeftButton,pos=point)
        motion.animation.pause()
        motion.animation.setCurrentTime(max(1,motion.animation.duration()//2))
        pose=motion.current_rect(); host=dialog.geometry(); native=dialog.winId()
        QTest.mousePress(dialog,Qt.LeftButton,pos=point)
        self.assertEqual(motion.target,1.)
        self.assertEqual(motion.current_rect(),pose)
        QTest.mouseRelease(dialog,Qt.LeftButton,pos=point)
        self.land(motion)
        self.assertEqual(dialog.geometry(),host);self.assertEqual(dialog.winId(),native)
        self.assertEqual(clicks,[])
        QTest.mouseClick(dialog,Qt.LeftButton,pos=point)
        self.land(motion)
        ws.until(lambda:not motion._app_filter)
        self.assertEqual(motion._swallow_button,Qt.NoButton)

    def test_source_click_landing_on_transparent_host_toggles_once(self):
        source, dialog, motion = self.open(overview=True)
        self.land(motion)
        point=dialog.mapFromGlobal(motion.source_rect.center().toPoint())
        self.assertFalse(motion.current_rect().contains(dialog.mapToGlobal(point)))
        self.assertTrue(dialog.rect().contains(point))
        QTest.mouseClick(dialog, Qt.LeftButton, pos=point)
        self.assertEqual(motion.target, 0., 'Transparent native host ate the source button click')
        self.land(motion)
        self.assertFalse(dialog.isVisible())

    def test_owner_activation_before_source_release_cannot_reopen(self):
        source, dialog, motion = self.open()
        self.land(motion)
        with patch.object(QApplication,'mouseButtons',return_value=Qt.LeftButton), \
             patch.object(QCursor,'pos',return_value=motion.source_rect.center().toPoint()):
            QApplication.setActiveWindow(self.shelf)
            ws.flush()
        QTest.mouseClick(source, Qt.LeftButton)
        self.assertEqual(motion.target, 0., 'Owner activation dismissed first, then clicked reopened')
        self.land(motion)
        self.assertFalse(dialog.isVisible())


if __name__=='__main__':
    import unittest
    unittest.main()
