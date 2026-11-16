"""Click-to-position for the real settings sliders and their linked fields."""
import unittest
import math
from PySide6.QtCore import QPoint,QPointF,QEvent,Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QStyle,QStyleOptionSlider
import app_settings
import test_html_native_workspace as w


def click_at(slider,fraction):
    option=QStyleOptionSlider();slider.initStyleOption(option)
    groove=slider.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderGroove,slider)
    handle=slider.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,slider)
    horizontal=slider.orientation()==Qt.Horizontal
    length=(groove.width()-handle.width()) if horizontal else (groove.height()-handle.height())
    coordinate=round((groove.left()+handle.width()/2 if horizontal else groove.top()+handle.height()/2)+fraction*length)
    point=QPoint(coordinate,groove.center().y()) if horizontal else QPoint(groove.center().x(),coordinate)
    QTest.mouseClick(slider,Qt.LeftButton,pos=point)
    return QStyle.sliderValueFromPosition(slider.minimum(),slider.maximum(),round(fraction*length),length,option.upsideDown)


class SliderClickTests(unittest.TestCase):
    def setUp(self):
        self.fixture=w.WorkspaceTests();self.fixture.setUp()
        self.dialog=app_settings.SettingsDialog(self.fixture.settings);self.dialog.show();w.flush()

    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();w.flush();self.fixture.tearDown()

    def test_all_visible_settings_tracks_change_their_paired_values(self):
        rows=list(self.dialog.display_trigger_editor.rows.values());self.assertTrue(rows)
        group=rows[0];group.set_expanded(True,animate=False);w.flush()
        pairs=[(self.dialog.shake_sensitivity_slider,self.dialog.shake_sensitivity_spin)]
        pairs.extend((group.sliders[key],group.controls[key]) for key in ('position','length','thickness','opacity'))
        for slider,field in pairs:
            self.dialog.scroll.ensureWidgetVisible(slider);w.flush()
            for fraction in (.1,.5,.9):
                slider.setValue(slider.maximum() if fraction<.5 else slider.minimum())
                expected=click_at(slider,fraction)
                tolerance=max(1,math.ceil((slider.maximum()-slider.minimum())/max(1,slider.width()-18)))
                self.assertLessEqual(abs(slider.value()-expected),tolerance,slider.accessibleName())
                self.assertEqual(field.value(),slider.value())
                self.assertFalse(slider.isSliderDown())

    def test_drag_keyboard_and_inverted_tracks_remain_usable(self):
        slider=app_settings.SettingsSlider(Qt.Horizontal);slider.resize(220,28);slider.setRange(0,100);slider.show();w.flush()
        try:
            for inverted in (False,True):
                slider.setInvertedAppearance(inverted)
                expected=click_at(slider,.8);self.assertLessEqual(abs(slider.value()-expected),1)
            slider.setInvertedAppearance(False);slider.setValue(20)
            option=QStyleOptionSlider();slider.initStyleOption(option)
            handle=slider.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,slider)
            start=handle.center();end=QPoint(slider.width()-1,start.y())
            QTest.mousePress(slider,Qt.LeftButton,pos=start)
            event=QMouseEvent(QEvent.MouseMove,QPointF(end),QPointF(slider.mapToGlobal(end)),Qt.NoButton,Qt.LeftButton,Qt.NoModifier)
            QApplication.sendEvent(slider,event);QTest.mouseRelease(slider,Qt.LeftButton,pos=end)
            self.assertEqual(slider.value(),100);self.assertFalse(slider.isSliderDown())
            QTest.keyClick(slider,Qt.Key_Left);self.assertEqual(slider.value(),99)
            slider.setEnabled(False);before=slider.value();QTest.mouseClick(slider,Qt.LeftButton,pos=QPoint(0,14));self.assertEqual(slider.value(),before)
        finally:slider.close();slider.deleteLater();w.flush()


if __name__=='__main__':unittest.main(verbosity=2)
