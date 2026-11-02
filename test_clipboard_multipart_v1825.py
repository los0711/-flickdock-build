"""Real frozen snapshot bytes through the clipboard multipart state machine."""
import base64,copy,hashlib,math,tempfile,time,unittest,threading
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from PySide6.QtCore import QTimer,QMimeData,QUrl
import clipboard_sync as subject
from cloud_api import CloudError
from test_shared_clipboard_files_v1821 import Peer
from test_clipboard_loop_v1824 import SequencedClipboard
from test_clipboard_sync_v1819 import Events,until,flush


class MultipartPeer(Peer):
    def __init__(self):
        super().__init__();self.capability=True;self.attempt=None;self.part_indices=[];self.body_bytes=0
        self.init_count=0;self.commit_count=0;self.lose_part=False;self.lose_commit=False
        self.reject_part=False;self.hold_part=False;self.held_part=None
        self.resume_part=None
        self.part_size=1024*1024
    def capability_value(self):
        return dict(part_size=self.part_size,threshold=subject.MULTIPART_THRESHOLD,
            max_bytes=subject.MAX_BYTES,max_archive_bytes=subject.MAX_ARCHIVE_BYTES)
    def request(self,method,path,callback,**options):
        if path=='/v1/clipboard' and method=='POST':
            def staged(result,error):
                if isinstance(result,dict) and self.capability:result=dict(result,multipart_v1=self.capability_value())
                callback(result,error)
            return super().request(method,path,staged,**options)
        if '/upload' not in path:return super().request(method,path,callback,**options)
        key=str(uuid4());self.active[key]=True;self.calls.append((method,path,copy.deepcopy(options.get('data'))))
        def respond(result=None,error=None):
            if not self.active.pop(key,False):return
            if isinstance(result,dict):result=dict(result,server_time=round(time.time()*1000))
            callback(result,error)
        def deliver():
            if key not in self.active:return
            identity=path.split('/')[3]
            if path.endswith('/upload'):
                self.init_count+=1;data=options['data'];size=data['size'];part_size=self.part_size
                if self.attempt is None:
                    raw=data.get('raw_file');full=hashlib.sha256()
                    if raw:full.update(base64.b64decode(raw['prefix']))
                    self.attempt=dict(id=str(uuid4()),data=data,index=0,size=size,full=full,raw=hashlib.sha256())
                attempt=self.attempt;assert attempt['data']==data
                respond(dict(multipart_v1=True,upload_id=attempt['id'],part_size=part_size,
                    part_count=math.ceil(size/part_size),next_part=attempt['index'],expires_at=round(time.time()*1000)+600000));return
            attempt=self.attempt;assert '/'+attempt['id']+'/' in path
            if '/parts/' in path:
                if self.hold_part:
                    self.held_part=(key,callback);self.resume_part=deliver;return
                index=int(path.rsplit('/',1)[1]);self.part_indices.append(index)
                if self.reject_part:respond(error=CloudError('part size refused',413,'CLIPBOARD_LIMIT'));return
                assert index==attempt['index'];length=options['source_size'];digest=hashlib.sha256();remaining=length
                with Path(options['source']).open('rb') as stream:
                    stream.seek(options['source_offset'])
                    while remaining:
                        chunk=stream.read(min(256*1024,remaining));assert chunk
                        digest.update(chunk);attempt['raw'].update(chunk);attempt['full'].update(chunk);remaining-=len(chunk)
                assert digest.hexdigest()==options['headers']['X-Content-SHA256']
                self.body_bytes+=length;attempt['index']+=1
                if options.get('progress'):options['progress'](length,length)
                if self.lose_part:self.lose_part=False;respond(error=CloudError('lost part receipt',0,'NETWORK_UNREACHABLE'));return
                respond(dict(next_part=attempt['index']));return
            if path.endswith('/commit'):
                self.commit_count+=1
                if not attempt.get('ready'):
                    data=attempt['data'];raw=data.get('raw_file')
                    if raw:
                        attempt['full'].update(base64.b64decode(raw['suffix']))
                        assert attempt['raw'].hexdigest()==raw['sha256']
                    assert attempt['full'].hexdigest()==data['sha256']
                    size=data['size']+(len(base64.b64decode(raw['prefix']))+len(base64.b64decode(raw['suffix'])) if raw else 0)
                    attempt['ready']=dict(self.staged[identity],ready=True,kind=data['kind'],size=size,sha256=data['sha256'])
                    self.heads[identity]=attempt['ready']
                    if self.head is None or self.head.get('copied_at',0)<=attempt['ready']['copied_at']:self.head=attempt['ready']
                if self.lose_commit:self.lose_commit=False;respond(error=CloudError('lost commit receipt',0,'NETWORK_UNREACHABLE'));return
                respond(dict(clipboard=attempt['ready'],shared_files=True));return
            raise AssertionError((method,path))
        QTimer.singleShot(0,deliver);return key


class MultipartTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-multipart-');self.base=Path(self.temp.name)
        self.threshold=patch.object(subject,'MULTIPART_THRESHOLD',1024);self.threshold.start()
        self.peer=MultipartPeer();self.clip=SequencedClipboard()
        self.client=subject.ClipboardSync(self.peer,Events(),self.base/'profile',enabled=True,clipboard=self.clip)
        self.client._sequence=lambda:self.clip.sequence;self.client._last_sequence=self.clip.sequence
        self.client._clock_anchor=(time.monotonic(),round(time.time()*1000));self.results=[]
        self.client.copy_result.connect(lambda ok,context:self.results.append(ok))
    def tearDown(self):
        self.client.close();until(lambda:not self.client._jobs);flush();self.threshold.stop();self.temp.cleanup()
    def copy_file(self,size=2*1024*1024+17):
        source=self.base/'original.bin'
        with source.open('wb') as stream:stream.truncate(size)
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);mime.setData('application/x-original-format',b'original')
        self.clip.setMimeData(mime);self.client.change_debounce.stop();self.client._changed()
        return source,mime
    def receive_old_ready_head(self):
        item=subject.inline_text(('text',dict(text='synthetic previous ready head',html='')))
        head=dict(id=str(uuid4()),version=1,ready=True,kind='text',size=item['size'],sha256=item['sha256'],
            inline=item['inline']['body'],copied_at=round(time.time()*1000)-1000)
        self.peer.head=head;self.peer.heads[head['id']]=head;self.client.refresh()
        until(lambda:self.client._seen==head['id'] and not self.client._fetching)
        return head['id']
    def test_exact_parts_preserve_original_and_commit_is_the_only_success(self):
        source,mime=self.copy_file();until(lambda:self.results==[True])
        self.assertEqual(self.peer.body_bytes,source.stat().st_size);self.assertEqual(self.peer.part_indices,[0,1,2])
        self.assertEqual(self.peer.commit_count,1);self.assertIs(self.clip.mimeData(),mime)
        self.assertFalse(any(m=='PUT' and p.endswith('/content') for m,p,o in self.peer.calls))
    def test_lost_part_receipt_reinitializes_from_next_part_without_reuploading(self):
        self.peer.lose_part=True;source,mime=self.copy_file()
        until(lambda:self.results==[False] and not self.client._pending.get('uploading'))
        self.client.retry.stop();self.client._retry();until(lambda:self.results==[False,True])
        self.assertEqual(self.peer.init_count,2);self.assertEqual(self.peer.part_indices,[0,1,2])
        self.assertEqual(self.peer.body_bytes,source.stat().st_size);self.assertIs(self.clip.mimeData(),mime)
    def test_lost_commit_receipt_does_not_resend_any_body_or_create_another_head(self):
        self.peer.lose_commit=True;source,mime=self.copy_file()
        until(lambda:self.results==[False] and not self.client._pending.get('uploading'))
        identity=self.peer.head['id'];self.client.retry.stop();self.client._retry()
        until(lambda:self.results==[False,True])
        self.assertEqual(self.peer.head['id'],identity);self.assertEqual(self.peer.body_bytes,source.stat().st_size)
        self.assertEqual(self.peer.commit_count,2);self.assertEqual(self.peer.part_indices,[0,1,2])
    def test_missing_capability_stops_once_and_same_native_noise_never_reposts(self):
        old_head=self.receive_old_ready_head()
        self.peer.capability=False;source,mime=self.copy_file();until(lambda:self.results==[False] and self.client._pending is None)
        self.assertFalse(self.client.retry.isActive());before=len(self.peer.calls)
        for _ in range(5):self.clip.reannounce();self.client.change_debounce.stop();self.client._changed()
        self.client._retry();flush()
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
        self.assertEqual(self.results,[False]);self.assertEqual(self.peer.init_count,0);self.assertIs(self.clip.mimeData(),mime)
        self.assertEqual(self.peer.head['id'],old_head)
    def test_part_413_is_terminal_without_retries_or_success_or_local_mutation(self):
        old_head=self.receive_old_ready_head()
        self.peer.reject_part=True;source,mime=self.copy_file();until(lambda:self.results==[False] and self.client._pending is None)
        self.assertFalse(self.client.retry.isActive());self.assertEqual(self.peer.part_indices,[0])
        before=len(self.peer.calls)
        for _ in range(4):self.clip.reannounce();self.client.change_debounce.stop();self.client._changed();self.client.file_jobs._retry()
        self.client._retry();flush()
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
        self.assertEqual(self.results,[False]);self.assertIs(self.clip.mimeData(),mime)
        self.assertEqual(self.peer.head['id'],old_head)
    def test_disable_and_late_part_callback_never_commit_or_notice(self):
        self.peer.hold_part=True;source,mime=self.copy_file();until(lambda:self.peer.held_part is not None)
        key,callback=self.peer.held_part;self.client.set_enabled(False);before=len(self.peer.calls)
        callback(dict(next_part=1),None);flush()
        self.assertEqual(len(self.peer.calls),before);self.assertEqual(self.results,[])
        self.assertEqual(self.peer.commit_count,0);self.assertIs(self.clip.mimeData(),mime)
    def test_unchanged_small_file_uses_the_existing_single_put(self):
        with patch.object(subject,'MULTIPART_THRESHOLD',24*1024*1024):
            source,mime=self.copy_file();until(lambda:bool(self.peer.held))
            self.peer.finish(self.client.view['id']);until(lambda:self.results==[True])
        self.assertEqual(self.peer.init_count,0);self.assertEqual(len(self.peer.puts),1)
        self.assertIs(self.clip.mimeData(),mime)
    def test_actual_24mib_threshold_uses_bounded_4mib_parts_for_large_snapshot(self):
        self.peer.part_size=4*1024*1024
        with patch.object(subject,'MULTIPART_THRESHOLD',24*1024*1024):
            source,mime=self.copy_file(size=25*1024*1024+1);until(lambda:self.results==[True])
        self.assertEqual(self.peer.part_indices,list(range(7)))
        self.assertEqual(self.peer.body_bytes,source.stat().st_size)
        self.assertEqual(self.peer.commit_count,1);self.assertIs(self.clip.mimeData(),mime)
    def test_terminal_failure_is_not_replayed_after_restart_with_a_shelf_holder(self):
        self.peer.hold_part=True;source,mime=self.copy_file();until(lambda:self.peer.held_part is not None)
        task=self.client._pending;item_id=str(uuid4());claimed=[]
        self.assertTrue(self.client.file_jobs.claim(str(source),item_id,lambda value:claimed.append(value)))
        until(lambda:bool(claimed));self.assertIsNotNone(claimed[0])
        key,callback=self.peer.held_part;self.peer.active.pop(key,None)
        callback(None,CloudError('part size refused',413,'CLIPBOARD_LIMIT'))
        until(lambda:self.results==[False] and self.client._pending is None)
        self.client.close();before=len(self.peer.calls)
        self.client=subject.ClipboardSync(self.peer,Events(),self.base/'profile',enabled=True,clipboard=self.clip)
        until(lambda:task['id'] in self.client.file_jobs.tasks and self.client.file_jobs.tasks[task['id']]['_file_phase']=='unavailable')
        flush();self.assertFalse(any(m in ('POST','PUT') and not p.endswith('/release') for m,p,o in self.peer.calls[before:]))
        self.assertIn(item_id,self.peer.holds[task['id']]);self.assertIs(self.clip.mimeData(),mime)
    def test_service_capability_recovery_allows_a_new_copy_not_obsolete_replay(self):
        self.peer.capability=False;source,mime=self.copy_file();until(lambda:self.results==[False])
        old_id=self.client.view['id'];before=len(self.peer.calls)
        self.peer.capability=True;self.clip.text('synthetic different new content')
        until(lambda:self.results==[False,True]);self.copy_file();until(lambda:self.results==[False,True,True])
        self.assertNotEqual(self.peer.head['id'],old_id)
        self.assertFalse(any(m=='POST' and p=='/v1/clipboard' and o.get('id')==old_id for m,p,o in self.peer.calls[before:]))
        self.assertEqual(self.peer.body_bytes,source.stat().st_size)
    def test_new_copy_cancels_unreferenced_held_multipart_without_old_commit(self):
        self.peer.hold_part=True;source,mime=self.copy_file();until(lambda:self.peer.held_part is not None)
        old_key=self.peer.held_part[0];self.clip.text('synthetic new current clipboard')
        until(lambda:self.peer.head is not None and self.peer.head.get('kind')=='text')
        identity=self.peer.head['id'];self.peer.hold_part=False;self.peer.resume_part();flush()
        self.assertNotIn(old_key,self.peer.active);self.assertEqual(self.peer.head['id'],identity)
        self.assertEqual(self.peer.commit_count,0);self.assertEqual(self.results,[True])
    def test_shelf_held_multipart_finishes_without_replacing_newer_clipboard(self):
        self.peer.hold_part=True;source,mime=self.copy_file();until(lambda:self.peer.held_part is not None)
        old_id=self.client.view['id'];item_id=str(uuid4());claimed=[]
        self.assertTrue(self.client.file_jobs.claim(str(source),item_id,lambda value:claimed.append(value)))
        until(lambda:bool(claimed));self.assertIsNotNone(claimed[0])
        self.clip.text('synthetic newer current clipboard')
        until(lambda:self.peer.head is not None and self.peer.head.get('kind')=='text');new_id=self.peer.head['id']
        self.peer.hold_part=False;self.peer.resume_part()
        until(lambda:self.client.file_jobs.tasks[old_id]['_file_phase']=='ready')
        self.assertEqual(self.peer.head['id'],new_id);self.assertEqual(self.client.view['id'],new_id)
        self.assertEqual(self.peer.body_bytes,source.stat().st_size);self.assertEqual(self.results,[True])
    def test_missing_or_resized_frozen_snapshot_is_terminal_not_reinitialized_forever(self):
        for change in ('missing','resized'):
            with self.subTest(change=change):
                if self.peer.attempt is not None:
                    # Each real Copy has its own immutable server attempt.
                    self.peer.attempt=None
                request=self.peer.request;mutated=[False]
                def mutate_after_init(method,path,callback,**options):
                    if path.endswith('/upload') and not mutated[0]:
                        def initialized(result,error):
                            mutated[0]=True;cache=Path(self.client._pending['package']['path'])
                            if change=='missing':cache.unlink()
                            else:
                                with cache.open('ab') as stream:stream.write(b'x')
                            callback(result,error)
                        return request(method,path,initialized,**options)
                    return request(method,path,callback,**options)
                self.peer.request=mutate_after_init;before_results=len(self.results)
                source,mime=self.copy_file();until(lambda:len(self.results)>before_results and self.client._pending is None)
                self.assertFalse(self.results[-1]);self.assertFalse(self.client.retry.isActive());count=len(self.peer.calls)
                for _ in range(3):self.clip.reannounce();self.client.change_debounce.stop();self.client._changed();self.client._retry()
                flush();self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[count:]))
                self.assertIs(self.clip.mimeData(),mime);self.peer.request=request
    def test_transient_snapshot_permission_error_remains_retryable_and_recovers(self):
        request=self.peer.request;armed=[False];denied=[False];opened=Path.open
        def arm_after_init(method,path,callback,**options):
            if path.endswith('/upload'):
                def initialized(result,error):armed[0]=True;callback(result,error)
                return request(method,path,initialized,**options)
            return request(method,path,callback,**options)
        def temporarily_denied(path,*args,**kwargs):
            if armed[0] and not denied[0] and path.name=='content.bin' and args and args[0]=='rb':
                denied[0]=True;raise PermissionError('synthetic temporary cache lock')
            return opened(path,*args,**kwargs)
        self.peer.request=arm_after_init
        with patch.object(Path,'open',new=temporarily_denied):
            source,mime=self.copy_file();until(lambda:self.results==[False] and not self.client._pending.get('uploading'))
            self.client.retry.stop();self.client._retry();until(lambda:self.results==[False,True])
        self.assertEqual(self.peer.init_count,2);self.assertEqual(self.peer.body_bytes,source.stat().st_size)
        self.assertIs(self.clip.mimeData(),mime)
    def test_new_local_package_runs_before_queued_old_probe_when_a_worker_becomes_free(self):
        first=threading.Event();second=threading.Event();old=threading.Event()
        started_first=threading.Event();started_second=threading.Event();order=[]
        def blocked(gate,started):
            def action(cancel):started.set();gate.wait(5);return None
            job=self.client._work(action,lambda _:None);job._shared_probe=True
        blocked(first,started_first);blocked(second,started_second)
        until(lambda:started_first.is_set() and started_second.is_set())
        def old_probe(cancel):order.append('old-probe');old.wait(5);return None
        job=self.client._work(old_probe,lambda _:None);job._shared_probe=True
        original_package=subject.package
        def new_package(*args,**kwargs):order.append('new-package');return original_package(*args,**kwargs)
        try:
            with patch.object(subject,'package',side_effect=new_package):
                self.copy_file(size=1024);first.set();until(lambda:bool(order))
                self.assertEqual(order[0],'new-package','queued old probe must not occupy the first freed worker before the latest Copy')
                old.set();second.set();until(lambda:self.results==[True])
            self.assertIn('old-probe',order);self.assertEqual(self.client.pool.maxThreadCount(),2)
        finally:first.set();second.set();old.set()


if __name__=='__main__':unittest.main()
