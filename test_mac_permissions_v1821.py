"""Permission metadata and explicit user setup, no real OS permissions or clipboard."""
import unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import Qt,QRect,QMimeData,QUrl
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
        board,kit=self.board(1);opened=[]
        backend=MacPermissionBackend(appkit=kit,opener=lambda url:opened.append(url.toString()) or True)
        self.assertTrue(backend.request('clipboard'));board.pasteboardItems.assert_called_once()
        self.assertEqual(backend.clipboard(),'ask');self.assertEqual(opened,['x-apple.systempreferences:com.apple.preference.security?Privacy_Pasteboard'])

    def test_working_pointer_tap_does_not_request_extra_input_monitoring(self):
        board,kit=self.board(2);quartz=SimpleNamespace(CGPreflightListenEventAccess=Mock(return_value=False))
        backend=MacPermissionBackend(appkit=kit,quartz=quartz,ax=SimpleNamespace(AXIsProcessTrusted=lambda:True))
        states=backend.inspect(pointer_ok=True,peers=False)
        self.assertEqual(states['input'],'not_required');quartz.CGPreflightListenEventAccess.assert_not_called()
        self.assertEqual(states['network'],'unknown');self.assertEqual(states['files'],'on_demand')


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
    def test_guided_steps_recheck_real_state_and_never_mark_unknown_network_allowed(self):
        states=dict(clipboard='ask',accessibility='denied',input='denied',network='unknown',files='on_demand')
        calls=[];backend=SimpleNamespace(inspect=lambda **_:dict(states),request=lambda kind:calls.append(kind) or True)
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='dark'),pointer_paste=None,lan=None)
        with patch('native_ui_common.reduced_motion',return_value=True):
            dialog=MacPermissionsDialog(manager,backend=backend)
            try:
                dialog.show();flush();dialog.setup_button.click();self.assertEqual(calls,['clipboard'])
                dialog.setup_button.click();self.assertEqual(calls,['clipboard','clipboard'])
                self.assertIn('需要',dialog.rows['clipboard'][0].text())
                states['clipboard']='allowed';dialog.setup_button.click();self.assertEqual(calls[-1],'accessibility')
                states.update(accessibility='allowed',input='not_required');dialog.setup_button.click();self.assertEqual(calls[-1],'network')
                self.assertEqual(dialog.states['network'],'unknown');self.assertIn('确认',dialog.rows['network'][0].text())
                self.assertEqual(dialog.states['files'],'on_demand')
                manager.settings.theme='light';dialog.refresh();self.assertFalse(dialog.close_button.dark)
                dialog.setup_button.click();flush();self.assertFalse(dialog.isVisible());self.assertFalse(dialog.timer.isActive())
            finally:dialog.close();dialog.deleteLater();owner.deleteLater();flush()

    def test_short_display_scrolls_all_permission_actions(self):
        backend=SimpleNamespace(inspect=lambda **_:dict(clipboard='ask',accessibility='denied',input='denied',network='unknown',files='on_demand'))
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='light'),pointer_paste=None,lan=None)
        with patch('app_settings.ReferenceModalDialog._available_geometry',return_value=QRect(0,0,640,480)):
            dialog=MacPermissionsDialog(manager,backend=backend)
            try:
                dialog.show();flush();self.assertLessEqual(dialog.height(),464)
                self.assertGreater(dialog.scroll.verticalScrollBar().maximum(),0)
                dialog.scroll.ensureWidgetVisible(dialog.setup_button);flush()
                self.assertTrue(dialog.setup_button.isVisible())
            finally:dialog.hide();dialog.deleteLater();owner.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
