"""Offscreen regressions for drag validation and the screen-edge handoff."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_import_profile = tempfile.TemporaryDirectory(prefix='flickdock-trigger-import-')
os.environ['APPDATA'] = _import_profile.name

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class TriggerReliabilityTests(unittest.TestCase):
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
        for control in (shelf.cloud_button,shelf.network_status,shelf.lan_button,shelf.zones['permanent'].clipboard_button):
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
            self.assertEqual(manager.shelf.visible_state,'hiding')
            from test_html_native_workspace import until
            until(lambda:not manager.shelf.isVisible())
            manager.poll()
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

    def test_edge_reveal_works_while_lan_is_open_and_after_it_closes(self):
        manager = self.manager
        manager.show_lan_dialog()
        for lan_open in (True, False):
            if not lan_open:
                manager.shelf.hide_immediately()
                manager.update_handles()
                manager.lan_dialog.close()
            with patch.object(flickdock, 'raise_shelf_for_reveal', return_value=True) as native_raise:
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
        with patch.object(flickdock,'lower_shelf_after_reveal',side_effect=lambda shelf:order.append('lower') or True), \
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
