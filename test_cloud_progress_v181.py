"""Actual queued Qt upload signals, cancellation and local HTTP byte delivery."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,QThread,Signal
from PySide6.QtNetwork import QNetworkReply,QNetworkRequest
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


if __name__=='__main__':unittest.main(verbosity=2)
