"""Real Qt HTTP/disk receive across policy changes, without a native clipboard."""
import copy
import gc
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from cloud_api import CloudApi
from shelf_sync import ShelfSync
from test_sync_receive_priority import APP, Adapter, Service, file_record, spin


class ReceiveHttpLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='receive-http-lifecycle-')
        self.rows=[];self.blobs={};self.calls=[]
        self.hold=threading.Event();self.hold.set();self.body_started=threading.Event()
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                parsed=urlsplit(self.path);fixture.calls.append(parsed.path)
                if parsed.path=='/v1/sync/changes':
                    cursor=int(parse_qs(parsed.query)['cursor'][0])
                    result=dict(schema_version=1,changes=copy.deepcopy(fixture.rows[cursor:]),
                                cursor=str(len(fixture.rows)),has_more=False,transfers=[])
                elif parsed.path.endswith('/content'):
                    identity=parsed.path.split('/')[3]
                    record=next(row['record'] for row in fixture.rows if row['record']['id']==identity)
                    result=dict(schema_version=1,record=record,chunk_size=65536,part_count=1)
                elif '/parts/' in parsed.path:
                    body=fixture.blobs[parsed.path.split('/')[3]]
                    self.send_response(200);self.send_header('Content-Length',str(len(body)))
                    self.send_header('X-Content-SHA256',hashlib.sha256(body).hexdigest());self.end_headers()
                    fixture.body_started.set()
                    if not fixture.hold.wait(3):raise TimeoutError('Synthetic response was not released')
                    try:self.wfile.write(body)
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
                    return
                else:raise AssertionError(parsed.path)
                body=json.dumps(result).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.api=CloudApi(f'http://127.0.0.1:{self.server.server_port}',allow_test_http=True)
        self.api.token='synthetic-session'
        self.service=Service([],{});self.service.api=self.api
        self.adapter=Adapter();self.engine=ShelfSync(self.directory.name,self.service,self.adapter,poll_ms=600000)
        self.engine.set_directions(False,False)
        self.service.account_changed.emit({'id':'http-lifecycle-account'})

    def tearDown(self):
        self.hold.set();self.engine.close();self.api.close()
        self.assertTrue(spin(lambda:not self.engine.jobs and not self.api._writers,seconds=3))
        self.engine.deleteLater();self.api.deleteLater();self.service.deleteLater();APP.processEvents()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.assertFalse(self.thread.is_alive());self.directory.cleanup()

    def publish(self):
        identity=uuid4().hex;body=('Synthetic original '+identity+'\n').encode()
        self.blobs[identity]=body
        self.rows.append(dict(cursor=str(len(self.rows)+1),record=file_record(identity,'original-'+identity+'.bin',body)))
        return identity

    def require_received(self,identity):
        self.assertTrue(spin(lambda:identity in self.engine.state['entries'] and not self.engine.running),
                        self.diagnostics())
        entry=self.engine.state['entries'][identity]
        self.assertEqual(Path(entry['ref']).read_bytes(),self.blobs[identity])
        self.assertNotIn(identity,self.engine.state.get('downloads',{}))
        self.assertFalse(self.api._writers)

    def diagnostics(self):
        return dict(epoch=self.engine.epoch,running=self.engine.running,
                    requests=len(self.engine.requests),api_requests=len(self.api._requests),
                    downloads=len(self.engine.state.get('downloads',{})),
                    writers=[dict(terminal=writer._terminal,completed=writer._future.done(),
                                  cancelled=writer._cancelled) for writer in self.api._writers])

    def test_receive_off_on_after_completed_original_retires_each_real_writer(self):
        for _ in range(8):
            self.engine.set_directions(False,False);identity=self.publish()
            APP.processEvents();self.assertNotIn(identity,self.engine.state['entries'])
            self.engine.set_directions(False,True);self.require_received(identity)
            gc.collect();APP.processEvents()
        self.assertEqual(len(self.adapter.view['permanent']),8)
        self.assertEqual(sum('/parts/' in path for path in self.calls),8)

    def test_disable_during_real_body_then_enable_finishes_same_original_once(self):
        self.hold.clear();identity=self.publish();self.engine.set_directions(False,True)
        self.assertTrue(spin(self.body_started.is_set))
        self.engine.set_directions(False,False)
        self.assertNotIn(identity,self.engine.state['entries'])
        self.hold.set();self.engine.set_directions(False,True);self.require_received(identity)
        self.assertEqual(len(self.adapter.view['permanent']),1)

    def test_terminal_callback_exception_is_recorded_without_payload_and_still_exposed(self):
        private='synthetic-private-content-must-not-be-logged'
        from gui_watchdog import GuiWatchdog
        failures=[]
        def broken_callback(result,error):
            self.assertIsNone(error)
            raise ValueError(private)
        with patch('sys.excepthook',side_effect=lambda kind,error,traceback:failures.append((kind,error))):
            with patch('gui_watchdog.faulthandler.dump_traceback_later'),patch('gui_watchdog.faulthandler.cancel_dump_traceback_later'):
                watchdog=GuiWatchdog(APP,Path(self.directory.name))
                try:
                    key=self.api.request('GET','/v1/sync/changes?cursor=0',broken_callback)
                    self.assertTrue(spin(lambda:bool(failures)))
                finally:watchdog.close();watchdog.deleteLater()
        self.assertEqual(len(failures),1);self.assertIs(failures[0][0],ValueError)
        self.assertEqual(str(failures[0][1]),private)
        self.assertNotIn(key,self.api._requests)
        raw=(Path(self.directory.name)/'network-diagnostics.json').read_text()
        self.assertNotIn(private,raw)
        entry=json.loads(raw)[-1]
        self.assertEqual(entry['code'],'CALLBACK_EXCEPTION')
        self.assertEqual(entry['component'],'ui')
        self.assertEqual(entry['details']['cause'],'ValueError')
        self.assertRegex(entry['details']['location'],r'^test_receive_http_lifecycle_v1825\.py:[0-9]+$')


if __name__=='__main__':unittest.main()
