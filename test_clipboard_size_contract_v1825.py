"""Inclusive 100MiB originals; framing allowance is not extra user content."""
import hashlib,json,tempfile,tracemalloc,unittest,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QBuffer,QIODevice,QMimeData,QUrl,QTimer,QEventLoop
from PySide6.QtGui import QImage,QColor
import clipboard_sync as subject
from test_shared_clipboard_files_v1821 import Peer
from test_clipboard_sync_v1819 import Clipboard,Events,until,flush
from test_clipboard_loop_v1824 import SequencedClipboard
from cloud_api import CloudApi,UploadSlice


class ClipboardSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-size-contract-');self.base=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_real_100mib_file_packages_and_extracts_with_bounded_reads_and_memory(self):
        self.assertEqual(subject.MAX_BYTES,100*1024*1024)
        self.assertEqual(subject.MAX_ARCHIVE_BYTES,101*1024*1024)
        source=self.base/'original-100MiB.bin'
        with source.open('wb') as stream:stream.truncate(subject.MAX_BYTES)
        folder=self.base/'package';folder.mkdir();opened=Path.open;reads=[]
        class BoundedReader:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def __getattr__(self,name):return getattr(self.stream,name)
            def read(self,size=-1):
                if not 0<size<=256*1024:raise AssertionError('unbounded original-file read')
                reads.append(size);return self.stream.read(size)
        def open_path(path,*args,**kwargs):
            stream=opened(path,*args,**kwargs)
            return BoundedReader(stream) if path==source and args and args[0]=='rb' else stream
        tracemalloc.start()
        try:
            with patch.object(Path,'open',new=open_path):item=subject.package(('files',[str(source)]),folder)
            self.assertIsNotNone(item);self.assertEqual(item['raw_file']['size'],subject.MAX_BYTES)
            self.assertGreater(item['size'],subject.MAX_BYTES);self.assertLessEqual(item['size'],subject.MAX_ARCHIVE_BYTES)
            extracted=Path(subject.unpack('files',item['path'],folder)[1][0])
            with source.open('rb') as stream:original_digest=hashlib.file_digest(stream,'sha256').hexdigest()
            with extracted.open('rb') as stream:extracted_digest=hashlib.file_digest(stream,'sha256').hexdigest()
            self.assertEqual(original_digest,extracted_digest)
            self.assertEqual(item['raw_file']['sha256'],original_digest)
            peak=tracemalloc.get_traced_memory()[1]
        finally:tracemalloc.stop()
        self.assertTrue(reads);self.assertLessEqual(max(reads),256*1024)
        self.assertLess(peak,16*1024*1024,'file snapshot must not allocate a 100MiB Python buffer')
        print(json.dumps(dict(real_original_bytes=source.stat().st_size,package_bytes=item['size'],
            max_original_read_bytes=max(reads),python_peak_bytes=peak,original_digest_verified=True)))
    def test_real_100mib_plus_one_is_rejected_before_any_original_body_read(self):
        source=self.base/'over-limit.bin'
        with source.open('wb') as stream:stream.truncate(subject.MAX_BYTES+1)
        folder=self.base/'package';folder.mkdir();opened=Path.open
        def open_path(path,*args,**kwargs):
            if path==source and args and args[0]=='rb':raise AssertionError('oversize body was read')
            return opened(path,*args,**kwargs)
        with patch.object(Path,'open',new=open_path):self.assertIsNone(subject.package(('files',[str(source)]),folder))
        self.assertFalse((folder/'content.bin').exists())
        self.assertEqual(source.stat().st_size,subject.MAX_BYTES+1)
    def test_real_100mib_qt_file_body_streams_while_original_mime_remains_available(self):
        source=self.base/'wire-100MiB.bin'
        with source.open('wb') as stream:stream.truncate(subject.MAX_BYTES)
        with source.open('rb') as stream:expected=hashlib.file_digest(stream,'sha256').hexdigest()
        entered=threading.Event();release=threading.Event();received=[];errors=[];reads=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_PUT(self):
                entered.set()
                try:
                    if not release.wait(8):raise AssertionError('test body hold timed out')
                    total=int(self.headers['Content-Length']);remaining=total;digest=hashlib.sha256()
                    while remaining:
                        chunk=self.rfile.read(min(256*1024,remaining))
                        if not chunk:raise AssertionError('actual body truncated')
                        digest.update(chunk);remaining-=len(chunk)
                    if total!=100*1024*1024 or digest.hexdigest()!=expected:raise AssertionError('wire body integrity mismatch')
                    received.append(total);body=b'{"schema_version":1,"verified":true}'
                    self.send_response(200);self.send_header('Content-Length',str(len(body)))
                    self.end_headers();self.wfile.write(body)
                except Exception as error:errors.append(type(error).__name__)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        api=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True);api.token='synthetic-fixture'
        clip=Clipboard();mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);clip.setMimeData(mime)
        original_read=UploadSlice.readData;result=[];heartbeats=[];loop=QEventLoop()
        def read_slice(device,maximum):
            data=original_read(device,maximum);reads.append(len(data));return data
        try:
            with patch.object(UploadSlice,'readData',new=read_slice):
                api.request('PUT','/v1/clipboard/'+str(uuid4())+'/content',lambda r,e:(result.append((r,e)),loop.quit()),
                    source=source,source_size=subject.MAX_BYTES,priority='high')
                QTimer.singleShot(0,lambda:heartbeats.append(True))
                until(lambda:entered.is_set() and bool(heartbeats))
                self.assertIs(clip.mimeData(),mime);self.assertTrue(source.exists())
                self.assertLess(sum(reads),subject.MAX_BYTES,'held network must not first buffer the entire source')
                self.assertFalse(result);release.set()
                timeout=QTimer();timeout.setSingleShot(True);timeout.timeout.connect(loop.quit);timeout.start(20000)
                loop.exec();timeout.stop();self.assertTrue(result,'100MiB local HTTP body did not complete')
            self.assertIsNone(result[0][1]);self.assertTrue(result[0][0]['verified'])
            self.assertEqual(received,[subject.MAX_BYTES]);self.assertFalse(errors)
            self.assertLessEqual(max(reads),1024*1024);self.assertEqual(sum(reads),subject.MAX_BYTES)
            self.assertIs(clip.mimeData(),mime);self.assertFalse(api._requests)
            print(json.dumps(dict(actual_local_http_body_bytes=received[0],max_upload_read_bytes=max(reads),
                ui_event_delivered_while_body_held=bool(heartbeats),native_mime_object_unchanged=True)))
        finally:
            if not result:print(json.dumps(dict(source_bytes_read=sum(reads),received_bytes=received,server_errors=errors)))
            release.set();api.close();flush();server.shutdown();server.server_close();thread.join(3)
    def test_scaled_text_and_original_image_boundaries_are_inclusive(self):
        image=QImage(2,2,QImage.Format_ARGB32);image.fill(QColor('#8a5cf0'))
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);self.assertTrue(image.save(buffer,'PNG'))
        png=bytes(buffer.data());cap=max(512,len(png));raw=png+b'\0'*(cap-len(png))
        with patch.object(subject,'MAX_BYTES',cap),patch.object(subject,'MAX_ARCHIVE_BYTES',cap+1024):
            for index,(kind,data) in enumerate((('text',dict(text='x'*cap,html='')),('image',dict(raw=raw,mime='image/png')))):
                folder=self.base/str(index);folder.mkdir();item=subject.package((kind,data),folder)
                self.assertIsNotNone(item);value=subject.unpack(kind,item['path'],folder)
                self.assertEqual(value[1]['text'] if kind=='text' else value[1]['raw'],data['text'] if kind=='text' else raw)
            for index,value in enumerate((('text',dict(text='x'*(cap+1),html='')),('image',dict(raw=raw+b'x',mime='image/png')))):
                folder=self.base/('over'+str(index));folder.mkdir();self.assertIsNone(subject.package(value,folder))
            legacy=json.dumps(dict(text='x'*cap,html='')).encode()
            self.assertEqual(subject.text_value(legacy)[1]['text'],'x'*cap)
            with self.assertRaises(ValueError):subject.text_value(json.dumps(dict(text='x'*(cap+1),html='')).encode())
            with self.assertRaises(ValueError):subject.text_value(json.dumps(dict(text='é'*(cap//2),html='x')).encode())
        with patch.object(subject,'MAX_BYTES',len(png)):
            folder=self.base/'native';folder.mkdir();self.assertIsNotNone(subject.package(('image',dict(image=image)),folder))
        with patch.object(subject,'MAX_BYTES',len(png)-1):
            folder=self.base/'native-over';folder.mkdir();self.assertIsNone(subject.package(('image',dict(image=image)),folder))
    def test_shared_file_registration_includes_100mib_but_not_one_byte_more(self):
        peer=Peer();clip=Clipboard();client=subject.ClipboardSync(peer,Events(),self.base/'profile',enabled=True,clipboard=clip)
        try:
            for size,expected in ((subject.MAX_BYTES,True),(subject.MAX_BYTES+1,False)):
                source=self.base/str(size)
                with source.open('wb') as stream:stream.truncate(size)
                task=dict(id=str(uuid4()),sequence=1,created_at=__import__('time').time(),intent={},package=None)
                client.file_jobs.register(task,('files',[str(source)]))
                self.assertEqual(task['id'] in client.file_jobs.tasks,expected)
            self.assertFalse(any(m in ('POST','PUT') for m,p,o in peer.calls))
        finally:client.close();until(lambda:not client._jobs);flush()
    def test_receive_admission_keeps_body_integrity_pending_and_rejects_over_limits(self):
        # This checks metadata admission only. No fake bytes or success result
        # are supplied; a permitted head remains un-applied until real download
        # size/hash/unpack verification succeeds.
        for kind in ('text','files','image'):
            cap=subject.MAX_BYTES if kind=='image' else subject.MAX_ARCHIVE_BYTES
            for size,accepted in ((cap,True),(cap+1,False)):
                with self.subTest(kind=kind,size=size):
                    peer=Peer();clip=Clipboard();clip.text('synthetic current native value');mime=clip.mimeData()
                    peer.head=dict(id=str(uuid4()),version=1,ready=True,kind=kind,size=size,sha256='0'*64,copied_at=1)
                    request=peer.request;downloads=[]
                    def held_body(method,path,callback,**options):
                        if path.endswith('/content'):
                            downloads.append(options['expected']);return str(uuid4())
                        return request(method,path,callback,**options)
                    peer.request=held_body
                    client=subject.ClipboardSync(peer,Events(),self.base/(kind+str(size)),enabled=False,clipboard=clip)
                    client.enabled=True
                    try:
                        client.refresh();until(lambda:bool(downloads) or not client._fetching)
                        self.assertEqual(bool(downloads),accepted)
                        if accepted:self.assertEqual(downloads[0]['size'],size)
                        self.assertIs(clip.mimeData(),mime);self.assertNotEqual(client.view['phase'],'synced')
                    finally:client.close();until(lambda:not client._jobs);flush()
    def _local_preparation(self,oversize):
        source=self.base/'local-original.bin'
        with source.open('wb') as stream:stream.truncate(subject.MAX_BYTES+1 if oversize else 1024)
        peer=Peer();clip=SequencedClipboard()
        client=subject.ClipboardSync(peer,Events(),self.base/'profile',enabled=False,clipboard=clip)
        client._sequence=lambda:clip.sequence;client._last_sequence=clip.sequence;client.enabled=True
        results=[];client.copy_result.connect(lambda ok,context:results.append(ok))
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);mime.setData('application/x-original',b'original')
        try:
            manager=patch.object(subject,'package',wraps=subject.package) if oversize else patch.object(subject,'package',side_effect=OSError('synthetic cache unavailable'))
            with manager as prepared:
                clip.setMimeData(mime);client.change_debounce.stop();client._changed()
                until(lambda:not client._jobs and (client._pending is None if oversize else
                    client.view['phase']=='error' and client.retry.isActive()))
                if oversize:
                    for _ in range(3):clip.reannounce();client.change_debounce.stop();client._changed()
                    self.assertEqual(prepared.call_count,1)
                else:
                    self.assertIsNone(client._pending);self.assertIsNotNone(client._local_retry)
                    self.assertEqual(client.retry.interval(),5000)
            self.assertIs(clip.mimeData(),mime);self.assertTrue(source.exists())
            self.assertEqual(results,[] if oversize else [False]);self.assertEqual(peer.calls,[])
            self.assertEqual(client.view['phase'],'local' if oversize else 'error')
        finally:client.close();until(lambda:not client._jobs);flush()
    def test_oversize_is_a_gray_local_policy_skip_without_failure_or_network(self):
        self._local_preparation(True)
    def test_real_prepare_io_failure_remains_failure_instead_of_policy_skip(self):
        self._local_preparation(False)
    def test_100mib_shared_journal_restores_without_upload_and_invalid_size_keeps_release_cleanup(self):
        peer=Peer();clip=Clipboard();profile=self.base/'profile'
        client=subject.ClipboardSync(peer,Events(),profile,enabled=True,clipboard=clip)
        source=self.base/'shared-100MiB.bin'
        with source.open('wb') as stream:stream.truncate(subject.MAX_BYTES)
        item=subject.package(('files',[str(source)]),client._directory())
        identity=str(uuid4());holder=str(uuid4())
        task=dict(id=identity,sequence=1,intent=dict(mono=time.monotonic(),at=round(time.time()*1000),floor=0),
            staged=True,prepared=True,created_at=time.time(),package=item)
        client.file_jobs.register(task,('files',[str(source)]));client.file_jobs.prepared(task)
        task['holders']={holder:str(source)};client.file_jobs.save();journal=client.file_jobs.journal
        peer.heads[identity]=dict(id=identity,ready=True,sha256=item['sha256'])
        peer.holds[identity]={holder};client.close();before=len(peer.calls)
        restored=subject.ClipboardSync(peer,Events(),profile,enabled=True,clipboard=clip)
        try:
            until(lambda:identity in restored.file_jobs.tasks and restored.file_jobs.tasks[identity]['_file_phase']=='ready')
            self.assertEqual(restored.file_jobs.tasks[identity]['_file_received'],subject.MAX_BYTES)
            self.assertFalse(any(m in ('POST','PUT') for m,p,o in peer.calls[before:]))
        finally:restored.close();until(lambda:not restored._jobs);flush()
        saved=json.loads(journal.read_text(encoding='utf8'));row=saved['tasks'][0];released=str(uuid4())
        row['source_stat'][2]=subject.MAX_BYTES+1;row['package']['raw_file']['size']=subject.MAX_BYTES+1
        row['releases']={released:True};journal.write_text(json.dumps(saved),encoding='utf8');before=len(peer.calls)
        invalid=subject.ClipboardSync(peer,Events(),profile,enabled=True,clipboard=clip)
        try:
            with patch.object(invalid,'_work',side_effect=AssertionError('invalid snapshot body must not be read')):
                until(lambda:identity in invalid.file_jobs.tasks and invalid.file_jobs.tasks[identity]['_file_phase']=='unavailable')
                flush()
            self.assertTrue(any(p.endswith('/release') for m,p,o in peer.calls[before:]))
            self.assertFalse(any(m=='PUT' or m=='POST' and not p.endswith('/release') for m,p,o in peer.calls[before:]))
            self.assertIn(holder,peer.holds[identity]);self.assertEqual(source.stat().st_size,subject.MAX_BYTES)
        finally:invalid.close();until(lambda:not invalid._jobs);flush()


if __name__=='__main__':unittest.main()
