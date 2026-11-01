"""Menus remain usable when animation preference changes mid-presentation."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from test_html_native_workspace import APP, flush
from flickdock import AnimatedMenu, style_menu


class MenuMotionTests(unittest.TestCase):
    def setUp(self):
        self.reduced=False
        self.policy=patch('native_ui_common.reduced_motion',side_effect=lambda:self.reduced)
        self.policy.start();self.addCleanup(self.policy.stop)
        self.menu=AnimatedMenu();style_menu(self.menu)
        self.selected=[]
        self.action=self.menu.addAction('原有操作',lambda:self.selected.append('selected'))

    def tearDown(self):
        self.menu.hide();self.menu.deleteLater();flush()

    def test_reduce_finishes_visible_menu_without_triggering_or_rebuilding_action(self):
        self.menu.popup(QPoint(40,40));flush();self.menu.fade.setCurrentTime(35)
        self.assertEqual(self.menu.fade.state(),self.menu.fade.State.Running)
        self.assertLess(self.menu.windowOpacity(),1)
        self.reduced=True;self.menu.fade.setCurrentTime(65)
        self.assertEqual(self.menu.fade.state(),self.menu.fade.State.Stopped)
        self.assertEqual(self.menu.windowOpacity(),1)
        self.assertTrue(self.menu.isVisible());self.assertEqual(self.selected,[])
        self.assertIs(self.menu.actions()[0],self.action)
        self.menu.setActiveAction(self.action)
        QTest.keyClick(self.menu,Qt.Key_Return);flush()
        self.assertEqual(self.selected,['selected']);self.assertFalse(self.menu.isVisible())

    def test_hide_stops_clock_and_reopen_under_reduction_is_immediately_usable(self):
        self.menu.popup(QPoint(40,40));flush();self.menu.fade.setCurrentTime(30)
        self.menu.hide();flush()
        self.assertEqual(self.menu.fade.state(),self.menu.fade.State.Stopped)
        self.assertEqual(self.menu.windowOpacity(),1)
        self.reduced=True;self.menu.popup(QPoint(50,50));flush()
        self.assertEqual(self.menu.fade.state(),self.menu.fade.State.Stopped)
        self.assertEqual(self.menu.windowOpacity(),1);self.assertTrue(self.menu.isVisible())
        self.assertEqual(self.selected,[])


if __name__=='__main__':unittest.main()
