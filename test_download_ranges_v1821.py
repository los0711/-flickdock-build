"""Real HTTP truncation, real Qt network IO and durable verified chunk reuse.

Loopback only. This checks the actual client recovery path, not VMware routing
or public-network throughput.
"""
import collections,hashlib,json,os,socket,sys,tempfile,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QObject,Signal
from test_html_native_workspace import APP,until,flush
from test_small_upload_lane import Adapter
from cloud_api import CloudApi
from shelf_sync import ShelfSync


class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,api):super().__init__();self.api=api
    @property
    def signed_in(self):return bool(self.api.token)


class DownloadRecoveryTests(unittest.TestCase):
    def test_real_body_interruptions_retry_only_failed_ranges_across_cycle(self):
        payload=b'original bytes\x00'+bytes(range(256))*(9*1024*1024//256)+b'non-aligned tail'
        chunk=4*1024*1024;counts=collections.Counter();lock=threading.Lock();errors=[]
        record=dict(id=str(uuid4()),version=1,deleted=False,kind='file',name='original.bin',
                    mime_type='application/octet-stream',size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                try:
                    if self.headers.get('Authorization')!='Bearer isolated-fixture':self.send_error(401);return
                    if self.path.startswith('/v1/sync/changes?'):
                        cursor=int(self.path.split('cursor=')[1].split('&')[0])
                        self.json(dict(changes=[dict(cursor='1',record=record)] if not cursor else [],cursor='1',has_more=False,transfers=[]));return
                    if self.path.endswith('/content'):
                        assert self.headers.get('X-FlickDock-Range-Bytes')=='4194304'
                        with lock:counts['metadata']+=1
                        self.json(dict(record=record,chunk_size=chunk,part_count=(len(payload)+chunk-1)//chunk));return
                    if '/parts/' in self.path:
                        assert self.headers.get('X-FlickDock-Range-Bytes')=='4194304'
                        index=int(self.path.split('/parts/')[1].split('?')[0]);data=payload[index*chunk:(index+1)*chunk]
                        with lock:counts[index]+=1;attempt=counts[index]
                        self.send_response(200);self.send_header('Content-Type','application/octet-stream')
                        self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-SHA256',hashlib.sha256(data).hexdigest());self.end_headers()
                        if index==1 and attempt<=3:
                            self.wfile.write(data[:128*1024]);self.wfile.flush()
                            self.connection.shutdown(socket.SHUT_WR);self.close_connection=True;return
                        self.wfile.write(data);return
                    self.send_error(404)
                except (BrokenPipeError,ConnectionResetError):pass
                except BaseException as error:errors.append(str(error));self.close_connection=True
            def json(self,value):
                data=json.dumps(dict(schema_version=1,**value)).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with tempfile.TemporaryDirectory(prefix='flickdock-http-ranges-') as folder:
            api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True);api.token='isolated-fixture'
            account=Account(api);adapter=Adapter();engine=ShelfSync(folder,account,adapter,poll_ms=600000)
            statuses=[];snapshots=[];engine.status_changed.connect(statuses.append);engine.transfers_changed.connect(snapshots.append)
            engine._retry_delay=lambda error:.05;engine.set_directions(False,True)
            qt_errors=[]
            try:
                with patch.object(sys,'excepthook',lambda kind,value,trace:qt_errors.append(str(value))):
                    account.account_changed.emit({'id':str(uuid4())})
                    try:until(lambda:any(Path(ref).is_file() and Path(ref).name=='original.bin' for ref in adapter.view['permanent']),seconds=15)
                    except AssertionError:
                        self.fail(json.dumps(dict(counts=dict(counts),statuses=statuses,errors=errors,qt_errors=qt_errors,
                                                  deferred=engine.state.get('deferred_downloads'),requests=len(engine.requests)),ensure_ascii=False))
                    output=next(Path(ref) for ref in adapter.view['permanent'] if Path(ref).name=='original.bin')
                    self.assertEqual(output.read_bytes(),payload)
                    self.assertEqual(counts[0],1);self.assertEqual(counts[2],1);self.assertEqual(counts[1],4)
                    self.assertGreaterEqual(counts['metadata'],2)
                    self.assertTrue(any('等待重试' in value for value in statuses),statuses)
                    self.assertTrue(any(any(row.get('received',0)>0 for row in rows) for rows in snapshots))
                    self.assertFalse(engine.state['downloads']);self.assertFalse(engine.state.get('deferred_downloads'))
                    self.assertEqual(qt_errors,[]);self.assertEqual(errors,[])
            finally:
                engine.close();until(lambda:not engine.jobs);api.close();flush()
                server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
