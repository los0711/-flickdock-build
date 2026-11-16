"""Actual local streamed upload keeps the Qt UI responsive on slow storage."""
import hashlib
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
import test_html_native_workspace as w
from cloud_api import CloudApi
from flickdock import ShelfManager


class UploadResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.fixture=w.WorkspaceTests();self.fixture.setUp()
        self.shelf=self.fixture.shelf;self.popup=self.fixture.overview()
        self.path=self.fixture.base/'slow-upload.bin'
        with self.path.open('wb') as stream:stream.truncate(16*1024*1024)
        self.received=[]
        received=self.received
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_PUT(self):
                remaining=int(self.headers['Content-Length']);digest=hashlib.sha256()
                while remaining:
                    data=self.rfile.read(min(65536,remaining))
                    if not data:return
                    digest.update(data);remaining-=len(data)
                received.append(digest.hexdigest())
                body=json.dumps({'schema_version':1,'accepted':True}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.api=CloudApi(f'http://127.0.0.1:{self.server.server_port}',allow_test_http=True);self.api.token='disposable-test'

    def tearDown(self):
        self.api.close();self.api.deleteLater();self.server.shutdown();self.server.server_close();self.server_thread.join(2)
        self.fixture.tearDown()

    def test_slow_upload_reads_do_not_block_clicks_and_progress_is_coalesced(self):
        original=Path.open;read_threads=[];target=self.path
        class SlowStream:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def seek(self,*args):return self.stream.seek(*args)
            def read(self,*args):
                read_threads.append(threading.get_ident());time.sleep(.006)
                return self.stream.read(*args)
        def open_file(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return SlowStream(stream) if path==target and args and args[0]=='rb' else stream

        ref=self.fixture.paths[0];size=self.path.stat().st_size
        state=dict(id='slow-upload',ref=ref,phase='uploading',received=0,total=size)
        self.fixture.manager._sync_transfer_refs=set()
        def notify(sent):
            state.update(received=sent)
            ShelfManager._transfers_changed(self.fixture.manager,[state])
        notify(0)
        heartbeats=[];clicks=[];result=[]
        timer=QTimer();timer.setInterval(15);timer.timeout.connect(lambda:heartbeats.append(time.monotonic()));timer.start()
        def navigate():
            QTest.mouseClick(self.popup.category_buttons['uploading'],Qt.LeftButton)
            clicks.append((self.popup.category,self.popup.transfer_strip.isVisible(),bool(result)))
        def completed(body,error):
            if error is None:
                # The real sync journal advances the accepted part on the
                # server response even when Qt coalesces its last byte signal.
                notify(size)
            result.append((body,error))
        with patch.object(Path,'open',open_file):
            self.api.request('PUT','/v1/parts/0',completed,
                source=self.path,source_offset=0,source_size=size,
                progress=lambda sent,total:notify(sent))
            QTimer.singleShot(150,navigate)
            w.until(lambda:bool(result),seconds=15)
        timer.stop()
        self.assertIsNone(result[0][1]);self.assertEqual(self.received,[hashlib.sha256(bytes(size)).hexdigest()])
        self.assertTrue(clicks);self.assertEqual(clicks[0],('uploading',True,False))
        self.assertTrue(read_threads);self.assertNotIn(threading.get_ident(),read_threads)
        self.assertGreater(len(heartbeats),10)
        self.assertLess(max(b-a for a,b in zip(heartbeats,heartbeats[1:])),.5)
        w.until(lambda:self.popup.strip_rows[0][2].text()=='100%')
        self.shelf.content.transfers.clear();self.shelf.transfers_updated()

    def test_disk_read_failure_ends_request_instead_of_hanging(self):
        original=Path.open;target=self.path;result=[]
        class BrokenStream:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def seek(self,*args):return self.stream.seek(*args)
            def read(self,*args):raise OSError('simulated read failure')
        def opened(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return BrokenStream(stream) if path==target and args and args[0]=='rb' else stream
        with patch.object(Path,'open',opened):
            self.api.request('PUT','/v1/parts/0',lambda body,error:result.append((body,error)),
                source=self.path,source_size=self.path.stat().st_size)
            w.until(lambda:bool(result),seconds=5)
        self.assertIsNotNone(result[0][1]);self.assertIn('无法读取待上传文件',str(result[0][1]))
        self.assertFalse(self.api._requests);self.assertTrue(self.path.exists())


if __name__=='__main__':unittest.main(verbosity=2)
