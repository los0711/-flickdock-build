"""Native surface continuity and settings/network outcomes; no real OS input."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import QPoint,QRect,Qt,QTimer
from PySide6.QtWidgets import QFrame,QDialog
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as workspace
import flickdock,app_settings
from cloud_connectivity import CloudConnectivity
from settings_edge_hint import SettingsEdgeHint


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp()
        self.fixture.patches.enter_context(patch('native_ui_common.reduced_motion',return_value=False))
        self.shelf=self.fixture.shelf;self.manager=self.fixture.manager
        self.manager.edge=SimpleNamespace(screen_area=lambda:QRect(0,0,1920,1080),geometry=lambda:QRect(1912,300,8,90))
        self.manager.edge_suppressed=False;self.shelf.pinned=False
    def tearDown(self):self.fixture.tearDown()

    def test_edge_opens_final_overview_in_one_show_then_quick_return_stays_in_place(self):
        self.manager.motion=True;self.shelf.presentation.motion=True;self.shelf.geometry_morph.motion=True
        for edge in ('right','left','top','bottom'):
            self.manager.settings.update(dock_edge=edge);self.shelf.hide_immediately()
            frames=[];show=self.shelf.show
            with patch.object(self.shelf,'show',side_effect=lambda:(frames.append((self.shelf.expanded,self.shelf.visual_geometry())),show())):
                self.shelf.reveal_from_dock(expanded=True)
            self.assertEqual(len(frames),1);self.assertTrue(frames[0][0]);self.assertFalse(self.shelf.geometry_morph.active)
            target=self.shelf.visual_geometry();host=self.shelf.geometry()
            until(lambda:not self.shelf.presentation._timer.isActive())
            for _ in range(3):
                self.shelf.hide_animated(explicit=False);QTest.qWait(32)
                values=dict(self.shelf.presentation.values)
                self.shelf.reveal_from_dock(expanded=True)
                self.assertEqual(self.shelf.visual_geometry(),target);self.assertEqual(self.shelf.geometry(),host)
                self.assertEqual(self.shelf.presentation.values,values);self.assertFalse(self.shelf.geometry_morph.active)
                until(lambda:not self.shelf.presentation._timer.isActive())
                self.assertTrue(self.shelf.isVisible());self.assertEqual(self.shelf.visible_state,'floating')

    def test_transfer_return_enters_with_scale_and_fade_at_the_same_position(self):
        dialog=QFrame();dialog.setGeometry(150,170,1040,600);dialog.card=dialog
        self.manager.shelf=self.shelf;self.manager.reset_drag_gesture=Mock()
        for motion in (True,False):
            self.shelf.hide_immediately();self.manager.motion=motion;self.shelf.presentation.motion=motion
            flickdock.ShelfManager.show_overview_from_transfer(self.manager,dialog)
            target=QRect(dialog.mapToGlobal(QPoint()),dialog.size()).adjusted(-14,-7,14,20)
            self.assertEqual(self.shelf.visual_geometry(),target)
            values=self.shelf.presentation.values
            self.assertEqual((values['dx'],values['dy'],values['alpha']),(0,0,0))
            self.assertEqual(values['sx'],.97 if motion else 1)
            until(lambda:not self.shelf.presentation._timer.isActive())
            self.assertEqual(self.shelf.visual_geometry(),target);self.assertTrue(self.shelf.expanded)
        dialog.deleteLater()


class SettingsNetworkTests(unittest.TestCase):
    def test_settings_edge_hint_has_a_real_cancel_button_and_hover_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            store=app_settings.SettingsStore(Path(directory));settings=app_settings.SettingsDialog(store)
            manager=SimpleNamespace(settings=store,settings_dialog=settings,update_handles=Mock(),
                edge=SimpleNamespace(geometry=lambda:QRect(780,220,8,100),screen_area=lambda:QRect(0,0,800,600)))
            manager.close_settings_from_edge=lambda:flickdock.ShelfManager.close_settings_from_edge(manager)
            hint=SettingsEdgeHint(manager);manager.settings_edge_hint=hint
            settings.show();flush();settings.folder_edit.setText('unsaved-draft')
            try:
                hint.show_hint();flush();self.assertTrue(hint.isVisible())
                self.assertIn('关闭设置',hint.message.text());self.assertTrue(hint.close_button.isVisible())
                self.assertTrue(hint.contains_pointer(QPoint(778,270)))
                QTest.mouseClick(hint.close_button,Qt.LeftButton);flush()
                self.assertFalse(settings.isVisible());self.assertFalse(hint.isVisible())
                self.assertNotEqual(store.capture_dir,'unsaved-draft');self.assertEqual(settings.result(),QDialog.Rejected)
            finally:hint.close();settings.close();hint.deleteLater();settings.deleteLater();flush()

    def test_health_probe_uses_file_service_route_and_recovers_without_login(self):
        api=SimpleNamespace(request=Mock(return_value='probe'),cancel=Mock());monitor=CloudConnectivity(api,enabled=False)
        states=[];monitor.changed.connect(states.append)
        monitor.check();args=api.request.call_args
        self.assertEqual(args.args[:2],('GET','/v1/health'));self.assertFalse(args.kwargs['authenticated'])
        args.args[2]({'service':'flickdock-cloud'},None);self.assertEqual(monitor.state,'online')
        monitor.check();api.request.call_args.args[2](None,RuntimeError('network unavailable'));self.assertEqual(monitor.state,'offline')
        monitor.check();api.request.call_args.args[2]({'service':'flickdock-cloud'},None);self.assertEqual(states,['online','offline','online'])
        monitor.check();monitor.close();api.cancel.assert_called_once_with('probe')

    def test_check_button_does_not_move_keyboard_focus_to_close(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(directory)));dialog.show();dialog.activateWindow();flush()
            dialog.update_requested.connect(lambda:dialog.set_update_state('checking'))
            try:
                QTest.mouseClick(dialog.update_button,Qt.LeftButton);flush()
                self.assertFalse(dialog.close_button.hasFocus());self.assertFalse(dialog.close_button._keyboard_focus)
                dialog.set_update_state('latest');flush()
                self.assertTrue(dialog.update_activity.isHidden());self.assertEqual(dialog.update_activity.height(),0)
                self.assertFalse(dialog.close_button.hasFocus());self.assertFalse(dialog.close_button.property('keyboardFocus'))
            finally:dialog.close();dialog.deleteLater();flush()


if __name__=='__main__':unittest.main()
