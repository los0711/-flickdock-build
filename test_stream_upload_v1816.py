"""Real Qt HTTP upload slices and bounded disk hashing; no external services."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib,json,tempfile,time,tracemalloc,threading,unittest
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication,QEvent
from cloud_api import CloudApi,UploadSlice
from sync_files import hash_part,stage_item
APP=QApplication.instance() or QApplication([])

class StreamTests(unittest.TestCase):
    def test_staging_reuses_large_chunk_checksums_without_another_disk_pass(self):
        from shelf_sync import ShelfSync
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'large.bin'
            with source.open('wb') as stream:stream.truncate(97*1024*1024+17)
            staged=stage_item(str(source),None,source.name,root/'outbox',threading.Event())
            self.assertEqual(len(staged['chunk_hashes']),2)
            for i,digest in enumerate(staged['chunk_hashes']):
                self.assertEqual(digest,hash_part(staged['path'],i,staged['chunk_bytes'],threading.Event()))
            sync=ShelfSync.__new__(ShelfSync);sync._part_cache={};sync.api=SimpleNamespace(supports_file_slices=True)
            sync._resume_matches=lambda transfer:True;sync._work=lambda *args:self.fail('Staged chunk was unnecessarily re-read')
            requests=[];sync._request=lambda *args,**options:requests.append((args,options))
            transfer=dict(staged,id='fixture',upload_id='upload',chunk_size=staged['chunk_bytes'],part_count=2,name=source.name)
            sync._send_part(transfer,0)
            self.assertEqual(requests[0][1]['headers']['X-Content-SHA256'],staged['chunk_hashes'][0])
            self.assertEqual(requests[0][1]['source_size'],64*1024*1024)
    def test_device_bounds_seek_and_hash_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'original.bin';payload=bytes(range(256))*40000;path.write_bytes(payload)
            device=UploadSlice(path,12345,1234567)
            try:
                self.assertEqual(bytes(device.readAll()),payload[12345:12345+1234567])
                self.assertTrue(device.atEnd());self.assertTrue(device.seek(0))
                self.assertEqual(bytes(device.read(41)),payload[12345:12386])
                self.assertFalse(device.seek(1234568))
            finally:
                device.close();device.deleteLater()
                QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            tracemalloc.start()
            digest=hash_part(path,1,4*1024*1024,threading.Event())
            peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            self.assertEqual(digest,hashlib.sha256(payload[4*1024*1024:8*1024*1024]).hexdigest())
            self.assertLess(peak,3*1024*1024)

    def test_real_http_receives_only_requested_parts_and_keeps_original(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_PUT(self):
                size=int(self.headers['Content-Length']);count=0;checksum=hashlib.sha256()
                while count<size:
                    block=self.rfile.read(min(65536,size-count));checksum.update(block);count+=len(block)
                received.append((self.path,count,checksum.hexdigest()))
                result=json.dumps({'schema_version':1,'accepted':True}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'immutable.bin';data=bytes(range(251))*70000;path.write_bytes(data)
            api=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True);api.token='isolated-fixture'
            try:
                for index,(offset,size) in enumerate(((0,5*1024*1024),(5*1024*1024,5*1024*1024),(10*1024*1024,len(data)-10*1024*1024))):
                    result=[]
                    api.request('PUT','/v1/parts/'+str(index),lambda body,error:result.append((body,error)),source=path,source_offset=offset,source_size=size)
                    deadline=time.monotonic()+20
                    while not result and time.monotonic()<deadline:APP.processEvents();time.sleep(.001)
                    self.assertTrue(result,'Qt stream did not finish');self.assertIsNone(result[0][1])
                    self.assertEqual(received[-1][1:],(size,hashlib.sha256(data[offset:offset+size]).hexdigest()))
                self.assertEqual(path.read_bytes(),data)
                self.assertFalse(api._requests)
            finally:
                api.close();api.deleteLater()
                QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);APP.processEvents()
                server.shutdown();server.server_close();worker.join(3)

if __name__=='__main__':unittest.main(verbosity=2)
