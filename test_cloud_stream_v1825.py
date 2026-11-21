"""Real HTTP bytes exercise the production shelf downloader and disk writer."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from uuid import uuid4

from PySide6.QtCore import QObject,Signal
from PySide6.QtWidgets import QApplication

from cloud_api import CloudApi
from shelf_sync import ShelfSync

APP=QApplication.instance() or QApplication([])
MIB=1024*1024


class Service(QObject):
    account_changed=Signal(object)
    def __init__(self,api):
        super().__init__();self.api=api;self.signed_in=False


class Adapter:
    def snapshot(self):return dict(permanent=[],texts={},metadata={})


class CloudStreamTests(unittest.TestCase):
    def test_medium_original_streams_once_with_live_progress_and_full_hash(self):
        payload=bytes(range(256))*(20*MIB//256)
        record=dict(id=str(uuid4()),version=1,name='stream-fixture.bin',kind='file',
                    size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fast=self.headers.get('X-FlickDock-Fast-Content')=='1'
                chunk=len(payload) if fast else 4*MIB
                calls.append((self.path,fast))
                if self.path.endswith('/content'):
                    body=json.dumps(dict(schema_version=1,record=record,chunk_size=chunk,
                                         part_count=(len(payload)+chunk-1)//chunk)).encode()
                    mime='application/json'
                else:
                    index=int(self.path.split('/parts/')[1].split('?')[0])
                    body=payload[index*chunk:(index+1)*chunk];mime='application/octet-stream'
                self.send_response(200);self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(len(body)))
                if mime=='application/octet-stream':self.send_header('X-Content-SHA256',hashlib.sha256(body).hexdigest())
                self.end_headers()
                try:
                    for offset in range(0,len(body),256*1024):
                        self.wfile.write(body[offset:offset+256*1024]);self.wfile.flush()
                        if mime=='application/octet-stream':time.sleep(.003)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=Thread(target=server.serve_forever,daemon=True);worker.start()
        with tempfile.TemporaryDirectory(prefix='flickdock-stream-') as root:
            api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True);api.token='disposable'
            engine=ShelfSync(root,Service(api),Adapter())
            engine.account_id='isolated';engine.owner='isolated'
            engine.state=dict(entries={},downloads={},cursor='0')
            engine.download_transfers[record['id']]=dict(record,received=0)
            progress=[];result=[]
            engine.transfers_changed.connect(lambda rows:progress.extend(row['received'] for row in rows if row['id']==record['id']))
            try:
                target=engine._download_target(record)
                engine._download_record(record,target,lambda path,error:result.append((path,error)))
                end=time.monotonic()+15
                while not result and time.monotonic()<end:APP.processEvents();time.sleep(.002)
                self.assertTrue(result,'Stream timed out')
                self.assertIsNone(result[0][1])
                self.assertEqual(target.read_bytes(),payload)
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),record['sha256'])
                self.assertEqual(len(calls),2,'A medium original should need one metadata and one streamed payload request')
                self.assertTrue(all(fast for _,fast in calls))
                self.assertTrue(any(0<value<len(payload) for value in progress),'Progress must move while receiving')
            finally:
                engine.close();api.close();server.shutdown();server.server_close();worker.join(2)


if __name__=='__main__':unittest.main()
