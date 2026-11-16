"""Scroll momentum and bounded visual bounce using only offscreen widgets."""
import os, unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from unittest.mock import patch
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget
from settings_navigation import InertialWheelScroll, install_smooth_scrolling

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

def wheel(angle=0,pixels=0,phase=Qt.NoScrollPhase):
    return QWheelEvent(QPointF(10,10),QPointF(10,10),QPoint(0,pixels),QPoint(0,angle),
                       Qt.NoButton,Qt.NoModifier,phase,False)

class ScrollTests(unittest.TestCase):
    def setUp(self):
        self.area=QScrollArea();self.area.resize(320,240)
        content=QWidget();content.resize(300,1500);self.area.setWidget(content)
        self.area.show();APP.processEvents()
        self.controller=InertialWheelScroll(self.area)
        self.motion=patch('settings_navigation.motion_allowed',return_value=True);self.motion.start()

    def tearDown(self):
        self.area.hide();self.area.deleteLater();APP.processEvents();self.motion.stop()

    def test_wheel_glides_decelerates_and_reverses_without_a_jump(self):
        bar=self.area.verticalScrollBar();self.assertGreater(bar.maximum(),0)
        self.assertTrue(self.controller.wheel(wheel(-120)))
        motion=self.controller.motion;self.assertEqual(bar.value(),1)
        motion.pause();motion.setCurrentTime(40);first=bar.value()
        motion.setCurrentTime(100);second=bar.value()
        motion.setCurrentTime(200);third=bar.value()
        self.assertGreater(first,0);self.assertGreater(second,first);self.assertGreaterEqual(third,second)
        self.assertLess((third-second)/100,(second-first)/60)
        # A new wheel gesture starts at the current position, not the old target.
        self.assertTrue(self.controller.wheel(wheel(120)))
        self.assertGreaterEqual(bar.value(),third-1);self.assertEqual(round(motion.startValue()),bar.value())
        motion.setCurrentTime(motion.duration());self.assertEqual(bar.value(),0)

    def test_top_and_bottom_bounce_never_move_the_real_scrollbar(self):
        bar=self.area.verticalScrollBar()
        for at_bottom in (False,True):
            bar.setValue(bar.maximum() if at_bottom else bar.minimum())
            value=bar.value();self.assertTrue(self.controller.wheel(wheel(-120 if at_bottom else 120)))
            bounce=self.area._flickdock_boundary_bounce
            self.assertTrue(bounce.overlay.isVisible());self.assertEqual(bar.value(),value)
            self.assertLess(bounce.overlay.offset,0) if at_bottom else self.assertGreater(bounce.overlay.offset,0)
            self.assertEqual(bar.value(),value)
            bounce.release();bounce.animation.pause();bounce.animation.setCurrentTime(70)
            bounce.animation.setCurrentTime(bounce.animation.duration())
            self.assertFalse(bounce.overlay.isVisible());self.assertEqual(bar.value(),value)

    def test_native_precision_wheel_stays_native_inside_range_and_bounces_at_edge(self):
        bar=self.area.verticalScrollBar();bar.setValue(50)
        self.assertFalse(self.controller.wheel(wheel(pixels=-7)))
        self.assertEqual(self.controller.motion.state(),QAbstractAnimation.Stopped)
        bar.setValue(bar.maximum());self.assertTrue(self.controller.wheel(wheel(pixels=-7)))
        self.assertTrue(self.area._flickdock_boundary_bounce.overlay.isVisible())

    def test_nested_scroll_passes_to_a_parent_that_can_continue(self):
        outer=QScrollArea();outer.resize(350,250);holder=QWidget();holder.resize(330,1200);outer.setWidget(holder)
        self.area.setParent(holder);self.area.move(0,0);outer.show();self.area.show();APP.processEvents()
        try:
            self.area.verticalScrollBar().setValue(self.area.verticalScrollBar().maximum())
            self.assertLess(outer.verticalScrollBar().value(),outer.verticalScrollBar().maximum())
            self.assertFalse(self.controller.wheel(wheel(-120)))
            self.assertIsNone(getattr(self.area,'_flickdock_boundary_bounce',None))
        finally:self.area.setParent(None);outer.close();outer.deleteLater();APP.processEvents()

    def test_reduced_motion_keeps_boundary_fixed_without_overlay(self):
        self.area.verticalScrollBar().setValue(self.area.verticalScrollBar().maximum())
        with patch('settings_navigation.motion_allowed',return_value=False):
            self.assertTrue(self.controller.wheel(wheel(-120)))
            self.assertFalse(self.area._flickdock_boundary_bounce.overlay.isVisible())

    def test_application_router_handles_real_viewport_and_scrollbar_events(self):
        install_smooth_scrolling(APP)
        for target in (self.area.viewport(),self.area.verticalScrollBar()):
            self.area.verticalScrollBar().setValue(0)
            event=wheel(-120);QCoreApplication.sendEvent(target,event)
            routed=self.area._flickdock_wheel_scroll
            self.assertEqual(routed.motion.state(),QAbstractAnimation.Running)
            routed.motion.setCurrentTime(routed.motion.duration())
            self.assertGreater(self.area.verticalScrollBar().value(),0)
        self.area.verticalScrollBar().setValue(self.area.verticalScrollBar().maximum())
        QCoreApplication.sendEvent(self.area.viewport(),wheel(-120))
        self.assertTrue(self.area._flickdock_boundary_bounce.overlay.isVisible())

    def test_settings_anchor_and_wheel_share_the_same_bounded_bounce(self):
        import test_html_native_workspace as fixture_module
        from app_settings import SettingsDialog
        fixture=fixture_module.WorkspaceTests();fixture.setUp();dialog=SettingsDialog(fixture.settings)
        try:
            dialog.show();APP.processEvents();navigation=dialog._anchor_navigation;bar=dialog.scroll.verticalScrollBar()
            self.assertGreater(bar.maximum(),0);bar.setValue(bar.minimum())
            self.assertTrue(navigation._wheel(wheel(120)))
            bounce=dialog.scroll._flickdock_boundary_bounce
            self.assertGreater(bounce.overlay.offset,0);self.assertEqual(bar.value(),bar.minimum())
            navigation.go(1);self.assertFalse(bounce.overlay.isVisible())
            navigation.motion.setCurrentTime(navigation.motion.duration())
            self.assertEqual(dialog.category_list.currentRow(),1)
            bar.setValue(bar.maximum());self.assertTrue(navigation._wheel(wheel(-120)))
            self.assertEqual(bar.value(),bar.maximum());self.assertTrue(bounce.overlay.isVisible())
        finally:dialog.hide();dialog.deleteLater();APP.processEvents();fixture.tearDown()

    def test_real_overview_bounce_uses_composited_theme_at_both_edges(self):
        import test_html_native_workspace as w
        import ui_theme,shelf_ui_common
        from PySide6.QtCore import QRect
        fixture=w.WorkspaceTests();fixture.setUp()
        try:
            for number in range(12):
                path=fixture.base/f'scroll-{number}.dat';path.write_bytes(b'x');fixture.shelf.files['permanent'].append(str(path))
            fixture.shelf.refresh();popup=fixture.overview()
            for theme in ('light','dark'):
                fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
                fixture.shelf._tree_theme=None;fixture.shelf.refresh();w.flush()
                area=popup.scroll;bar=area.verticalScrollBar();self.assertGreater(bar.maximum(),0)
                for bottom in (False,True):
                    bar.setValue(bar.maximum() if bottom else bar.minimum());w.flush()
                    viewport=area.viewport();window=viewport.window()
                    expected=window.grab(QRect(viewport.mapTo(window,QPoint()),viewport.size())).toImage().pixelColor(0,0)
                    controller=InertialWheelScroll(area)
                    self.assertTrue(controller.wheel(wheel(-120 if bottom else 120)))
                    bounce=area._flickdock_boundary_bounce;self.assertTrue(bounce.overlay.isVisible())
                    self.assertEqual(bounce.overlay.snapshot.toImage().pixelColor(0,0),expected)
                    self.assertEqual(bounce.overlay.background,expected)
                    self.assertGreater(expected.lightness(),200) if theme=='light' else self.assertLess(expected.lightness(),80)
                    self.assertNotEqual(bounce.overlay.offset,0)
                    bounce.stop()
        finally:fixture.shelf.image_cache.close();fixture.shelf.image_cache._pool.waitForDone(5000);fixture.tearDown()

    def test_edge_stretch_tracks_input_caps_reverses_and_returns_on_end(self):
        bar=self.area.verticalScrollBar();bar.setValue(bar.minimum())
        self.assertTrue(self.controller.wheel(wheel(120)))
        bounce=self.area._flickdock_boundary_bounce;first=bounce.overlay.offset
        for _ in range(20):self.assertTrue(self.controller.wheel(wheel(120)))
        maximum=bounce.overlay.offset
        self.assertGreater(maximum,first);self.assertLessEqual(maximum,36)
        self.assertTrue(self.controller.wheel(wheel(-120)))
        self.assertLess(bounce.overlay.offset,maximum);self.assertEqual(bar.value(),bar.minimum())
        self.controller.wheel(wheel(phase=Qt.ScrollEnd))
        self.assertEqual(bounce.animation.state(),QAbstractAnimation.Running)
        bounce.animation.pause();bounce.animation.setCurrentTime(100);during=bounce.overlay.offset
        self.assertGreater(during,0);self.assertLess(during,maximum)
        self.assertTrue(self.controller.wheel(wheel(120)))
        self.assertGreater(bounce.overlay.offset,during)
        self.assertEqual(bounce.animation.state(),QAbstractAnimation.Stopped)

    def test_notched_wheel_returns_after_input_stops(self):
        from PySide6.QtTest import QTest
        self.controller.wheel(wheel(120));bounce=self.area._flickdock_boundary_bounce
        self.assertTrue(bounce.idle.isActive());self.assertEqual(bounce.animation.state(),QAbstractAnimation.Stopped)
        QTest.qWait(130)
        self.assertEqual(bounce.animation.state(),QAbstractAnimation.Running)

if __name__=='__main__':unittest.main(verbosity=2)
