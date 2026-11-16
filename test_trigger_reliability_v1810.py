"""Offscreen regressions for drag validation and the screen-edge handoff."""
import shelf_window
import os
from itertools import product
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_import_profile = tempfile.TemporaryDirectory(prefix='flickdock-trigger-import-')
os.environ['APPDATA'] = _import_profile.name

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class TriggerReliabilityTests(unittest.TestCase):
    def test_edge_hover_recovers_after_closing_animated_settings_with_account_panel(self):
        from account_panel import AccountPanel
        from test_account_settings_v181 import Service
        from ui_transitions import WindowFade
        manager=self.manager
        dialog=app_settings.SettingsDialog(manager.settings);manager.settings_dialog=dialog
        dialog.preview_changed.connect(manager.preview_dock);dialog.finished.connect(manager._settings_closed)
        dialog.set_account_panel(AccountPanel(Service(),dialog));dialog.select_category('账号与设备')
        def settle(predicate):
            deadline=time.monotonic()+3
            while not predicate() and time.monotonic()<deadline:APP.processEvents();QTest.qWait(5)
            self.assertTrue(predicate())
        with patch.object(WindowFade,'enabled',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            # Reproduce a window without a first paint, as when settings is
            # occluded. This also avoids relying on offscreen native exposure.
            dialog.setUpdatesEnabled(False);dialog.show();APP.processEvents()
            self.assertTrue(dialog._window_fade._awaiting_capture)
            self.assertIsNotNone(manager._dock_preview_values)
            dialog.close();dialog._window_fade.animation._frames.stop()
            settle(lambda:not dialog.isVisible());dialog.setUpdatesEnabled(True)
        self.assertIsNone(manager._dock_preview_values);self.assertFalse(manager.settings_open())
        manager.edge_suppressed=True
        with patch.object(flickdock.QCursor,'pos',return_value=manager.edge.activation_region().center()+QPoint(-100,0)),patch.object(flickdock,'native_input_available',return_value=False):manager.poll()
        self.assertFalse(manager.edge_suppressed)
        with patch.object(flickdock.QCursor,'pos',return_value=manager.edge.activation_region().center()),patch.object(flickdock,'native_input_available',return_value=False):manager.poll()
        self.assertTrue(manager.shelf.isVisible())
        self.assertEqual(manager.shelf.presentation_kind,'dock')

    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix='flickdock-trigger-v1810-')
        self.patches = [
            patch.dict(os.environ, {'APPDATA': str(Path(self.profile.name) / 'profile')}),
            patch.object(flickdock, 'BASE', Path(self.profile.name) / 'state'),
            patch.object(flickdock, 'STATE_FILE', Path(self.profile.name) / 'state' / 'shelves.json'),
            patch.object(flickdock, 'OLD_STATE_FILE', Path(self.profile.name) / 'old' / 'shelves.json'),
            patch.object(QSystemTrayIcon, 'show'),
            patch.object(QSystemTrayIcon, 'hide'),
            patch.object(app_settings, '_read_startup_value'),
            patch.object(app_settings, '_write_startup_value'),
        ]
        for item in self.patches:
            item.start()
        self.manager = flickdock.ShelfManager(APP)
        self.manager.timer.stop()

    def tearDown(self):
        self.manager.quit()
        APP.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.profile.cleanup()

    def test_validated_drag_reveals_after_delayed_native_mime_delivery(self):
        """A confirmed drag and completed shake belong to the held press.

        Recording overlays can delay DragEnter beyond the probe's original
        timeout while sustained reversals keep the MIME probe alive.
        """
        manager = self.manager
        manager.was_down = True
        manager.drag_started_inside = False
        manager._content_gesture_at = 17.5
        payload = Path(self.profile.name) / 'dragged.txt'
        payload.write_text('real content', encoding='utf-8')
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(payload))])
        with patch.object(manager, '_pointer_dragging', return_value=True), \
             patch.object(flickdock, 'native_window_move_active', return_value=False), \
             patch.object(flickdock.time, 'perf_counter', return_value=20.0):
            self.assertTrue(manager.drag_gate.arm(QPoint(320, 240)))
            self.assertTrue(manager.drag_gate._accepts(mime))
            manager.drag_gate._content_entered(QPoint(320, 240))
            APP.processEvents()
        self.assertTrue(manager.shelf.isVisible())
        self.assertEqual(manager.shelf.presentation_kind, 'drag')

    def test_edge_remains_until_shelf_is_shown(self):
        manager = self.manager
        manager.update_handles()
        self.assertTrue(manager.edge.isVisible())
        visible_during_show = []
        original_show = manager.shelf.show

        def show():
            visible_during_show.append(manager.edge.isVisible())
            original_show()

        with patch.object(manager.shelf, 'show', side_effect=show):
            manager.show_docked()
        self.assertEqual(visible_during_show, [True])
        self.assertTrue(manager.shelf.isVisible())
        self.assertFalse(manager.edge.isVisible())

    def test_shake_keeps_empty_trigger_and_edge_drag_hands_off_the_same_shelf(self):
        manager=self.manager;manager.motion=False;manager.settings.update(trigger_view='compact')
        shelf=manager.shelf;shelf.show_transient(QPoint(160,180),reason='drag');manager.auto_shown=shelf
        manager.update_handles();self.assertTrue(manager.edge.isVisible())
        for control in (shelf.lan_button,shelf.zones['permanent'].clipboard_button):
            self.assertTrue(control.isVisible())
        for control in (shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button):
            self.assertFalse(control.isVisible())
        source=Path(self.profile.name)/'retained.txt';source.write_bytes(b'original file')
        shelf.add_paths('permanent',[str(source)]);before=list(shelf.files['permanent'])
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))])
        event=QDragEnterEvent(manager.edge.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
        manager.edge.dragEnterEvent(event);APP.processEvents()
        self.assertTrue(event.isAccepted());self.assertIs(manager.shelf,shelf)
        self.assertEqual(shelf.presentation_kind,'dock');self.assertIsNone(manager.auto_shown)
        from test_html_native_workspace import until
        until(lambda:not shelf.geometry_morph.active)
        self.assertEqual(shelf.visual_geometry().topLeft(),shelf._dock_target_rect().topLeft())
        self.assertEqual(shelf.files['permanent'],before);self.assertEqual(source.read_bytes(),b'original file')

    def test_edge_preview_recovers_controls_and_hides_after_pointer_leaves(self):
        manager=self.manager;manager.motion=True;manager.settings.update(trigger_view='compact')
        shelf=manager.shelf;shelf.pinned=False;shelf.presentation.motion=True
        shelf.winId();handle=shelf.windowHandle();manager.edge_suppressed=False
        with patch.object(handle,'isExposed',return_value=False),\
             patch('native_ui_common.reduced_motion',return_value=False),\
             patch.object(flickdock,'native_input_available',return_value=False),\
             patch.object(flickdock,'mouse_buttons_mask',return_value=0):
            manager.show_docked();QTest.qWait(750);APP.processEvents()
            self.assertEqual(shelf.presentation_kind,'dock')
            controls=(shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button)
            self.assertTrue(all(control.isVisible() and getattr(control,'_entry_alpha',1.)==1. for control in controls))
            outside=QPoint(shelf.visual_geometry().left()-300,shelf.visual_geometry().top()-300)
            with patch.object(flickdock.QCursor,'pos',return_value=outside):manager.poll()
            self.assertEqual(shelf.visible_state,'hiding')
            QTest.qWait(750);APP.processEvents()
            self.assertFalse(shelf.isVisible());self.assertEqual(shelf.visible_state,'hidden')

    def test_shake_then_edge_restores_controls(self):
        manager=self.manager;manager.motion=True;manager.settings.update(trigger_view='compact')
        shelf=manager.shelf;shelf.pinned=False;shelf.presentation.motion=True
        shelf.show_transient(QPoint(160,180),reason='drag');manager.auto_shown=shelf
        controls=(shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button)
        self.assertTrue(all(not control.isVisible() for control in controls))
        shelf.reveal_from_dock();QTest.qWait(750);APP.processEvents()
        self.assertEqual(shelf.presentation_kind,'dock');self.assertIsNone(manager.auto_shown)
        self.assertTrue(all(control.isVisible() and getattr(control,'_entry_alpha',1.)==1. for control in controls))

    def test_shake_preview_hides_after_pointer_is_outside(self):
        manager=self.manager;manager.motion=True
        shelf=manager.shelf;shelf.pinned=False;shelf.presentation.motion=True
        shelf.winId();handle=shelf.windowHandle()
        with patch.object(handle,'isExposed',return_value=False),\
             patch('native_ui_common.reduced_motion',return_value=False),\
             patch.object(flickdock,'native_input_available',return_value=False),\
             patch.object(flickdock,'mouse_buttons_mask',return_value=0):
            shelf.show_transient(QPoint(160,180),reason='drag');manager.auto_shown=shelf
            shelf._shake_outside_since=time.monotonic()-4
            outside=QPoint(shelf.visual_geometry().right()+200,shelf.visual_geometry().bottom()+200)
            with patch.object(flickdock.QCursor,'pos',return_value=outside):manager.poll()
            self.assertEqual(shelf.visible_state,'hiding')
            QTest.qWait(750);APP.processEvents()
            self.assertFalse(shelf.isVisible());self.assertIsNone(manager.auto_shown)

    def test_external_browser_press_retires_obscured_shelf_with_lan_still_open(self):
        manager = self.manager
        manager.shelf.show_transient(QPoint(160, 180))
        manager.show_lan_dialog()
        self.assertTrue(manager.lan_dialog.isVisible())
        self.assertIsNone(manager.lan_dialog.parent())
        manager._app_was_active = True
        browser_point = manager.lan_dialog.frameGeometry().center()
        with patch.object(QApplication, 'applicationState',
                          return_value=Qt.ApplicationState.ApplicationInactive), \
             patch.object(flickdock.QCursor, 'pos', return_value=browser_point), \
             patch.object(flickdock, 'mouse_buttons_mask', return_value=1), \
             patch.object(flickdock, 'native_input_available', return_value=True), \
             patch.object(flickdock, 'native_window_move_active', return_value=False), \
             patch.object(manager.shake_detector, 'update', return_value=True), \
             patch.object(manager._drag_probe_detector, 'update', return_value=False), \
             patch.object(manager.drag_gate, 'sustain_for_shake') as probe:
            manager.poll()
            # A genuine shake while the shelf is closing lands that hide
            # immediately so the drag reveal can be prepared in the same poll.
            self.assertEqual(manager.shelf.visible_state,'hidden')
        self.assertFalse(manager.shelf.isVisible(), 'an obscured shelf must release the edge immediately')
        self.assertTrue(manager.edge.isVisible())
        self.assertTrue(manager.lan_dialog.isVisible(), 'LAN transfer remains independently usable')
        probe.assert_called_once_with(browser_point)

    def test_external_press_above_overview_keeps_animated_exit_when_app_deactivates(self):
        manager=self.manager;shelf=manager.shelf
        manager.show_overview();shelf.geometry_morph.stop()
        shelf.presentation.motion=True;manager.motion=True
        manager._app_was_active=True;manager._button_mask=0
        outside=QPoint(shelf.visual_geometry().center().x(),shelf.visual_geometry().top()-40)
        with patch.object(QApplication,'applicationState',return_value=Qt.ApplicationState.ApplicationInactive), \
             patch.object(flickdock.QCursor,'pos',return_value=outside), \
             patch.object(flickdock,'mouse_buttons_mask',return_value=1), \
             patch.object(flickdock,'native_input_available',return_value=False):
            manager.poll()
        self.assertTrue(shelf.isVisible())
        self.assertEqual(shelf.visible_state,'hiding')
        self.assertEqual(shelf.presentation._targets['alpha'],0)
        from test_html_native_workspace import until
        until(lambda:not shelf.isVisible())
        self.assertTrue(manager.edge.isVisible())

    def test_opened_file_can_hide_before_close_finishes_without_losing_edge(self):
        manager=self.manager;shelf=manager.shelf
        manager.settings.update(capture_mode='reference',keep_empty_handle=True)
        manager.motion=True;shelf.presentation.motion=True;shelf.pinned=False
        path=Path(self.profile.name)/'document.txt';path.write_text('fixture',encoding='utf-8')
        shelf.add_paths('permanent',[str(path)])
        manager.show_overview();shelf.geometry_morph.complete();APP.processEvents()
        # Offscreen screens can choose the compact overview layout.
        shelf.popup._narrow_preview=True
        shelf.popup.select_item(shelf.files['permanent'][0]);APP.processEvents()
        self.assertTrue(shelf.popup.preview.open_button.isEnabled())
        with patch.object(QDesktopServices,'openUrl',return_value=True) as opened:
            shelf.popup.preview.open_button.click()
        opened.assert_called_once()
        manager._app_was_active=True
        outside=QPoint(shelf.visual_geometry().left()-100,shelf.visual_geometry().top()-100)
        with patch.object(QApplication,'applicationState',return_value=Qt.ApplicationState.ApplicationInactive), \
             patch.object(flickdock.QCursor,'pos',return_value=outside), \
             patch.object(flickdock,'mouse_buttons_mask',return_value=0), \
             patch.object(flickdock,'native_input_available',return_value=False):
            manager.poll()
            self.assertEqual(shelf.visible_state,'hiding')
            self.assertFalse(manager.edge.isVisible())
            # The native host may already be hidden while its close deadline
            # is still outstanding after focus moves to another application.
            shelf.windowHandle().hide()
            QTest.qWait(600);APP.processEvents()
            self.assertEqual(shelf.visible_state,'hidden')
            self.assertTrue(manager.edge.isVisible())
            manager.poll()
        with patch.object(flickdock.QCursor,'pos',return_value=manager.edge.activation_region().center()), \
             patch.object(flickdock,'mouse_buttons_mask',return_value=0), \
             patch.object(flickdock,'native_input_available',return_value=False):manager.poll()
        self.assertTrue(shelf.isVisible())
        self.assertEqual(shelf.presentation_kind,'dock')

    def test_opened_file_hidden_before_deactivation_restores_edge_and_tray(self):
        manager=self.manager;shelf=manager.shelf
        manager.settings.update(capture_mode='reference',keep_empty_handle=True)
        shelf.pinned=False
        path=Path(self.profile.name)/'document.txt';path.write_text('fixture',encoding='utf-8')
        shelf.add_paths('permanent',[str(path)])
        manager.show_overview();shelf.geometry_morph.complete();APP.processEvents()
        shelf.popup._narrow_preview=True
        shelf.popup.select_item(shelf.files['permanent'][0]);APP.processEvents()
        with patch.object(QDesktopServices,'openUrl',side_effect=lambda _url:(shelf.windowHandle().hide() or True)) as opened:
            shelf.popup.preview.open_button.click()
        opened.assert_called_once()
        self.assertFalse(shelf.isVisible())
        self.assertFalse(manager.edge.isVisible())
        manager._app_was_active=True
        outside=QPoint(shelf.visual_geometry().left()-100,shelf.visual_geometry().top()-100)
        with patch.object(QApplication,'applicationState',return_value=Qt.ApplicationState.ApplicationInactive), \
             patch.object(flickdock.QCursor,'pos',return_value=outside), \
             patch.object(flickdock,'mouse_buttons_mask',return_value=0), \
             patch.object(flickdock,'native_input_available',return_value=False):manager.poll()
        self.assertEqual(shelf.visible_state,'hidden')
        self.assertTrue(manager.edge.isVisible())
        manager.tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertTrue(shelf.isVisible())
        shelf.hide_animated(explicit=True)
        QTest.qWait(600);APP.processEvents()
        self.assertEqual(shelf.visible_state,'hidden')
        self.assertTrue(manager.edge.isVisible())

    def test_overview_button_interactions_can_close_and_restore_edge(self):
        manager=self.manager;shelf=manager.shelf
        manager.settings.update(capture_mode='reference',keep_empty_handle=True)
        manager.motion=True;shelf.presentation.motion=True;shelf.pinned=False
        path=Path(self.profile.name)/'document.txt';path.write_text('fixture',encoding='utf-8')
        shelf.add_paths('permanent',[str(path)])
        names=('sort','grid','list','select-all','copy-path','open','category','pin')
        for name,native_hide in product(names,(False,True)):
            with self.subTest(action=name,native_hide=native_hide):
                manager.edge_suppressed=False
                shelf.pinned=False;shelf.pin_button.setChecked(False)
                manager.show_overview();shelf.geometry_morph.complete();APP.processEvents()
                shelf.popup._narrow_preview=True
                shelf.popup.select_item(shelf.files['permanent'][0]);APP.processEvents()
                controls={'sort':shelf.popup.sort_buttons['time'],'grid':shelf.popup.view_grid,
                          'list':shelf.popup.view_list,'select-all':shelf.popup.select_all_button,
                          'copy-path':shelf.popup.preview.copy_path_button,'open':shelf.popup.preview.open_button,
                          'category':shelf.popup.sidebar_buttons['images'],'pin':shelf.pin_button}
                control=controls[name]
                self.assertTrue(control.isEnabled(),name)
                with patch.object(QDesktopServices,'openUrl',return_value=True):
                    QTest.mouseClick(control,Qt.LeftButton);APP.processEvents()
                # Close immediately while the clicked control can still be
                # animating; the edge handoff must not depend on that effect.
                QTest.mouseClick(shelf.close_button,Qt.LeftButton)
                if native_hide:
                    self.assertEqual(shelf.visible_state,'hiding',name)
                    shelf.windowHandle().hide()
                QTest.qWait(600);APP.processEvents()
                self.assertEqual(shelf.visible_state,'hidden',name)
                self.assertFalse(shelf.isVisible(),name)
                self.assertTrue(manager.edge.isVisible(),name)
                with patch.object(flickdock.QCursor,'pos',return_value=QPoint(10,10)), \
                     patch.object(flickdock,'mouse_buttons_mask',return_value=0), \
                     patch.object(flickdock,'native_input_available',return_value=False):manager.poll()
                with patch.object(flickdock.QCursor,'pos',return_value=manager.edge.activation_region().center()), \
                     patch.object(flickdock,'mouse_buttons_mask',return_value=0), \
                     patch.object(flickdock,'native_input_available',return_value=False):manager.poll()
                self.assertTrue(shelf.isVisible(),name)
                self.assertEqual(shelf.presentation_kind,'dock',name)

    def test_edge_reveal_works_while_lan_is_open_and_after_it_closes(self):
        manager = self.manager
        manager.show_lan_dialog()
        for lan_open in (True, False):
            if not lan_open:
                manager.shelf.hide_immediately()
                manager.update_handles()
                manager.lan_dialog.close()
            with patch.object(shelf_window, 'raise_shelf_for_reveal', return_value=True) as native_raise:
                manager.show_docked()
                self.assertTrue(manager.shelf.isVisible())
                self.assertFalse(manager.edge.isVisible())
                native_raise.assert_called()
            manager.shelf.hide_immediately()
            manager.update_handles()
            self.assertTrue(manager.edge.isVisible())

    def test_video_open_releases_shelf_native_level_before_raising_the_independent_window(self):
        from video_panel import VideoDialog
        manager=self.manager;manager.show_overview();manager.shelf._native_revealed=True
        order=[];show=VideoDialog.show
        def open_window(dialog):order.append('show');return show(dialog)
        with patch.object(shelf_window,'lower_shelf_after_reveal',side_effect=lambda shelf:order.append('lower') or True), \
             patch.object(VideoDialog,'show',open_window),patch.object(VideoDialog,'raise_') as raised:
            manager.show_video_dialog()
            self.assertEqual(order[:2],['lower','show'])
            self.assertFalse(manager.shelf._native_revealed)
            self.assertFalse(manager.video_dialog.windowFlags()&Qt.WindowStaysOnTopHint)
            self.assertIsNone(manager.video_dialog.parentWidget())
            manager.show_overview()
            self.assertEqual(raised.call_count,1,'A later overview entry raised the video window again')


if __name__ == '__main__':
    unittest.main(verbosity=2)
