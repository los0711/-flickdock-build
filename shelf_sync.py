"""Account-scoped, journaled shelf replication using the private cloud API.

Only file bytes and display names cross devices; local paths never do. Disk
work runs outside the GUI thread. Every callback belongs to an account epoch.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import shutil
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal
from account_store import atomic_json
from sync_files import (DiskWork, DIRECTORY_MIME, assemble_parts, fingerprint, read_part, read_utf8, safe_name,
                        stage_item, unpack_directory,MAX_FILE_BYTES)


class ShelfSync(QObject):
    status_changed = Signal(str)

    def __init__(self, base, service, adapter, parent=None, *, poll_ms=60000):
        super().__init__(parent)
        self.root = Path(base)/'cloud-shelves'; self.root.mkdir(parents=True,exist_ok=True)
        self.service, self.api, self.adapter = service, service.api, adapter
        self.meta_path = self.root/'profiles.json'
        self.meta = self._read(self.meta_path,{'owner':None,'imported_guest':False})
        self.owner = self.meta.get('owner'); self.account_id = None
        self.state = None; self.epoch = 0; self.running = False; self.closed = False
        self.applying = False; self.requests = set(); self.jobs = set()
        self.pool = QThreadPool(self); self.pool.setMaxThreadCount(2)
        self.retry_ms = 15000; self.transfer = None
        self.timer = QTimer(self); self.timer.setInterval(poll_ms); self.timer.timeout.connect(self.tick)
        self.debounce = QTimer(self); self.debounce.setSingleShot(True); self.debounce.timeout.connect(self.tick)
        service.account_changed.connect(self.select_account)

    @staticmethod
    def _read(path, fallback):
        try:
            if path.stat().st_size > 32*1024*1024: return copy.deepcopy(fallback)
            data = json.loads(path.read_text(encoding='utf8'))
            return data if isinstance(data,dict) else copy.deepcopy(fallback)
        except (OSError,ValueError): return copy.deepcopy(fallback)

    def _profile(self, owner):
        if owner is None: return self.root/'guest.json'
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(owner)): raise ValueError('账号标识无效。')
        return self.root/owner/'state.json'

    def _persist(self):
        if self.state is None: return
        self.state['view'] = self.adapter.snapshot()
        atomic_json(self._profile(self.account_id),self.state)

    def _snapshot_owner(self):
        if self.state is not None and self.owner == self.account_id:
            self._persist()
        else:
            path = self._profile(self.owner)
            previous = self._read(path,{'entries':{},'cursor':'0'})
            previous['view'] = self.adapter.snapshot(); atomic_json(path,previous)

    def select_account(self, account):
        if self.closed: return
        target = account.get('id') if isinstance(account,dict) else None
        if target == self.account_id and self.state is not None:
            if self.service.signed_in and not self.timer.isActive(): self.timer.start()
            return
        self.epoch += 1; self.timer.stop(); self.debounce.stop()
        self.running = False; self.transfer = None
        for job in tuple(self.jobs): job.cancel.set()
        for key in tuple(self.requests): self.api.cancel(key)
        self.requests.clear()
        self._snapshot_owner()
        self.account_id = target
        path = self._profile(target)
        self.state = self._read(path,{'entries':{},'cursor':'0','view':None})
        if self.state.get('view') is None:
            if target and self.owner is None and not self.meta.get('imported_guest'):
                self.state['view'] = self.adapter.snapshot(); self.meta['imported_guest'] = True
            else: self.state['view'] = {'permanent':[],'texts':{},'metadata':{}}
        self.owner = target; self.meta['owner'] = target
        atomic_json(self.meta_path,self.meta)
        self.applying = True
        try: self.adapter.restore(self.state['view'])
        finally: self.applying = False
        self._persist()
        if target:
            self.local_changed(); self.timer.start(); self.debounce.start(1)
        else: self.status_changed.emit('未登录；内容仅保留在这台设备。')

    def local_changed(self):
        if self.closed or self.applying or self.state is None: return
        if not self.account_id:
            self._persist(); return
        view = self.adapter.snapshot(); refs = view.get('permanent',[])
        entries = self.state.setdefault('entries',{})
        by_ref = {e['ref']:e for e in entries.values() if not e.get('deleted')}
        for ref in refs:
            text = view.get('texts',{}).get(ref)
            name = self.adapter.title(ref)
            fp = fingerprint(ref,text,name)
            entry = by_ref.get(ref)
            if entry is None:
                identity = str(uuid4())
                entry = {'id':identity,'ref':ref,'version':0,'seq':0}
                entries[identity] = entry
            if entry.get('fingerprint') != fp or entry.get('deleted'):
                entry.pop('blocked_fingerprint',None); entry.pop('last_error',None)
                entry.update(fingerprint=fp,name=name,dirty=True,deleted=False,
                             kind='url' if text is not None and re.fullmatch(r'https?://\S+',text.strip()) else 'text' if text is not None else 'file',
                             seq=entry.get('seq',0)+1)
        for entry in entries.values():
            if entry.get('ref') not in refs and not entry.get('deleted'):
                entry.update(deleted=True,dirty=True,seq=entry.get('seq',0)+1)
        self._persist()
        self.debounce.start(400)

    def _request(self, method, path, callback, **kwargs):
        epoch = self.epoch; holder = []
        def done(result,error):
            if holder: self.requests.discard(holder[0])
            if self.closed or epoch != self.epoch: return
            callback(result,error)
        key = self.api.request(method,path,done,**kwargs)
        holder.append(key); self.requests.add(key)
        return key

    def _work(self, function, args, callback):
        epoch = self.epoch; job = DiskWork(function,*args); self.jobs.add(job)
        def done(result,error):
            self.jobs.discard(job)
            if self.closed or epoch != self.epoch:
                if isinstance(result,dict) and result.get('path'): Path(result['path']).unlink(missing_ok=True)
                return
            callback(result,error)
        job.signals.done.connect(done); self.pool.start(job)

    def _error(self,error):
        transfer = self.transfer
        if transfer and transfer.get('path'):
            try: Path(transfer['path']).unlink(missing_ok=True)
            except OSError: pass
        self.running = False; self.transfer = None
        if getattr(error,'code','') == 'cancelled': return
        message = {'ACCOUNT_QUOTA':'云空间已满，请清理内容后重试。',
                   'GLOBAL_QUOTA':'共享云空间已满，内容会先保留在本机。',
                   'RATE_LIMITED':'同步请求暂时达到限额，稍后会自动重试。',
                   'STORAGE_REQUEST_BUDGET':'本月云存储请求已达限额，内容会先保留在本机。',
                   'UPLOAD_LIMIT':'正在清理中断的上传，稍后会自动重试。',
                   'SERVICE_UNAVAILABLE':'云服务暂时不可用，稍后会自动重试。'}.get(getattr(error,'code',''))
        self.status_changed.emit(message or (str(error) if not getattr(error,'code','') else '云同步暂时中断，稍后会自动重试。'))
        self.debounce.start(30000)

    def tick(self):
        if self.closed or self.running or not self.account_id or not self.service.signed_in: return
        self.running = True; self.status_changed.emit('正在同步…')
        self._retire_uploads()

    def _retire_uploads(self):
        abandoned = self.state.setdefault('pending_uploads',[])
        if not abandoned: self._pull(); return
        identity = abandoned[0]
        def done(result,error):
            if error and getattr(error,'status',0) not in (404,409): self._error(error); return
            abandoned.pop(0); self._persist(); self._retire_uploads()
        self._request('DELETE','/v1/uploads/'+quote(identity,safe=''),done)

    def _pull(self):
        cursor = str(self.state.get('cursor','0'))
        if not cursor.isdecimal(): cursor = '0'
        def done(result,error):
            if error: self._error(error); return
            changes = result.get('changes',[])
            if not isinstance(changes,list) or len(changes)>100: self._error(ValueError('云同步记录无效。')); return
            def next_record(index=0):
                if index >= len(changes):
                    self.state['cursor'] = str(result['cursor']); self._persist()
                    if result.get('has_more'): self._pull()
                    else: self._push_next()
                    return
                self._receive(changes[index]['record'],lambda:next_record(index+1))
            next_record()
        self._request('GET','/v1/sync/changes?cursor='+cursor+'&limit=100',done)

    def _fork(self,entry):
        fork = dict(entry,id=str(uuid4()),version=0,dirty=True,seq=entry.get('seq',0)+1)
        old_ref = entry['ref']
        if old_ref.startswith('flickdock:text:'):
            fork['ref'] = 'flickdock:text:'+fork['id']
            def change(view):
                view['permanent'] = [fork['ref'] if ref==old_ref else ref for ref in view['permanent']]
                view.setdefault('texts',{})[fork['ref']] = view['texts'].pop(old_ref)
                view.setdefault('metadata',{})[fork['ref']] = view['metadata'].pop(old_ref,{})
            self._change_view(change)
        self.state['entries'][fork['id']] = fork
        return fork

    def _receive(self, record, callback):
        identity = str(record.get('id',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity): self._error(ValueError('同步项目标识无效。')); return
        entries = self.state['entries']; entry = entries.get(identity)
        if entry and record['version'] <= entry.get('version',0): callback(); return
        # A pending removal stays queued when another device updates that item.
        if entry and entry.get('deleted') and entry.get('dirty'):
            entry['version'] = record['version']
            if record.get('deleted'): entries.pop(identity,None)
            self._persist(); callback(); return
        if record.get('deleted'):
            if entry:
                if entry.get('dirty'): self._fork(entry)
                else: self._remove_ref(entry['ref'])
                entries.pop(identity,None)
            self._persist(); callback(); return
        if not isinstance(record.get('size'),int) or not 0 <= record['size'] <= MAX_FILE_BYTES or not re.fullmatch(r'[0-9a-f]{64}',str(record.get('sha256',''))):
            self._error(ValueError('同步文件信息无效。')); return
        # A connection can close after the server commits but before its reply
        # arrives. Recognize our exact staged revision instead of duplicating it.
        if (entry and entry.get('dirty') and entry.get('pending_hash') == record['sha256']
                and entry.get('pending_seq') == entry.get('seq') and entry.get('name') == record['name']):
            entry.update(version=record['version'],sha256=record['sha256'],dirty=False)
            entry.pop('pending_hash',None); entry.pop('pending_seq',None)
            self._persist(); callback(); return
        if entry and not entry.get('dirty') and entry.get('sha256') == record['sha256']:
            entry['version'] = record['version']; entry['name'] = record['name']
            self._set_title(entry['ref'],record['name']); self._persist(); callback(); return
        if entry and entry.get('dirty'):
            self._fork(entry)
            entries.pop(identity,None)
            entry = None
            self.status_changed.emit('另一台设备也修改了此项，已保留两个版本。')
        target = self.root/self.account_id/'files'/identity/str(record['version'])/safe_name(record['name'])
        def downloaded(path,error):
            if error: self._error(error); return
            # Edits made while downloading must be preserved too.
            current = entries.get(identity)
            if current and current.get('dirty'):
                self._fork(current); entries.pop(identity,None); current = None
            def apply_ref(ref,text=None):
                if current: self._remove_ref(current['ref'])
                if text is not None: ref = 'flickdock:text:'+identity
                self._add_ref(ref,text,record['name'])
                entries[identity] = {'id':identity,'ref':ref,'version':record['version'],
                    'sha256':record['sha256'],'name':record['name'],'kind':record['kind'],
                    'fingerprint':fingerprint(ref,text,record['name']),'dirty':False,'deleted':False,'seq':0}
                self._persist(); callback()
            if record['kind'] in ('text','url'):
                def decoded(text,error):
                    if error: self._error(error)
                    else: apply_ref('',text)
                self._work(read_utf8,(path,),decoded)
            elif record.get('mime_type') == DIRECTORY_MIME:
                destination = target.with_name(target.name+'-folder')
                def unpacked(ref,error):
                    if error: self._error(error)
                    else: apply_ref(ref)
                self._work(unpack_directory,(path,destination),unpacked)
            else: apply_ref(str(path))
        self._download_record(record,target,downloaded)

    def _download_record(self, record, target, callback):
        identity = quote(record['id'],safe='')
        def metadata(result,error):
            if error: callback(None,error); return
            current = result.get('record',{})
            if current.get('version') != record['version'] or current.get('sha256') != record['sha256']:
                self.running = False; self.tick(); return
            chunk_size, count = result.get('chunk_size'), result.get('part_count')
            if (not isinstance(chunk_size,int) or not 65536<=chunk_size<=4*1024*1024 or not isinstance(count,int)
                    or count != (record['size']+chunk_size-1)//chunk_size):
                callback(None,ValueError('同步分块信息无效。')); return
            target.parent.mkdir(parents=True,exist_ok=True)
            parts = []
            def part(index=0):
                if index >= count:
                    self._work(assemble_parts,(parts,target,record),callback); return
                path = target.with_name('.'+target.name+f'.chunk-{index}')
                def received(result,error):
                    if error:
                        for previous in parts: previous.unlink(missing_ok=True)
                        callback(None,error); return
                    parts.append(result); part(index+1)
                self._request('GET',f'/v1/items/{identity}/parts/{index}?version={record["version"]}',received,
                    destination=path,expected={'size':min(chunk_size,record['size']-index*chunk_size)},timeout_ms=120000)
            part()
        self._request('GET','/v1/items/'+identity+'/content',metadata)

    def _change_view(self, operation):
        self.applying = True
        try:
            view = self.adapter.snapshot(); operation(view); self.adapter.restore(view)
        finally: self.applying = False

    def _remove_ref(self,ref):
        # A conflict fork may share this ref: it owns the local bytes now.
        if any(e.get('ref')==ref and e.get('dirty') and not e.get('deleted') for e in self.state['entries'].values()): return
        def change(view):
            view['permanent'] = [x for x in view.get('permanent',[]) if x!=ref]
            view.setdefault('texts',{}).pop(ref,None); view.setdefault('metadata',{}).pop(ref,None)
        self._change_view(change)

    def _set_title(self,ref,name):
        self._change_view(lambda view:view.setdefault('metadata',{}).setdefault(ref,{}).update(title=name))

    def _add_ref(self,ref,text,name):
        def change(view):
            if ref not in view['permanent']: view['permanent'].append(ref)
            if text is not None: view.setdefault('texts',{})[ref] = text
            view.setdefault('metadata',{}).setdefault(ref,{})['title'] = name
        self._change_view(change)

    def _push_next(self):
        pending = next((e for e in self.state['entries'].values() if e.get('dirty') and
                        (e.get('deleted') or e.get('blocked_fingerprint') != e.get('fingerprint'))),None)
        if pending is None:
            self.running = False; self.transfer = None
            blocked = sum(bool(e.get('dirty')) for e in self.state['entries'].values())
            self.status_changed.emit(f'可同步内容已同步；{blocked} 项暂留本机，请检查文件大小或位置。' if blocked else '搁架已同步。'); return
        identity = pending['id']
        if pending.get('deleted'):
            if pending.get('version',0) == 0:
                self.state['entries'].pop(identity,None); self._persist(); self._push_next(); return
            def removed(result,error):
                if error:
                    if getattr(error,'status',0)==409:
                        self.running = False; self.tick()
                    else: self._error(error)
                    return
                current = self.state['entries'].get(identity)
                if current and current.get('deleted'): self.state['entries'].pop(identity,None)
                self._persist(); self._push_next()
            self._request('DELETE','/v1/items/'+identity,removed,data={'base_version':pending['version']}); return
        text = self.adapter.snapshot().get('texts',{}).get(pending['ref'])
        snapshot = dict(pending); self.transfer = snapshot
        def staged(result,error):
            if error:
                entry = self.state['entries'].get(identity)
                if entry and entry.get('seq')==snapshot['seq']:
                    entry.update(blocked_fingerprint=snapshot['fingerprint'],last_error=str(error))
                self._persist(); self.status_changed.emit(str(error)); self._push_next(); return
            snapshot.update(result)
            def initialized(result,error):
                if error:
                    Path(snapshot['path']).unlink(missing_ok=True)
                    if getattr(error,'status',0)==409: self.running=False; self.tick()
                    else: self._error(error)
                    return
                snapshot.update(upload_id=result['upload_id'],chunk_size=result['chunk_size'],part_count=result['part_count'])
                self.state.setdefault('pending_uploads',[]).append(result['upload_id'])
                current = self.state['entries'].get(identity)
                if current: current.update(pending_hash=snapshot['sha256'],pending_seq=snapshot['seq'])
                self._persist()
                self._send_part(snapshot,0)
            self._request('POST','/v1/uploads',initialized,data={
                'item_id':identity,'base_version':snapshot.get('version',0),'kind':snapshot['kind'],
                'name':snapshot['name'],'mime_type':result['mime_type'],'size':result['size'],'sha256':result['sha256']})
        self._work(stage_item,(pending['ref'],text,pending['name'],self.root/self.account_id/'outbox'),staged)

    def _send_part(self,transfer,index):
        if index >= transfer['part_count']:
            def committed(result,error):
                Path(transfer['path']).unlink(missing_ok=True)
                if error:
                    if getattr(error,'status',0)==409: self.running=False; self.tick()
                    else: self._error(error)
                    return
                record = result['record']; entry = self.state['entries'].get(transfer['id'])
                pending_uploads = self.state.setdefault('pending_uploads',[])
                if transfer['upload_id'] in pending_uploads: pending_uploads.remove(transfer['upload_id'])
                if entry:
                    entry.update(version=record['version'],sha256=record['sha256'])
                    if entry.get('seq')==transfer['seq']:
                        entry.update(dirty=False,fingerprint=transfer['fingerprint'])
                    entry.pop('pending_hash',None); entry.pop('pending_seq',None)
                self._persist(); self._push_next()
            self._request('POST','/v1/uploads/'+transfer['upload_id']+'/commit',committed,data={}); return
        def read(result,error):
            if error: self._error(error); return
            data,digest = result
            def sent(result,error):
                if error:
                    Path(transfer['path']).unlink(missing_ok=True); self._error(error); return
                self.status_changed.emit(f'正在上传 {transfer["name"]} · {index+1}/{transfer["part_count"]}')
                self._send_part(transfer,index+1)
            self._request('PUT',f'/v1/uploads/{transfer["upload_id"]}/parts/{index}',sent,
                          raw=data,headers={'X-Content-SHA256':digest})
        self._work(read_part,(transfer['path'],index,transfer['chunk_size']),read)

    def close(self):
        if self.closed: return
        self.local_changed(); self._persist()
        self.closed = True; self.epoch += 1
        self.service.account_changed.disconnect(self.select_account)
        self.timer.stop(); self.debounce.stop()
        for key in tuple(self.requests): self.api.cancel(key)
        for job in tuple(self.jobs): job.cancel.set()
        self.pool.waitForDone(3000)
        if self.transfer and self.transfer.get('path'):
            try: Path(self.transfer['path']).unlink(missing_ok=True)
            except OSError: pass
