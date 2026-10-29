"""Small original bytes commit while the bulk payload is deliberately held."""
import copy,hashlib,os,tempfile,time,unittest
from pathlib import Path
from uuid import uuid4
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QObject,QTimer,Signal
from PySide6.QtWidgets import QApplication
from shelf_sync import ShelfSync

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
        super().__init__();self.pending={};self.uploads={};self.committed={};self.held={};self.cancelled=[]
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
                assert hashlib.sha256(row['bytes']).hexdigest()==row['sha256']
                result={}
            elif path.endswith('/commit'):
                row=self.uploads[path.split('/')[3]];self.committed[row['name']]=row['bytes']
                result=dict(record=dict(id=row['item_id'],version=1,sha256=row['sha256']))
            elif path.startswith('/v1/sync/changes'):
                result=dict(changes=[],cursor='0',has_more=False,transfers=[])
            elif method=='DELETE':result={}
            else:raise AssertionError((method,path))
            callback(result,None)
        if '/parts/' in path and self.uploads[path.split('/')[3]]['size']>4*1024*1024:
            self.held[key]=deliver
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

if __name__=='__main__':unittest.main()
