"""Small original bytes commit while the bulk payload is deliberately held."""
import copy,hashlib,os,tempfile,time,unittest
from pathlib import Path
from uuid import uuid4
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QObject,QTimer,Signal
from PySide6.QtWidgets import QApplication
from shelf_sync import ShelfSync
from cloud_api import CloudError

APP=QApplication.instance() or QApplication([])
def spin(predicate,seconds=6):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        APP.processEvents()
        if predicate():return True
        time.sleep(.002)
    return False

class Api(QObject):
    token='fixture'
    def __init__(self):
        super().__init__();self.pending={};self.uploads={};self.committed={};self.held={};self.cancelled=[];self.fail_small_once=False;self.hold_small=False;self.held_names={}
    def request(self,method,path,callback,**kwargs):
        key=uuid4().hex;self.pending[key]=True
        def deliver():
            if not self.pending.pop(key,False):return
            data=kwargs.get('data',{})
            if path=='/v1/sync/intake':result={}
            elif path=='/v1/uploads':
                self.uploads[data['upload_id']]=dict(data,bytes=b'')
                result=dict(upload_id=data['upload_id'],chunk_size=max(65536,data['size']),part_count=1)
            elif '/parts/' in path:
                row=self.uploads[path.split('/')[3]];row['bytes']=kwargs['raw']
                if self.fail_small_once and row['size']<=4*1024*1024:
                    self.fail_small_once=False;callback(None,CloudError('temporary',503));return
                assert hashlib.sha256(row['bytes']).hexdigest()==row['sha256']
                result={}
            elif method=='GET' and path.startswith('/v1/uploads/'):
                row=self.uploads[path.split('/')[3]]
                result=dict(upload_id=row['upload_id'],item_id=row['item_id'],base_version=row['base_version'],size=row['size'],sha256=row['sha256'],
                            chunk_size=max(65536,row['size']),part_count=1,state='pending',next_part=0)
            elif path.endswith('/commit'):
                row=self.uploads[path.split('/')[3]];self.committed[row['name']]=row['bytes']
                result=dict(record=dict(id=row['item_id'],version=1,sha256=row['sha256']))
            elif path.startswith('/v1/sync/changes'):
                result=dict(changes=[],cursor='0',has_more=False,transfers=[])
            elif method=='DELETE':result={}
            else:raise AssertionError((method,path))
            callback(result,None)
        if '/parts/' in path and (self.uploads[path.split('/')[3]]['size']>4*1024*1024 or self.hold_small):
            self.held[key]=deliver
            self.held_names[key]=self.uploads[path.split('/')[3]]['name']
        else:QTimer.singleShot(0,deliver)
        return key
    def cancel(self,key):
        self.cancelled.append(key);self.pending.pop(key,None);self.held.pop(key,None)

class Service(QObject):
    account_changed=Signal(object)
    def __init__(self,api):super().__init__();self.api=api
    @property
    def signed_in(self):return bool(self.api.token)

class Adapter:
    def __init__(self):self.view=dict(permanent=[],texts={},metadata={})
    def snapshot(self):return copy.deepcopy(self.view)
    def restore(self,value):self.view=copy.deepcopy(value)
    def title(self,ref):return Path(ref).name

class SmallLaneTests(unittest.TestCase):
    def test_small_files_finish_without_cancelling_slow_bulk_and_clipboard_preempts_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bulk=root/'large.bin';bulk.write_bytes(b'L'*(5*1024*1024))
            api=Api();service=Service(api);adapter=Adapter();adapter.view['permanent']=[str(bulk)]
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:bool(api.held)))
                held=set(api.held)
                small=root/'image.png';original=b'\x89PNG\r\n\x1a\noriginal bytes';small.write_bytes(original)
                second=root/'second.bin';second.write_bytes(b'second')
                adapter.view['permanent'] += [str(small),str(second)];engine.local_changed()
                self.assertTrue(spin(lambda:len(api.committed)==2))
                self.assertEqual(api.committed,{'image.png':original,'second.bin':b'second'})
                self.assertTrue(held.issubset(api.pending));self.assertFalse(api.cancelled)
                engine.set_clipboard_priority(True)
                self.assertFalse(engine.running);self.assertIsNone(engine.small_lane.active)
                self.assertTrue(held.issubset(set(api.cancelled)))
                self.assertTrue(engine.state['resumable_uploads'])
            finally:engine.close()

    def test_account_switch_discards_late_small_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);api=Api();service=Service(api);adapter=Adapter()
            engine=ShelfSync(root,service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'first'})
            token=dict(id='stale');engine.small_lane.active=token
            events=[]
            engine.small_lane.run(token,lambda:engine._request('POST','/v1/sync/intake',lambda *args:events.append(args),data={}))
            service.account_changed.emit({'id':'second'})
            self.assertTrue(spin(lambda:not engine.running))
            self.assertEqual(events,[]);self.assertIsNone(engine.small_lane.active)
            engine.close()

    def test_transient_small_failure_resumes_without_waiting_for_bulk(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bulk=root/'large.bin';bulk.write_bytes(b'L'*(5*1024*1024))
            api=Api();service=Service(api);adapter=Adapter();adapter.view['permanent']=[str(bulk)]
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:bool(api.held)));held=set(api.held)
                small=root/'small.bin';small.write_bytes(b'original');api.fail_small_once=True
                adapter.view['permanent'].append(str(small));engine.local_changed()
                self.assertTrue(spin(lambda:bool(engine.small_lane.retry_at)))
                self.assertNotIn('small.bin',api.committed)
                identity=next(e['id'] for e in engine.state['entries'].values() if e['ref']==str(small))
                upload_id=engine.state['resumable_uploads'][identity]['upload_id']
                engine.small_lane.retry_at[identity]=0;engine.small_lane.retry.stop();engine.small_lane.kick()
                self.assertTrue(spin(lambda:'small.bin' in api.committed))
                self.assertEqual(api.committed['small.bin'],b'original');self.assertIn(upload_id,api.uploads)
                self.assertEqual(len(api.uploads),2);self.assertTrue(held.issubset(api.pending));self.assertFalse(api.cancelled)
            finally:engine.close()

    def test_bulk_finishing_first_does_not_hide_or_unprotect_small_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bulk=root/'large.bin';bulk.write_bytes(b'L'*(5*1024*1024))
            api=Api();service=Service(api);adapter=Adapter();adapter.view['permanent']=[str(bulk)]
            engine=ShelfSync(root/'profile',service,adapter,poll_ms=600000);statuses=[];engine.status_changed.connect(statuses.append)
            engine.set_directions(True,False);service.account_changed.emit({'id':'fixture'})
            try:
                self.assertTrue(spin(lambda:bool(api.held)))
                small=root/'small.bin';small.write_bytes(b'original');api.hold_small=True
                adapter.view['permanent'].append(str(small));engine.local_changed()
                self.assertTrue(spin(lambda:len(api.held)==2))
                large_key=next(key for key,name in api.held_names.items() if name=='large.bin')
                small_key=next(key for key,name in api.held_names.items() if name=='small.bin')
                statuses.clear();api.held.pop(large_key)()
                self.assertTrue(spin(lambda:'large.bin' in api.committed and engine.transfer is None))
                self.assertTrue(engine.running);self.assertIsNotNone(engine.small_lane.active)
                self.assertNotIn('搁架已同步。',statuses)
                engine.set_clipboard_priority(True)
                self.assertIn(small_key,api.cancelled);self.assertIsNone(engine.small_lane.active)
                self.assertNotIn('small.bin',api.committed)
            finally:engine.close()

if __name__=='__main__':unittest.main()
