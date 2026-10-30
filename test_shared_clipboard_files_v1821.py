"""Real Qt tasks and immutable bytes, with a deterministic account peer.

Server semantics are separately exercised against workerd/D1/R2. This peer
deliberately stalls a PUT, counts every payload request and exposes actual
progress, so hiding a duplicate upload cannot make these checks pass.
"""
import base64,copy,hashlib,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QObject,QTimer,QMimeData,QUrl,Signal
from test_html_native_workspace import APP,until,flush
from test_clipboard_sync_v1819 import Clipboard,Events
from test_small_upload_lane import Adapter
from clipboard_sync import ClipboardSync,package
from shelf_sync import ShelfSync
from cloud_api import CloudError


class Peer(QObject):
    account_changed=Signal(object)
    def __init__(self):
        super().__init__();self.api=self;self.token='fixture-token';self.account={'id':str(uuid4())};self.device_id=str(uuid4())
        self.head=None;self.staged={};self.heads={};self.holds={};self.objects={};self.items={};self.active={};self.held={}
        self.calls=[];self.puts=[];self.payload_bytes=0;self.cancelled=[];self.hold_reply=False;self.replies={}
    @property
    def signed_in(self):return bool(self.token)
    def request(self,method,path,callback,**options):
        key=str(uuid4());self.active[key]=True;self.calls.append((method,path,copy.deepcopy(options.get('data'))))
        def respond(result=None,error=None):
            if not self.active.pop(key,False):return
            if isinstance(result,dict):result['server_time']=round(time.time()*1000)
            callback(result,error)
        def deliver():
            if key not in self.active:return
            data=options.get('data') or {}
            if path=='/v1/clipboard' and method=='GET':respond({'clipboard':self.head});return
            if path=='/v1/clipboard' and method=='POST':
                identity=data['id']
                if identity in self.heads and self.heads[identity].get('ready'):
                    respond(dict(clipboard=self.heads[identity],shared_files=True));return
                pending=dict(id=identity,device=self.device_id,version=len(self.staged)+1,ready=False,copied_at=data['copied_at'])
                self.staged[identity]=pending
                if 'inline' in data:
                    old=self.head;pending.update(data['inline'],inline=data['inline']['body'],ready=True)
                    self.head=self.heads[identity]=pending
                    self._retire(old)
                respond(dict(clipboard=pending,shared_files=True));return
            if path.startswith('/v1/clipboard/'):
                identity=path.split('/')[3]
                if path.endswith('/retain'):
                    self.holds.setdefault(identity,set()).add(data['item_id'])
                    respond(dict(retained=True,clipboard=self.heads.get(identity),shared_files=True));return
                if path.endswith('/release'):
                    self.holds.get(identity,set()).discard(data['item_id']);self._retire(self.heads.get(identity))
                    respond({'retained':False,'shared_files':True});return
                if path.endswith('/shared'):
                    respond(dict(clipboard=self.heads.get(identity) or self.staged.get(identity),shared_files=True));return
                if method=='PUT':
                    self.puts.append((identity,key,options));self.held[identity]=(key,options,respond);return
                if method=='GET' and path.endswith('/content'):
                    head=self.heads[identity];raw=self.objects[identity]['bytes'];descriptor=head['raw_file']
                    archive=base64.b64decode(descriptor['prefix'])+raw+base64.b64decode(descriptor['suffix'])
                    assert len(archive)==options['expected']['size']
                    assert hashlib.sha256(archive).hexdigest()==options['expected']['sha256']
                    Path(options['destination']).write_bytes(archive);respond(str(options['destination']));return
                raise AssertionError((method,path))
            if path=='/v1/sync/intake' or method=='DELETE':respond({});return
            if path.startswith('/v1/sync/changes'):
                respond(dict(changes=[],cursor='0',has_more=False,transfers=[]));return
            if path=='/v1/uploads':
                assert data.get('reuse_only') is True,'A second ordinary upload was started'
                source=next((value for value in self.objects.values() if value['size']==data['size'] and value['sha256']==data['sha256']),None)
                if source is None:respond(error=CloudError('missing object',409,'REUSE_UNAVAILABLE'));return
                record=dict(id=data['item_id'],version=data['base_version']+1,**{name:data[name] for name in ('kind','name','size','sha256','mime_type')})
                self.items[record['id']]=record;respond(dict(instant=True,record=record));return
            raise AssertionError((method,path))
        QTimer.singleShot(0,deliver);return key
    def cancel(self,key):self.cancelled.append(key);self.active.pop(key,None)
    def progress(self,identity,ratio):
        _,options,_=self.held[identity];size=options['source_size'];options['progress'](int(size*ratio),size)
    def finish(self,identity,*,lose_reply=False):
        key,options,respond=self.held.pop(identity)
        assert key in self.active,'The shared payload was incorrectly cancelled'
        descriptor=__import__('json').loads(options['headers']['X-Clipboard-Raw-File'])
        with Path(options['source']).open('rb') as stream:
            stream.seek(options['source_offset']);raw=stream.read(options['source_size'])
        self.payload_bytes+=len(raw)
        assert hashlib.sha256(raw).hexdigest()==descriptor['sha256']
        archive=base64.b64decode(descriptor['prefix'])+raw+base64.b64decode(descriptor['suffix'])
        assert hashlib.sha256(archive).hexdigest()==options['headers']['X-Content-SHA256']
        ready=dict(self.staged[identity],kind='files',size=len(archive),sha256=hashlib.sha256(archive).hexdigest(),ready=True,
                   raw_file=descriptor,upload_id=str(uuid4()))
        old=self.head
        # An old held task completes without replacing a newer clipboard head.
        if self.head is None or self.head.get('copied_at',0)<=ready['copied_at']:self.head=ready
        self.heads[identity]=ready;self.objects[identity]=dict(size=len(raw),sha256=descriptor['sha256'],bytes=raw)
        if old and old['id']!=identity:self._retire(old)
        if not lose_reply:respond(dict(clipboard=ready,shared_files=True))
    def _retire(self,head):
        if not head or self.head is head or self.holds.get(head['id']):return
        identity=head['id'];value=self.objects.get(identity)
        if value and not any(item['sha256']==value['sha256'] for item in self.items.values()):self.objects.pop(identity,None)


class SharedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-shared-files-');self.base=Path(self.temp.name)
        self.qt_errors=[];self.hook=patch.object(sys,'excepthook',lambda kind,value,trace:self.qt_errors.append((kind,value)))
        self.hook.start();self.addCleanup(self.hook.stop)
        self.peer=Peer();self.adapter=Adapter();self.clip=Clipboard();self.events=Events();self.clients=[];self.engines=[]
        self.client=self.new_client();self.engine=self.new_engine();self.transfers=[]
        self.engine.transfers_changed.connect(lambda rows:self.transfers.append(rows));flush()
    def new_client(self):
        client=ClipboardSync(self.peer,self.events,self.base/'profile',enabled=True,clipboard=self.clip)
        client._clock_anchor=(time.monotonic(),round(time.time()*1000));self.clients.append(client);return client
    def new_engine(self):
        engine=ShelfSync(self.base/'profile',self.peer,self.adapter,poll_ms=600000)
        engine.set_directions(True,False);engine.select_account(self.peer.account);engine.attach_clipboard(self.clients[-1])
        self.engines.append(engine);return engine
    def tearDown(self):
        for engine in self.engines:engine.close()
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs for client in self.clients) and all(not engine.jobs for engine in self.engines))
        flush();self.temp.cleanup()
        self.assertEqual(self.qt_errors,[],'Qt callback exceptions must fail the test')
    def source(self,name='original.bin',size=512*1024):
        path=self.base/name;path.write_bytes((b'original raw bytes 1234567890'*((size+27)//28))[:size]);return path
    def copy(self,path):
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);self.clip.setMimeData(mime)
        self.client.change_debounce.stop();self.client._changed()
        return self.client.view['id']
    def add(self,path):
        self.adapter.view['permanent'].append(str(path));self.engine.local_changed()
        return next(row for row in self.engine.state['entries'].values() if row['ref']==str(path))

    def test_attach_at_sixty_percent_has_one_payload_and_one_progress_source(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        self.peer.progress(identity,.6);entry=self.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        row=next(row for row in self.transfers[-1] if row['id']==entry['id'])
        self.assertEqual(row['received'],int(path.stat().st_size*.6));self.assertEqual(row['total'],path.stat().st_size)
        self.assertEqual((self.client.view['received'],self.client.view['total']),(row['received'],row['total']))
        self.assertEqual(len(self.peer.puts),1);self.assertFalse(self.engine.state['resumable_uploads'])
        self.peer.finish(identity);until(lambda:not entry['dirty'])
        self.assertEqual(len(self.peer.puts),1);self.assertEqual(self.peer.payload_bytes,path.stat().st_size)
        self.assertEqual(len(self.peer.objects),1);self.assertEqual(self.peer.objects[identity]['bytes'],path.read_bytes())

    def test_completed_copy_links_without_second_payload_and_survives_clipboard_replace(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        self.peer.finish(identity);until(lambda:self.client._pending is None)
        entry=self.add(path);until(lambda:not entry['dirty'])
        self.clip.text('new clipboard');until(lambda:self.peer.head['id']!=identity)
        self.assertIn(identity,self.peer.objects);self.assertEqual(len(self.peer.puts),1);self.assertEqual(path.read_bytes(),self.peer.objects[identity]['bytes'])

    def test_attach_before_preparation_and_newer_copy_does_not_cancel_shelf_payload(self):
        path=self.source();gate=threading.Event()
        def held_package(value,directory,cancel):
            while not gate.wait(.005):
                if cancel.is_set():return None
            return package(value,directory,cancel)
        try:
            with patch('clipboard_sync.package',side_effect=held_package):
                identity=self.copy(path);entry=self.add(path)
                until(lambda:entry.get('shared_clipboard')==identity)
                self.clip.text('latest text wins');until(lambda:self.peer.head and self.peer.head.get('kind')=='text')
                newest=self.peer.head['id'];gate.set();until(lambda:identity in self.peer.held)
                self.peer.finish(identity);until(lambda:not entry['dirty'])
                self.assertEqual(self.peer.head['id'],newest);self.assertEqual(len(self.peer.puts),1)
                self.assertEqual(self.client.view['id'],newest)
        finally:gate.set()

    def test_same_bytes_other_path_reuses_but_same_name_or_size_is_not_enough(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        duplicate=self.base/'copy.bin';duplicate.write_bytes(path.read_bytes());entry=self.add(duplicate)
        until(lambda:entry.get('shared_clipboard')==identity)
        wrong=self.base/'other'/'original.bin';wrong.parent.mkdir();wrong.write_bytes(b'Z'*path.stat().st_size)
        result=[];self.assertTrue(self.client.file_jobs.claim(str(wrong),str(uuid4()),result.append));until(lambda:bool(result))
        self.assertEqual(result,[None]);self.peer.finish(identity);until(lambda:not entry['dirty'])
        self.assertEqual(len(self.peer.puts),1)

    def test_cancelling_shelf_consumer_does_not_cancel_current_clipboard(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        entry=self.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        self.assertTrue(self.engine.cancel_upload(entry['id']));flush()
        self.assertFalse(self.client.file_jobs.tasks[identity]['holders'])
        self.assertNotIn(self.peer.held[identity][0],self.peer.cancelled)
        self.peer.finish(identity);until(lambda:self.client._pending is None)
        self.assertEqual(self.peer.head['id'],identity);self.assertFalse(self.peer.items)

    def test_restart_after_lost_complete_reply_links_existing_object_without_reupload(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        entry=self.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        self.peer.finish(identity,lose_reply=True);self.engine.close();self.client.close();flush()
        self.client=self.new_client();self.engine=self.new_engine()
        until(lambda:any(not row.get('dirty') and row.get('ref')==str(path) for row in self.engine.state['entries'].values()))
        self.assertEqual(len(self.peer.puts),1);self.assertEqual(self.peer.payload_bytes,path.stat().st_size)
        self.assertIn(identity,self.peer.objects)

    def test_account_switch_during_pending_hold_recovers_without_stale_probe(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        original=self.peer.request;stalled=[]
        def request(method,path,callback,**options):
            if path.endswith('/retain'):
                key=str(uuid4());self.peer.active[key]=True;stalled.append(key);return key
            return original(method,path,callback,**options)
        self.peer.request=request;entry=self.add(path);until(lambda:bool(stalled))
        account=self.peer.account;token=self.peer.token
        self.peer.token='';self.peer.account=None;self.peer.account_changed.emit(None);flush()
        self.peer.request=original;self.peer.account=account;self.peer.token=token;self.peer.account_changed.emit(account)
        until(lambda:identity in self.peer.held and self.peer.held[identity][0] in self.peer.active)
        self.peer.finish(identity)
        until(lambda:not self.engine.state['entries'][entry['id']]['dirty'])
        self.assertFalse(self.engine.clipboard_bridge.probing)

    def test_remote_push_cancels_only_stale_receive_not_retained_upload(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        entry=self.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        self.clip.text('new local text');until(lambda:self.peer.head and self.peer.head.get('kind')=='text')
        key=self.peer.held[identity][0];original=self.peer.request
        head=dict(self.peer.head,id=str(uuid4()),version=100,device=str(uuid4()))
        self.peer.head=head;self.peer.heads[head['id']]=head
        def request(method,path,callback,**options):
            if method=='GET' and path.endswith('/content'):
                key=str(uuid4());self.peer.active[key]=True;return key
            return original(method,path,callback,**options)
        self.peer.request=request;self.client._seen='';self.client.refresh();until(lambda:self.client._fetching)
        self.client._pushed({'clipboard':dict(head,id=str(uuid4()),version=101)});flush()
        self.assertNotIn(key,self.peer.cancelled);self.assertIn(entry['id'],self.client.file_jobs.tasks[identity]['holders'])

    def test_failed_release_keeps_durable_cleanup_and_retries_without_another_payload(self):
        path=self.source();identity=self.copy(path);until(lambda:identity in self.peer.held)
        entry=self.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        self.clip.text('replace clipboard');until(lambda:self.peer.head and self.peer.head.get('kind')=='text')
        original=self.peer.request;failed=[]
        def request(method,path,callback,**options):
            if path.endswith('/release'):
                key=str(uuid4());failed.append(key)
                QTimer.singleShot(0,lambda:callback(None,CloudError('connection lost',503)))
                return key
            return original(method,path,callback,**options)
        self.peer.request=request;self.engine.cancel_upload(entry['id']);until(lambda:bool(failed));flush()
        task=self.client.file_jobs.tasks[identity]
        self.assertIn(entry['id'],task['releases']);self.assertIn(entry['id'],self.peer.holds[identity])
        journal=__import__('json').loads(self.client.file_jobs.journal.read_text(encoding='utf8'))
        self.assertTrue(any(row['id']==identity and entry['id'] in row['releases'] for row in journal['tasks']))
        previous=self.peer.head['id'];self.clip.text('another newer copy')
        until(lambda:self.peer.head['id']!=previous)
        self.assertIn(identity,self.client.file_jobs.tasks)
        self.peer.request=original;task['_release_retry']={};self.client.file_jobs._retry()
        until(lambda:identity not in self.client.file_jobs.tasks)
        self.assertFalse(self.peer.holds[identity]);self.assertEqual(len(self.peer.puts),1);self.assertTrue(path.exists())


if __name__=='__main__':unittest.main()
