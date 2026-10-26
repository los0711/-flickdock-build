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
            uid=data.get('upload_id') or uuid4().hex;size=data['size'];chunk=65536
            if uid in self.uploads:
                existing=self.uploads[uid];assert existing['token']==token and existing['spec']==data
                return dict(upload_id=uid,chunk_size=chunk,part_count=(size+chunk-1)//chunk)
            self.uploads[uid]=dict(spec=copy.deepcopy(data),chunks=[],token=token,state='pending')
            return dict(upload_id=uid,chunk_size=chunk,part_count=(size+chunk-1)//chunk)
        if parts[0]=='uploads' and len(parts)==2:
            upload=self.uploads.get(parts[1])
            if not upload or upload['token']!=token: raise CloudError('Missing',404,'NOT_FOUND')
            if method=='DELETE':
                if upload['state']=='committed':
                    record=account['items'].get(upload['spec']['item_id'],{}).get('record')
                    raise CloudError('Already committed',409,'ALREADY_COMMITTED',{'error':{'current_record':record}})
                del self.uploads[parts[1]];return {}
            if upload['state'] not in ('pending','verifying','committed'): raise CloudError('Expired',409,'UPLOAD_EXPIRED')
            spec=upload['spec']
            return dict(upload_id=parts[1],state=upload['state'],next_part=len(upload['chunks']),chunk_size=65536,
                        part_count=(spec['size']+65535)//65536,expires_at='2099-01-01T00:00:00Z',
                        **{k:spec[k] for k in ('item_id','base_version','sha256','size')})
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
            upload['state']='committed'
            return dict(record=copy.deepcopy(record))
        if parts[0]=='items' and method=='DELETE':
            item=account['items'][parts[1]]
            if data['base_version']!=item['record']['version']:
                raise CloudError('Conflict',409,'VERSION_CONFLICT',{'error':{'current_record':copy.deepcopy(item['record'])}})
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


class FaultServer(Server):
    def __init__(self):
        super().__init__();self.calls=[];self.fail_index=None;self.fail_after_accept=False;self.rate_limit=False;self.fail_commit=False;self.fail_download=None
        self.block_reads=False;self.fail_cleanup=False;self.fail_initialize=False;self.churn_deletes=0;self.fail_verifying=False
    def dispatch(self,token,method,path,data,raw):
        self.calls.append((token,method,path))
        parts=path.split('?')[0].split('/')[2:]
        if method=='GET' and self.block_reads:
            raise CloudError('Daily transfer budget',429,'RATE_LIMITED',{'error':{'retry_after':3600}})
        if method=='DELETE' and self.fail_cleanup:
            self.fail_cleanup=False
            raise CloudError('Cleanup budget',429,'RATE_LIMITED',{'error':{'retry_after':600}})
        if method=='DELETE' and parts[0]=='items' and self.churn_deletes:
            self.churn_deletes-=1
            item=self.account(token)['items'][parts[1]];item['record']['version']+=1
            changes=self.account(token)['changes'];changes.append(dict(cursor=str(len(changes)+1),record=copy.deepcopy(item['record'])))
        if method=='POST' and parts==['uploads'] and self.fail_initialize:
            self.fail_initialize=False;super().dispatch(token,method,path,data,raw)
            raise CloudError('Upload initialization reply lost')
        if method=='POST' and parts[-1]=='commit' and self.fail_verifying:
            self.fail_verifying=False;self.uploads[parts[1]]['state']='verifying'
            raise CloudError('Commit worker interrupted',423,'UPLOAD_BUSY')
        if method=='GET' and len(parts)==4 and parts[0]=='items' and self.fail_download is not None and int(parts[3])==self.fail_download:
            self.fail_download=None
            raise CloudError('Daily download budget',429,'RATE_LIMITED',{'schema_version':1,'error':{'retry_after':3600}})
        if method=='POST' and parts[-1]=='commit' and self.fail_commit:
            self.fail_commit=False;super().dispatch(token,method,path,data,raw)
            raise CloudError('Commit reply lost')
        if method=='PUT' and self.fail_index is not None and int(parts[-1])==self.fail_index:
            self.fail_index=None
            if self.fail_after_accept:super().dispatch(token,method,path,data,raw)
            if self.rate_limit:raise CloudError('Daily budget',429,'RATE_LIMITED',{'retry_after':3600})
            raise CloudError('Reply lost')
        return super().dispatch(token,method,path,data,raw)


with tempfile.TemporaryDirectory(prefix='flickdock-resume-v180-') as temp:
    root=Path(temp);server=FaultServer();service=Service(server);adapter=Adapter()
    source=root/'original-image.png'
    # Includes arbitrary metadata bytes: original payload must survive exactly.
    payload=b'\x89PNG\r\n\x1a\n'+bytes(range(256))*1500+b'original metadata\x00'
    source.write_bytes(payload);adapter.add(str(source))
    engine=ShelfSync(root/'client',service,adapter,poll_ms=600000)
    server.fail_index=1;server.fail_after_accept=True;service.signin('resume-A')
    until(lambda:not engine.running and bool(engine.state.get('resumable_uploads')))
    transfer=next(iter(engine.state['resumable_uploads'].values()))
    identity,upload_id,stage=transfer['id'],transfer['upload_id'],Path(transfer['path'])
    assert transfer['next_part']==1 and len(server.uploads[upload_id]['chunks'])==2
    assert stage.read_bytes()==payload
    # The durable file already exists before any graceful shutdown cleanup.
    import json
    persisted=json.loads(engine._profile('resume-A').read_text(encoding='utf8'))
    assert persisted['resumable_uploads'][identity]['upload_id']==upload_id
    engine.state['not_before']=0;engine.close();assert stage.exists()
    engine=ShelfSync(root/'client',service,adapter,poll_ms=600000);service.signin('resume-A')
    until(lambda:not engine.running and bool(server.account('resume-A')['items']))
    assert server.account('resume-A')['items'][identity]['data']==payload
    assert sum(m=='POST' and p=='/v1/uploads' for _,m,p in server.calls)==1
    assert sum(m=='PUT' and p==f'/v1/uploads/{upload_id}/parts/1' for _,m,p in server.calls)==1
    assert any(m=='GET' and p==f'/v1/uploads/{upload_id}' for _,m,p in server.calls)
    assert not stage.exists() and not engine.state['resumable_uploads']
    engine.close();print('PASS durable resume uses server boundary after accepted part reply is lost')

    # Daily limits are persisted, so timer ticks/restarts do not hammer the API.
    service=Service(server);adapter=Adapter();adapter.add(str(source))
    engine=ShelfSync(root/'quota',service,adapter,poll_ms=600000)
    server.fail_index=1;server.fail_after_accept=False;server.rate_limit=True;service.signin('quota-A')
    until(lambda:not engine.running and engine.state.get('not_before',0)>time.time()+3500)
    transfer=next(iter(engine.state['resumable_uploads'].values()));stage=Path(transfer['path']);uid=transfer['upload_id']
    calls=len(server.calls)
    for _ in range(5):engine.tick();app.processEvents()
    assert len(server.calls)==calls and stage.exists()
    engine.close();engine=ShelfSync(root/'quota',service,adapter,poll_ms=600000);service.signin('quota-A')
    app.processEvents();engine.tick();assert len(server.calls)==calls
    # A different account never resumes or sends another account's snapshot.
    service.signin('quota-B');until(lambda:not engine.running)
    assert not server.account('quota-B')['items'] and stage.exists()
    assert not any(token=='quota-B' and f'/uploads/{uid}' in path for token,_,path in server.calls)
    service.signin('quota-A');engine.state['not_before']=0;engine.tick()
    until(lambda:not engine.running and bool(server.account('quota-A')['items']))
    assert not stage.exists();engine.close()
    print('PASS persisted retry_after and account-isolated upload continuation')

    # Changing/deleting a local item retires its old cloud allocation and stage.
    for mutation in ('edit','delete','expired','corrupt'):
        service=Service(server);adapter=Adapter();adapter.add(str(source))
        engine=ShelfSync(root/mutation,service,adapter,poll_ms=600000)
        server.fail_index=1;server.rate_limit=False;service.signin('case-'+mutation)
        until(lambda:not engine.running and bool(engine.state.get('resumable_uploads')))
        transfer=next(iter(engine.state['resumable_uploads'].values()));old=transfer['upload_id'];stage=Path(transfer['path'])
        if mutation=='edit':
            source.write_bytes(payload+b'new revision');engine.local_changed()
        elif mutation=='delete':
            adapter.view['permanent'].clear();engine.local_changed()
        elif mutation=='expired':server.uploads[old]['state']='garbage'
        else:stage.write_bytes(b'x'*transfer['size'])
        engine.state['not_before']=0;engine.tick()
        until(lambda:not engine.running and old not in server.uploads)
        account=server.account('case-'+mutation)
        if mutation=='delete':assert not account['items']
        else:
            assert len(account['items'])==1
            assert next(iter(account['items'].values()))['data']==source.read_bytes()
        assert not stage.exists() and source.exists()
        engine.close();source.write_bytes(payload)
        print('PASS interrupted upload '+mutation+' safely retires old snapshot/allocation')

    service=Service(server);adapter=Adapter();adapter.add(str(source))
    engine=ShelfSync(root/'commit',service,adapter,poll_ms=600000)
    server.fail_commit=True;service.signin('commit-A')
    until(lambda:not engine.running and bool(server.account('commit-A')['items']))
    transfer=next(iter(engine.state['resumable_uploads'].values()));stage=Path(transfer['path'])
    assert stage.exists()
    engine.state['not_before']=0;engine.tick()
    until(lambda:not engine.running and not engine.state['resumable_uploads'])
    assert len(server.account('commit-A')['items'])==1 and not stage.exists()
    assert len(server.account('commit-A')['changes'])==1
    engine.close();print('PASS committed revision with lost reply retires journal without duplicate')

    service=Service(server);adapter=Adapter();adapter.add(str(source))
    engine=ShelfSync(root/'owned',service,adapter,poll_ms=600000)
    server.fail_index=1;service.signin('owned-A')
    until(lambda:not engine.running and bool(engine.state.get('resumable_uploads')))
    transfer=next(iter(engine.state['resumable_uploads'].values()))
    valid_stage=Path(transfer['path']);outside=root/'keep-original.blob';outside.write_bytes(payload)
    transfer['path']=str(outside)
    engine.state['not_before']=0;engine.tick()
    until(lambda:not engine.running)
    assert outside.read_bytes()==payload
    engine.close();valid_stage.unlink(missing_ok=True)
    print('PASS invalid journal cannot read/delete a file outside account outbox')
print('SHELF_RESUME_V180_OK')


with tempfile.TemporaryDirectory(prefix='flickdock-download-resume-v180-') as temp:
    root=Path(temp);server=FaultServer();payload=b'original-image-bytes\x00'+bytes(range(256))*1800
    def seed(account,identity,version,data):
        record=dict(id=identity,version=version,deleted=False,kind='file',name='original.png',mime_type='image/png',
                    size=len(data),sha256=hashlib.sha256(data).hexdigest())
        server.account(account)['items'][identity]=dict(record=copy.deepcopy(record),data=data)
        changes=server.account(account)['changes'];changes.append(dict(cursor=str(len(changes)+1),record=record))
        return record
    for corrupt in (False,True):
        account='download-corrupt' if corrupt else 'download-restart';identity=uuid4().hex
        record=seed(account,identity,1,payload);service=Service(server);adapter=Adapter()
        base=root/account;engine=ShelfSync(base,service,adapter,poll_ms=600000)
        server.fail_download=2;service.signin(account)
        until(lambda:not engine.running and engine.state.get('not_before',0)>time.time()+3500)
        cached=engine.state['downloads'][identity];target=engine._download_target(dict(record))
        part0=target.with_name('.'+target.name+'.chunk-0');part1=target.with_name('.'+target.name+'.chunk-1')
        assert part0.read_bytes()==payload[:65536] and part1.exists() and len(cached['hashes'])==2
        if corrupt:part0.write_bytes(b'x'*65536)
        engine.state['not_before']=0;engine.close()
        assert part0.exists() and part1.exists()
        engine=ShelfSync(base,service,adapter,poll_ms=600000);service.signin(account)
        until(lambda:not engine.running and len(adapter.view['permanent'])==1)
        assert Path(adapter.view['permanent'][0]).read_bytes()==payload
        calls=[p for token,m,p in server.calls if token==account and m=='GET' and '/parts/' in p]
        assert sum('/parts/0?' in p for p in calls)==(2 if corrupt else 1)
        assert sum('/parts/1?' in p for p in calls)==1
        assert not engine.state['downloads'] and not part0.exists() and not part1.exists()
        engine.close();print('PASS download '+('corrupt chunk redownload' if corrupt else 'restart reuses verified original bytes'))

    # A later server revision supersedes an interrupted one; old cached parts
    # are removed and the cursor can advance past the obsolete change record.
    account='download-version';identity=uuid4().hex;record=seed(account,identity,1,payload)
    service=Service(server);adapter=Adapter();engine=ShelfSync(root/account,service,adapter,poll_ms=600000)
    server.fail_download=2;service.signin(account)
    until(lambda:not engine.running and bool(engine.state.get('downloads')))
    old_target=engine._download_target(record);old_part=old_target.with_name('.'+old_target.name+'.chunk-0')
    seed(account,identity,2,payload+b'new revision')
    engine.state['not_before']=0;engine.tick()
    until(lambda:not engine.running and len(adapter.view['permanent'])==1 and not old_part.exists())
    assert Path(adapter.view['permanent'][0]).read_bytes()==payload+b'new revision'
    assert engine.state['cursor']=='2' and engine.state['entries'][identity]['version']==2
    engine.close();print('PASS interrupted old download version cleanup and changefeed progress')

    # Account B must not reuse A's chunks even when the item id is identical.
    account='download-A';identity=uuid4().hex;record=seed(account,identity,1,payload)
    seed('download-B',identity,1,payload+b'belongs to B')
    service=Service(server);adapter=Adapter();engine=ShelfSync(root/'accounts',service,adapter,poll_ms=600000)
    server.fail_download=2;service.signin(account)
    until(lambda:not engine.running and engine.state.get('not_before',0)>time.time()+3500)
    target=engine._download_target(record);part0=target.with_name('.'+target.name+'.chunk-0')
    service.signin('download-B');until(lambda:not engine.running and bool(adapter.view['permanent']))
    assert Path(adapter.view['permanent'][0]).read_bytes()==payload+b'belongs to B'
    assert part0.exists()
    service.signin(account);engine.state['not_before']=0;engine.tick()
    until(lambda:not engine.running and bool(adapter.view['permanent']))
    assert Path(adapter.view['permanent'][0]).read_bytes()==payload
    engine.close();print('PASS account-isolated cached download chunks')
print('SHELF_DOWNLOAD_RESUME_V180_OK')


with tempfile.TemporaryDirectory(prefix='flickdock-priority-delete-v180-') as temp:
    root=Path(temp);source=root/'keep-original.png';payload=b'original bytes'+bytes(range(256))*1200;source.write_bytes(payload)
    for scenario in ('normal','cleanup-budget','version-conflict'):
        server=FaultServer();service=Service(server);adapter=Adapter();adapter.add(str(source))
        engine=ShelfSync(root/scenario,service,adapter,poll_ms=600000);service.signin(scenario)
        until(lambda:not engine.running and bool(server.account(scenario)['items']))
        identity=next(iter(server.account(scenario)['items']))
        server.block_reads=True;engine.tick()
        until(lambda:not engine.running and engine.state.get('not_before',0)>time.time()+3500)
        transfer_deadline=engine.state['not_before'];calls=len(server.calls)
        if scenario=='cleanup-budget':server.fail_cleanup=True
        if scenario=='version-conflict':
            item=server.account(scenario)['items'][identity];item['record']['version']+=1
            changes=server.account(scenario)['changes'];changes.append(dict(cursor=str(len(changes)+1),record=copy.deepcopy(item['record'])))
        adapter.view['permanent'].clear();engine.local_changed()
        until(lambda:not engine.running and any(m=='DELETE' for _,m,_ in server.calls[calls:]))
        assert all(m=='DELETE' for _,m,_ in server.calls[calls:])
        assert engine.state['not_before']==transfer_deadline and source.read_bytes()==payload
        if scenario=='normal':assert server.account(scenario)['items'][identity]['record']['deleted']
        elif scenario=='cleanup-budget':
            assert engine.state['cleanup_not_before']>time.time()+550
            attempts=len(server.calls)
            for _ in range(5):engine.tick();app.processEvents()
            assert len(server.calls)==attempts
            engine.state['cleanup_not_before']=0;engine.tick()
            until(lambda:not engine.running and server.account(scenario)['items'][identity]['record']['deleted'])
            assert engine.state['not_before']==transfer_deadline
        else:
            assert server.account(scenario)['items'][identity]['record']['deleted']
            assert sum(m=='DELETE' for _,m,_ in server.calls[calls:])==2
        engine.close();print('PASS priority cloud delete '+scenario+' independent of transfer backoff')

    for scenario in ('partial-upload','lost-commit'):
        server=FaultServer();service=Service(server);adapter=Adapter();adapter.add(str(source))
        engine=ShelfSync(root/scenario,service,adapter,poll_ms=600000)
        if scenario=='partial-upload':server.fail_index=1;server.rate_limit=True
        else:server.fail_commit=True
        service.signin(scenario)
        until(lambda:not engine.running and bool(engine.state.get('resumable_uploads')))
        transfer=next(iter(engine.state['resumable_uploads'].values()));uid=transfer['upload_id'];stage=Path(transfer['path'])
        server.block_reads=True;engine.state['not_before']=time.time()+3600;calls=len(server.calls)
        adapter.view['permanent'].clear();engine.local_changed()
        until(lambda:not engine.running and not stage.exists() and (uid not in server.uploads if scenario=='partial-upload'
              else next(iter(server.account(scenario)['items'].values()))['record']['deleted']))
        assert all(m=='DELETE' for _,m,_ in server.calls[calls:]) and source.exists()
        engine.close();print('PASS priority cancel/deletion '+scenario+' without a transfer GET')

    server=FaultServer();service=Service(server);adapter=Adapter();adapter.add(str(source))
    engine=ShelfSync(root/'lost-init',service,adapter,poll_ms=600000);server.fail_initialize=True;service.signin('lost-init')
    until(lambda:not engine.running and len(server.uploads)==1 and bool(engine.state.get('resumable_uploads')))
    transfer=next(iter(engine.state['resumable_uploads'].values()));uid=transfer['upload_id'];stage=Path(transfer['path'])
    assert len(uid)==36 and 'chunk_size' not in transfer and stage.read_bytes()==payload
    engine.state['not_before']=0;engine.close()
    engine=ShelfSync(root/'lost-init',service,adapter,poll_ms=600000);service.signin('lost-init')
    until(lambda:not engine.running and bool(server.account('lost-init')['items']))
    assert len(server.uploads)==1 and uid in server.uploads
    assert next(iter(server.account('lost-init')['items'].values()))['data']==payload
    assert not stage.exists();engine.close()
    print('PASS lost initialization reply reuses pre-journaled UUID without orphan reservation')
print('SHELF_PRIORITY_DELETE_V180_OK')


with tempfile.TemporaryDirectory(prefix='flickdock-recovery-v180-') as temp:
    root=Path(temp);source=root/'original.png';payload=bytes(range(256))*600;source.write_bytes(payload)
    server=FaultServer();service=Service(server);adapter=Adapter();adapter.add(str(source))
    engine=ShelfSync(root/'churn',service,adapter,poll_ms=600000);service.signin('churn')
    until(lambda:not engine.running and bool(server.account('churn')['items']))
    identity=next(iter(server.account('churn')['items']));server.block_reads=True
    engine.state['not_before']=time.time()+3600;deadline=engine.state['not_before']
    server.churn_deletes=2;start=len(server.calls);adapter.view['permanent'].clear();engine.local_changed()
    until(lambda:not engine.running and engine.state.get('cleanup_not_before',0)>time.time()+20)
    assert sum(m=='DELETE' for _,m,_ in server.calls[start:])==2
    assert all(m=='DELETE' for _,m,_ in server.calls[start:])
    assert engine.state['entries'][identity]['version']==3 and engine.state['not_before']==deadline
    calls=len(server.calls)
    for _ in range(5):engine.tick();app.processEvents()
    assert len(server.calls)==calls
    engine.state['cleanup_not_before']=0;engine.tick()
    until(lambda:not engine.running and server.account('churn')['items'][identity]['record']['deleted'])
    assert all(m=='DELETE' for _,m,_ in server.calls[start:]);engine.close()
    print('PASS repeated delete CAS conflicts consume owner versions with bounded independent cleanup retry')

    server=FaultServer();service=Service(server);adapter=Adapter();adapter.add(str(source))
    engine=ShelfSync(root/'verifying',service,adapter,poll_ms=600000);server.fail_verifying=True;service.signin('verifying')
    until(lambda:not engine.running and any(u['state']=='verifying' for u in server.uploads.values()))
    transfer=next(iter(engine.state['resumable_uploads'].values()));uid=transfer['upload_id']
    engine.state['not_before']=0;engine.tick()
    until(lambda:not engine.running and bool(server.account('verifying')['items']))
    assert server.uploads[uid]['state']=='committed' and next(iter(server.account('verifying')['items'].values()))['data']==payload
    assert sum(m=='POST' and p=='/v1/uploads' for _,m,p in server.calls)==1
    engine.close();print('PASS interrupted verifying commit resumes finalization without reupload')

    import shelf_sync as sync_module
    import sync_files as file_module
    import threading
    real_assemble=sync_module.assemble_parts;real_path=file_module.Path
    for interruption in ('io-error','close','account-switch'):
        server=FaultServer();service=Service(server);adapter=Adapter();account='recover-'+interruption
        identity=uuid4().hex
        record=dict(id=identity,version=1,deleted=False,kind='file',name='original.png',mime_type='image/png',
                    size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
        server.account(account)['items'][identity]=dict(record=copy.deepcopy(record),data=payload)
        server.account(account)['changes'].append(dict(cursor='1',record=record))
        engine=ShelfSync(root/interruption,service,adapter,poll_ms=600000)
        entered=threading.Event()
        if interruption=='io-error':
            class BrokenPath(type(Path())):
                def open(self,*args,**kwargs):
                    if self.name.endswith('chunk-1'):raise OSError('Simulated transient disk error')
                    return super().open(*args,**kwargs)
            file_module.Path=BrokenPath
        else:
            def paused_assemble(parts,target,expected,cancel):
                entered.set()
                while not cancel.is_set():time.sleep(.001)
                return real_assemble(parts,target,expected,cancel)
            sync_module.assemble_parts=paused_assemble
        try:
            service.signin(account)
            if interruption=='io-error':until(lambda:not engine.running and engine.state.get('not_before',0)>time.time())
            else:until(entered.is_set)
            target=engine._download_target(record);count=(len(payload)+65535)//65536
            paths=[target.with_name('.'+target.name+f'.chunk-{i}') for i in range(count)]
            assert all(p.exists() for p in paths)
            assert len(engine.state['downloads'][identity]['hashes'])==count
            if interruption=='close':engine.close()
            elif interruption=='account-switch':
                service.signin('another-account');until(lambda:not engine.running and engine.pool.activeThreadCount()==0)
            assert all(p.exists() for p in paths)
        finally:
            sync_module.assemble_parts=real_assemble;file_module.Path=real_path
        if interruption=='close':
            engine=ShelfSync(root/interruption,service,adapter,poll_ms=600000);service.signin(account)
        elif interruption=='account-switch':service.signin(account)
        else:engine.state['not_before']=0;engine.tick()
        until(lambda:not engine.running and bool(adapter.view['permanent']))
        assert Path(adapter.view['permanent'][0]).read_bytes()==payload
        assert sum(token==account and m=='GET' and '/parts/' in p for token,m,p in server.calls)==count
        assert not engine.state['downloads'] and not any(p.exists() for p in paths)
        engine.close();print('PASS download assembly '+interruption+' retains verified chunks and journal')

    bad=root/'corrupt-part';bad.write_bytes(b'bad')
    try:real_assemble([bad],root/'should-not-exist',{'size':3,'sha256':hashlib.sha256(b'abc').hexdigest()},threading.Event())
    except ValueError:pass
    else:raise AssertionError('Corrupt full-file SHA was accepted')
    assert not bad.exists() and not (root/'should-not-exist').exists()
    print('PASS explicit full-file SHA mismatch discards corrupt cached parts')
print('SHELF_RECOVERY_V180_OK')
