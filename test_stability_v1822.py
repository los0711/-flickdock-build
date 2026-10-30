"""Failure-boundary regressions: no native hooks, real clipboard or installs."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import tempfile,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import QTimer
from test_html_native_workspace import APP,until,flush
import test_shared_clipboard_files_v1821 as shared
from PySide6.QtTest import QTest
from cloud_api import CloudApi
from mac_permissions_ui import MacPermissionsDialog
from pointer_paste import PointerPasteController
from PySide6.QtWidgets import QWidget


class DownloadDiskTests(unittest.TestCase):
    def test_cancel_during_publish_cannot_overwrite_a_newer_download(self):
        import download_writer
        old=b'old valid bytes';new=b'new valid bytes'
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=new if self.path.endswith('new') else old
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.send_header('X-Content-SHA256',hashlib.sha256(body).hexdigest())
                self.end_headers();self.wfile.write(body)
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        entered=threading.Event();release=threading.Event();old_result=[];new_result=[]
        original=download_writer.os.replace;first=[True]
        def blocked_replace(source,target):
            if first[0]:first[0]=False;entered.set();release.wait(3)
            return original(source,target)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'same.bin';api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True)
            try:
                with patch('download_writer.os.replace',side_effect=blocked_replace):
                    key=api.request('GET','/v1/test/old',lambda value,error:old_result.append((value,error)),
                        authenticated=False,destination=target,expected=dict(size=len(old),sha256=hashlib.sha256(old).hexdigest()))
                    until(entered.is_set);api.cancel(key);flush();self.assertFalse(old_result)
                    api.request('GET','/v1/test/new',lambda value,error:new_result.append((value,error)),
                        authenticated=False,destination=target,expected=dict(size=len(new),sha256=hashlib.sha256(new).hexdigest()))
                    release.set();until(lambda:old_result and new_result)
                self.assertEqual(len(old_result),1);self.assertEqual(old_result[0][1].code,'cancelled')
                self.assertEqual(new_result,[(target,None)]);self.assertEqual(target.read_bytes(),new)
                self.assertFalse(list(Path(directory).glob('.*.part')))
            finally:
                release.set();api.close();until(lambda:not api._writers)
                api.deleteLater();flush();server.shutdown();server.server_close()

    def test_slow_disk_keeps_ui_live_and_publishes_only_after_verified_save(self):
        payload=b'original uncompressed data'*100000;sha=hashlib.sha256(payload).hexdigest()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length',str(len(payload)))
                self.send_header('X-Content-SHA256',sha);self.end_headers();self.wfile.write(payload)
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        entered=threading.Event();release=threading.Event();threads=[];beats=[];result=[]
        timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:beats.append(time.monotonic()))
        def slow_sync(fd):
            threads.append(threading.get_ident());entered.set();release.wait(2)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'download.bin';api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True)
            try:
                with patch('download_writer.os.fsync',side_effect=slow_sync):
                    timer.start();api.request('GET','/v1/test/content',lambda value,error:result.append((value,error)),
                        authenticated=False,destination=target,expected=dict(size=len(payload),sha256=sha))
                    until(entered.is_set);before=len(beats)
                    # Timer coalescing differs by platform. Require repeated
                    # GUI callbacks while the worker is still held, not a
                    # particular number of nominal 5ms frames in 80ms.
                    until(lambda:len(beats)-before>=3,seconds=1)
                    self.assertFalse(target.exists());self.assertFalse(result)
                    release.set();until(lambda:bool(result))
                self.assertNotEqual(threads,[threading.get_ident()]);self.assertEqual(result,[(target,None)])
                self.assertEqual(target.read_bytes(),payload);self.assertFalse(list(Path(directory).glob('.*.part')))
            finally:
                release.set();timer.stop();api.close();until(lambda:not api._writers)
                api.deleteLater();flush();server.shutdown();server.server_close()


class PriorityIsolationTests(unittest.TestCase):
    setUp=shared.SharedFilesTests.setUp
    tearDown=shared.SharedFilesTests.tearDown
    new_client=shared.SharedFilesTests.new_client
    new_engine=shared.SharedFilesTests.new_engine
    source=shared.SharedFilesTests.source
    copy=shared.SharedFilesTests.copy
    add=shared.SharedFilesTests.add

    def test_stalled_clipboard_keeps_metadata_and_shared_retry_alive(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        entry=self.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        original_put=self.peer.held[identity][0]
        self.engine.set_directions(True,True)
        baseline=sum('/v1/sync/changes' in path for _,path,_ in self.peer.calls)
        self.engine.request_refresh();until(lambda:sum('/v1/sync/changes' in path for _,path,_ in self.peer.calls)>baseline)
        self.assertIn(original_put,self.peer.active)
        self.engine.cancel_upload(str(path));self.engine.retry_upload(str(path))
        self.assertFalse(entry.get('paused_upload'));self.assertTrue(entry['dirty'])
        self.peer.finish(identity);until(lambda:not entry['dirty'])
        self.assertEqual(len(self.peer.puts),1);self.assertEqual(self.peer.payload_bytes,path.stat().st_size)

    def test_priority_lease_expires_without_cancelling_inflight_cycle(self):
        self.engine.running=True
        with patch.object(self.engine,'_interrupt_cycle') as interrupt:
            self.engine.set_clipboard_priority(True)
            interrupt.assert_not_called();self.assertTrue(self.engine.running)
            self.engine._clipboard_lease.timeout.emit()
            self.assertFalse(self.engine._clipboard_priority)


class PermissionRecoveryTests(unittest.TestCase):
    def test_tap_failure_is_not_reported_as_permission_denial(self):
        controller=PointerPasteController(APP,lambda:None,lambda *_:None,native=False)
        controller._native=True;controller.backend=SimpleNamespace(available=False,permission_required=True,close=lambda:None)
        try:
            with patch('pointer_paste.sys.platform','darwin'),patch('mac_permissions.MacPermissionBackend.accessibility',return_value='allowed'):
                self.assertFalse(controller.permission_required);self.assertTrue(controller.listener_pending)
                controller.backend.available=True;self.assertFalse(controller.listener_pending)
        finally:controller.close()

    def test_recovered_listener_clears_pending_hint(self):
        states=dict(clipboard='allowed',accessibility='allowed',input='allowed',network='unknown',files='on_demand')
        backend=SimpleNamespace(inspect=lambda **_:dict(states))
        pointer=SimpleNamespace(backend=SimpleNamespace(available=False),retry_native=lambda:None)
        owner=QWidget();manager=SimpleNamespace(shelf=owner,settings=SimpleNamespace(theme='dark'),pointer_paste=pointer,lan=None)
        dialog=MacPermissionsDialog(manager,backend=backend)
        try:
            self.assertIn('待恢复',dialog.rows['input'][0].text())
            pointer.backend.available=True;dialog.refresh()
            self.assertNotIn('尚未恢复',dialog.hint.text());self.assertIn('已恢复',dialog.hint.text())
        finally:dialog.hide();dialog.deleteLater();owner.deleteLater();flush()


if __name__=='__main__':unittest.main()
