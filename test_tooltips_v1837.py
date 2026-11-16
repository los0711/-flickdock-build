"""Application-wide square tooltips in both themes, with native trigger."""
import os,unittest
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QEvent,QPoint,Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton,QToolTip,QWidget
import native_ui_common,ui_theme
from reference_widgets import button

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)
native_ui_common.configure_reference_font(APP)

class TooltipTests(unittest.TestCase):
    def test_system_buttons_have_accessible_names_without_hover_hints(self):
        from window_chrome import _WindowButton
        window=QWidget()
        try:
            for role in ('min','max','close'):
                control=_WindowButton(window,role)
                self.assertTrue(control.accessibleName());self.assertFalse(control.toolTip())
        finally:window.deleteLater();APP.processEvents()

    def test_button_hint_waits_one_second_and_leaving_cancels_it(self):
        ui_theme.apply_application(APP,'Microsoft YaHei')
        control=QPushButton('下载');control.setToolTip('下载文件');control.show();APP.processEvents()
        try:
            handler=APP._flickdock_tooltip_corners
            with patch.object(control,'underMouse',return_value=True),patch.object(QToolTip,'showText') as shown:
                APP.sendEvent(control,QEvent(QEvent.Enter))
                APP.sendEvent(control,QHelpEvent(QEvent.ToolTip,QPoint(2,2),QPoint(50,50)))
                QTest.qWait(850);shown.assert_not_called()
                QTest.qWait(200);shown.assert_called_once()
                shown.reset_mock();APP.sendEvent(control,QEvent(QEvent.Enter))
                APP.sendEvent(control,QHelpEvent(QEvent.ToolTip,QPoint(2,2),QPoint(50,50)))
                APP.sendEvent(control,QEvent(QEvent.Leave));QTest.qWait(1050);shown.assert_not_called()
            self.assertFalse(APP.isEffectEnabled(Qt.UI_AnimateTooltip));self.assertFalse(APP.isEffectEnabled(Qt.UI_FadeTooltip))
        finally:control.hide();control.deleteLater();QToolTip.hideText();APP.processEvents()

    def test_buttons_share_theme_font_square_surface_and_native_tooltip_text(self):
        host=QWidget();host.resize(320,180)
        controls=[QPushButton('普通按钮',host),button('','download',parent=host)]
        for i,control in enumerate(controls):control.move(20+i*90,30);control.setToolTip('打开所在文件夹')
        host.show();APP.processEvents();original=ui_theme.current_theme()
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme);ui_theme.apply_application(APP,'Microsoft YaHei')
                self.assertIn('QToolTip{',APP.styleSheet());self.assertIn('border-radius:0px',APP.styleSheet())
                self.assertIn('background:'+native_ui_common.colors(theme)['popup_surface'],APP.styleSheet())
                for control in controls:
                    QToolTip.showText(QPoint(50,50),control.toolTip(),control);QTest.qWait(20)
                    tip=next(w for w in APP.topLevelWidgets() if w.metaObject().className()=='QTipLabel' and w.isVisible())
                    self.assertEqual(tip.text(),'打开所在文件夹')
                    self.assertTrue(tip.mask().isEmpty())
                    self.assertTrue(tip.testAttribute(Qt.WA_TranslucentBackground))
                    image=tip.grab().toImage()
                    self.assertGreater(image.pixelColor(0,0).alpha(),0,'tooltip corners should be square')
                    self.assertEqual(image.pixelColor(image.width()//2,image.height()-6).name(),native_ui_common.colors(theme)['popup_surface'].lower())
                    self.assertGreaterEqual(tip.width(),control.fontMetrics().horizontalAdvance(tip.text()))
                    self.assertLessEqual(tip.height(),tip.fontMetrics().height()+14)
                    if os.name=='nt':self.assertEqual(tip.font().family(),'Inter')
                    QToolTip.hideText();QTest.qWait(20)
        finally:
            QToolTip.hideText();host.hide();host.deleteLater();ui_theme.set_theme(original);ui_theme.apply_application(APP,'Microsoft YaHei');APP.processEvents()

if __name__=='__main__':unittest.main(verbosity=2)
