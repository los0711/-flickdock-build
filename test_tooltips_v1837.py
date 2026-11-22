"""Application-wide painted tooltips with native hit testing and delay."""
import os,unittest
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QEvent,QPoint,Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton,QWidget
import native_ui_common,ui_theme
from reference_widgets import button

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)
native_ui_common.configure_reference_font(APP)

class TooltipTests(unittest.TestCase):
    def test_drawn_system_buttons_use_accessible_names_for_hover_hints(self):
        from window_chrome import _WindowButton
        window=QWidget()
        try:
            for role in ('min','max','close'):
                control=_WindowButton(window,role)
                self.assertTrue(control.accessibleName())
                self.assertEqual(ui_theme._TooltipCorners._text(control),control.accessibleName())
        finally:window.deleteLater();APP.processEvents()

    def test_labelled_control_can_opt_into_a_descriptive_hint(self):
        from ui_theme import _TooltipCorners
        control=QPushButton('自动翻译')
        control.setToolTip('复制的文字会自动翻译并写入剪贴板')
        self.assertEqual(_TooltipCorners._text(control),'')
        control.setProperty('flickdockHoverHelp',True)
        self.assertEqual(_TooltipCorners._text(control),control.toolTip())

    def test_icon_hint_waits_half_a_second_without_a_native_tooltip_event(self):
        ui_theme.apply_application(APP,'Microsoft YaHei')
        control=button('','download');control.setToolTip('下载文件');control.show();APP.processEvents()
        try:
            handler=APP._flickdock_tooltip_corners
            handler._clear()
            with patch.object(control,'underMouse',return_value=True) as hovered,patch.object(handler,'show_tip',wraps=handler.show_tip) as shown:
                APP.sendEvent(control,QEvent(QEvent.Enter))
                self.assertEqual(handler._timer.interval(),500)
                QTest.qWait(390);shown.assert_not_called()
                QTest.qWait(250);shown.assert_called_once()
                APP.sendEvent(control,QHelpEvent(QEvent.ToolTip,QPoint(2,2),QPoint(50,50)))
                QTest.qWait(100);shown.assert_called_once()
                APP.sendEvent(control,QEvent(QEvent.Leave))
                shown.reset_mock();APP.sendEvent(control,QEvent(QEvent.Enter))
                hovered.return_value=False
                APP.sendEvent(control,QEvent(QEvent.Leave));QTest.qWait(570);shown.assert_not_called()
                labelled=QPushButton('下载');labelled.setToolTip('下载文件')
                APP.sendEvent(labelled,QEvent(QEvent.Enter))
                self.assertFalse(handler._timer.isActive())
            self.assertFalse(APP.isEffectEnabled(Qt.UI_AnimateTooltip));self.assertFalse(APP.isEffectEnabled(Qt.UI_FadeTooltip))
        finally:control.hide();control.deleteLater();APP.processEvents()

    def test_hover_tip_and_copy_feedback_both_have_transparent_corners(self):
        host=QWidget();host.resize(320,180)
        controls=[QPushButton('普通按钮',host),button('','download',parent=host)]
        for i,control in enumerate(controls):control.move(20+i*90,30);control.setToolTip('打开所在文件夹')
        host.show();APP.processEvents();original=ui_theme.current_theme()
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme);ui_theme.apply_application(APP,'Microsoft YaHei')
                for control in controls:
                    ui_theme.show_tooltip_feedback(control,control.toolTip());QTest.qWait(20)
                    tip=APP._flickdock_tooltip_corners._popup
                    self.assertTrue(tip.isVisible())
                    self.assertEqual(tip._text,'打开所在文件夹')
                    self.assertTrue(tip.mask().isEmpty())
                    self.assertTrue(tip.testAttribute(Qt.WA_TranslucentBackground))
                    image=tip.grab().toImage()
                    self.assertEqual(image.pixelColor(0,0).alpha(),0)
                    self.assertEqual(image.pixelColor(image.width()//2,2).name(),native_ui_common.colors(theme)['popup_surface'].lower())
                    self.assertGreaterEqual(tip.width(),control.fontMetrics().horizontalAdvance(tip._text))
                    self.assertLessEqual(tip.height(),tip.fontMetrics().height()+14)
                    self.assertEqual(tip.font(),control.font())
                    APP._flickdock_tooltip_corners._clear()
        finally:
            host.hide();host.deleteLater();ui_theme.set_theme(original);ui_theme.apply_application(APP,'Microsoft YaHei');APP.processEvents()

if __name__=='__main__':unittest.main(verbosity=2)
