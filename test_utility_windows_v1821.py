"""Utility-window actions with disposable profiles and fake transport/native IO."""
import os,tempfile,time,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
from app_settings import SettingsStore,SettingsDialog
from lan_panel import LanDialog
from window_chrome import dock_window_state,WindowChrome
from test_html_native_lan import APP,FakeLan
import test_update_inline_v171 as update_fixture
from update_dialog import UpdateDialog


class UtilityTests(unittest.TestCase):
    def setUp(self):
        self.screen=patch.object(WindowChrome,'_available',return_value=QRect(0,0,1920,1080));self.screen.start()
        self.temp=tempfile.TemporaryDirectory();self.store=SettingsStore(Path(self.temp.name))
        self.service=FakeLan();self.service._transfers={'active':SimpleNamespace(active=True)}
        self.dialog=LanDialog(self.service,self.store);self.dialog.show();APP.processEvents()

    def tearDown(self):
        self.service._transfers.clear();self.dialog._closing_confirmed=True
        # A test may emulate Win32 during surface destruction on an offscreen
        # Mac host. Remove its registry entry after restoring the real platform.
        dock_window_state(self.dialog,False)
        self.dialog.close();self.dialog.deleteLater();APP.processEvents();self.temp.cleanup();self.screen.stop()

    def test_close_and_escape_cancel_keep_transfer_confirm_cancels(self):
        self.dialog.close();APP.processEvents()
        self.assertTrue(self.dialog.isVisible());self.assertTrue(self.dialog._close_prompt.isVisible())
        self.dialog.close_cancel_button.click();APP.processEvents()
        self.assertTrue(self.dialog.isVisible());self.assertEqual(self.service.calls,[])
        self.dialog.reject();APP.processEvents();self.assertTrue(self.dialog.isVisible())
        self.dialog.close_confirm_button.click();APP.processEvents()
        self.assertIn(('cancel','active'),self.service.calls);self.assertFalse(self.dialog.isVisible())

    def test_navigation_cancel_minimize_and_mode_do_not_cancel(self):
        seen=[];self.dialog.overview_requested.connect(lambda:seen.append('overview'))
        self.dialog.back_button.click();self.dialog.close_cancel_button.click();APP.processEvents()
        self.assertEqual(seen,[]);self.assertTrue(self.dialog.isVisible())
        self.dialog.showMinimized();APP.processEvents();self.assertEqual(self.service.calls,[])
        self.dialog.showNormal();APP.processEvents()
        other=FakeLan();self.dialog.set_service(other)
        self.assertIs(self.dialog.panel.service,other);self.assertEqual(self.service.calls,[])
        self.dialog.close();self.dialog.close_confirm_button.click();APP.processEvents()
        self.assertEqual(seen,[]);self.assertIn(('cancel','active'),self.service.calls)

    def test_reopen_resets_default_before_show_and_controls_use_full_bounds(self):
        self.service._transfers.clear();self.dialog.resize(1010,740);APP.processEvents()
        self.dialog.chrome.save_size()
        second=LanDialog(self.service,self.store)
        self.assertEqual(second.size().toTuple(),(980,704))
        self.dialog.hide();self.dialog.show();APP.processEvents()
        self.assertEqual(self.dialog.size().toTuple(),(980,704))
        self.assertEqual(second.minimumSize().toTuple(),(824,660))
        if hasattr(second.chrome,'buttons'):
            second.show();APP.processEvents()
            for role in ('max','min'):
                button=second.chrome.buttons[role]
                QTest.mouseClick(button,Qt.LeftButton,pos=QPoint(1,1));APP.processEvents()
                self.assertTrue(second.isMaximized() if role=='max' else second.isMinimized())
            second.showNormal();second.chrome.buttons['close'].click();APP.processEvents()
            self.assertFalse(second.isVisible())
        second.close();second.deleteLater();APP.processEvents()

    def test_dock_policy_tracks_all_utility_windows_including_minimized(self):
        policies=[]
        kit=SimpleNamespace(NSApplicationActivationPolicyRegular=0,NSApplicationActivationPolicyAccessory=1,
            NSApplication=SimpleNamespace(sharedApplication=lambda:SimpleNamespace(setActivationPolicy_=policies.append)))
        other=LanDialog(self.service,self.store)
        dock_window_state(self.dialog,True,appkit=kit);dock_window_state(other,True,appkit=kit)
        dock_window_state(self.dialog,False,appkit=kit);self.assertEqual(policies[-1],0)
        dock_window_state(other,False,appkit=kit);self.assertEqual(policies[-1],1)
        other.deleteLater();APP.processEvents()

    def test_corner_resize_fallback_keeps_controls_available(self):
        self.service._transfers.clear();window=self.dialog;surface=window.chrome.surface
        before=window.size();point=surface.rect().bottomRight()-QPoint(1,1)
        with patch('window_chrome.native_input_available',return_value=False):
            QTest.mousePress(surface,Qt.LeftButton,pos=point)
            QTest.mouseMove(surface,point+QPoint(40,30));QTest.mouseRelease(surface,Qt.LeftButton,pos=point+QPoint(40,30))
        APP.processEvents()
        self.assertGreater(window.width(),before.width());self.assertGreater(window.height(),before.height())
        self.assertGreaterEqual(window.width(),window.minimumWidth());self.assertGreaterEqual(window.height(),window.minimumHeight())

    def test_destroyed_or_hidden_native_surface_is_never_recreated_by_frame_setup(self):
        from window_chrome import ensure_native_resize
        self.service._transfers.clear();window=self.dialog
        with patch('window_chrome.sys.platform','win32'),patch('window_chrome.native_input_available',return_value=True),patch('ctypes.WinDLL',create=True) as api:
            window.destroy();APP.processEvents()
            self.assertFalse(window.testAttribute(Qt.WA_WState_Created))
            self.assertFalse(ensure_native_resize(window));api.assert_not_called()
            window.hide();self.assertFalse(ensure_native_resize(window));api.assert_not_called()


class SharedUpdateTests(unittest.TestCase):
    def test_copy_feedback_is_nonactivating_clamped_and_auto_dismisses(self):
        from intake_notice import ClipboardNotice
        from PySide6.QtCore import QPoint
        manager=SimpleNamespace(settings=SimpleNamespace(theme='dark'))
        view=ClipboardNotice(manager)
        try:
            area=APP.primaryScreen().availableGeometry()
            view.show_result(True,dict(point=area.bottomRight().toTuple(),fullscreen=False))
            self.assertEqual(view.message.text(),'复制已同步')
            QTest.qWait(240);APP.processEvents()
            self.assertTrue(area.contains(view.geometry()))
            self.assertTrue(view.testAttribute(Qt.WA_ShowWithoutActivating))
            self.assertTrue(view.windowFlags()&Qt.WindowTransparentForInput)
            view.show_result(False,dict(point=area.center().toTuple(),fullscreen=True))
            self.assertEqual(view.message.text(),'复制同步失败')
            self.assertEqual(view.x(),area.right()-view.width()-15)
            self.assertEqual(view.y(),area.top()+16);view.timer.start(20)
            deadline=time.monotonic()+1
            while view.isVisible() and time.monotonic()<deadline:QTest.qWait(10)
            self.assertFalse(view.isVisible())
        finally:view.close();view.deleteLater();APP.processEvents()

    def test_close_and_reopen_view_does_not_cancel_or_duplicate_download(self):
        fixture=update_fixture.InlineUpdates();fixture.setUp()
        view=UpdateDialog('1.7.0');view.bind_controller(fixture.controller)
        try:
            view.show();fixture.controller.open();fixture.client.available(fixture.manifest)
            self.assertEqual(view.state,'available');view.primary_button.click()
            fixture.client.available(fixture.manifest);self.assertEqual(view.state,'downloading')
            view.close();APP.processEvents()
            self.assertTrue(fixture.client.busy);self.assertNotIn(('cancel',),fixture.client.calls)
            view.show();fixture.client.progress.emit(1024,len(fixture.body));APP.processEvents()
            self.assertEqual(view.state,'downloading')
            self.assertEqual(sum(row[0]=='download' for row in fixture.client.calls),1)
            fixture.client.complete(fixture.path,fixture.manifest)
            self.assertEqual(view.state,'ready');self.assertEqual(fixture.events,[])
            self.assertEqual(view.primary_button.text(),'重启并安装')
            self.assertFalse(view.skip_button.isVisible())
        finally:view.close();view.deleteLater();fixture.tearDown()


if __name__=='__main__':unittest.main()
