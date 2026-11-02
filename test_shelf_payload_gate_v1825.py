"""Same-batch immutable byte reuse through real ShelfSync callbacks, no cloud."""
import copy,hashlib,sys,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QObject,QTimer
from test_small_upload_lane import APP,Service,Adapter,spin
from shelf_sync import ShelfSync
from cloud_api import CloudError


class ReuseApi(QObject):
    def __init__(self):
        super().__init__();self.token='account-A';self.pending={};self.held={};self.callbacks={};self.calls=[]
        self.uploads={};self.allocations=[];self.accounts={};self.body_calls=[];self.hold_parts=True
        self.part_problem=None;self.commit_problem=None;self.cancelled=[]
        self.allow_instant=True
    def account(self,token):return self.accounts.setdefault(token,dict(items={},changes=[]))
    def request(self,method,path,callback,**options):
        key=uuid4().hex;token=self.token;self.pending[key]=True;self.callbacks[key]=callback
        data=copy.deepcopy(options.get('data') or {});self.calls.append((token,method,path,data))
        def respond(value=None,error=None):
            if self.pending.pop(key,False):callback(value,error)
        def deliver():
            if key not in self.pending:return
            account=self.account(token)
            if path=='/v1/sync/intake':respond({});return
            if path.startswith('/v1/sync/changes'):
                cursor=int(path.split('cursor=')[1].split('&')[0]);changes=account['changes'][cursor:]
                respond(dict(changes=copy.deepcopy(changes),cursor=str(cursor+len(changes)),has_more=False,transfers=[]));return
            if method=='POST' and path=='/v1/uploads':
                source=next((v for v in account['items'].values() if not v['record'].get('deleted') and v['record']['sha256']==data['sha256'] and v['record']['size']==data['size']),None)
                if source is not None and self.allow_instant:
                    record=dict(id=data['item_id'],version=data['base_version']+1,deleted=False,**{n:data[n] for n in ('kind','name','mime_type','size','sha256')})
                    account['items'][record['id']]=dict(record=record,data=source['data']);account['changes'].append(dict(cursor=str(len(account['changes'])+1),record=copy.deepcopy(record)))
                    respond(dict(instant=True,record=record));return
                identity=data['upload_id'];self.allocations.append((token,identity))
                self.uploads[identity]=dict(token=token,spec=data,chunks=[],state='pending')
                respond(dict(upload_id=identity,chunk_size=65536,part_count=(data['size']+65535)//65536));return
            identity=path.split('/')[3] if path.startswith('/v1/uploads/') else None
            if identity is not None:
                row=self.uploads.get(identity)
                if row is None:respond(None,CloudError('missing',404,'NOT_FOUND'));return
                assert row['token']==token
                if method=='DELETE':
                    if row['state']=='committed':respond(None,CloudError('already committed',409,'ALREADY_COMMITTED',dict(error=dict(current_record=row['record']))))
                    else:self.uploads.pop(identity);respond({})
                    return
                if method=='GET':
                    spec=row['spec'];respond(dict(upload_id=identity,state=row['state'],next_part=len(row['chunks']),chunk_size=65536,
                        part_count=(spec['size']+65535)//65536,**{n:spec[n] for n in ('item_id','base_version','size','sha256')}));return
                if method=='PUT':
                    index=int(path.rsplit('/',1)[1]);raw=options['raw'];assert index==len(row['chunks'])
                    self.body_calls.append((token,identity,len(raw)));row['chunks'].append(raw)
                    problem,self.part_problem=self.part_problem,None
                    if self.hold_parts:self.held[key]=lambda:respond(None,problem) if problem else respond({})
                    else:respond(None,problem) if problem else respond({})
                    return
                if path.endswith('/commit'):
                    spec=row['spec'];raw=b''.join(row['chunks']);assert len(raw)==spec['size'] and hashlib.sha256(raw).hexdigest()==spec['sha256']
                    if row['state']!='committed':
                        record=dict(id=spec['item_id'],version=spec['base_version']+1,deleted=False,**{n:spec[n] for n in ('kind','name','mime_type','size','sha256')})
                        account['items'][record['id']]=dict(record=record,data=raw);account['changes'].append(dict(cursor=str(len(account['changes'])+1),record=copy.deepcopy(record)));row.update(state='committed',record=record)
                    problem,self.commit_problem=self.commit_problem,None
                    respond(None,problem) if problem else respond(dict(record=copy.deepcopy(row['record'])));return
            raise AssertionError((method,path))
        QTimer.singleShot(0,deliver);return key
    def cancel(self,key):self.cancelled.append(key);self.pending.pop(key,None);self.held.pop(key,None)
    def release(self,key):self.held.pop(key)()


class PayloadGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fd-payload-gate-');self.root=Path(self.temp.name)
        self.api=ReuseApi();self.service=Service(self.api);self.adapter=Adapter();self.engine=None;self.paths=[]
        self.profiles=0;self.qt_errors=[];self.hook=patch.object(sys,'excepthook',lambda kind,value,trace:self.qt_errors.append((kind.__name__,str(value))))
        self.hook.start();self.addCleanup(self.hook.stop)
    def tearDown(self):
        if self.engine is not None:
            self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
        APP.processEvents();self.temp.cleanup()
        self.assertEqual(self.qt_errors,[])
    def file(self,name,data):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);self.paths.append(path);return str(path)
    def start(self,paths):
        self.profiles+=1;self.profile=self.root/f'profile-{self.profiles}'
        self.adapter.view['permanent']=list(paths);self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000)
        self.engine.set_directions(True,False);self.service.account_changed.emit({'id':self.api.token})
    def waiting(self,count=1):self.assertTrue(spin(lambda:len(self.engine.payload_gate.waiters)==count and bool(self.api.held)))
    def drain(self):
        def settled():
            return (not self.engine.running and not self.api.held and not self.engine.payload_gate.waiters
                    and all(not e.get('dirty') or e.get('deleted') or e.get('paused_upload') for e in self.engine.state['entries'].values()))
        for _ in range(40):
            for key in list(self.api.held):self.api.release(key)
            if spin(settled,seconds=.1):return
        self.fail('payload pipeline did not settle')
    def due(self):
        self.engine.state['not_before']=0
        for identity in self.engine.small_lane.retry_at:self.engine.small_lane.retry_at[identity]=0
        self.engine.tick();self.engine.small_lane.kick()
    def entry(self,path):return next(e for e in self.engine.state['entries'].values() if e['ref']==path)
    def assert_originals(self,expected):
        for path,data in expected.items():self.assertEqual(Path(path).read_bytes(),data)
    def test_three_duplicates_wait_without_occupying_slots_and_different_file_arrives(self):
        raw=b'exact original bytes'*2000;paths=[self.file(n,raw) for n in ('A.bin','B.bin','C.bin')];other=self.file('different.bin',b'unrelated original')
        self.start(paths+[other]);self.waiting(2)
        self.assertTrue(spin(lambda:len(self.api.body_calls)==2))
        self.assertEqual(len(self.api.allocations),2);self.assertEqual(len(self.api.held),2)
        self.assertTrue(any(row['spec']['name']=='different.bin' for row in self.api.uploads.values()))
        self.drain();items=self.api.account('account-A')['items'];self.assertEqual(len(items),4)
        self.assertEqual(len(self.api.body_calls),2);self.assertEqual(len(self.api.allocations),2)
        self.assertEqual({r['record']['name']:r['data'] for r in items.values()},{'A.bin':raw,'B.bin':raw,'C.bin':raw,'different.bin':b'unrelated original'})
        self.assertFalse(self.engine.state['resumable_uploads']);self.assertFalse(self.engine.payload_gate.groups);self.assertFalse(self.engine.payload_gate.waiters)
        self.assert_originals(dict(zip(paths,[raw]*3))|{other:b'unrelated original'})
    def test_lost_part_and_commit_receipt_keep_same_owner_and_do_not_send_second_body(self):
        for failure in ('part','commit'):
            with self.subTest(failure=failure):
                if self.engine is not None:self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
                self.api=ReuseApi();self.service=Service(self.api);self.adapter=Adapter();self.engine=None
                raw=b'original '+failure.encode()*1000;paths=[self.file(f'{failure}-{n}.bin',raw) for n in ('A','B')]
                if failure=='part':self.api.part_problem=CloudError('reply lost',503,'SERVICE_UNAVAILABLE')
                else:self.api.commit_problem=CloudError('commit reply lost',503,'SERVICE_UNAVAILABLE')
                self.start(paths);self.waiting();uid=self.api.allocations[0][1];self.api.release(next(iter(self.api.held)))
                self.assertTrue(spin(lambda:bool(self.engine.small_lane.retry_at)))
                self.assertEqual(len(self.api.body_calls),1);self.assertEqual(len(self.api.allocations),1)
                self.assertEqual(len(self.engine.payload_gate.waiters),1)
                self.due();self.drain();self.assertEqual(len(self.api.account('account-A')['items']),2)
                self.assertEqual(len(self.api.body_calls),1);self.assertEqual(self.api.allocations,[('account-A',uid)])
    def test_cancel_follower_does_not_cancel_owner(self):
        raw=b'original bytes'*100;paths=[self.file(n,raw) for n in ('A.bin','B.bin')];self.start(paths);self.waiting()
        follower=next(iter(self.engine.payload_gate.waiters));entry=self.engine.state['entries'][follower];owner_key=next(iter(self.api.held))
        self.assertTrue(self.engine.cancel_upload(entry['ref']));self.assertIn(owner_key,self.api.pending)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),1);self.assertEqual(len(self.api.body_calls),1)
        self.assertTrue(entry.get('paused_upload'));self.assert_originals(dict.fromkeys(paths,raw))
    def test_cancel_owner_promotes_one_follower_and_late_old_callback_is_ignored(self):
        raw=b'original bytes'*100;paths=[self.file(n,raw) for n in ('A.bin','B.bin')];self.start(paths);self.waiting()
        group=next(iter(self.engine.payload_gate.groups.values()));owner=group.owner;old_key=next(iter(self.api.held));old_callback=self.api.callbacks[old_key]
        self.engine.cancel_upload(owner['ref']);self.assertTrue(spin(lambda:bool(self.api.held) and len(self.api.allocations)==2))
        new_owner=next(iter(self.engine.payload_gate.groups.values())).owner
        old_callback({},None);APP.processEvents();self.assertIs(next(iter(self.engine.payload_gate.groups.values())).owner,new_owner)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),1);self.assertTrue(self.entry(owner['ref']).get('paused_upload'))
        self.assertEqual(len(self.api.body_calls),2);self.assert_originals(dict.fromkeys(paths,raw))
    def test_direction_off_on_and_restart_preserve_accepted_body_before_reuse(self):
        for transition in ('directions','restart'):
            with self.subTest(transition=transition):
                if self.engine is not None:self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
                self.api=ReuseApi();self.service=Service(self.api);self.adapter=Adapter();self.engine=None
                raw=b'restart original '*100;paths=[self.file(f'{transition}-{n}.bin',raw) for n in ('A','B')]
                self.start(paths);self.waiting();uid=self.api.allocations[0][1]
                if transition=='directions':self.engine.set_directions(False,False);self.engine.set_directions(True,False)
                else:
                    self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs));self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000);self.engine.set_directions(True,False);self.service.account_changed.emit({'id':'account-A'})
                self.drain();self.assertEqual(len(self.api.account('account-A')['items']),2)
                self.assertEqual(self.api.allocations,[('account-A',uid)]);self.assertEqual(len(self.api.body_calls),1)
    def test_same_filename_different_bytes_never_coalesces(self):
        first=self.file('left/same.bin',b'original A');second=self.file('right/same.bin',b'original B')
        self.start([first,second]);self.assertTrue(spin(lambda:len(self.api.held)==2));self.assertFalse(self.engine.payload_gate.waiters)
        self.drain();self.assertEqual(len(self.api.allocations),2);self.assertEqual(len(self.api.body_calls),2)
        self.assertEqual({v['data'] for v in self.api.account('account-A')['items'].values()},{b'original A',b'original B'})
    def test_legacy_normal_reservation_is_honored_without_fake_instant_success(self):
        raw=b'legacy server original'*100;paths=[self.file(n,raw) for n in ('A.bin','B.bin')]
        self.api.allow_instant=False;self.start(paths);self.waiting();self.drain()
        self.assertEqual(len(self.api.account('account-A')['items']),2);self.assertEqual(len(self.api.allocations),2)
        self.assertEqual(len(self.api.body_calls),2);self.assertFalse(self.engine.payload_gate.groups)
        self.assert_originals(dict.fromkeys(paths,raw))
    def test_two_legacy_reserved_payloads_resume_without_rebinding_or_discarding_them(self):
        raw=b'legacy accepted original'*100;paths=[self.file(n,raw) for n in ('A.bin','B.bin')]
        self.adapter.view['permanent']=paths;self.profiles+=1;self.profile=self.root/f'profile-{self.profiles}'
        self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000);self.engine.set_directions(True,False)
        with patch.object(self.engine.payload_gate,'admit',return_value=True):
            self.service.account_changed.emit({'id':'account-A'});self.assertTrue(spin(lambda:len(self.api.held)==2))
        original_ids=set(self.api.uploads);self.engine.set_directions(False,False)
        self.adapter.view['permanent'].append(self.file('C.bin',raw));self.engine.local_changed();self.engine.set_directions(True,False)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),3)
        self.assertEqual(set(self.api.uploads),original_ids);self.assertEqual(len(self.api.body_calls),2)
        self.assertFalse(self.engine.state['resumable_uploads']);self.assert_originals(dict.fromkeys(paths,raw))
    def test_source_revision_change_promotes_old_byte_follower_without_losing_new_revision(self):
        raw=b'original prior revision'*200;paths=[self.file(n,raw) for n in ('A.bin','B.bin')];self.start(paths);self.waiting()
        owner=next(iter(self.engine.payload_gate.groups.values())).owner;path=owner['ref'];new=raw+b'new source revision'
        Path(path).write_bytes(new);self.engine.local_changed();old_key=next(iter(self.api.held));old_callback=self.api.callbacks[old_key]
        self.api.release(old_key);self.assertTrue(spin(lambda:len(self.api.allocations)==3 and len(self.api.held)==2))
        current=self.engine.state['resumable_uploads'][owner['id']]
        old_callback({},None);APP.processEvents();self.assertIs(self.engine.state['resumable_uploads'][owner['id']],current)
        self.drain();items=self.api.account('account-A')['items'];self.assertEqual(len(items),2)
        self.assertEqual({v['data'] for v in items.values()},{raw,new});self.assertEqual(Path(path).read_bytes(),new)
        self.assertFalse(self.engine.state['resumable_uploads']);self.assertFalse(self.engine.payload_gate.waiters)
    def test_expired_owner_stops_and_follower_uses_its_own_unchanged_stage(self):
        raw=b'original retained stage'*100;paths=[self.file(n,raw) for n in ('A.bin','B.bin')]
        self.api.part_problem=CloudError('expired upload',410,'UPLOAD_EXPIRED');self.start(paths);self.waiting()
        owner=next(iter(self.engine.payload_gate.groups.values())).owner;self.api.release(next(iter(self.api.held)))
        self.assertTrue(spin(lambda:bool(self.entry(owner['ref']).get('paused_upload')) and len(self.api.allocations)==2))
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),1)
        self.assertNotIn(owner['id'],self.engine.small_lane.retry_at);self.assertEqual(len(self.api.body_calls),2)
        self.assert_originals(dict.fromkeys(paths,raw))
    def test_global_auth_or_quota_is_not_bypassed_and_new_account_never_reuses_old_owner(self):
        for status,code in ((401,'UNAUTHORIZED'),(429,'GLOBAL_QUOTA')):
            with self.subTest(code=code):
                if self.engine is not None:self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
                self.api=ReuseApi();self.service=Service(self.api);self.adapter=Adapter();self.engine=None
                raw=b'account-bound original'*100;paths=[self.file(f'{code}-{n}.bin',raw) for n in ('A','B')]
                self.api.part_problem=CloudError('account unavailable',status,code);self.start(paths);self.waiting()
                old_key=next(iter(self.api.held));old_callback=self.api.callbacks[old_key];self.api.release(old_key)
                self.assertTrue(spin(lambda:not self.engine.running and self.engine.state.get('not_before',0)>time.time()))
                before=len(self.api.calls)
                for _ in range(5):self.engine.tick();self.engine.small_lane.kick();APP.processEvents()
                self.assertEqual(len(self.api.calls),before);self.assertEqual(len(self.api.body_calls),1);self.assertEqual(len(self.api.allocations),1)
                self.api.token='';self.service.account_changed.emit(None)
                self.api.token='account-B';self.service.account_changed.emit({'id':'account-B'})
                self.assertTrue(spin(lambda:not self.engine.running));self.assertFalse(self.engine.payload_gate.groups)
                old_callback({},None);APP.processEvents();self.assertFalse(self.api.account('account-B')['items'])
                self.adapter.view['permanent']=paths;self.engine.local_changed();self.engine.tick();self.drain()
                self.assertEqual(len(self.api.account('account-B')['items']),2)
                self.assertEqual(sum(token=='account-B' for token,identity in self.api.allocations),1)
                self.assertEqual(sum(token=='account-B' for token,identity,n in self.api.body_calls),1)
                self.assert_originals(dict.fromkeys(paths,raw))

if __name__=='__main__':unittest.main()
