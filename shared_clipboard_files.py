"""One immutable clipboard payload, shared by independent shelf consumers.

Only control requests are added when a shelf attaches. The raw payload remains
owned by ClipboardSync; the shelf uses the server's existing instant receipt.
"""
from __future__ import annotations
import hashlib,json,mimetypes,os,time
from pathlib import Path
from uuid import UUID,uuid4
from PySide6.QtCore import QObject,QTimer,Signal


def uuid(value):
    try:return str(UUID(value))==value
    except (ValueError,TypeError,AttributeError):return False


def file_stamp(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink() or path.is_junction():return None
    info=path.stat()
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]


class ClipboardFileJobs(QObject):
    changed=Signal()
    progress_changed=Signal()

    def __init__(self,client,base):
        super().__init__(client);self.client=client;self.tasks={};self.journal=None
        self.base=Path(base)/'clipboard-file-jobs'
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.timeout.connect(self._retry)
        self.reset(load=True)

    def valid(self,task):
        return (not self.client.closed and self.client.account.signed_in
                and task.get('_session')==self.client._identity
                and self.tasks.get(task.get('id')) is task
                and time.time()-task['created_at']<86400)

    @staticmethod
    def held(task):return bool(task.get('holders') or task.get('_probes'))

    def register(self,task,value):
        if not self.journal or not value or value[0]!='files' or len(value[1])!=1:return
        try:stamp=file_stamp(value[1][0])
        except OSError:return
        if stamp is None or not 0<stamp[2]<20*1024*1024:return
        task.update(file_source=str(Path(value[1][0]).absolute()),source_stat=stamp,holders={},
                    _file_phase='preparing',_file_received=0,_session=self.client._identity)
        self.tasks[task['id']]=task;self.save();self.changed.emit()

    def prepared(self,task):
        if not self.valid(task):return
        if not (task.get('package') or {}).get('raw_file'):
            task['_file_phase']='unavailable'
        else:
            task['_file_phase']='queued'
            task['source_stat']=task['package']['raw_file']['stat']
        self.save();self.changed.emit()

    def update(self,task,phase,received=None,error=None):
        if not self.valid(task):return
        previous=task.get('_file_phase')
        task['_file_phase']=phase
        if received is not None:task['_file_received']=received
        if error is not None:task['_file_error']=str(error)
        elif phase!='retrying':task.pop('_file_error',None)
        if self.client.view.get('id')==task['id']:
            raw=(task.get('package') or {}).get('raw_file') or {}
            self.client.view.update(received=task.get('_file_received',0),total=raw.get('size',task['source_stat'][2]))
            self.client.view_changed.emit(dict(self.client.view))
        if phase in ('ready','retrying','unavailable'):self.save()
        if previous==phase:self.progress_changed.emit()
        else:self.changed.emit()

    def supersede(self):
        for identity,task in tuple(self.tasks.items()):
            if not self.held(task) and not task.get('releases'):self.tasks.pop(identity,None)
        self.save()

    def expire(self):
        expired=[task for task in self.tasks.values() if time.time()-task['created_at']>=86400]
        for task in expired:
            self.tasks.pop(task['id'],None)
            for key in tuple(task.get('_keys',())):self.client._keys.discard(key);self.client.api.cancel(key)
            job=task.get('_job')
            if job is not None:job.cancelled.set()
        if expired:self.save();self.changed.emit()

    def save(self):
        if self.journal is None:return
        fields=('id','sequence','staged','prepared','created_at','package','intent','file_source',
                'source_stat','holders','releases','shared_files','_file_phase','_file_received')
        rows=[{key:task.get(key) for key in fields} for task in self.tasks.values()]
        try:
            self.journal.parent.mkdir(parents=True,exist_ok=True)
            temp=self.journal.with_suffix('.tmp')
            temp.write_text(json.dumps(dict(device=self.client._identity[1],tasks=rows),ensure_ascii=False),encoding='utf8')
            os.replace(temp,self.journal)
            for task in self.tasks.values():task.pop('_journal_error',None)
        except OSError as error:
            for task in self.tasks.values():task['_journal_error']=str(error)

    def reset(self,*,load=False):
        self.save();self.retry.stop();self.tasks={};self.journal=None
        identity=self.client._identity
        if not identity or not uuid(identity[0]):return
        self.journal=self.base/(identity[0]+'.json')
        if not load or self.client.closed:return
        try:
            if self.journal.stat().st_size>2*1024*1024:return
            data=json.loads(self.journal.read_text(encoding='utf8'))
            if data.get('device')!=identity[1]:return
            rows=data.get('tasks',[])
            if not isinstance(rows,list) or len(rows)>128:return
        except (OSError,ValueError,AttributeError):return
        for row in rows:
            if not isinstance(row,dict) or not uuid(row.get('id')):continue
            created=row.get('created_at');holders=row.get('holders');item=row.get('package');releases=row.get('releases') or {}
            if (type(created) not in (int,float) or not 0<=time.time()-created<86400
                    or not isinstance(holders,dict) or len(holders)>128
                    or any(not uuid(key) or not isinstance(value,str) for key,value in holders.items())
                    or not isinstance(releases,dict) or len(releases)>128 or any(not uuid(key) for key in releases)
                    or (not holders and not releases and row.get('_file_phase')!='ready')):continue
            if not isinstance(row.get('source_stat'),list) or len(row['source_stat'])!=4:continue
            if not isinstance(row.get('file_source'),str):continue
            if not isinstance(row.get('intent'),dict) or type(row.get('sequence')) is not int:continue
            if item is not None:
                try:
                    path=Path(item['path']);parent=path.parent
                    if (path.name!='content.bin' or not path.resolve().is_relative_to(self.client.cache.resolve())
                            or path.is_symlink() or parent.is_symlink() or parent.is_junction()
                            or json.loads((parent/'.lease.json').read_text(encoding='utf8')).get('flickdock_clipboard')!=1
                            or not item.get('raw_file')):continue
                except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
            row.update(_session=identity,_file_phase='recovering',_file_received=0,uploading=False,releases=releases)
            self.tasks[row['id']]=row
            QTimer.singleShot(0,self,lambda row=row:self._restore(row))

    def _restore(self,task):
        if not self.valid(task):return
        for identity in tuple(task.get('releases',{})):self.release(task['id'],identity)
        if task.get('releases') and not self.held(task):return
        item=task.get('package')
        if not item:
            try:
                if file_stamp(task['file_source'])!=task['source_stat']:raise OSError('原文件已发生变化。')
                directory=self.client._directory()
            except OSError as error:self.fail(task,error);return
            from clipboard_sync import package
            def prepared(result):
                if not self.valid(task):return
                if not result:self.fail(task,ValueError('共享上传快照无法恢复。'));return
                task.update(package=result,prepared=True)
                self.prepared(task);self.client._stage(task)
            task['_job']=self.client._work(lambda cancel:package(('files',[task['file_source']]),directory,cancel),prepared)
            return
        def verify(cancel):
            with Path(item['path']).open('rb') as stream:
                digest=hashlib.sha256();size=0
                while chunk:=stream.read(256*1024):
                    if cancel.is_set():return False
                    digest.update(chunk);size+=len(chunk)
            return size==item['size'] and digest.hexdigest()==item['sha256']
        def verified(ok):
            if not self.valid(task):return
            if not ok:self.fail(task,ValueError('共享上传快照校验失败。'));return
            def status(result,error):
                if error:self.fail(task,error);return
                head=(result or {}).get('clipboard') or {}
                if head.get('id')==task['id'] and head.get('ready') and head.get('sha256')==item['sha256']:
                    self.update(task,'ready',item['raw_file']['size'])
                elif task.get('holders'):self.client._stage(task)
                else:self.update(task,'unavailable')
            self.client._request('GET',f'/v1/clipboard/{task["id"]}/shared',status,_task=task)
        task['_job']=self.client._work(verify,verified)

    def fail(self,task,error):
        if not self.valid(task):return
        self.update(task,'retrying',error=error)
        task['_file_retry_at']=time.monotonic()+5;self.retry.start(5001)

    def _retry(self):
        later=False
        for task in tuple(self.tasks.values()):
            if self.valid(task) and task.get('releases'):
                for identity in tuple(task['releases']):self.release(task['id'],identity)
                later=True
            if not self.valid(task) or not self.held(task) or task.get('_file_phase')!='retrying':continue
            if self.client._pending is not None and self.client._pending is not task:
                later=True;continue
            if task.get('_file_retry_at',0)>time.monotonic():later=True;continue
            task['uploading']=False
            if task.get('prepared'):self.client._stage(task)
            else:self._restore(task)
        if later:self.retry.start(1000)

    def claim(self,ref,item_id,callback):
        """Return whether matching is in progress; acknowledge a real hold."""
        try:stamp=file_stamp(ref)
        except OSError:return False
        if stamp is None:return False
        candidates=[task for task in self.tasks.values() if self.valid(task)
                    and task.get('_file_phase')!='unavailable' and task.get('source_stat',[0,0,-1])[2]==stamp[2]]
        if not candidates:return False
        exact=next((task for task in candidates if os.path.normcase(os.path.abspath(ref))==
                    os.path.normcase(os.path.abspath(task['file_source'])) and task['source_stat']==stamp),None)
        if exact:self._hold(exact,ref,item_id,callback);return True
        prepared=[task for task in candidates if (task.get('package') or {}).get('raw_file')]
        if not prepared:return False
        for task in prepared:task['_probes']=task.get('_probes',0)+1
        def digest(cancel):
            value=hashlib.sha256()
            with Path(ref).open('rb') as stream:
                while chunk:=stream.read(256*1024):
                    if cancel.is_set():return None
                    value.update(chunk)
            return value.hexdigest() if file_stamp(ref)==stamp else None
        def matched(value):
            task=next((row for row in prepared if self.valid(row) and row['package']['raw_file']['sha256']==value),None)
            # Register a real local consumer before removing the temporary
            # hashing fence, so a newer copy cannot cancel this file task.
            if task:self._hold(task,ref,item_id,callback)
            for row in prepared:row['_probes']=max(0,row.get('_probes',0)-1)
            if task is None:callback(None)
        job=self.client._work(digest,matched);job._shared_probe=True
        return True

    def _hold(self,task,ref,item_id,callback):
        task['holders'][item_id]=ref;self.save()
        if item_id in task.get('releases',{}):
            QTimer.singleShot(1000,self,lambda:self._hold(task,ref,item_id,callback)
                              if self.valid(task) and item_id in task['holders'] else callback(None))
            return
        if task.get('_journal_error'):
            task['holders'].pop(item_id,None);callback(None);return
        def held(result,error):
            if error and getattr(error,'status',0) not in (400,401,403,404,410,426):
                task['_hold_error']=str(error)
                # The server may already have reserved the id. Retry this
                # idempotent hold; do not start a second payload on uncertainty.
                QTimer.singleShot(5000,self,lambda:self._hold(task,ref,item_id,callback)
                                  if self.valid(task) and item_id in task['holders'] else callback(None))
                return
            if error or not isinstance(result,dict) or not result.get('retained') or not result.get('shared_files'):
                task['holders'].pop(item_id,None);self.save();callback(None);return
            if not self.valid(task):return
            task.pop('_hold_error',None)
            task['shared_files']=True;self.save();callback(task)
            if task.get('_file_phase')=='retrying':self.retry.start(1)
        self.client._request('POST',f'/v1/clipboard/{task["id"]}/retain',held,data={'item_id':item_id},_task=task)

    def release(self,identity,item_id):
        task=self.tasks.get(identity)
        if task is None or item_id not in task.get('holders',{}) and item_id not in task.get('releases',{}):return
        first=item_id not in task.get('releases',{})
        if first:task['holders'].pop(item_id,None)
        task.setdefault('releases',{})[item_id]=True;self.save()
        inflight=task.setdefault('_release_inflight',set())
        if item_id in inflight or task.get('_release_retry',{}).get(item_id,0)>time.monotonic():
            self.retry.start(5001);return
        inflight.add(item_id)
        def released(result,error):
            inflight.discard(item_id)
            if error and getattr(error,'status',0) not in (404,410):
                task.setdefault('_release_retry',{})[item_id]=time.monotonic()+5
                self.retry.start(5001);return
            task['releases'].pop(item_id,None);task.get('_release_retry',{}).pop(item_id,None);self.save()
            if not self.held(task) and self.client._pending is not task and self.client.view.get('id')!=identity:
                if task['releases']:return
                self.tasks.pop(identity,None)
                for key in tuple(task.get('_keys',())):self.client._keys.discard(key);self.client.api.cancel(key)
                job=task.get('_job')
                if job is not None:job.cancelled.set()
                self.save()
        self.client._request('POST',f'/v1/clipboard/{identity}/release',released,data={'item_id':item_id},_task=task)

    def protected_directories(self):
        return {Path(task['package']['path']).parent for task in self.tasks.values() if (task.get('package') or {}).get('path')}


class ShelfClipboardBridge(QObject):
    """Attach dirty shelf entries to a clipboard job and link one cloud object."""
    def __init__(self,owner,client):
        super().__init__(owner);self.owner=owner;self.client=client;self.files=client.file_jobs
        self.linking={};self.probing=set();self.retry_at={};self.misses={}
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.timeout.connect(self.reconcile)
        self.files.changed.connect(self.changed)
        self.files.progress_changed.connect(owner._notify_transfers)

    def reset(self):
        self.linking.clear();self.probing.clear();self.retry_at.clear();self.misses.clear();self.retry.stop()

    def _match_key(self,entry):
        candidates=tuple(sorted((identity,(task.get('package') or {}).get('raw_file',{}).get('sha256',''),
                                 tuple(task['source_stat'])) for identity,task in self.files.tasks.items()
                                if self.files.valid(task) and task.get('_file_phase')!='unavailable'))
        return (entry.get('seq'),entry.get('fingerprint'),candidates)

    def changed(self):
        if not self.owner.closed:self.owner._notify_transfers();self.reconcile()

    def _active(self):
        return (not self.owner.closed and self.owner.state is not None and self.owner.service.signed_in
                and self.owner.account_id==(self.client._identity or ('',))[0])

    def reconcile(self):
        if not self._active():return
        owner=self.owner;entries=owner.state['entries'];state=owner.state
        self.linking={identity:epoch for identity,epoch in self.linking.items() if epoch==owner.epoch}
        for entry in tuple(entries.values()):
            identity=entry['id'];shared=entry.get('shared_clipboard')
            if entry.get('deleted') or entry.get('paused_upload') or not entry.get('dirty'):
                self.misses.pop(identity,None)
                if identity in self.probing:
                    self.probing.discard(identity)
                    for task in tuple(self.files.tasks.values()):self.files.release(task['id'],identity)
                if shared:self.files.release(shared,identity);entry.pop('shared_clipboard',None);owner._persist()
                continue
            if shared:
                task=self.files.tasks.get(shared)
                if task is None or not self.files.valid(task) or task.get('_file_phase')=='unavailable':
                    entry.pop('shared_clipboard',None);owner._persist();owner.debounce.start(1)
                elif task.get('_file_phase')=='ready' and owner.upload_enabled:self._link(entry,task)
                continue
            if (entry.get('kind')!='file' or identity in self.probing
                    or identity in state.get('resumable_uploads',{})
                    or self.retry_at.get(identity,0)>time.monotonic()):continue
            match_key=self._match_key(entry)
            if self.misses.get(identity)==match_key:continue
            self.probing.add(identity);seq=entry['seq']
            def claimed(task,entry=entry,identity=identity,seq=seq,state=state,match_key=match_key):
                self.probing.discard(identity)
                valid=(self._active() and owner.state is state and state['entries'].get(identity) is entry
                       and entry['seq']==seq and not entry.get('deleted') and not entry.get('paused_upload'))
                if task and valid:entry['shared_clipboard']=task['id'];owner._persist()
                elif task:self.files.release(task['id'],identity)
                elif valid:self.misses[identity]=match_key
                owner._notify_transfers();owner.debounce.start(1)
                if task and valid and task.get('_file_phase')=='ready':self._link(entry,task)
            if not self.files.claim(entry['ref'],identity,claimed):
                self.probing.discard(identity);self.misses[identity]=match_key

    def waiting(self,entry):return entry['id'] in self.probing or bool(entry.get('shared_clipboard'))

    def progress(self,entry):
        task=self.files.tasks.get(entry.get('shared_clipboard'))
        if task is None:return None
        raw=(task.get('package') or {}).get('raw_file') or {}
        return dict(received=task.get('_file_received',0),total=raw.get('size',task['source_stat'][2]),
                    phase='verifying' if task.get('_file_phase')=='ready' else task.get('_file_phase','queued'),
                    message=task.get('_file_error',''))

    def _link(self,entry,task):
        identity=entry['id'];owner=self.owner
        if identity in self.linking or self.retry_at.get(identity,0)>time.monotonic():return
        raw=(task.get('package') or {}).get('raw_file')
        if not raw:return
        from sync_files import fingerprint
        if fingerprint(entry['ref'],None,entry['name'])!=entry.get('fingerprint'):
            # The immutable clipboard snapshot must never be committed as the
            # shelf's new file after its local source changed during upload.
            owner.local_changed();return
        seq=entry['seq'];version=entry.get('version',0);self.linking[identity]=owner.epoch
        receipt=entry.get('intake_id') or str(uuid4());state=owner.state;epoch=owner.epoch
        def linked(result,error):
            self.linking.pop(identity,None)
            if owner.closed or state is not owner.state or owner.epoch!=epoch:return
            record=(result or {}).get('record') if isinstance(result,dict) else None
            if error or not isinstance(result,dict) or result.get('instant') is not True or not isinstance(record,dict) or (
                    record.get('id')!=identity or record.get('version')!=version+1
                    or record.get('sha256')!=raw['sha256'] or record.get('size')!=raw['size']):
                code=getattr(error,'code','')
                if code in ('REUSE_UNAVAILABLE','SHARED_FILE_PENDING'):
                    self.misses[identity]=self._match_key(entry)
                    entry.pop('shared_clipboard',None);self.files.release(task['id'],identity)
                else:
                    entry['last_error']=str(error or '云端共享文件提交校验失败。')
                    self.retry_at[identity]=time.monotonic()+5;self.retry.start(5001)
                owner._persist();owner._notify_transfers();owner.debounce.start(1);return
            entry.update(version=record['version'],sha256=raw['sha256'])
            if entry['seq']==seq and not entry.get('deleted'):entry['dirty']=False
            if entry.get('shared_clipboard')==task['id']:entry.pop('shared_clipboard',None)
            entry.pop('last_error',None);self.retry_at.pop(identity,None)
            owner._end_intake(entry);owner._persist();owner._notify_transfers()
            self.files.release(task['id'],identity);owner.debounce.start(1)
        # The receipt only associates existing verified bytes with the shelf.
        # The server MUST reject a miss; this path never issues a body PUT.
        owner._request('POST','/v1/uploads',linked,data=dict(upload_id=receipt,item_id=identity,
            base_version=version,kind='file',name=entry['name'],size=raw['size'],sha256=raw['sha256'],
            mime_type=mimetypes.guess_type(entry['name'])[0] or 'application/octet-stream',
            fast_upload=True,fast_multipart=True,reuse_only=True))
