"""Real Qt HTTP plus real disk workers; terminal signals cannot precede registration."""
import hashlib,json,queue,tempfile,threading,time,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from cloud_api import CloudApi
import download_writer

APP=QApplication.instance() or QApplication([])
def spin(predicate,seconds=3):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        APP.processEvents()
        if predicate():return True
        time.sleep(.002)
    return False

class DownloadTerminalTests(unittest.TestCase):
    def http_case(self,root,submit,*,bad_parent=False):
        payload=b'original response bytes';digest=hashlib.sha256(payload).hexdigest()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length',str(len(payload)))
                self.send_header('X-Content-SHA256',digest);self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        parent=root/'parent'
        if bad_parent:parent.write_bytes(b'original sentinel')
        target=parent/'result.bin'
        api=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True)
        api.token='isolated-fixture';out=[]
        try:
            with patch.object(download_writer._disk_pool,'submit',submit):
                key=api.request('GET','/v1/items/fixture/parts/0',lambda result,error:out.append((result,error)),
                                destination=target,expected=dict(size=len(payload),sha256=digest),timeout_ms=100)
                self.assertTrue(spin(lambda:bool(out),seconds=1),
                                'finished before start() returned must still finish the API request')
            self.assertEqual(len(out),1);self.assertNotIn(key,api._requests)
            self.assertFalse(api._writers)
            if bad_parent:self.assertEqual(parent.read_bytes(),b'original sentinel')
            return out[0],target,payload
        finally:
            api.close();APP.processEvents()
            server.shutdown();server.server_close();thread.join(2);self.assertFalse(thread.is_alive())
            self.assertEqual(len(api._requests),0)

    def test_early_real_disk_failure_is_delivered_once_and_retires_request(self):
        original=download_writer._disk_pool.submit
        def early(action):
            future=original(action);future.result(timeout=2);return future
        with tempfile.TemporaryDirectory() as folder:
            (result,error),_,_=self.http_case(Path(folder),early,bad_parent=True)
            self.assertIsNone(result);self.assertEqual(error.code,'DOWNLOAD_SAVE_FAILED')

    def test_queue_full_reports_real_error_and_retires_registered_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            def busy(_):raise queue.Full()
            (result,error),target,_=self.http_case(Path(folder),busy)
            self.assertIsNone(result);self.assertEqual(error.code,'DOWNLOAD_QUEUE_BUSY')
            self.assertFalse(target.exists())

    def test_normal_http_download_keeps_digest_and_original_bytes(self):
        original=download_writer._disk_pool.submit
        with tempfile.TemporaryDirectory() as folder:
            (result,error),target,payload=self.http_case(Path(folder),original)
            self.assertIsNone(error);self.assertEqual(result,target);self.assertEqual(target.read_bytes(),payload)

    def test_cancel_before_start_is_terminal_once_and_does_not_touch_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'original.bin';target.write_bytes(b'original')
            writer=download_writer.DownloadWriter(target,'before-start');out=[];writer.finished.connect(out.append)
            with patch.object(download_writer._disk_pool,'submit',side_effect=AssertionError('unstarted writer queued work')):
                writer.cancel();writer.cancel();self.assertFalse(writer.start())
            self.assertEqual(out,[None]);self.assertEqual(target.read_bytes(),b'original')
            self.assertFalse(writer.part.exists());writer.deleteLater()

    def test_cancel_during_start_waits_for_worker_and_emits_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'original.bin';target.write_bytes(b'original')
            writer=download_writer.DownloadWriter(target,'during-start');out=[];writer.finished.connect(out.append)
            entered=threading.Event();cancelled=threading.Event();failures=[]
            def cancel():
                entered.wait(2)
                try:writer.cancel()
                except BaseException as error:failures.append(type(error).__name__)
                finally:cancelled.set()
            thread=threading.Thread(target=cancel);thread.start()
            original=download_writer._disk_pool.submit
            def held(action):
                entered.set();self.assertTrue(cancelled.wait(2));return original(action)
            try:
                with patch.object(download_writer._disk_pool,'submit',held):self.assertTrue(writer.start())
                self.assertTrue(spin(lambda:bool(out)));self.assertEqual(out,[None]);self.assertFalse(failures)
                self.assertEqual(target.read_bytes(),b'original');self.assertFalse(writer.part.exists())
            finally:thread.join(2);writer.cancel();writer.deleteLater()

    def test_queue_full_rolls_back_only_its_unstarted_target_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'same.bin'
            first=download_writer.DownloadWriter(target,'first')
            second=download_writer.DownloadWriter(target,'second')
            with patch.object(download_writer._disk_pool,'submit',side_effect=queue.Full):
                with self.assertRaises(queue.Full):second.start()
            self.assertIs(first._target.latest(),first)
            second.cancel();first.cancel();second.deleteLater();first.deleteLater()

    def test_cancel_queued_future_completes_once_without_running_disk(self):
        from concurrent.futures import Future
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'queued.bin';future=Future()
            writer=download_writer.DownloadWriter(target,'queued');out=[];writer.finished.connect(out.append)
            with patch.object(download_writer._disk_pool,'submit',return_value=future) as submit:
                self.assertTrue(writer.start());self.assertFalse(writer.start());submit.assert_called_once()
                writer.cancel();writer.cancel()
            self.assertTrue(future.cancelled());self.assertEqual(out,[None]);self.assertFalse(target.exists())
            writer.deleteLater()

    def test_cancel_during_failed_admission_completes_once(self):
        with tempfile.TemporaryDirectory() as folder:
            writer=download_writer.DownloadWriter(Path(folder)/'failed.bin','failed');out=[];writer.finished.connect(out.append)
            def failed(_):writer.cancel();raise queue.Full()
            with patch.object(download_writer._disk_pool,'submit',failed):
                with self.assertRaises(queue.Full):writer.start()
            writer.cancel();self.assertEqual(out,[None]);self.assertFalse(writer.start());writer.deleteLater()

    def test_active_cancel_cannot_publish_over_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'original.bin';target.write_bytes(b'original')
            writer=download_writer.DownloadWriter(target,'active');out=[];writer.finished.connect(out.append)
            writer._target.lock.acquire()
            try:
                writer.start();writer.write(b'new');writer.finish(3,hashlib.sha256(b'new').hexdigest())
                self.assertTrue(spin(lambda:writer._future.running()))
                writer.cancel();writer.cancel()
            finally:writer._target.lock.release()
            self.assertTrue(spin(lambda:writer._future.done() and bool(out)));self.assertEqual(len(out),1)
            self.assertTrue(out[0] is None or out[0].get('code')=='cancelled')
            self.assertEqual(target.read_bytes(),b'original');self.assertFalse(writer.part.exists());writer.deleteLater()

if __name__=='__main__':unittest.main()
