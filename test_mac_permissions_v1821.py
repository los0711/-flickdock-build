"""Permission metadata and explicit user setup, no real OS permissions or clipboard."""
import unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import Qt,QRect,QMimeData,QSize,QUrl
from PySide6.QtWidgets import QWidget
from mac_permissions import MacPermissionBackend,background_clipboard_allowed
from mac_permissions_ui import MacPermissionsDialog
from test_html_native_workspace import APP,flush
import test_clipboard_fast_v1821 as fast


class PermissionTests(unittest.TestCase):
    def board(self,state):
        board=SimpleNamespace(accessBehavior=Mock(return_value=state),pasteboardItems=Mock(return_value=[]))
        kit=SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda:board))
        return board,kit

    def test_background_check_only_reads_metadata_never_triggers_a_paste_alert(self):
        for number,state in ((0,'default'),(1,'ask'),(2,'allowed'),(3,'denied')):
            board,kit=self.board(number);backend=MacPermissionBackend(appkit=kit)
            self.assertEqual(backend.clipboard(),state);board.pasteboardItems.assert_not_called()
            with patch('mac_permissions.clipboard_access',return_value=state):
                self.assertEqual(background_clipboard_allowed(),number==2)
        with patch('mac_permissions.clipboard_access',return_value='unknown'):self.assertFalse(background_clipboard_allowed())

    def test_older_os_needs_no_new_clipboard_grant_and_queries_are_cached(self):
        kit=SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda:object()))
        self.assertEqual(MacPermissionBackend(appkit=kit).clipboard(),'legacy')
        with (patch('mac_permissions.sys.platform','darwin'),patch('mac_permissions.native_input_available',return_value=True),
              patch('mac_permissions._clipboard_cache',None),patch.object(MacPermissionBackend,'clipboard',return_value='allowed') as query):
            for _ in range(20):self.assertTrue(background_clipboard_allowed())
            self.assertEqual(query.call_count,1)
            self.assertTrue(background_clipboard_allowed(force=True));self.assertEqual(query.call_count,2)

    def test_only_explicit_setup_requests_paste_access_and_does_not_claim_success(self):
        for status,reads in ((0,1),(1,0),(2,0),(3,0)):
            with self.subTest(status=status):
                board,kit=self.board(status);opened=[]
                backend=MacPermissionBackend(appkit=kit,opener=lambda url:opened.append(url.toString()) or True)
                previous=backend.clipboard()
                self.assertTrue(backend.request('clipboard'))
                self.assertEqual(board.pasteboardItems.call_count,reads)
                self.assertEqual(backend.clipboard(),previous)
                self.assertEqual(opened,['x-apple.systempreferences:com.apple.preference.security?Privacy_Pasteboard'])

    def test_working_pointer_tap_does_not_request_extra_input_monitoring(self):
        board,kit=self.board(2);quartz=SimpleNamespace(CGPreflightListenEventAccess=Mock(return_value=False))
        backend=MacPermissionBackend(appkit=kit,quartz=quartz,ax=SimpleNamespace(AXIsProcessTrusted=lambda:True))
        states=backend.inspect(pointer_ok=True,peers=False)
        self.assertEqual(states['input'],'not_required');quartz.CGPreflightListenEventAccess.assert_not_called()
        self.assertEqual(states['network'],'unknown');self.assertEqual(states['files'],'on_demand')

    def test_accessibility_settings_opens_the_pane_without_requesting_a_prompt(self):
        authorize=Mock()
        check=Mock(return_value=False)
        opener=Mock(return_value=True)
        backend=MacPermissionBackend(ax=SimpleNamespace(AXIsProcessTrusted=check,AXIsProcessTrustedWithOptions=authorize),
                                     quartz=SimpleNamespace(CGPreflightPostEventAccess=lambda:False),opener=opener)
        with patch('mac_permissions.ctypes.CDLL') as library:
            self.assertTrue(backend.request('accessibility'))
        opener.assert_called_once_with(QUrl('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'))
        authorize.assert_not_called()
        check.assert_not_called()
        library.assert_not_called()


class PermissionIdentityTests(unittest.TestCase):
    def test_direct_paste_reports_the_missing_permission(self):
        from clipboard_history_native import mac_history_paste_blocker
        with patch('clipboard_history_native.mac_paste_allowed',return_value=False):
            self.assertEqual(mac_history_paste_blocker(),'accessibility')
        with patch('clipboard_history_native.mac_paste_allowed',return_value=True):
            for status in ('ask','default','denied','unknown'):
                with patch.object(MacPermissionBackend,'clipboard',return_value=status):
                    self.assertEqual(mac_history_paste_blocker(),'clipboard')
            for status in ('allowed','legacy'):
                with patch.object(MacPermissionBackend,'clipboard',return_value=status):
                    self.assertIsNone(mac_history_paste_blocker())

    def test_accessibility_grant_is_not_overruled_by_event_preflight(self):
        query=Mock(return_value=False)
        backend=MacPermissionBackend(ax=SimpleNamespace(AXIsProcessTrusted=lambda:True),
                                     quartz=SimpleNamespace(CGPreflightPostEventAccess=query))
        self.assertEqual(backend.paste(),'allowed')
        query.assert_not_called()
        with patch('mac_permissions.MacPermissionBackend',return_value=backend):
            from clipboard_history_native import mac_paste_allowed
            self.assertTrue(mac_paste_allowed())

    def test_current_binary_needs_a_real_grant_even_if_an_old_app_was_allowed(self):
        backend=MacPermissionBackend(ax=SimpleNamespace(AXIsProcessTrusted=lambda:False),
                                     quartz=SimpleNamespace(CGPreflightPostEventAccess=lambda:False))
        self.assertEqual(backend.paste(),'denied')

    def test_no_restart_prompt_when_current_process_is_already_allowed(self):
        from mac_permissions import setup_needed,mark_setup_shown
        backend=SimpleNamespace(accessibility=lambda:'allowed',clipboard=lambda:'allowed')
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(setup_needed(root,backend))
            backend.accessibility=lambda:'denied'
            self.assertTrue(setup_needed(root,backend))
            mark_setup_shown(root)
            self.assertFalse(setup_needed(root,backend))
            self.assertEqual(backend.accessibility(),'denied')

    def test_permission_guide_rechecks_only_when_the_new_build_needs_it(self):
        from mac_permissions import setup_needed,mark_setup_shown
        backend=SimpleNamespace(accessibility=lambda:'denied',clipboard=lambda:'allowed')
        with tempfile.TemporaryDirectory() as root:
            with patch('app_version.APP_BUILD','1.8.40'):
                mark_setup_shown(root)
                self.assertFalse(setup_needed(root,backend))
            with patch('app_version.APP_BUILD','1.8.41'):
                self.assertTrue(setup_needed(root,backend))
                backend.accessibility=lambda:'allowed'
                self.assertFalse(setup_needed(root,backend))

    def test_same_version_reinstall_rechecks_missing_grants_once(self):
        from mac_permissions import setup_needed,mark_setup_shown
        backend=SimpleNamespace(accessibility=lambda:'allowed',clipboard=lambda:'denied')
        with tempfile.TemporaryDirectory() as root:
            app=Path(root)/'FlickDock'
            app.write_bytes(b'original executable')
            (Path(root)/'mac-permissions-shown').write_text('1.8.40',encoding='utf8')
            self.assertTrue(setup_needed(root,backend,executable=app))
            mark_setup_shown(root,executable=app)
            self.assertFalse(setup_needed(root,backend,executable=app))
            candidate=Path(root)/'update'
            candidate.write_bytes(b'updated executable')
            candidate.replace(app)
            self.assertTrue(setup_needed(root,backend,executable=app))
            backend.clipboard=lambda:'allowed'
            self.assertFalse(setup_needed(root,backend,executable=app))


class ClipboardPermissionTests(unittest.TestCase):
    def setUp(self):fast.ClipboardFastTests.setUp(self)
    def tearDown(self):fast.ClipboardFastTests.tearDown(self)

    def test_denied_reads_do_not_upload_or_change_local_copy_and_grant_restores_latest(self):
        sender,receiver=self.clients;clip=self.clips[1]
        with patch.object(receiver,'_native_accessible',return_value=False):
            receiver.recheck_native_access(force=True);clip.text('native copy stays usable')
            self.clips[0].text('latest shared text')
            fast.until(lambda:self.hub.head and self.hub.head.get('ready'))
            self.assertEqual(clip.mimeData().text(),'native copy stays usable')
            self.assertFalse(receiver._active());self.assertFalse(receiver._priority_active())
            with patch.object(clip,'mimeData',side_effect=AssertionError('background read while denied')):
                self.assertIsNone(receiver._snapshot())
                self.assertFalse(receiver._apply(('text',dict(text='forbidden',html='')),Path(self.temp.name)))
        receiver.recheck_native_access(force=True)
        fast.until(lambda:clip.mimeData().text()=='latest shared text')
        self.assertEqual(sum(method=='POST' for method,path in self.hub.calls),1)

    def test_unreadable_clipboard_never_causes_file_cache_deletion(self):
        import time
        sender,receiver=self.clients;clip=self.clips[1]
        source=Path(self.temp.name)/'original.txt';source.write_bytes(b'paste remains usable')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        fast.until(lambda:clip.mimeData().hasUrls())
        received=Path(clip.mimeData().urls()[0].toLocalFile());self.assertTrue(received.exists())
        with patch.object(receiver,'_native_accessible',return_value=False),patch('clipboard_sync.time.time',return_value=time.time()+90000):
            receiver.recheck_native_access();receiver._cleanup()
            fast.until(lambda:not receiver._jobs)
            self.assertEqual(received.read_bytes(),b'paste remains usable');self.assertTrue(clip.mimeData().hasUrls())
        receiver.recheck_native_access();clip.text('a new copy releases the old native file reference')
        fast.until(lambda:self.hub.head and self.hub.head['kind']=='text')
        with patch('clipboard_sync.time.time',return_value=time.time()+90000):receiver._cleanup()
        fast.until(lambda:not receiver._jobs)
        self.assertFalse(received.exists());self.assertEqual(source.read_bytes(),b'paste remains usable')


class PermissionUITests(unittest.TestCase):
    def test_permission_guide_stays_outside_settings_on_small_and_secondary_displays(self):
        from mac_permission_guide import guide_geometry
        area=QRect(-1440,24,1440,852);size=QSize(306,132)
        window=QRect(-1300,42,870,600)
        position=guide_geometry(window,area,size)
        self.assertEqual(position.top(),window.bottom()+11)
        window.moveBottom(area.bottom()-8)
        self.assertTrue(guide_geometry(window,area,size).isEmpty())
        self.assertTrue(guide_geometry(area,area,size).isEmpty())

    def test_permission_guide_drags_exact_bundle_and_disappears_after_grant(self):
        from mac_permission_guide import MacAccessibilityGuide
        bundle=Path('/Applications/FlickDock.app')
        backend=SimpleNamespace(accessibility=Mock(return_value='denied'))
        reveal=Mock()
        guide=MacAccessibilityGuide(bundle,backend,'light',APP.font(),reveal)
        try:
            area=APP.primaryScreen().availableGeometry()
            window=QRect(area.x()+4,area.y()+4,area.width()-8,area.height()-guide.height()-20)
            with patch('mac_permission_guide.settings_window',return_value=window):
                guide.start();flush()
                self.assertTrue(guide.isVisible())
                self.assertFalse(guide.geometry().intersects(window))
                self.assertGreater(guide.geometry().top(),window.bottom())
                self.assertEqual(guide.app_icon.mime_data().urls(),[QUrl.fromLocalFile(str(bundle))])
                guide.app_icon.click();reveal.assert_called_once()
                backend.accessibility.return_value='allowed'
                guide._follow_settings()
                self.assertFalse(guide.isVisible())
                self.assertFalse(guide.timer.isActive())
        finally:guide.stop();guide.deleteLater();flush()

    def test_finder_button_selects_the_running_bundle(self):
        from PySide6.QtWidgets import QPushButton
        bundle=Path('/Applications/FlickDock.app')
        states=dict(clipboard='allowed',accessibility='denied',input='denied',network='unknown',files='on_demand')
        backend=SimpleNamespace(inspect=lambda **_:dict(states),reveal_application=Mock())
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='light'))
        with patch('platform_native.app_bundle',return_value=bundle):
            dialog=MacPermissionsDialog(manager,backend=backend)
        try:
            self.assertIn(str(bundle),dialog.application_path.text())
            button=next(b for b in dialog.findChildren(QPushButton) if b.text()=='在 Finder 中显示')
            button.click()
            backend.reveal_application.assert_called_once_with(bundle)
        finally:dialog.deleteLater();owner.deleteLater();flush()

    def test_guided_steps_recheck_real_state_and_never_mark_unknown_network_allowed(self):
        states=dict(clipboard='ask',accessibility='denied',input='denied',network='unknown',files='on_demand')
        calls=[];backend=SimpleNamespace(inspect=lambda **_:dict(states),request=lambda kind:calls.append(kind) or True)
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='dark',base=tempfile.gettempdir()),pointer_paste=None,lan=None)
        with patch('native_ui_common.reduced_motion',return_value=True):
            dialog=MacPermissionsDialog(manager,backend=backend)
            try:
                dialog.show();flush();dialog.setup_button.click();flush();self.assertEqual(calls,['clipboard'])
                self.assertFalse(dialog.isVisible())
                dialog._returned_to_app();flush()
                self.assertEqual(calls,['clipboard'])
                self.assertTrue(dialog.isVisible())
                self.assertIn('需要',dialog.rows['clipboard'][0].text())
                dialog.setup_button.click();flush()
                states['clipboard']='allowed';dialog._returned_to_app();flush()
                self.assertEqual(calls,['clipboard','clipboard']);self.assertTrue(dialog.isVisible())
                dialog.setup_button.click();flush();self.assertEqual(calls[-1],'accessibility')
                states.update(accessibility='allowed',input='not_required')
                dialog._returned_to_app();flush()
                self.assertEqual(calls,['clipboard','clipboard','accessibility'])
                self.assertEqual(dialog.states['network'],'unknown');self.assertIn('确认',dialog.rows['network'][0].text())
                self.assertEqual(dialog.states['files'],'on_demand')
                manager.settings.theme='light';dialog.refresh();self.assertFalse(dialog.close_button.dark)
                self.assertFalse(dialog.isVisible());self.assertFalse(dialog.timer.isActive())
            finally:dialog.close();dialog.deleteLater();owner.deleteLater();flush()

    def test_short_display_scrolls_all_permission_actions(self):
        backend=SimpleNamespace(inspect=lambda **_:dict(clipboard='ask',accessibility='denied',input='denied',network='unknown',files='on_demand'))
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='light',base=tempfile.gettempdir()),pointer_paste=None,lan=None)
        with patch('app_settings.ReferenceModalDialog._available_geometry',return_value=QRect(0,0,640,480)):
            dialog=MacPermissionsDialog(manager,backend=backend)
            try:
                dialog.show();flush();self.assertLessEqual(dialog.height(),464)
                self.assertGreater(dialog.scroll.verticalScrollBar().maximum(),0)
                dialog.scroll.ensureWidgetVisible(dialog.setup_button);flush()
                self.assertTrue(dialog.setup_button.isVisible())
            finally:dialog.hide();dialog.deleteLater();owner.deleteLater();flush()

    def test_permission_status_colors_follow_actual_grants(self):
        from native_ui_common import colors

        states=dict(clipboard='allowed',accessibility='denied',input='unknown',network='observed',files='on_demand')
        backend=SimpleNamespace(inspect=lambda **_:dict(states))
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='light'))
        dialog=MacPermissionsDialog(manager,backend=backend)
        try:
            for theme in ('light','dark'):
                manager.settings.theme=theme
                dialog.refresh()
                palette=colors(theme)
                for kind,token in (('clipboard','green'),('accessibility','red'),('network','secondary')):
                    self.assertIn(palette[token],dialog.rows[kind][0].styleSheet())
            states['accessibility']='allowed'
            dialog.refresh()
            self.assertIn(colors('dark')['green'],dialog.rows['accessibility'][0].styleSheet())
        finally:dialog.close();dialog.deleteLater();owner.deleteLater();flush()

    def test_clipboard_permission_shortcut_opens_system_pane_without_an_extra_dialog(self):
        from flickdock import ShelfManager

        states=dict(clipboard='default',accessibility='denied',input='unknown',network='unknown',files='on_demand')
        backend=SimpleNamespace(inspect=lambda **_:dict(states),open_settings=Mock(return_value=True),request=Mock())
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='light'),_quitting=False,
            clipboard_presenter=SimpleNamespace(state={'permission_required':True}),clipboard_preview_dialog=None)
        dialog=MacPermissionsDialog(manager,backend=backend)
        manager._mac_permissions=lambda:dialog
        manager.open_mac_permission=lambda kind:ShelfManager.open_mac_permission(manager,kind)
        try:
            ShelfManager.show_clipboard_preview(manager);flush()
            backend.open_settings.assert_called_once_with('clipboard')
            backend.request.assert_not_called()
            self.assertTrue(dialog.awaiting_system)
            self.assertFalse(dialog.isVisible())
            dialog._returned_to_app();flush()
            self.assertFalse(dialog.isVisible())
        finally:dialog.close();dialog.deleteLater();owner.deleteLater();flush()

    def test_automatic_permission_setup_waits_for_startup_keychain_result(self):
        from flickdock import ShelfManager

        manager=SimpleNamespace(_quitting=False,cocoa_smoke=False,account=SimpleNamespace(resume_complete=False),
            settings=SimpleNamespace(base=Path(tempfile.gettempdir())),show_mac_permissions=Mock())
        with (patch('flickdock.sys.platform','darwin'),patch('flickdock.sys.frozen',True,create=True),
              patch('flickdock.native_input_available',return_value=True),
              patch('mac_permissions.setup_needed',return_value=True) as needed):
            ShelfManager._present_mac_setup(manager)
            needed.assert_not_called()
            manager.show_mac_permissions.assert_not_called()
            manager.account.resume_complete=True
            ShelfManager._present_mac_setup(manager)
            needed.assert_called_once_with(manager.settings.base)
            manager.show_mac_permissions.assert_called_once_with()


if __name__=='__main__':unittest.main(verbosity=2)
