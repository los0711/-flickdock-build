"""An earlier large download must not hide a later independent small item."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from cloud_api import CloudError
from shelf_sync import ShelfSync


APP = QApplication.instance() or QApplication([])


def spin(predicate, seconds=3):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        APP.processEvents()
        if predicate(): return True
        time.sleep(.002)
    return False


class Api(QObject):
    def __init__(self, records, blobs):
        super().__init__()
        self.records = records
        self.blobs = blobs
        self.pending = {}
        self.held = {}
        self.download_started = []
        self.fail_large_once = False

    def request(self, method, path, callback, **kwargs):
        key = uuid4().hex
        self.pending[key] = True

        def deliver():
            if not self.pending.pop(key, False): return
            if path.startswith('/v1/sync/changes?'):
                result = {'changes':copy.deepcopy(self.records), 'cursor':str(len(self.records)),
                          'has_more':False, 'transfers':[]}
            elif path.endswith('/content'):
                identity = path.split('/')[3]
                data = self.blobs[identity]
                chunk = len(data) if len(data)>4*1024*1024 else 65536
                result = {'record':copy.deepcopy(next(x['record'] for x in self.records
                                                       if x['record']['id']==identity)),
                          'chunk_size':chunk, 'part_count':(len(data)+chunk-1)//chunk}
            elif '/parts/' in path:
                identity = path.split('/')[3]
                result = self.blobs[identity]
                if len(result)>4*1024*1024 and self.fail_large_once:
                    self.fail_large_once=False
                    callback(None,CloudError('Temporary download error'))
                    return
                assert len(result)==kwargs['expected']['size']
                destination = Path(kwargs['destination'])
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(result)
                result = destination
            else: raise AssertionError(path)
            callback(result,None)

        if '/parts/' in path:
            identity = path.split('/')[3]
            self.download_started.append(identity)
            if len(self.blobs[identity])>4*1024*1024:
                self.held[key] = deliver
                return key
        QTimer.singleShot(0,deliver)
        return key

    def release_large(self):
        for key, deliver in list(self.held.items()):
            self.held.pop(key)
            QTimer.singleShot(0,deliver)

    def cancel(self, key):
        self.pending.pop(key,None)
        self.held.pop(key,None)


class Service(QObject):
    account_changed = Signal(object)

    def __init__(self, records, blobs):
        super().__init__()
        self.api = Api(records,blobs)

    @property
    def signed_in(self): return True


class Adapter:
    def __init__(self): self.view={'permanent':[],'texts':{},'metadata':{}}
    def snapshot(self): return copy.deepcopy(self.view)
    def restore(self, view): self.view=copy.deepcopy(view)
    def title(self, ref): return Path(ref).name


def file_record(identity, name, blob):
    return {'id':identity,'version':1,'deleted':False,'kind':'file','name':name,
            'mime_type':'application/octet-stream','size':len(blob),
            'sha256':hashlib.sha256(blob).hexdigest()}


def test_later_small_item_arrives_before_held_large_download():
    large_id,small_id=uuid4().hex,uuid4().hex
    large_blob=bytes(range(256))*20480  # 5 MiB, the large-file priority class.
    small_blob=b'original small file bytes'
    changes=[{'cursor':'1','record':file_record(large_id,'older-large.bin',large_blob)},
             {'cursor':'2','record':file_record(small_id,'newer-small.bin',small_blob)}]
    with tempfile.TemporaryDirectory(prefix='flickdock-receive-priority-') as root:
        service=Service(changes,{large_id:large_blob,small_id:small_blob})
        adapter=Adapter();engine=ShelfSync(root,service,adapter,poll_ms=600000)
        try:
            service.account_changed.emit({'id':'priority-account'})
            assert spin(lambda:len(adapter.view['permanent'])==1 and bool(service.api.held))
            assert Path(adapter.view['permanent'][0]).name=='newer-small.bin'
            assert engine.state['cursor']=='0', 'unfinished page must not advance the durable cursor'
            assert service.api.download_started == [small_id,large_id]
            service.api.fail_large_once=True
            service.api.release_large()
            assert spin(lambda:not engine.running and engine.state.get('not_before',0)>time.time())
            assert engine.state['cursor']=='0'
            assert [Path(ref).name for ref in adapter.view['permanent']]==['newer-small.bin']
            engine.state['not_before']=0
            engine.tick()
            assert spin(lambda:bool(service.api.held))
            service.api.release_large()
            assert spin(lambda:engine.state['cursor']=='2' and not engine.running)
            assert [Path(ref).name for ref in adapter.view['permanent']]==[
                'older-large.bin','newer-small.bin'], 'newest item must remain visually first'
            assert [Path(ref).read_bytes() for ref in adapter.view['permanent']]==[large_blob,small_blob]
        finally: engine.close()


def test_revisions_and_tombstone_of_same_item_keep_order():
    large_id,small_id=uuid4().hex,uuid4().hex
    large=file_record(large_id,'large.bin',b'x'*(5*1024*1024))
    delete={**large,'version':2,'deleted':True,'size':0,'sha256':None}
    small=file_record(small_id,'small.bin',b'small')
    changes=[{'cursor':'1','record':large},{'cursor':'2','record':delete},
             {'cursor':'3','record':small}]
    with tempfile.TemporaryDirectory(prefix='flickdock-receive-order-') as root:
        service=Service(changes,{})
        adapter=Adapter();engine=ShelfSync(root,service,adapter,poll_ms=600000)
        received=[]
        def fake_receive(record, callback, *, remote_order=None):
            received.append((record['id'],record['version'],remote_order))
            QTimer.singleShot(0,callback)
        engine._receive=fake_receive
        try:
            service.account_changed.emit({'id':'order-account'})
            assert spin(lambda:engine.state['cursor']=='3' and not engine.running)
            assert received==[(small_id,1,3),(large_id,1,1),(large_id,2,2)]
        finally:engine.close()


if __name__=='__main__':
    test_later_small_item_arrives_before_held_large_download()
    test_revisions_and_tombstone_of_same_item_keep_order()
    print('PASS priority receive, stable newest-first order, same-item tombstone, durable cursor')
