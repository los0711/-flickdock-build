"""Rendered and widget-based shadows remain neutral in both appearances."""
import unittest
from collections import OrderedDict
from PySide6.QtCore import QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter
from PySide6.QtWidgets import QApplication,QWidget
from surface_morph import neutral_shadow_color,paint_surface_shadow
from flickdock_motion import MotionShadow
from update_dialog import UpdateDialog
from update_notice import UpdateNotice

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)

def assert_neutral(test,color):
    test.assertEqual(color.red(),color.green());test.assertEqual(color.green(),color.blue())

class NeutralShadowTests(unittest.TestCase):
    def test_cached_painted_shadow_has_no_colour_and_preserves_alpha(self):
        original=QColor(18,31,53,75);neutral=neutral_shadow_color(original)
        assert_neutral(self,neutral);self.assertEqual(neutral.alpha(),original.alpha())
        image=QImage(140,100,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
        painter=QPainter(image);paint_surface_shadow(painter,QRectF(30,25,80,50),OrderedDict(),color=original);painter.end()
        visible=[image.pixelColor(x,y) for x in range(image.width()) for y in range(image.height()) if image.pixelColor(x,y).alpha()>0]
        self.assertTrue(visible)
        for color in visible:assert_neutral(self,color)

    def test_shelf_and_update_surfaces_use_neutral_shadows(self):
        host=QWidget();effect=MotionShadow(host);assert_neutral(self,effect.color())
        notice=UpdateNotice();assert_neutral(self,notice.surface.graphicsEffect().color())
        dialog=UpdateDialog('1.8.37')
        for theme in ('light','dark'):
            dialog.apply_theme(theme);assert_neutral(self,dialog.surface.graphicsEffect().color())
        notice.close();notice.deleteLater();dialog.close();dialog.deleteLater();host.deleteLater();APP.processEvents()

if __name__=='__main__':unittest.main(verbosity=2)
