"""Actual queued Qt upload signals, cancellation and local HTTP byte delivery."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,QThread,Signal
from PySide6.QtNetwork import QNetworkReply,QNetworkRequest,QNetworkProxy
from PySide6.QtWidgets import QApplication
from cloud_api import CloudApi

APP=QApplication.instance() or QApplication([])


def spin(predicate,timeout=5):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:APP.processEvents();time.sleep(.002)
    APP.processEvents();return bool(predicate())


class Reply(QObject):
    readyRead=Signal();finished=Signal();uploadProgress=Signal(int,int)
    def __init__(self):super().__init__();self.status=0;self.body=b'';self.done=False;self.aborted=False
    def attribute(self,value):return self.status
    def bytesAvailable(self):return len(self.body)
    def read(self,size):data,self.body=self.body[:size],self.body[size:];return data
    def error(self):return QNetworkReply.NetworkError.NoError
    def isFinished(self):return self.done
    def abort(self):self.aborted=True;self.done=True;self.finished.emit()
    def complete(self):self.status=200;self.body=b'{"schema_version":1,"ok":true}';self.done=True;self.finished.emit()


class Network(QObject):
    def __init__(self):super().__init__();self.reply=Reply();self.request=None
    def sendCustomRequest(self,request,method,payload):self.request=request;return self.reply


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.network=Network();self.api=CloudApi('https://fixture.invalid',network=self.network);self.api.token='fixture-session'
        self.progress=[];self.finished=[]

    def tearDown(self):self.api.close();APP.processEvents()

    def request(self,progress=None):
        return self.api.request('PUT','/v1/uploads/fixture/parts/0',lambda value,error:self.finished.append((value,error)),raw=b'x'*1000,progress=progress or (lambda sent,total:self.progress.append((sent,total))))

    def test_failed_configured_proxy_retries_same_authenticated_origin_directly_once(self):
        class Failed(Reply):
            requestSent=Signal()
            def error(self):return QNetworkReply.NetworkError.ProxyConnectionRefusedError
            def errorString(self):return 'Fixture proxy refused connection'
        self.network.reply=Failed();direct=Network();self.api._owns_network=True
        self.api._direct_networks['normal']=direct;origins=[];self.api.origin_changed.connect(origins.append)
        routes=[QNetworkProxy(QNetworkProxy.HttpProxy,'127.0.0.1',1),QNetworkProxy(QNetworkProxy.NoProxy)]
        with patch('cloud_api.proxy_routes',return_value=routes):
            self.api.request('GET','/v1/health',lambda value,error:self.finished.append((value,error)))
            self.network.reply.done=True;self.network.reply.finished.emit()
            self.assertIsNotNone(direct.request);direct.reply.complete();APP.processEvents()
        self.assertEqual(len(self.finished),1);self.assertIsNone(self.finished[0][1])
        self.assertEqual(direct.request.url(),self.network.request.url());self.assertEqual(origins,[])
        self.assertEqual(bytes(direct.request.rawHeader('Authorization')),b'Bearer fixture-session')
        self.assertTrue(self.api.prefers_direct);self.assertFalse(self.api._fallback_aliases)

    def test_proxy_handoff_never_replays_an_ambiguous_post(self):
        class Failed(Reply):
            requestSent=Signal()
            def error(self):return QNetworkReply.NetworkError.RemoteHostClosedError
            def errorString(self):return 'Fixture disconnected after HTTP request'
        self.network.reply=Failed();direct=Network();self.api._owns_network=True
        self.api._direct_networks['normal']=direct
        routes=[QNetworkProxy(QNetworkProxy.HttpProxy,'127.0.0.1',1),QNetworkProxy(QNetworkProxy.NoProxy)]
        with patch('cloud_api.proxy_routes',return_value=routes):
            self.api.request('POST','/v1/clipboard',lambda value,error:self.finished.append((value,error)),data={'fixture':True})
            self.network.reply.requestSent.emit();self.network.reply.done=True;self.network.reply.finished.emit();APP.processEvents()
        self.assertEqual(len(self.finished),1);self.assertIsNotNone(self.finished[0][1]);self.assertIsNone(direct.request)

    def test_progress_is_queued_monotonic_bounded_and_throttled_with_final_value(self):
        threads=[]
        self.request(lambda sent,total:(self.progress.append((sent,total)),threads.append(QThread.currentThread())))
        with patch('cloud_api.time.monotonic',return_value=100.):
            self.network.reply.uploadProgress.emit(10,1000);self.assertFalse(self.progress);APP.processEvents()
            for count in range(11,100):self.network.reply.uploadProgress.emit(count,1000)
            self.network.reply.uploadProgress.emit(-1,-1);APP.processEvents()
            self.network.reply.uploadProgress.emit(1001,1000);APP.processEvents()
            self.network.reply.uploadProgress.emit(500,1000);APP.processEvents()
        self.assertEqual(self.progress,[(10,1000),(1000,1000)])
        self.assertTrue(all(thread==APP.thread() for thread in threads))
        self.assertEqual(self.network.request.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute),QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)

    def test_cancel_suppresses_progress_already_in_queue_and_later_finish(self):
        key=self.request();self.network.reply.uploadProgress.emit(100,1000)
        self.api.cancel(key);self.network.reply.uploadProgress.emit(1000,1000);self.network.reply.complete();APP.processEvents()
        self.assertFalse(self.progress);self.assertEqual(len(self.finished),1);self.assertEqual(self.finished[0][1].code,'cancelled')
        self.assertTrue(self.network.reply.aborted)

    def test_transport_signal_from_worker_delivers_progress_on_gui_thread(self):
        threads=[];self.request(lambda sent,total:threads.append(QThread.currentThread()))
        source=threading.Thread(target=lambda:self.network.reply.uploadProgress.emit(123,1000));source.start();source.join(2)
        self.assertFalse(threads);self.assertTrue(spin(lambda:bool(threads)));self.assertEqual(threads,[APP.thread()])

    def test_close_and_token_change_hide_stale_progress(self):
        self.request();self.api.token='another-session';self.network.reply.uploadProgress.emit(100,1000);APP.processEvents();self.assertFalse(self.progress)
        self.network.reply.uploadProgress.emit(200,1000);self.api.close();APP.processEvents()
        self.assertFalse(self.progress);self.assertFalse(self.finished)

    def test_progress_view_failure_does_not_abort_transport(self):
        def stale_view(*args):raise RuntimeError('deleted progress view')
        self.request(stale_view);self.network.reply.uploadProgress.emit(100,1000);APP.processEvents();self.network.reply.complete()
        self.assertEqual(len(self.finished),1);self.assertIsNone(self.finished[0][1]);self.assertTrue(self.finished[0][0]['ok'])

    def test_cancelling_deferred_validation_error_emits_one_cancellation(self):
        invalid=CloudApi('');callbacks=[]
        key=invalid.request('GET','/v1/me',lambda value,error:callbacks.append(error))
        invalid.cancel(key);APP.processEvents();self.assertEqual(len(callbacks),1);self.assertEqual(callbacks[0].code,'cancelled');invalid.close()

    def test_real_qt_put_reports_bytes_and_server_receives_original_payload(self):
        payload=bytes(range(256))*32768;received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_PUT(self):
                remaining=int(self.headers['Content-Length']);digest=hashlib.sha256()
                while remaining:
                    data=self.rfile.read(min(65536,remaining));remaining-=len(data);digest.update(data);time.sleep(.002)
                received.append(digest.hexdigest());body=b'{"schema_version":1,"stored":true}'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True);api.token='local-fixture';progress=[];result=[]
        try:
            api.request('PUT','/v1/uploads/local/parts/0',lambda value,error:result.append((value,error)),raw=payload,progress=lambda sent,total:progress.append((sent,total)))
            self.assertTrue(spin(lambda:bool(result),10));self.assertIsNone(result[0][1]);self.assertTrue(progress)
            self.assertTrue(all(0<=sent<=total==len(payload) for sent,total in progress));self.assertEqual(progress[-1],(len(payload),len(payload)))
            self.assertEqual([sent for sent,total in progress],sorted({sent for sent,total in progress}));self.assertEqual(received,[hashlib.sha256(payload).hexdigest()])
        finally:api.close();server.shutdown();server.server_close();thread.join(2)


class LiveCancelTests(unittest.TestCase):
    def test_cancel_real_stream_while_server_is_slow_keeps_gui_responsive(self):
        """Exercise a real large QIODevice upload, not a mocked network reply."""
        from PySide6.QtCore import QTimer,Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QPushButton
        started=threading.Event();stop=threading.Event();received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_PUT(self):
                remaining=int(self.headers['Content-Length'])
                try:
                    while remaining and not stop.is_set():
                        data=self.rfile.read(min(16384,remaining))
                        if not data:return
                        remaining-=len(data);received.append(len(data));started.set();time.sleep(.01)
                except (OSError,ConnectionError):return
                if remaining:return
                try:
                    body=b'{"schema_version":1}'
                    self.send_response(200);self.send_header('Content-Length',str(len(body)))
                    self.end_headers();self.wfile.write(body)
                except (OSError,ConnectionError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True)
        api.token='isolated-fixture';results=[];ticks=[];durations=[]
        button=QPushButton('取消上传');button.show()
        timer=QTimer();timer.setInterval(20);timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                source=Path(folder)/'large.bin'
                with source.open('wb') as stream:stream.truncate(96*1024*1024)
                key=api.request('PUT','/v1/uploads/local/parts/0',lambda value,error:results.append((value,error)),
                    source=source,source_offset=0,source_size=64*1024*1024,progress=lambda *_:None)
                self.assertTrue(spin(lambda:started.is_set(),10),'stream never reached the local server')
                def cancel():
                    began=time.monotonic();api.cancel(key);durations.append(time.monotonic()-began)
                button.clicked.connect(cancel)
                QTest.mouseClick(button,Qt.LeftButton)
                self.assertEqual(len(durations),1)
                self.assertLess(durations[0],1.,'cancellation blocked the GUI thread')
                self.assertTrue(spin(lambda:len(ticks)>=3 and bool(results),2))
                self.assertEqual(results[0][1].code,'cancelled')
                self.assertNotIn(key,api._requests)
                self.assertTrue(source.is_file())
        finally:
            stop.set();timer.stop();button.close();api.close();server.shutdown();server.server_close();thread.join(2)

    def test_preview_cancel_during_real_large_shelf_upload(self):
        """Real shelf journal, preview button, Qt HTTP and a throttled local server."""
        from PySide6.QtCore import QTimer,Qt
        from PySide6.QtTest import QTest
        from flickdock import ShelfManager
        from shelf_sync import ShelfSync
        from test_html_native_workspace import WorkspaceTests,flush
        from test_shelf_transfers_v181 import Adapter
        started=threading.Event();stop=threading.Event();deleted=threading.Event();calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def response(self,body):
                data=json.dumps(dict(schema_version=1,**body)).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data)
            def do_GET(self):
                calls.append(('GET',self.path))
                if self.path.startswith('/v1/sync/changes'):
                    self.response(dict(changes=[],cursor='0',has_more=False,transfers=[]))
                else:self.response(dict(transfers=[]))
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
                calls.append(('POST',self.path))
                if self.path=='/v1/uploads':
                    size=payload['size'];chunk=64*1024*1024
                    self.response(dict(upload_id=payload['upload_id'],chunk_size=chunk,part_count=(size+chunk-1)//chunk))
                else:self.response(dict(announced=True))
            def do_PUT(self):
                calls.append(('PUT',self.path));remaining=int(self.headers['Content-Length'])
                try:
                    while remaining and not stop.is_set():
                        data=self.rfile.read(min(16384,remaining))
                        if not data:return
                        remaining-=len(data);started.set();time.sleep(.01)
                except (OSError,ConnectionError):return
            def do_DELETE(self):
                calls.append(('DELETE',self.path));deleted.set();self.response(dict(cancelled=True))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        fixture=WorkspaceTests(methodName='runTest');fixture.setUp()
        api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True);api.token='isolated-fixture'
        class Service(QObject):
            account_changed=Signal(object)
            signed_in=True
            device_id='isolated-device'
        service=Service();service.api=api;adapter=Adapter()
        source=fixture.base/'large-cancel.bin'
        with source.open('wb') as stream:stream.truncate(96*1024*1024)
        adapter.add(str(source));engine=ShelfSync(fixture.base/'sync',service,adapter)
        fixture.shelf.add_paths('permanent',[str(source)]);flush()
        fixture.manager.sync=engine;fixture.manager._sync_transfer_refs=set()
        notify=lambda rows:ShelfManager._transfers_changed(fixture.manager,rows)
        engine.transfers_changed.connect(notify)
        ticks=[];timer=QTimer();timer.setInterval(20);timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start()
        try:
            service.account_changed.emit({'id':'isolated-account'})
            self.assertTrue(spin(lambda:started.is_set(),20),'real shelf upload did not reach the local server')
            popup=fixture.overview();popup.select_item(str(source));flush()
            preview=popup.preview;self.assertTrue(preview.cancel_transfer.isVisible())
            before=time.monotonic();QTest.mouseClick(preview.cancel_transfer,Qt.LeftButton)
            self.assertLess(time.monotonic()-before,1.)
            self.assertTrue(spin(lambda:deleted.is_set() and str(source) not in fixture.shelf.content.transfers,5))
            entry=next(e for e in engine.state['entries'].values() if e.get('ref')==str(source))
            self.assertTrue(entry.get('paused_upload'));self.assertTrue(source.is_file())
            self.assertGreaterEqual(len(ticks),3)
        finally:
            stop.set();timer.stop();engine.transfers_changed.disconnect(notify);engine.close();api.close()
            fixture.tearDown();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main(verbosity=2)
