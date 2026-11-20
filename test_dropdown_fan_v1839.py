"""Exercise the native dropdown spring and the compact shelf's file fan."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from shelf_ui_common import RoundedMenu, style_menu


class DropdownFanTests(unittest.TestCase):
    def test_parentless_tray_menu_can_install_the_dropdown_frame_clock(self):
        app=QApplication.instance() or QApplication([])
        menu=RoundedMenu()
        try:
            style_menu(menu)
            self.assertIs(menu._dropdown_motion.parent(),menu)
            self.assertIs(menu._dropdown_motion.height._timer.widget,menu)
        finally:
            menu.close()

    def test_popup_reopens_from_its_current_animation_and_releases_the_layer(self):
        app=QApplication.instance() or QApplication([])
        parent=QWidget()
        parent.show()
        menu=RoundedMenu(parent)
        style_menu(menu)
        for title in ('打开总览','文件互传','设置'):menu.addAction(title)
        point=parent.mapToGlobal(QPoint(20,40))
        motion=menu._dropdown_motion
        try:
            menu.popup(point)
            motion.enabled=True
            motion._open()
            QTest.qWait(100)
            self.assertEqual(len(motion.rows),3)
            self.assertTrue(motion.presentation.isVisible())
            original=motion.presentation
            menu.hide()
            QTest.qWait(40)
            before=motion.height.values['height']
            self.assertGreater(before,0)
            menu.popup(point)
            motion.enabled=True
            motion._open()
            self.assertIs(motion.presentation,original)
            self.assertAlmostEqual(motion.height.values['height'],before,places=4)
            QTest.qWait(720)
            self.assertTrue(menu.isVisible())
            self.assertFalse(original.isVisible())
            menu.hide()
            QTest.qWait(720)
            self.assertIsNone(motion.presentation)
        finally:
            parent.close()

    def test_single_card_hover_stays_clipped_inside_its_paint_area_even_while_arriving(self):
        from test_html_native_workspace import WorkspaceTests
        fixture=WorkspaceTests();fixture.setUp()
        try:
            stack=fixture.shelf.zones['permanent'].icon
            stack.set_stack(fixture.shelf.content,fixture.paths[:1])
            artwork=QRectF(0,0,192,128)
            _,rest,_,_=stack.presentation_cards()[0]
            stack._set_fan_index(0,snap=True)
            _,hover,_,_=stack.presentation_cards()[0]
            self.assertAlmostEqual(rest.mapRect(artwork).bottom()-hover.mapRect(artwork).bottom(),5.)
            self.assertGreaterEqual(hover.mapRect(artwork).top(),12.)
            stack.arrival=0.
            _,incoming,_,_=stack.presentation_cards()[0]
            self.assertGreaterEqual(incoming.mapRect(artwork).top(),3.-1e-8)
        finally:fixture.tearDown()

    def test_compact_empty_drop_is_bigger_and_clears_when_content_appears(self):
        from test_html_native_workspace import WorkspaceTests
        fixture=WorkspaceTests();fixture.setUp()
        try:
            stack=fixture.shelf.zones['permanent'].icon
            stack.set_stack(fixture.shelf.content,[])
            position=stack.empty_drop_rect().center()
            self.assertGreater(stack.empty_drop_rect().width(),176)
            self.assertGreater(stack.empty_drop_rect().height(),128)
            self.assertTrue(QRectF(stack.rect()).contains(stack.empty_drop_rect()))
            stack.set_drop_preview(True,position)
            self.assertTrue(stack.border_motion.dragging)
            stack.set_stack(fixture.shelf.content,fixture.paths[:1])
            self.assertFalse(stack.border_motion.dragging)
            stack.set_drop_preview(True,position)
            self.assertFalse(stack.border_motion.active)
            self.assertFalse(stack.border_motion.landing._frames.isActive())
        finally:fixture.tearDown()

    def test_card_context_menu_reasserts_native_owner_during_open_and_close(self):
        from unittest.mock import patch
        from PySide6.QtGui import QContextMenuEvent
        from test_html_native_workspace import WorkspaceTests, flush
        fixture=WorkspaceTests();fixture.setUp()
        try:
            shelf=fixture.shelf;zone=shelf.zones['permanent']
            local=zone.icon.geometry().center()
            self.assertFalse(shelf._native_revealed)
            with patch('platform_native.raise_owned_popup',return_value=True) as raise_popup:
                event=QContextMenuEvent(QContextMenuEvent.Mouse,local,zone.mapToGlobal(local))
                zone.contextMenuEvent(event)
                menu=shelf._more_menu
                self.assertTrue(event.isAccepted())
                self.assertTrue(menu.isVisible())
                self.assertTrue(menu.windowFlags() & Qt.WindowStaysOnTopHint)
                raise_popup.assert_called_once_with(menu,shelf)
                motion=menu._dropdown_motion
                motion.enabled=True;motion._open()
                self.assertTrue(motion.presentation.windowFlags() & Qt.WindowStaysOnTopHint)
                self.assertTrue(motion.presentation.windowFlags() & Qt.WindowTransparentForInput)
                raise_popup.assert_called_with(motion.presentation,shelf,above=menu)
                motion.height.stop();motion.alpha.stop();motion._settle()
                raise_popup.assert_called_with(menu,shelf)
                menu.close()
                raise_popup.assert_called_with(motion.presentation,shelf,above=menu)
                flush()
                self.assertFalse(shelf._menu_open)
        finally:fixture.tearDown()

    def test_compact_preview_spreads_both_ways_and_raises_the_hovered_file(self):
        from test_html_native_workspace import WorkspaceTests
        fixture=WorkspaceTests()
        fixture.setUp()
        try:
            files=[]
            for name in ('预算.txt','产品说明.pdf','交付清单.zip'):
                path=fixture.base/name
                path.write_bytes(b'preview example')
                files.append(str(path))
            stack=fixture.shelf.zones['permanent'].icon
            stack.set_stack(fixture.shelf.content,files)
            stack.spread.jump({'spread':1.})
            frames=stack.presentation_cards()
            by_file={ref:transform.map(QPointF(96,64)) for ref,transform,_,_ in frames}
            midpoint=stack.width()/2
            self.assertLess(min(point.x() for point in by_file.values()),midpoint-40)
            self.assertGreater(max(point.x() for point in by_file.values()),midpoint+40)
            ref=files[0]
            stack._set_fan_index(stack.refs.index(ref),snap=True)
            self.assertEqual(stack.presentation_cards()[-1][0],ref)
            self.assertEqual(stack._fan_lift.values[stack.refs.index(ref)],-22)
            for _,transform,_,_ in stack.presentation_cards():
                self.assertGreater(transform.mapRect(QRectF(0,0,192,128)).width(),100)
            stack._set_fan_index(None,snap=True)
            hit_area=QRectF(0,0,192,128)
            poses=stack.presentation_cards(hit_test=True)
            inverses=[transform.inverted()[0] for _,transform,_,_ in poses]
            overlap=next(QPointF(x,y) for y in range(25,165) for x in range(25,260)
                         if sum(hit_area.contains(inv.map(QPointF(x,y))) for inv in inverses)>1)
            stack.set_hover(True,overlap)
            winner=stack._fan_index
            self.assertIsNotNone(winner)
            stack._set_fan_index(winner,snap=True)
            self.assertEqual(stack.presentation_cards(hit_test=True),poses)
            stack.set_hover(True,overlap)
            self.assertEqual(stack._fan_index,winner)
        finally:
            fixture.shelf.image_cache.close()
            fixture.shelf.image_cache._pool.waitForDone(5000)
            fixture.tearDown()
