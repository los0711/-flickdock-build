"""Actual Qt HTTP bytes yield to clipboard traffic and resume intact."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from cloud_api import CloudApi
from transfer_priority import set_clipboard_priority,yield_bulk_payload

APP=QApplication.instance() or QApplication([])

def spin(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        APP.processEvents()
        if predicate():return True
        time.sleep(.002)
    return False


class PriorityTests(unittest.TestCase):
    def tearDown(self):set_clipboard_priority(False)

    def test_cloud_bulk_waits_while_high_priority_completes_then_original_resumes(self):
        received={};started=set()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_PUT(self):
                started.add(self.path)
                body=self.rfile.read(int(self.headers['Content-Length']))
                received[self.path]=body
                result=json.dumps({'schema_version':1,'ok':True}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(result)))
                self.end_headers();self.wfile.write(result)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True);api.token='isolated'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bulk.bin';payload=os.urandom(3*1024*1024);path.write_bytes(payload)
            completed=[]
            try:
                set_clipboard_priority(True)
                for index in range(6):api.request('PUT',f'/v1/bulk/{index}',lambda body,error:completed.append(('bulk',error)),source=path)
                self.assertTrue(spin(lambda:len(started)==6))
                api.request('PUT','/v1/clipboard/body',lambda body,error:completed.append(('clipboard',error)),raw=b'new clipboard',priority='high')
                self.assertTrue(spin(lambda:bool(completed)))
                self.assertEqual(completed,[('clipboard',None)])
                self.assertFalse(any(key.startswith('/v1/bulk/') for key in received))
                set_clipboard_priority(False)
                self.assertTrue(spin(lambda:len(completed)==7))
                self.assertEqual(completed[-1],('bulk',None))
                for index in range(6):self.assertEqual(hashlib.sha256(received[f'/v1/bulk/{index}']).digest(),hashlib.sha256(payload).digest())
                self.assertFalse(api._requests)
            finally:
                set_clipboard_priority(False);api.close();api.deleteLater();APP.processEvents()
                server.shutdown();server.server_close();worker.join(3)

    def test_priority_keeps_applying_after_first_trickle_and_cancellation_remains_prompt(self):
        set_clipboard_priority(True);cancel=threading.Event();finished=[]
        def send():
            try:
                yield_bulk_payload(cancel)
                finished.append('trickle')
                yield_bulk_payload(cancel)
                finished.append('second')
            except InterruptedError:finished.append('cancelled')
        worker=threading.Thread(target=send);worker.start()
        try:
            self.assertTrue(spin(lambda:finished==['trickle'],3))
            cancel.set();worker.join(.5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(finished,['trickle','cancelled'])
        finally:set_clipboard_priority(False);cancel.set();worker.join(3)


if __name__=='__main__':unittest.main()
