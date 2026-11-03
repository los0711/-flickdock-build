"""Real Qt toolbar/menu input during reversible shelf geometry transitions."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QObject,QPoint,QRect,QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMenu

import test_html_native_workspace as workspace
from shiboken6 import delete,isValid
APP,flush,until=workspace.APP,workspace.flush,workspace.until


class HeaderSequenceTests(unittest.TestCase):
    setUp=workspace.WorkspaceTests.setUp
    tearDown=workspace.WorkspaceTests.tearDown

    def enable_motion(self):
        self.manager.motion=True
        self.manager.edge=SimpleNamespace(geometry=lambda:QRect(1912,200,8,160),
                                          screen_area=lambda:QRect(0,0,1920,1080))
        self.shelf.presentation.motion=True;self.shelf.geometry_morph.motion=True
        self.shelf.presentation_kind='dock'
        self.patches.enter_context(patch('native_ui_common.reduced_motion',return_value=False))

    def choose_dock_menu(self):
        seen=[]
        def select():
            menu=QApplication.activePopupWidget()
            if not isinstance(menu,QMenu):return
            action=next(action for action in menu.actions() if action.text()=='收起到屏幕边缘')
            seen.append(action.text());menu.setActiveAction(action)
            QTest.keyClick(menu,Qt.Key_Return)
        # A watchdog prevents an unexpectedly missing action hanging the suite.
        watchdog=QTimer();watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda:QApplication.activePopupWidget().close() if QApplication.activePopupWidget() else None)
        watchdog.start(1000)
        QTimer.singleShot(30,select)
        QTest.mouseClick(self.shelf.menu_button,Qt.LeftButton)
        watchdog.stop();self.assertEqual(seen,['收起到屏幕边缘'])

    def assert_recovered(self):
        until(lambda:self.shelf.visible_state=='hidden',seconds=2)
        self.assertFalse(self.shelf._menu_open)
        self.assertFalse(self.shelf.geometry_morph.active)
        self.assertFalse(self.shelf._morph_host_active)
        self.assertFalse(self.shelf._transition_preparing)
        # Match the manager's real leave/re-enter lifecycle after explicit dock.
        self.manager.edge_suppressed=False
        self.shelf.reveal_from_dock()
        until(lambda:self.shelf.presentation._motion_done and self.shelf._toolbar_entrance.progress==1.,seconds=2)
        self.assertTrue(self.shelf.isVisible())
        self.assertEqual(self.shelf.visible_state,'floating')
        self.assertGreater(self.shelf.windowOpacity(),.99)
        for control in (self.shelf.cloud_button,self.shelf.network_status,self.shelf.pin_button,
                        self.shelf.expand_button,self.shelf.menu_button,self.shelf.close_button):
            self.assertTrue(control.isVisible(),control.objectName())
            self.assertTrue(control.isEnabled())
            self.assertGreater(getattr(control,'_entry_alpha',1),.99)
        self.assertIsNone(QApplication.activePopupWidget())

    def test_expand_then_menu_dock_during_animation_and_reopen(self):
        self.enable_motion()
        QTest.mouseClick(self.shelf.expand_button,Qt.LeftButton)
        self.assertTrue(self.shelf.expanded)
        self.choose_dock_menu();self.assert_recovered()

    def test_expand_collapse_then_menu_dock_and_reopen(self):
        self.enable_motion()
        for pause in (0,35,120,260):
            QTest.mouseClick(self.shelf.expand_button,Qt.LeftButton)
            if pause:QTest.qWait(pause)
            QTest.mouseClick(self.shelf.expand_button,Qt.LeftButton)
            self.choose_dock_menu();self.assert_recovered()

    def test_retired_qt_screen_is_rebound_before_it_can_interrupt_hide(self):
        from display_triggers import display_id
        from flickdock import DockHandle,ShelfManager
        class Screen(QObject):
            def __init__(self):super().__init__();self.setObjectName('owned-synthetic-display')
            def name(self):return self.objectName()
            def manufacturer(self):return 'Synthetic'
            def model(self):return 'Owned'
            def serialNumber(self):return '12345'
            def geometry(self):return QRect(0,0,1920,1080)
            def availableGeometry(self):return self.geometry().adjusted(0,0,0,-40)
        old,current=Screen(),Screen()
        self.enable_motion()
        with patch('flickdock.QGuiApplication.primaryScreen',return_value=current),patch('flickdock.QGuiApplication.screens',return_value=[current]):
            edge=DockHandle(self.manager,old)
            self.manager.edges={display_id(old):edge};self.manager.edge=edge
            self.manager.shelves=[self.shelf];self.manager._dock_preview_values=None
            self.manager.settings_open=lambda:False
            self.manager.update_handles=lambda:ShelfManager.update_handles(self.manager)
            self.manager._refresh_displays=lambda:ShelfManager._refresh_displays(self.manager)
            try:
                # Retire an owned QObject proxy, never a real system QScreen.
                # The OS now exposes the same display through a live Qt object.
                delete(old);self.assertFalse(isValid(old))
                self.shelf.hide_animated(explicit=True)
                until(lambda:self.shelf.visible_state=='hidden',seconds=2)
                self.assertIs(edge.display,current)
                self.manager.edge_suppressed=False
                self.shelf.reveal_from_dock()
                until(lambda:self.shelf.presentation._motion_done,seconds=2)
                self.assertTrue(self.shelf.isVisible())
                self.assertTrue(self.shelf.menu_button.isVisible())
                self.choose_dock_menu()
                until(lambda:self.shelf.visible_state=='hidden',seconds=2)
            finally:
                self.manager.update_handles=lambda:None
                edge.hide();edge.deleteLater();current.deleteLater();flush()


if __name__=='__main__':unittest.main()
