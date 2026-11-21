"""Exercise real translucent menus and their native action interactions."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint,Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget

import ui_theme
from native_ui_common import colors
from shelf_ui_common import RoundedMenu,style_menu


APP=QApplication.instance() or QApplication([])


class RoundedMenuTests(unittest.TestCase):
    def test_popup_corners_selection_and_action_in_both_themes(self):
        for theme in ('dark','light'):
            with self.subTest(theme=theme):
                ui_theme.set_theme(theme)
                owner=QWidget();owner.show();menu=RoundedMenu(owner);style_menu(menu)
                chosen=[];first=menu.addAction('第一项',lambda:chosen.append('first'))
                menu.addAction('第二项');menu.popup(QPoint(30,30));QTest.qWait(50)
                try:
                    self.assertTrue(menu.testAttribute(Qt.WA_TranslucentBackground))
                    self.assertTrue(menu.mask().isEmpty())
                    image=menu.grab().toImage()
                    self.assertEqual(image.pixelColor(0,0).alpha(),0)
                    self.assertGreater(image.pixelColor(image.width()//2,image.height()//2).alpha(),200)
                    self.assertTrue(any(0<image.pixelColor(x,y).alpha()<255 for x in range(12) for y in range(12)))
                    menu.setActiveAction(first);QTest.qWait(20);image=menu.grab().toImage()
                    rect=menu.actionGeometry(first);p=QPoint(rect.left()+12,rect.center().y())
                    actual=image.pixelColor(p.x(),p.y());expected=QColor(colors(theme)['hover'])
                    self.assertLessEqual(max(abs(a-b) for a,b in zip(actual.getRgb(),expected.getRgb())),3)
                    QTest.mouseClick(menu,Qt.LeftButton,pos=rect.center());APP.processEvents()
                    self.assertEqual(chosen,['first'])
                finally:
                    menu.close();owner.close();menu.deleteLater();owner.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main()
