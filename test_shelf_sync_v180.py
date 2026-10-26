"""Two real Qt replicas, real temporary file IO, isolated protocol peer."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QObject,QTimer,Signal
from PySide6.QtWidgets import QApplication
from cloud_api import CloudError
from shelf_sync import ShelfSync

app = QApplication.instance() or QApplication([])


def until(condition,timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        app.processEvents()
        if condition(): return
        time.sleep(.002)
    raise AssertionError('Qt operation timed out')


class Server:
    def __init__(self): self.accounts={};self.uploads={};self.hold=False;self.held=[]
    def account(self,token): return self.accounts.setdefault(token,{'items':{},'changes':[]})
    def dispatch(self,token,method,path,data,raw):
        account=self.account(token); parts=path.split('?')[0].split('/')[2:]
        if parts[:2]==['sync','changes']:
            cursor=int(path.split('cursor=')[1].split('&')[0]);changes=account['changes'][cursor:cursor+100]
            return dict(changes=copy.deepcopy(changes),cursor=str(cursor+len(changes)),has_more=False)
        if parts==['uploads']:
            item=account['items'].get(data['item_id']); version=item['record']['version'] if item else 0
            if version!=data['base_version']: raise CloudError('Conflict',409,'VERSION_CONFLICT')
            uid=uuid4().hex;size=data['size'];chunk=65536
            self.uploads[uid]=dict(spec=copy.deepcopy(data),chunks=[],token=token)
            return dict(upload_id=uid,chunk_size=chunk,part_count=(size+chunk-1)//chunk)
        if parts[0]=='uploads' and len(parts)==4:
            upload=self.uploads[parts[1]];assert upload['token']==token
            assert int(parts[3])==len(upload['chunks']);upload['chunks'].append(raw)
            return {}
        if parts[0]=='uploads' and parts[-1]=='commit':
            upload=self.uploads[parts[1]];spec=upload['spec'];payload=b''.join(upload['chunks'])
            assert len(payload)==spec['size'] and hashlib.sha256(payload).hexdigest()==spec['sha256']
            old=account['items'].get(spec['item_id']);v=old['record']['version'] if old else 0
            if v!=spec['base_version']: raise CloudError('Conflict',409,'VERSION_CONFLICT')
            record=dict(id=spec['item_id'],version=v+1,deleted=False,**{k:spec[k] for k in ['kind','name','mime_type','size','sha256']})
            account['items'][record['id']]=dict(record=record,data=payload)
            account['changes'].append(dict(cursor=str(len(account['changes'])+1),record=copy.deepcopy(record)))
            return dict(record=copy.deepcopy(record))
        if parts[0]=='items' and method=='DELETE':
            item=account['items'][parts[1]]
            if data['base_version']!=item['record']['version']: raise CloudError('Conflict',409,'VERSION_CONFLICT')
            item['record'].update(version=item['record']['version']+1,deleted=True,size=0,sha256=None)
            account['changes'].append(dict(cursor=str(len(account['changes'])+1),record=copy.deepcopy(item['record'])))
            return dict(record=copy.deepcopy(item['record']))
        if parts[0]=='items' and parts[-1]=='content':
            item=account['items'][parts[1]];size=item['record']['size']
            return dict(record=copy.deepcopy(item['record']),chunk_size=65536,part_count=(size+65535)//65536)
        if parts[0]=='items' and parts[2]=='parts':
            item=account['items'][parts[1]];index=int(parts[3])
            return item['data'][index*65536:(index+1)*65536]
        raise AssertionError((method,path))


class Api(QObject):
    unauthorized=Signal()
    def __init__(self,server): super().__init__();self.server=server;self.token='';self.pending={}
    def request(self,method,path,callback,**kw):
        key=uuid4().hex;token=self.token
        def run():
            if key not in self.pending:return
            self.pending.pop(key)
            try:
                result=self.server.dispatch(token,method,path,kw.get('data'),kw.get('raw'))
                if kw.get('destination'):
                    assert len(result)==kw['expected']['size']
                    target=Path(kw['destination']);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(result);result=target
                callback(result,None)
            except Exception as error:callback(None,error)
        self.pending[key]=run
        if self.server.hold:self.server.held.append(run)
        else:QTimer.singleShot(0,run)
        return key
    def cancel(self,key):self.pending.pop(key,None)


class Service(QObject):
    account_changed=Signal(object)
    def __init__(self,server):super().__init__();self.api=Api(server)
    @property
    def signed_in(self):return bool(self.api.token)
    def signin(self,identity):self.api.token=identity or '';self.account_changed.emit({'id':identity} if identity else None)


class Adapter:
    def __init__(self):self.view={'permanent':[],'texts':{},'metadata':{}}
    def snapshot(self):return copy.deepcopy(self.view)
    def restore(self,view):self.view=copy.deepcopy(view)
    def title(self,ref):return self.view['metadata'].get(ref,{}).get('title') or (self.view['texts'][ref][:40] if ref in self.view['texts'] else Path(ref).name)
    def add(self,ref,text=None):
        self.view['permanent'].append(ref)
        if text is not None:self.view['texts'][ref]=text


with tempfile.TemporaryDirectory(prefix='flickdock-sync-v180-') as temp:
    root=Path(temp);server=Server();services=[Service(server),Service(server)];adapters=[Adapter(),Adapter()]
    original=root/'原始图片.svg';original.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',encoding='utf8')
    folder=root/'资料';folder.mkdir();(folder/'笔记.md').write_text('跨设备资料',encoding='utf8')
    adapters[0].add(str(original));adapters[0].add('flickdock:text:local','文字可直接拖入输入框');adapters[0].add(str(folder))
    engines=[ShelfSync(root/str(i),services[i],adapters[i],poll_ms=600000) for i in range(2)]
    for service in services:service.signin('account-A')
    until(lambda:not engines[0].running and len(server.account('account-A')['items'])==3)
    engines[1].tick()
    until(lambda:not engines[1].running and len(adapters[1].view['permanent'])==3)
    received=adapters[1].view
    assert '文字可直接拖入输入框' in received['texts'].values()
    svg=next(Path(x) for x in received['permanent'] if x.endswith('.svg'));assert svg.read_bytes()==original.read_bytes()
    remote_folder=next(Path(x) for x in received['permanent'] if not x.startswith('flickdock:') and Path(x).is_dir())
    assert (remote_folder/'笔记.md').read_text(encoding='utf8')=='跨设备资料'
    assert all(str(root/'0') not in str(x) for x in server.account('account-A')['changes'])
    # No original handle survives upload: Windows rename/delete succeeds.
    renamed=original.with_name('可以删除.svg');original.rename(renamed);renamed.unlink()
    print('PASS two-device file/text/folder bytes and no original handle/path leak')
    # Offline journal: change the text locally, restart and upload later.
    engines[0].close();adapters[1].view['texts'][next(iter(received['texts']))]='离线修改'
    engines[1].local_changed();engines[1].close()
    restarted=ShelfSync(root/'1',services[1],adapters[1],poll_ms=600000);engines[1]=restarted
    services[1].signin('account-A')
    until(lambda:not restarted.running and any(x['data']== '离线修改'.encode() for x in server.account('account-A')['items'].values()))
    print('PASS durable outbox/cursor restart')
    # Account A -> B must never upload A's shelf; guest contents remain separate.
    services[1].signin('account-B');until(lambda:not restarted.running)
    assert not adapters[1].view['permanent'] and not server.account('account-B')['items']
    adapters[1].add('flickdock:text:b','仅属于B');restarted.local_changed();restarted.tick()
    until(lambda:not restarted.running and len(server.account('account-B')['items'])==1)
    services[1].signin('account-A');until(lambda:not restarted.running)
    assert '仅属于B' not in adapters[1].view['texts'].values()
    print('PASS account namespace isolation and profile restoration')
    # Local pending edit and a remote edit preserve both versions.
    first_text=next(iter(adapters[1].view['texts']));adapters[1].view['texts'][first_text]='本机未上传编辑';restarted.local_changed()
    text_id=next(k for k,e in restarted.state['entries'].items() if e['ref']==first_text)
    cloud=server.account('account-A')['items'][text_id];payload='另一台设备的编辑'.encode()
    cloud['data']=payload;cloud['record'].update(version=cloud['record']['version']+1,size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
    server.account('account-A')['changes'].append(dict(cursor=str(len(server.account('account-A')['changes'])+1),record=copy.deepcopy(cloud['record'])))
    restarted.tick();until(lambda:not restarted.running)
    assert {'本机未上传编辑','另一台设备的编辑'} <= set(adapters[1].view['texts'].values())
    print('PASS simultaneous edit preserves both contents')
    # Removing a synchronized item creates a durable tombstone on the server.
    removed=adapters[1].view['permanent'].pop();restarted.local_changed();restarted.tick()
    until(lambda:not restarted.running)
    assert any(x['record']['deleted'] for x in server.account('account-A')['items'].values())
    print('PASS cloud deletion tombstone')
    # A delayed response cannot populate the next account or reopen a closed engine.
    server.hold=True;restarted.tick();services[1].signin('account-C')
    restarted.close();snapshot=adapters[1].snapshot()
    for pending in server.held:pending()
    app.processEvents();assert adapters[1].snapshot()==snapshot and not server.account('account-C')['items']
    print('PASS account epoch and close reject late responses')
print('SHELF_SYNC_V180_OK')
