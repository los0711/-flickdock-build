"""Review regressions: local HTTP, disposable folders, no account/OS secrets."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import hashlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from cloud_api import CloudApi
from sync_files import stage_item, unpack_directory
import sync_files

APP = QApplication.instance() or QApplication([])


def spin(predicate, seconds=2):
    deadline = time.perf_counter() + seconds
    while not predicate() and time.perf_counter() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(5, loop.quit)
        loop.exec()
    return predicate()


class SyncReviewTests(unittest.TestCase):
    def test_real_cloud_chunk_header_download_succeeds(self):
        payload = b'chunk-data-fixture'
        digest = hashlib.sha256(payload).hexdigest()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('X-Content-SHA256', digest)
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory(prefix='flickdock-sync-header-review-') as directory:
            api = CloudApi(f'http://127.0.0.1:{server.server_port}', allow_test_http=True)
            api.token = 'fake-test-token'
            result, diagnostics = [], []
            def inspect_header(reply):
                try:
                    reply.rawHeader(b'X-Content-SHA256')
                except TypeError:
                    diagnostics.append('Qt rawHeader rejects bytes name')
            api.network.finished.connect(inspect_header)
            target = Path(directory) / 'chunk'
            api.request('GET', '/v1/items/fixture/parts/0', lambda value, error: result.append((value, error)),
                        destination=target, expected={'size':len(payload)})
            try:
                self.assertTrue(spin(lambda:bool(result)))
                self.assertIsNone(result[0][1], f'{result[0][1]}; {diagnostics}')
                self.assertEqual(target.read_bytes(), payload)
            finally:
                api.close(); APP.processEvents()
                server.shutdown(); server.server_close(); thread.join(2)

    def test_closed_cloud_api_suppresses_deferred_validation_error(self):
        api, callbacks = CloudApi(''), []
        api.request('GET','/v1/me',lambda result,error:callbacks.append(error))
        api.close()
        loop=QEventLoop();QTimer.singleShot(20,loop.quit);loop.exec()
        self.assertEqual(callbacks, [])

    def test_folder_sync_preserves_empty_subdirectories(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-sync-folder-review-') as directory:
            root=Path(directory);source=root/'folder'
            (source/'empty'/'nested').mkdir(parents=True)
            (source/'file.txt').write_text('fixture')
            staged=stage_item(str(source),None,'folder',root/'outbox',threading.Event())
            target=root/'downloaded-folder'
            unpack_directory(staged['path'],target,threading.Event())
            self.assertTrue((target/'empty'/'nested').is_dir())
            self.assertEqual((target/'file.txt').read_text(),'fixture')

    @unittest.skipUnless(os.name=='nt','Windows sharing contract')
    def test_original_can_be_deleted_during_background_staging(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-sync-share-review-') as directory:
            root=Path(directory);source=root/'source.bin';source.write_bytes(b'fixture'*4096)
            entered,release=threading.Event(),threading.Event()
            real=sync_files._source_reader;errors=[]
            class SlowReader:
                def __init__(self,stream):self.stream=stream;self.once=False
                def __enter__(self):return self
                def __exit__(self,*args):self.stream.close()
                def read(self,count):
                    if not self.once:
                        self.once=True;entered.set();release.wait(2)
                    return self.stream.read(count)
            @contextmanager
            def open_file(path):
                with real(path) as stream:
                    if path==source:
                        entered.set();release.wait(2)
                    yield stream
            def run():
                try:stage_item(str(source),None,source.name,root/'outbox',threading.Event())
                except Exception as error:errors.append(error)
            with patch.object(sync_files,'_source_reader',open_file):
                thread=threading.Thread(target=run);thread.start()
                self.assertTrue(entered.wait(2))
                try:
                    try:source.unlink();deleted=True
                    except PermissionError:deleted=False
                finally:
                    release.set();thread.join(3)
                self.assertFalse(errors)
                self.assertTrue(deleted,'Original is locked against delete while the sync reader is open')


if __name__=='__main__':unittest.main(verbosity=2)
