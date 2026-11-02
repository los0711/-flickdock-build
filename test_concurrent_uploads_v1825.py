"""Real ShelfSync callbacks with a controlled, held transport (not cloud proof)."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from test_small_upload_lane import APP, Api, Service, Adapter, spin
from shelf_sync import ShelfSync
from shelf_payload_gate import ShelfPayloadGate
from shelf_stage_store import ShelfStageStore
from cloud_api import CloudError


class PerFileFailureApi(Api):
    """Real protocol callbacks; one chosen object fails once, others stay held."""
    def __init__(self,name,problem):
        super().__init__();self.failure_name=name;self.problem=problem
        self.failed=False;self.raw_part_callbacks=[]

    def request(self,method,path,callback,**kwargs):
        if '/parts/' in path:self.raw_part_callbacks.append(callback)
        def delivered(result,error):
            if ('/parts/' in path and not self.failed
                    and self.uploads[path.split('/')[3]]['name']==self.failure_name):
                self.failed=True;callback(None,self.problem)
            else:callback(result,error)
        return super().request(method,path,delivered,**kwargs)


class ConcurrentUploads(unittest.TestCase):
    def test_three_real_qt_file_streams_arrive_before_any_upload_acknowledgement(self):
        import hashlib,json,threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from cloud_api import CloudApi
        release=threading.Event();lock=threading.Lock();uploads={};received={};committed=set()
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def reply(self,value):
                body=json.dumps(dict(schema_version=1,**value)).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
                if self.path=='/v1/uploads':
                    with lock:uploads[value['upload_id']]=value
                    self.reply(dict(upload_id=value['upload_id'],chunk_size=value['size'],part_count=1))
                elif self.path.endswith('/commit'):
                    identity=self.path.split('/')[3]
                    with lock:
                        row=uploads[identity]
                        if received.get(identity)!=(row['size'],row['sha256']):raise AssertionError('commit before exact bytes were verified')
                        committed.add(identity)
                    self.reply(dict(record=dict(id=row['item_id'],version=1,sha256=row['sha256'])))
                elif self.path=='/v1/sync/intake':self.reply({})
                else:raise AssertionError('Unexpected controlled protocol POST')
            def do_PUT(self):
                identity=self.path.split('/')[3];expected=int(self.headers['Content-Length']);count=0;digest=hashlib.sha256()
                while count<expected:
                    block=self.rfile.read(min(65536,expected-count))
                    if not block:raise AssertionError('Incomplete actual Qt stream')
                    count+=len(block);digest.update(block)
                with lock:received[identity]=(count,digest.hexdigest())
                if not release.wait(15):return
                self.reply({})
            def do_GET(self):
                if self.path.startswith('/v1/sync/changes'):self.reply(dict(changes=[],cursor='0',has_more=False,transfers=[]))
                else:raise AssertionError('Unexpected controlled protocol GET')
            def do_DELETE(self):self.reply({})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=[]
            for i in range(3):
                path=root/f'original-{i}.bin'
                with path.open('wb') as stream:stream.truncate(5*1024*1024+i)
                paths.append(str(path))
            expected={Path(path).name:(Path(path).stat().st_size,hashlib.sha256(Path(path).read_bytes()).hexdigest()) for path in paths}
            api=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True);api.token='isolated-fixture'
            service=Service(api);adapter=Adapter();adapter.view['permanent']=paths
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:len(received)==3,seconds=8),'all three actual QFile streams must arrive while every PUT acknowledgement remains held')
                self.assertFalse(release.is_set());self.assertFalse(committed);self.assertEqual(len(uploads),3)
                self.assertEqual({row['name']:received[identity] for identity,row in uploads.items()},expected)
                from PySide6.QtCore import QFile
                held=[state for state in api._requests.values() if state['method']=='PUT']
                self.assertEqual(len(held),3)
                self.assertTrue(all(isinstance(state['source_file'],QFile) and state['source_file'].isOpen() for state in held),
                                'each actual request must retain a streamed QFile rather than a full raw payload')
                release.set()
                self.assertTrue(spin(lambda:len(committed)==3 and not engine.running,seconds=8))
                self.assertEqual(len(uploads),3,'no replacement reservation or duplicate complete upload')
                for path in paths:self.assertEqual((Path(path).stat().st_size,hashlib.sha256(Path(path).read_bytes()).hexdigest()),expected[Path(path).name])
            finally:
                release.set();engine.close();api.close();APP.processEvents()
                server.shutdown();server.server_close();worker.join(3)

    def test_new_file_uses_free_main_slot_while_both_auxiliary_uploads_are_held(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);paths=[]
            for index in range(4):
                path=root/f'tail-{index}.bin';path.write_bytes(str(index).encode());paths.append(str(path))
            api=Api();api.hold_small=True;service=Service(api);adapter=Adapter()
            adapter.view['permanent']=paths[:3]
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:len(api.held)==3))
                main_name=engine.transfer['name'];main_key=next(key for key in api.held if api.held_names[key]==main_name)
                api.held.pop(main_key)()
                self.assertTrue(spin(lambda:main_name in api.committed and engine.transfer is None))
                self.assertTrue(engine.running);self.assertEqual(len(engine.small_lane.tasks),2)
                neighbor_keys=set(api.held)
                adapter.view['permanent'].append(paths[3]);engine.local_changed()
                self.assertTrue(spin(lambda:len(api.held)==3,seconds=1),
                                'the explicit upload-tail state must reuse its idle main slot')
                self.assertTrue(neighbor_keys.issubset(api.pending));self.assertFalse(api.cancelled)
                self.assertEqual(engine.transfer['name'],'tail-3.bin')
                self.assertEqual(len(engine.small_lane.tasks),2)
            finally:engine.close()

    def test_receive_cycle_cannot_be_reentered_as_an_idle_upload_tail(self):
        from uuid import uuid4
        class HeldChangesApi(Api):
            def __init__(self):super().__init__();self.changes={}
            def request(self,method,path,callback,**kwargs):
                if path.startswith('/v1/sync/changes'):
                    key=uuid4().hex;self.pending[key]=True
                    def release():
                        if self.pending.pop(key,False):callback(dict(changes=[],cursor='0',has_more=False,transfers=[]),None)
                    self.changes[key]=release;return key
                return super().request(method,path,callback,**kwargs)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'incoming-cycle-local.bin';path.write_bytes(b'local original')
            api=HeldChangesApi();api.hold_small=True;service=Service(api);adapter=Adapter()
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:bool(api.changes)))
                self.assertTrue(engine.running);self.assertIsNone(engine.transfer)
                adapter.view['permanent'].append(str(path));engine.local_changed()
                # A clipboard lease release has the same queued continuation
                # path as a completed auxiliary request.
                engine.set_clipboard_priority(True);engine.set_clipboard_priority(False)
                self.assertFalse(spin(lambda:bool(api.uploads),seconds=.15),
                                 'receive owns the main cycle until its ordered callback completes')
                self.assertFalse(engine.small_lane.tasks)
                # A leftover upload token is not proof that receive released
                # its cycle either; the explicit phase owns this decision.
                engine.transfer=dict(id='prior-upload',name='prior-upload')
                engine.small_lane.kick();engine.small_lane._continue()
                self.assertFalse(spin(lambda:bool(api.uploads),seconds=.1))
                engine.transfer=None
                for release in list(api.changes.values()):release()
                self.assertTrue(spin(lambda:len(api.held)==1))
                self.assertEqual(path.read_bytes(),b'local original')
            finally:engine.close()

    def test_large_lane_cancel_and_priority_release_reuse_slot_without_cancelling_neighbors(self):
        import hashlib
        class StreamingApi(Api):
            supports_file_upload=True
            supports_file_slices=True
            def request(self,method,path,callback,**options):
                if options.get('source'):
                    with Path(options['source']).open('rb') as stream:
                        stream.seek(options.get('source_offset',0));options['raw']=stream.read(options.get('source_size',-1))
                elif method=='PUT' and len(options.get('raw',b''))>4*1024*1024:
                    raise AssertionError('large body must be a file device')
                return super().request(method,path,callback,**options)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);paths=[]
            for i in range(3):
                path=root/f'large-{i}.bin'
                with path.open('wb') as stream:stream.truncate(5*1024*1024+i)
                paths.append(str(path))
            small=root/'neighbor.bin';small.write_bytes(b'neighbor');paths.append(str(small))
            hashes={path:hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}
            api=StreamingApi();api.hold_small=True;service=Service(api);adapter=Adapter();adapter.view['permanent']=paths
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:len(api.held)==3))
                canceled=next(iter(engine.small_lane.tasks.values()))
                neighbors={key for key in api.held if api.held_names[key]!=canceled['name']}
                engine.set_clipboard_priority(True);self.assertTrue(engine.cancel_upload(canceled['ref']))
                self.assertFalse(spin(lambda:len(api.uploads)>3,seconds=.15),'priority prevents a new bulk admission')
                self.assertTrue(neighbors.issubset(api.pending));self.assertTrue(neighbors.isdisjoint(api.cancelled))
                engine.set_clipboard_priority(False)
                self.assertTrue(spin(lambda:len(api.uploads)==4 and len(api.held)==3,seconds=1.5),
                                'release must wake free upload slots even while the main PUT is held')
                for _ in range(5):
                    for key in list(api.held):api.held.pop(key)()
                    if spin(lambda:len(api.committed)==3 or bool(api.held)) and len(api.committed)==3:break
                self.assertEqual(len(api.committed),3);self.assertNotIn(canceled['name'],api.committed)
                for path,digest in hashes.items():self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),digest)
            finally:engine.close()

    def test_streaming_admission_does_not_probe_network_paths_on_gui_thread(self):
        from PySide6.QtCore import QObject
        from types import SimpleNamespace
        from small_upload_lane import SmallUploadLane
        owner=QObject();owner.api=SimpleNamespace(supports_file_upload=True,supports_file_slices=True)
        owner.clipboard_bridge=None;owner.payload_gate=ShelfPayloadGate(owner);owner.state={};owner.account_id=None;owner.stage_store=ShelfStageStore(owner);lane=SmallUploadLane(owner)
        entry=dict(id='admission',ref='//unavailable-host/share/tree',name='tree',version=0,
                   dirty=True,fingerprint='known-view-stamp',kind='file')
        with patch('small_upload_lane.Path.is_file',side_effect=AssertionError('GUI is_file')) as files,\
             patch('small_upload_lane.Path.is_dir',side_effect=AssertionError('GUI is_dir')) as dirs,\
             patch('small_upload_lane.Path.stat',side_effect=AssertionError('GUI stat')) as stats:
            self.assertTrue(lane.eligible(entry,{}))
            files.assert_not_called();dirs.assert_not_called();stats.assert_not_called()
        lane.reset();owner.deleteLater()

    def test_large_admission_needs_both_stream_capabilities_and_keeps_version_and_holder_fences(self):
        from PySide6.QtCore import QObject
        from types import SimpleNamespace
        from small_upload_lane import SmallUploadLane
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);large=root/'large.bin'
            with large.open('wb') as stream:stream.truncate(5*1024*1024)
            tree=root/'tree';tree.mkdir()
            owner=QObject();owner.clipboard_bridge=None
            owner.api=SimpleNamespace();owner.payload_gate=ShelfPayloadGate(owner);owner.state={};owner.account_id=None;owner.stage_store=ShelfStageStore(owner);lane=SmallUploadLane(owner)
            entry=dict(id='admission',ref=str(large),name='large.bin',version=0,
                       dirty=True,fingerprint='stamp',kind='file')
            try:
                for whole,slices in ((False,False),(True,False),(False,True),(True,True)):
                    owner.api.supports_file_upload=whole;owner.api.supports_file_slices=slices
                    for path in (large,tree):
                        entry['ref']=str(path)
                        self.assertEqual(lane.eligible(entry,{}),whole and slices)
                entry.update(ref=str(large),version=1)
                self.assertFalse(lane.eligible(entry,{}),'existing versions need separate conflict handling')
                entry['version']=0;owner.clipboard_bridge=SimpleNamespace(waiting=lambda value:True)
                self.assertFalse(lane.eligible(entry,{}),'clipboard holder still owns this exact upload')
            finally:lane.reset();owner.deleteLater()

    def test_permanent_directory_staging_error_blocks_only_that_revision_until_source_changes(self):
        import zipfile,io
        import sync_files
        class StreamingApi(Api):
            supports_file_upload=True
            supports_file_slices=True
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);first=root/'main.bin';first.write_bytes(b'main')
            bad=root/'tree';bad.mkdir();link=bad/'unsupported-link';link.write_bytes(b'original')
            others=[root/'neighbor-1.bin',root/'neighbor-2.bin']
            for path in others:path.write_bytes(path.name.encode())
            api=StreamingApi();api.hold_small=True;service=Service(api);adapter=Adapter()
            adapter.view['permanent']=[str(first),str(bad),*map(str,others)]
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);checks=[];original_is_link=sync_files._is_link
            def classified(path):
                if Path(path)==link:checks.append(str(path));return True
                return original_is_link(path)
            try:
                # Exercise the real stage_item link rejection without requiring
                # native symlink privileges on either desktop platform.
                with patch('sync_files._is_link',side_effect=classified):
                    service.account_changed.emit({'id':'fixture'})
                    self.assertTrue(spin(lambda:any(e.get('blocked_fingerprint')==e.get('fingerprint')
                        for e in engine.state['entries'].values() if e['ref']==str(bad))),
                        'permanent stage validation must reach the same terminal state as the main slot')
                    entry=next(e for e in engine.state['entries'].values() if e['ref']==str(bad))
                    self.assertNotIn(entry['id'],engine.small_lane.retry_at)
                    self.assertTrue(spin(lambda:len(api.held)==3))
                    self.assertEqual(checks,[str(link)])
                    for _ in range(8):engine.small_lane.kick();APP.processEvents()
                    self.assertEqual(checks,[str(link)],'no automatic retry of an unchanged rejected revision')
                    self.assertEqual(link.read_bytes(),b'original')
                    link.unlink();(bad/'fixed.txt').write_bytes(b'corrected content');engine.local_changed()
                    for _ in range(5):
                        for key in list(api.held):api.held.pop(key)()
                        if spin(lambda:'tree' in api.committed or bool(api.held)) and 'tree' in api.committed:break
                    self.assertIn('tree',api.committed)
                    with zipfile.ZipFile(io.BytesIO(api.committed['tree'])) as archive:
                        self.assertEqual(archive.read('fixed.txt'),b'corrected content')
                    self.assertEqual((bad/'fixed.txt').read_bytes(),b'corrected content')
            finally:engine.close()

    def test_late_directory_validation_error_cannot_block_a_newer_revision(self):
        import threading,zipfile,io
        import sync_files
        class StreamingApi(Api):
            supports_file_upload=True
            supports_file_slices=True
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);first=root/'main.bin';first.write_bytes(b'main')
            tree=root/'tree';tree.mkdir();(tree/'original.txt').write_bytes(b'original')
            neighbor=root/'neighbor.bin';neighbor.write_bytes(b'neighbor')
            api=StreamingApi();api.hold_small=True;service=Service(api);adapter=Adapter()
            adapter.view['permanent']=[str(first),str(tree),str(neighbor)]
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);entered=threading.Event();release=threading.Event();original_write=sync_files._write_sync_file
            first_write=[True]
            def delayed(*args):
                if first_write[0]:
                    first_write[0]=False;entered.set();release.wait(3)
                    raise ValueError('validation of the superseded directory revision failed')
                return original_write(*args)
            try:
                with patch('sync_files._write_sync_file',side_effect=delayed):
                    service.account_changed.emit({'id':'fixture'});self.assertTrue(spin(entered.is_set))
                    entry=next(e for e in engine.state['entries'].values() if e['ref']==str(tree));old_seq=entry['seq']
                    (tree/'new.txt').write_bytes(b'new revision')
                    title=adapter.title;adapter.title=lambda ref:'updated-tree' if ref==str(tree) else title(ref)
                    engine.local_changed();self.assertGreater(entry['seq'],old_seq);release.set()
                    self.assertTrue(spin(lambda:len(api.held)==3))
                    self.assertNotEqual(entry.get('blocked_fingerprint'),entry['fingerprint'])
                    for _ in range(5):
                        for key in list(api.held):api.held.pop(key)()
                        if spin(lambda:'updated-tree' in api.committed or bool(api.held)) and 'updated-tree' in api.committed:break
                    self.assertNotIn('tree',api.committed)
                    with zipfile.ZipFile(io.BytesIO(api.committed['updated-tree'])) as archive:
                        self.assertEqual(archive.read('new.txt'),b'new revision')
                        self.assertEqual(archive.read('original.txt'),b'original')
                    self.assertEqual((tree/'original.txt').read_bytes(),b'original')
            finally:release.set();engine.close()

    def test_large_files_and_directory_share_bounded_streaming_slots(self):
        import hashlib,zipfile
        class StreamingApi(Api):
            supports_file_upload=True
            supports_file_slices=True
            def __init__(self):
                super().__init__();self.sources=[];self.hold_small=True
            def request(self,method,path,callback,**options):
                if method=='PUT':
                    self.assert_stream(options)
                return super().request(method,path,callback,**options)
            def assert_stream(self,options):
                if options.get('source'):
                    self.sources.append(str(options['source']))
                    # Controlled fixture consumer only; production CloudApi
                    # uses a QFile/UploadSlice, not a Python bytes payload.
                    options['raw']=Path(options['source']).read_bytes()
                elif len(options.get('raw',b''))>4*1024*1024:
                    raise AssertionError('large upload was materialized into raw bytes')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);paths=[];digests={}
            for i in range(3):
                path=root/f'large-{i}.bin'
                with path.open('wb') as stream:stream.truncate(5*1024*1024+i)
                paths.append(str(path));digests[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
            directory=root/'tree';directory.mkdir();(directory/'original.txt').write_text('original',encoding='utf8')
            paths.append(str(directory));api=StreamingApi();service=Service(api);adapter=Adapter()
            adapter.view['permanent']=paths
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:len(api.held)==3),'large files must fill the same bounded slots')
                self.assertEqual(len(api.uploads),3);self.assertEqual(len(api.sources),3)
                first=next(iter(api.held));api.held.pop(first)()
                self.assertTrue(spin(lambda:len(api.uploads)==4 and len(api.held)==3),'directory must take the freed slot')
                for _ in range(4):
                    for key in list(api.held):api.held.pop(key)()
                    if spin(lambda:len(api.committed)==4 or bool(api.held)) and len(api.committed)==4:break
                self.assertEqual(len(api.committed),4)
                import io
                with zipfile.ZipFile(io.BytesIO(api.committed['tree'])) as archive:
                    self.assertEqual(archive.read('original.txt'),b'original')
                for path,digest in digests.items():self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),digest)
                self.assertEqual((directory/'original.txt').read_bytes(),b'original')
            finally:engine.close()

    def setup_files(self,root,api,count=7):
        api.hold_small=True;service=Service(api);adapter=Adapter()
        paths=[]
        for i in range(count):
            path=root/f'file-{i}.bin';path.write_bytes(f'original-{i}'.encode());paths.append(str(path))
        adapter.view['permanent']=paths
        engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
        engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
        self.assertTrue(spin(lambda:len(api.held)==3))
        return engine,service,adapter,paths

    def test_main_object_failure_frees_slot_without_delaying_neighbors_or_new_reservation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);api=PerFileFailureApi('file-0.bin',CloudError('one object temporary failure',503,'SERVICE_UNAVAILABLE'))
            engine,service,adapter,paths=self.setup_files(root,api)
            try:
                original_upload=next(k for k,v in api.uploads.items() if v['name']=='file-0.bin')
                main_key=next(k for k in api.held if api.held_names[k]=='file-0.bin')
                api.held.pop(main_key)()
                self.assertTrue(spin(lambda:len(api.uploads)==4 and len(api.held)==3,seconds=1.5),
                                'a failed object must release its slot while other files keep uploading')
                self.assertFalse(engine._paused('not_before'),'one object failure is not an account cooldown')
                identity=api.uploads[original_upload]['item_id']
                states=[];engine.transfers_changed.connect(states.append);engine._notify_transfers()
                feedback=next(row for row in states[-1] if row['id']==identity)
                self.assertEqual(feedback['phase'],'retrying')
                self.assertIn('one object temporary failure',feedback['message'])
                self.assertEqual(engine.state['resumable_uploads'][identity]['upload_id'],original_upload)
                # Controlled retry deadline, as in the existing small-lane
                # resume test: no real 30-second sleep and no new upload ID.
                engine.small_lane.retry_at[identity]=0
                engine.small_lane.retry.stop()
                for _ in range(12):
                    held=list(api.held)
                    for key in held:api.held.pop(key)()
                    if spin(lambda:len(api.committed)==7 or len(api.held)>0,seconds=.8) and len(api.committed)==7:break
                self.assertEqual(len(api.committed),7)
                self.assertEqual(len(api.uploads),7,'retry must reuse its existing server reservation')
                self.assertEqual([key for key,row in api.uploads.items() if row['item_id']==identity],[original_upload])
                for i,path in enumerate(paths):self.assertEqual(Path(path).read_bytes(),f'original-{i}'.encode())
            finally:engine.close()

    def test_lane_failure_reports_its_own_retry_state_and_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);api=PerFileFailureApi('file-1.bin',CloudError('one lane temporary failure',503,'SERVICE_UNAVAILABLE'))
            engine,service,adapter,paths=self.setup_files(root,api)
            try:
                upload=next(row for row in api.uploads.values() if row['name']=='file-1.bin')
                key=next(k for k in api.held if api.held_names[k]=='file-1.bin');api.held.pop(key)()
                self.assertTrue(spin(lambda:len(api.uploads)==4 and len(api.held)==3,seconds=1.5))
                states=[];engine.transfers_changed.connect(states.append);engine._notify_transfers()
                feedback=next(row for row in states[-1] if row['id']==upload['item_id'])
                self.assertEqual(feedback['phase'],'retrying')
                self.assertIn('one lane temporary failure',feedback['message'])
                self.assertFalse(engine._paused('not_before'))
            finally:engine.close()

    def test_account_authentication_and_global_quota_errors_do_not_launch_more_objects(self):
        cases=((401,'INVALID_SESSION'),(503,'PLATFORM_QUOTA'))
        for name in ('file-0.bin','file-1.bin'):
            for status,code in cases:
                with self.subTest(failing=name,status=status,code=code),tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);api=PerFileFailureApi(name,CloudError('account-level failure',status,code))
                    engine,service,adapter,paths=self.setup_files(root,api)
                    try:
                        key=next(k for k in api.held if api.held_names[k]==name)
                        api.held.pop(key)();self.assertTrue(spin(lambda:api.failed))
                        # Remaining already-owned work may settle, but it must
                        # not refill slots past an account-level failure gate.
                        for other in list(api.held):api.held.pop(other)()
                        self.assertFalse(spin(lambda:len(api.uploads)>3,seconds=.25),
                                         'global/auth errors must not be treated as independent file retry')
                    finally:engine.close()

    def test_cancel_one_lane_immediately_reuses_free_slot_without_touching_neighbors(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);api=Api();engine,service,adapter,paths=self.setup_files(root,api)
            try:
                cancelled=next(iter(engine.small_lane.tasks.values()))
                cancelled_name=cancelled['name'];other_keys={k for k in api.held if api.held_names[k]!=cancelled_name}
                self.assertTrue(engine.cancel_upload(cancelled['ref']))
                self.assertTrue(spin(lambda:len(api.uploads)==4 and len(api.held)==3,seconds=1.5),
                                'cancelled slot must not wait for a stalled neighboring transfer to complete')
                self.assertTrue(other_keys.issubset(api.pending));self.assertTrue(other_keys.isdisjoint(api.cancelled))
                self.assertTrue(all(Path(path).exists() for path in paths))
            finally:engine.close()

    def test_account_switch_discards_all_three_late_payload_callbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);api=PerFileFailureApi('none',CloudError('unused',503))
            engine,service,adapter,paths=self.setup_files(root,api)
            try:
                callbacks=list(api.raw_part_callbacks);self.assertEqual(len(callbacks),3)
                service.account_changed.emit({'id':'second-account'})
                for callback in callbacks:callback({},None)
                self.assertTrue(spin(lambda:not engine.running))
                self.assertFalse(engine.small_lane.tasks);self.assertFalse(api.committed)
                self.assertEqual(engine.account_id,'second-account')
                self.assertFalse(engine.state.get('resumable_uploads'))
                self.assertEqual(len(api.uploads),3)
            finally:engine.close()

    def test_small_payloads_do_not_wait_for_another_files_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);api=Api();api.hold_small=True
            service=Service(api);adapter=Adapter()
            paths=[]
            for i in range(7):
                path=root/f'file-{i}.bin';path.write_bytes(f'original-{i}'.encode());paths.append(str(path))
            adapter.view['permanent']=paths
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:len(api.held)==3), 'three independent files must reach PUT before any confirmation')
                self.assertEqual(len(api.uploads),3)
                for _ in range(30):APP.processEvents()
                self.assertEqual(len(api.held),3,'bounded concurrency must not create all seven payloads')
                first=next(iter(api.held));api.held.pop(first)()
                self.assertTrue(spin(lambda:len(api.committed)==1 and len(api.uploads)==4 and len(api.held)==3))
                self.assertEqual(len(api.held),3,'a freed slot is immediately reused')
                self.assertEqual(len(set(row['item_id'] for row in api.uploads.values())),4)
            finally:engine.close()


if __name__=='__main__':unittest.main()
