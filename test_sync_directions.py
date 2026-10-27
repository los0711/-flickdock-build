"""Offscreen upload/receive switches against an asynchronous fake cloud."""
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


app = QApplication.instance() or QApplication([])


def spin(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():return True
        time.sleep(.002)
    return False


class Cloud(QObject):
    def __init__(self):
        super().__init__()
        self.token = 'fixture-session'
        self.calls = []
        self.changes = []
        self.items = {}
        self.pending = set()
        self.fail_next_pull=False
        self.fail_next_upload=False
        self.hold_upload=False
        self.hold_pull=False
        self.held=[]
        self.held_pulls=[]

    def add(self, identity, text, *, version=1, name='远端文字'):
        data = text.encode('utf8')
        record = dict(id=identity, version=version, deleted=False, kind='text',
                      name=name, mime_type='text/plain', size=len(data),
                      sha256=hashlib.sha256(data).hexdigest())
        self.items[identity] = (record, data)
        self.changes.append(copy.deepcopy(record))
        return record

    def request(self, method, path, callback, **kwargs):
        self.calls.append((method,path))
        key = uuid4().hex
        self.pending.add(key)

        def complete():
            if key not in self.pending:return
            self.pending.remove(key)
            try:
                if method == 'GET' and path.startswith('/v1/sync/changes?'):
                    if self.fail_next_pull:
                        self.fail_next_pull=False
                        raise CloudError('同步请求超时，请检查网络。')
                    cursor = int(path.split('cursor=')[1].split('&')[0])
                    records = [dict(cursor=str(i+1),record=copy.deepcopy(row))
                               for i,row in enumerate(self.changes[cursor:],start=cursor)]
                    result = dict(changes=records,transfers=[],cursor=str(len(self.changes)),has_more=False)
                elif method == 'POST' and path == '/v1/uploads':
                    assert kwargs['data'].get('fast_upload') is True
                    if self.fail_next_upload:
                        self.fail_next_upload=False
                        raise CloudError('同步请求超时，请检查网络。')
                    data = kwargs['data']; identity = data['item_id']
                    old = self.items.get(identity)
                    if data['base_version'] != (old[0]['version'] if old else 0):
                        raise CloudError('云端版本变化。',409,'VERSION_CONFLICT')
                    # This fixture exercises the instant-reuse branch. It
                    # retains the actual staged SHA and name in the record.
                    record = dict(id=identity,version=data['base_version']+1,
                                  deleted=False,kind=data['kind'],name=data['name'],
                                  mime_type=data['mime_type'],size=data['size'],sha256=data['sha256'])
                    self.items[identity] = (record,b'')
                    self.changes.append(copy.deepcopy(record))
                    result = dict(instant=True,record=copy.deepcopy(record))
                elif method == 'GET' and path.endswith('/content'):
                    assert kwargs.get('headers',{}).get('X-FlickDock-Fast-Content')=='1'
                    identity = path.split('/')[3]
                    record,payload = self.items[identity]
                    assert payload, 'remote fixture content must have bytes'
                    result = dict(record=copy.deepcopy(record),
                                  chunk_size=len(payload) if len(payload)>4*1024*1024 else 65536,
                                  part_count=1)
                elif method == 'GET' and '/parts/' in path:
                    assert kwargs.get('headers',{}).get('X-FlickDock-Fast-Content')=='1'
                    identity = path.split('/')[3]
                    payload = self.items[identity][1]
                    target = Path(kwargs['destination'])
                    assert len(payload) == kwargs['expected']['size']
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(payload)
                    result = target
                elif method == 'DELETE' and path.startswith('/v1/items/'):
                    identity = path.split('/')[3]
                    record,payload = self.items[identity]
                    if kwargs['data']['base_version'] != record['version']:
                        raise CloudError('云端版本变化。',409,'VERSION_CONFLICT',
                                         {'error':{'current_record':copy.deepcopy(record)}})
                    record = dict(record,version=record['version']+1,deleted=True,size=0,sha256=None)
                    self.items[identity] = (record,b'')
                    self.changes.append(copy.deepcopy(record))
                    result = dict(record=copy.deepcopy(record))
                else:raise AssertionError((method,path))
                callback(result,None)
            except Exception as error:
                callback(None,error)

        if method=='POST' and path=='/v1/uploads' and self.hold_upload:self.held.append(complete)
        elif method=='GET' and path.startswith('/v1/sync/changes?') and self.hold_pull:self.held_pulls.append(complete)
        else:QTimer.singleShot(0,complete)
        return key

    def cancel(self, key):self.pending.discard(key)


class Service(QObject):
    account_changed = Signal(object)
    def __init__(self, api):
        super().__init__();self.api = api
    @property
    def signed_in(self):return bool(self.api.token)


class Adapter:
    def __init__(self, text='本机待上传'):
        self.view = {'permanent':['flickdock:text:local'],
                     'texts':{'flickdock:text:local':text},'metadata':{}}
    def snapshot(self):return copy.deepcopy(self.view)
    def restore(self,view):self.view=copy.deepcopy(view)
    def title(self,ref):return self.view['metadata'].get(ref,{}).get('title') or '本机文字'


with tempfile.TemporaryDirectory(prefix='flickdock-directions-') as directory:
    cloud=Cloud();service=Service(cloud);adapter=Adapter()
    engine=ShelfSync(directory,service,adapter,poll_ms=600000)
    assert engine.set_directions(False,False)
    service.account_changed.emit({'id':'direction-account'})
    assert spin(lambda:engine.state is not None and bool(engine.state['entries']))
    assert not cloud.calls and any(e['dirty'] for e in engine.state['entries'].values())
    assert engine.set_directions(False,True)
    assert spin(lambda:cloud.calls and not engine.running)
    assert all(method=='GET' for method,path in cloud.calls)
    assert any(e['dirty'] for e in engine.state['entries'].values())
    pull_count=sum(path.startswith('/v1/sync/changes') for method,path in cloud.calls)
    assert engine.set_directions(True,False)
    assert spin(lambda:any(method=='POST' for method,path in cloud.calls) and
                all(not e['dirty'] for e in engine.state['entries'].values()))
    assert sum(path.startswith('/v1/sync/changes') for method,path in cloud.calls)==pull_count
    assert engine.state['cursor']=='0'
    assert len(cloud.items)==1
    # Remote content cannot enter the local shelf while receive is off.
    remote_id=uuid4().hex
    cloud.add(remote_id,'来自另一台 Mac')
    engine.request_refresh()
    app.processEvents()
    assert '来自另一台 Mac' not in adapter.view['texts'].values()
    post_count=sum(method=='POST' for method,path in cloud.calls)
    assert engine.set_directions(False,True)
    assert spin(lambda:'来自另一台 Mac' in adapter.view['texts'].values())
    assert sum(method=='POST' for method,path in cloud.calls)==post_count
    # A local delete stays local while upload is off, then reaches the cloud
    # as soon as upload is enabled again.
    local_ref='flickdock:text:local'
    adapter.view['permanent'].remove(local_ref)
    adapter.view['texts'].pop(local_ref)
    engine.local_changed()
    local_id=next(identity for identity,item in cloud.items.items() if item[0]['name']=='本机文字')
    delete_count=sum(method=='DELETE' for method,path in cloud.calls)
    app.processEvents()
    assert sum(method=='DELETE' for method,path in cloud.calls)==delete_count
    assert engine.set_directions(True,True)
    assert spin(lambda:cloud.items[local_id][0]['deleted'])
    engine.close()
print('PASS independent directions, queued local edits, receive resume and delete resume')


with tempfile.TemporaryDirectory(prefix='flickdock-fast-receive-') as directory:
    cloud=Cloud();service=Service(cloud);adapter=Adapter()
    original='R'*(5*1024*1024)
    cloud.add(uuid4().hex,original,name='large-remote-text')
    engine=ShelfSync(directory,service,adapter,poll_ms=600000)
    engine.set_directions(False,True)
    service.account_changed.emit({'id':'fast-receive-account'})
    assert spin(lambda:original in adapter.view['texts'].values(),10)
    assert any(path.endswith('/content') for method,path in cloud.calls)
    assert any('/parts/0' in path for method,path in cloud.calls)
    engine.close()
print('PASS fast single-part remote content is accepted and verified')


with tempfile.TemporaryDirectory(prefix='flickdock-directions-inflight-') as directory:
    cloud=Cloud();cloud.hold_upload=True
    service=Service(cloud);engine=ShelfSync(directory,service,Adapter(),poll_ms=600000)
    engine.set_directions(True,False)
    service.account_changed.emit({'id':'direction-account'})
    assert spin(lambda:bool(cloud.held) and engine.running)
    engine.set_directions(False,False)
    for reply in cloud.held:reply()
    app.processEvents()
    assert not cloud.items and not engine.running
    assert any(e['dirty'] for e in engine.state['entries'].values())
    cloud.hold_upload=False
    engine.set_directions(True,False)
    assert spin(lambda:len(cloud.items)==1 and not engine.running)
    engine.close()
print('PASS disabling upload cancels its in-flight request and resumes the journal later')


with tempfile.TemporaryDirectory(prefix='flickdock-directions-hint-') as directory:
    cloud=Cloud();cloud.hold_pull=True
    service=Service(cloud);engine=ShelfSync(directory,service,Adapter(),poll_ms=600000)
    engine.set_directions(False,True)
    service.account_changed.emit({'id':'direction-account'})
    assert spin(lambda:bool(cloud.held_pulls) and engine.running)
    engine.request_refresh()
    cloud.hold_pull=False
    cloud.held_pulls.pop(0)()
    assert spin(lambda:sum(path.startswith('/v1/sync/changes?') for method,path in cloud.calls)>=2
                and not engine.running)
    engine.close()
print('PASS receive-only change hint while pull is running triggers another pull')


for failed, next_enabled in (('pull',(True,False)),('upload',(False,True))):
    with tempfile.TemporaryDirectory(prefix='flickdock-directions-backoff-') as directory:
        cloud=Cloud();service=Service(cloud);adapter=Adapter()
        if failed=='pull':cloud.fail_next_pull=True
        else:cloud.fail_next_upload=True
        engine=ShelfSync(directory,service,adapter,poll_ms=600000)
        engine.set_directions(failed=='upload',failed=='pull')
        service.account_changed.emit({'id':'direction-account'})
        assert spin(lambda:not engine.running and engine.state and engine.state.get('not_before',0)>time.time())
        assert engine.state['retry_direction']==('receive' if failed=='pull' else 'upload')
        engine.set_directions(*next_enabled)
        if failed=='pull':
            assert spin(lambda:len(cloud.items)==1 and not engine.running)
        else:
            assert spin(lambda:any(path.startswith('/v1/sync/changes') for method,path in cloud.calls)
                        and not engine.running)
        engine.close()
    print('PASS '+failed+' backoff does not block the other direction')


with tempfile.TemporaryDirectory(prefix='flickdock-directions-conflict-') as directory:
    cloud=Cloud();service=Service(cloud);adapter=Adapter('本机更新')
    engine=ShelfSync(directory,service,adapter,poll_ms=600000)
    engine.set_directions(True,False)
    # Existing remote version is unknown locally while inbound sync is off.
    identity=uuid4().hex
    cloud.add(identity,'另一台设备已更新',version=2)
    service.account_changed.emit({'id':'direction-account'})
    assert spin(lambda:not engine.running and all(not e['dirty'] for e in engine.state['entries'].values()))
    # The initial local item gets its own UUID, so inject a remote revision
    # for that very item and then edit it again locally.
    local_id=next(i for i in cloud.items if i!=identity)
    old=cloud.items[local_id][0]
    cloud.add(local_id,'冲突的云端内容',version=old['version']+1,name=old['name'])
    adapter.view['texts']['flickdock:text:local']='本机再次编辑'
    engine.local_changed()
    assert spin(lambda:engine.state.get('awaiting_receive') is True and not engine.running)
    attempts=sum(method=='POST' for method,path in cloud.calls)
    deadline=time.monotonic()+.15
    while time.monotonic()<deadline:app.processEvents();time.sleep(.002)
    assert sum(method=='POST' for method,path in cloud.calls)==attempts
    engine.set_directions(True,True)
    assert spin(lambda:'冲突的云端内容' in adapter.view['texts'].values() and
                '本机再次编辑' in adapter.view['texts'].values())
    engine.close()
print('PASS receive-off version conflict pauses uploads and reconciles when enabled')
