"""Owned canonical shelf bytes and per-revision resource bindings.

The journal remains the durable reference set. Resource jobs retain finished
ordinary stages until the journal binds the canonical body; actual IO terminal
callbacks, not cancellation requests, release temporary read leases.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4, uuid5

from PySide6.QtCore import QObject, QThreadPool, QThread, Qt
from account_store import atomic_json
from sync_files import DiskWork, MAX_FILE_BYTES
from selection_archive import _source_reader, _is_link

_NAMESPACE = UUID('a9428b39-1dbc-4276-b2cc-5d8d9f9341e8')
_BLOB = re.compile(r'[0-9a-f]{32}\.blob\Z')
_RECORD_LIMIT = 16384


class StageResourceError(ValueError):
    """A resource binding cannot safely be used or guessed from body mtime."""
    def __init__(self):
        super().__init__('同步缓存资源记录不可用，已暂停此项并保留缓存，请检查后重试。')


def canonical_path(path):
    return Path(path).parent.name == 'canonical-v1'


def _binding(snapshot, account):
    identity = {key:snapshot[key] for key in ('id','seq','fingerprint','size','sha256')}
    identity['account'] = account
    identity['reservation']=snapshot.get('upload_id') or snapshot.get('intake_id') or snapshot.get('reservation') or ''
    encoded = json.dumps(identity,sort_keys=True,separators=(',',':')).encode('utf8')
    return identity, hashlib.sha256(encoded).hexdigest()


def _canonical(outbox, account, snapshot):
    key = f"{account}:{snapshot['size']}:{snapshot['sha256']}"
    return Path(outbox)/'canonical-v1'/(uuid5(_NAMESPACE,key).hex+'.blob')


def linked_path(path):
    """Reject every Windows reparse point, not just symbolic links."""
    path=Path(path)
    if _is_link(path):return True
    try:info=path.lstat()
    except FileNotFoundError:return False
    return bool(getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))


def validate_owned_outbox(outbox):
    if any(linked_path(path) for path in (outbox,outbox.parent,outbox.parent.parent)):
        raise StageResourceError()


def owned_stage_path(path,outbox):
    path=Path(path);outbox=Path(outbox);validate_owned_outbox(outbox)
    directory=outbox/'canonical-v1' if canonical_path(path) else outbox
    return _regular(path,directory)


def _regular(path, directory):
    return (not linked_path(directory) and not linked_path(directory.parent)
            and not linked_path(path) and _BLOB.fullmatch(path.name) is not None
            and path.resolve().parent == directory.resolve())


def _record_digest(record):
    unsigned={key:value for key,value in record.items() if key!='record_sha256'}
    return hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':')).encode('utf8')).hexdigest()


def _checked_record(record, identity, target, reservation):
    if (not isinstance(record,dict) or set(record)!={'version','identity','canonical','source','reservation','original_mtime','record_sha256','operation'}
            or record.get('record_sha256')!=_record_digest(record) or record.get('version') != 1
            or record.get('identity') != identity or record.get('canonical') != target.name
            or not isinstance(record.get('source'),str) or not _BLOB.fullmatch(record['source'])
            or record.get('reservation') != reservation
            or not isinstance(record.get('operation'),str) or not re.fullmatch(r'[0-9a-f]{32}',record['operation'])):
        raise StageResourceError()
    stamp = record.get('original_mtime')
    if type(stamp) not in (float,int) or not math.isfinite(stamp) or not 0 <= stamp <= 253402300799:
        raise StageResourceError()
    return float(stamp)


def _read_record(path):
    try:
        if linked_path(path.parent) or linked_path(path) or path.stat().st_size > _RECORD_LIMIT:raise StageResourceError()
        with _source_reader(path) as stream:raw=stream.read(_RECORD_LIMIT+1)
        if len(raw)>_RECORD_LIMIT:raise StageResourceError()
        return json.loads(raw)
    except (OSError,ValueError,TypeError) as error:
        raise StageResourceError() from error


def _check_bytes(path, snapshot, cancel):
    digest=hashlib.sha256();size=0
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):
            if cancel.is_set():raise InterruptedError()
            size+=len(block)
            if size>snapshot['size']:raise StageResourceError()
            digest.update(block)
    if size!=snapshot['size'] or digest.hexdigest()!=snapshot['sha256']:raise StageResourceError()


def prepare_binding(outbox, account, snapshot, cancel):
    """Disk-only publication; never removes the old journal's ordinary body."""
    outbox=Path(outbox);source=Path(snapshot['path']);target=_canonical(outbox,account,snapshot)
    identity,digest=_binding(snapshot,account)
    reservation=snapshot.get('upload_id') or snapshot.get('intake_id') or ''
    directory=target.parent
    validate_owned_outbox(outbox)
    if linked_path(directory):raise StageResourceError()
    directory.mkdir(exist_ok=True)
    record_path=directory/(target.stem+'-'+digest+'.json')
    if canonical_path(source):
        if source!=target or not _regular(source,directory):raise StageResourceError()
        record=_read_record(record_path)
        stamp=_checked_record(record,identity,target,reservation)
        # _verify_staged already verified this body before a restored binding.
        return dict(path=str(target),original_mtime=stamp,source=str(outbox/record['source']),record=str(record_path))
    if not _regular(source,outbox):raise StageResourceError()
    stamp=source.stat().st_mtime
    if record_path.exists():
        record=_read_record(record_path)
        stamp=_checked_record(record,identity,target,reservation)
        if record['source']!=source.name:raise StageResourceError()
    else:
        record=dict(version=1,identity=identity,canonical=target.name,source=source.name,
                    reservation=reservation,original_mtime=stamp,operation=uuid4().hex)
        record['record_sha256']=_record_digest(record)
        # Publish the bounded ownership/cleanup intent before creating another
        # body. A metadata write failure must not leave an unowned full blob.
        atomic_json(record_path,record)
    if target.exists():
        if not _regular(target,directory):raise StageResourceError()
        try:_check_bytes(target,snapshot,cancel)
        except StageResourceError:
            # A sound binding still names these exact immutable bytes. Rebuild
            # them from a freshly verified owned stage, atomically preserving
            # the old inode for any already-open request until its terminal.
            for binding in directory.glob(target.stem+'-*.json'):_owned_record(binding,outbox)
            _check_bytes(source,snapshot,cancel)
            repair=directory/(target.stem+'-'+record['operation']+'.repair');created=False
            if linked_path(repair):raise StageResourceError()
            repair.unlink(missing_ok=True)
            try:
                with source.open('rb') as incoming,repair.open('xb') as outgoing:
                    created=True
                    while block:=incoming.read(1024*1024):
                        if cancel.is_set():raise InterruptedError()
                        outgoing.write(block)
                    outgoing.flush();os.fsync(outgoing.fileno())
                os.replace(repair,target)
            finally:
                if created:repair.unlink(missing_ok=True)
    else:
        # A same-volume link avoids another full disk write. The bounded copy
        # fallback is required on filesystems which do not support hard links.
        try:os.link(source,target)
        except FileExistsError:
            _check_bytes(target,snapshot,cancel)
        except OSError:
            partial=directory/(target.stem+'-'+record['operation']+'.partial');created=False
            if linked_path(partial):raise StageResourceError()
            partial.unlink(missing_ok=True)
            try:
                with source.open('rb') as incoming,partial.open('xb') as outgoing:
                    created=True
                    while block:=incoming.read(1024*1024):
                        if cancel.is_set():raise InterruptedError()
                        outgoing.write(block)
                    outgoing.flush();os.fsync(outgoing.fileno())
                _check_bytes(partial,snapshot,cancel)
                os.replace(partial,target)
            finally:
                if created:partial.unlink(missing_ok=True)
    # The old ordinary stage survives until its new journal binding is durable.
    return dict(path=str(target),original_mtime=float(stamp),source=str(source),record=str(record_path))


def _owned_record(record_path, outbox):
    validate_owned_outbox(outbox)
    if linked_path(record_path.parent):raise StageResourceError()
    record=_read_record(record_path)
    if not isinstance(record,dict):raise StageResourceError()
    identity=record.get('identity')
    if (not isinstance(identity,dict) or set(identity)!={'id','seq','fingerprint','size','sha256','account','reservation'}
            or identity['account']!=outbox.parent.name
            or not isinstance(identity['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity['id'])
            or type(identity['seq']) is not int or not 0<=identity['seq']<=2**53
            or not isinstance(identity['fingerprint'],str) or len(identity['fingerprint'])>4096
            or type(identity['size']) is not int or not 0<=identity['size']<=MAX_FILE_BYTES
            or not isinstance(identity['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',identity['sha256'])
            or not isinstance(record.get('reservation'),str) or len(record['reservation'])>128
            or identity['reservation']!=record['reservation']):
        raise StageResourceError()
    target=_canonical(outbox,identity['account'],identity)
    _,digest=_binding(identity,identity['account'])
    if record_path.name!=target.stem+'-'+digest+'.json':raise StageResourceError()
    _checked_record(record,identity,target,record['reservation'])
    return target,outbox/record['source']


def scan_bindings(outbox,cancel):
    outbox=Path(outbox);directory=outbox/'canonical-v1'
    validate_owned_outbox(outbox)
    if linked_path(directory):raise StageResourceError()
    rows=[];invalid=0
    for index,path in enumerate(directory.glob('*.json')):
        if cancel.is_set():raise InterruptedError()
        if index>=4096:raise StageResourceError()
        try:target,source=_owned_record(path,outbox)
        except (OSError,ValueError,TypeError,KeyError):invalid+=1;continue
        rows.append((str(target),str(source)))
    return rows,invalid


def remove_unreferenced(outbox, durable_refs, path, cancel):
    outbox=Path(outbox);path=Path(path);validate_owned_outbox(outbox)
    directory=outbox/'canonical-v1' if canonical_path(path) else outbox
    if not _regular(path,directory):raise StageResourceError()
    if str(path) in durable_refs:return False
    if cancel.is_set():raise InterruptedError()
    if canonical_path(path):
        records=list(directory.glob(path.stem+'-*.json'))
        # New canonical bytes without a sound ownership record are preserved.
        if not records:raise StageResourceError()
        originals=[];temporaries=[]
        for record_path in records:
            target,original=_owned_record(record_path,outbox)
            if target!=path or str(original) in durable_refs:return False
            if linked_path(original):raise StageResourceError()
            originals.append(original)
            record=_read_record(record_path)
            for suffix in ('.partial','.repair'):
                temporary=directory/(path.stem+'-'+record['operation']+suffix)
                if linked_path(temporary):raise StageResourceError()
                temporaries.append(temporary)
        for original in originals:original.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        for temporary in temporaries:temporary.unlink(missing_ok=True)
        for record_path in records:record_path.unlink(missing_ok=True)
    else:path.unlink(missing_ok=True)
    return True


class ResourceCleanupWork(DiskWork):
    """Already-authorized resource retirement; business cancellation is separate."""


class ShelfStageStore(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner
        self.pool=QThreadPool(owner);self.pool.setMaxThreadCount(1)
        self.leases={};self.retired={};self.retiring=set();self.stamps={}
        self.cleanup_blocked=set();self.durable_refs={};self.origins={};self.scanned=set()

    def cleanup_blocked_for(self,account):
        """A failed owned outbox cannot stop a different account's admissions."""
        if not account or not self.cleanup_blocked:return False
        outbox=os.path.normcase(os.path.abspath(self.owner.root/account/'outbox'))
        # These paths are assigned by successful owned binding preparation;
        # deriving the namespace does not stat/resolve a filesystem on Qt.
        return any(os.path.normcase(os.path.abspath(Path(path).parent))==outbox
                   for path in self.cleanup_blocked)

    def acquire(self,path):
        path=str(path)
        assert path not in self.retiring,'Cannot open a retiring shelf body'
        self.leases[path]=self.leases.get(path,0)+1
        active=True
        def release():
            nonlocal active
            assert active,'Shelf body lease released twice'
            active=False
            count=self.leases[path]-1
            if count:self.leases[path]=count
            else:self.leases.pop(path)
            self.collect()
        return release

    def _job(self,action,args,callback,*,owner_id=None):
        owner=self.owner
        cleanup=action is remove_unreferenced
        assert not cleanup or owner_id is None,'Authorized GC cannot belong to a cancellable upload'
        job=(ResourceCleanupWork if cleanup else DiskWork)(action,*args);owner.jobs.add(job)
        if owner_id is not None:owner._job_owners[job]=owner_id
        def done(result,error):
            assert QThread.currentThread()==owner.thread(), 'Resource completion must run on its Qt owner'
            owner.jobs.discard(job)
            if owner_id is not None:owner._job_owners.pop(job,None)
            callback(result,error)
        job.signals.done.connect(done,Qt.ConnectionType.QueuedConnection);self.pool.start(job)

    def bind(self,snapshot,callback):
        owner=self.owner
        if owner.closed:return
        account=owner.account_id;epoch=owner.epoch
        business_token=owner._upload_tokens.get(snapshot['id'])
        old=str(snapshot['path']);target=str(_canonical(owner.root/account/'outbox',account,snapshot))
        if target in self.retiring:
            # A terminal cleanup is already queued. Keep this slot and original
            # body until that terminal callback, rather than racing its unlink.
            def resume_binding():
                if owner.closed or owner.epoch!=epoch or owner.account_id!=account:return
                if (business_token is None or business_token.is_set()
                        or owner._upload_tokens.get(snapshot['id']) is not business_token):return
                if not owner.payload_gate._live(snapshot):return
                self.bind(snapshot,callback)
            self.retired[target][1].append(resume_binding);return
        release_old=self.acquire(old);release_target=self.acquire(target) if target!=old else None
        def finish(result,error):
            try:callback(result,error)
            finally:
                release_old()
                if release_target:release_target()
        def prepared(result,error):
            if result:
                self.origins.setdefault(result['path'],set()).add(result['source'])
            stale=(owner.closed or owner.epoch!=epoch or owner.state.get('resumable_uploads',{}).get(snapshot['id']) is not snapshot)
            if error or stale:
                if result:self.retire(account,dict(snapshot,path=result['path']))
                finish(None,error or InterruptedError());return
            snapshot['path']=result['path']
            try:owner._persist()
            except OSError as problem:
                snapshot['path']=old;finish(None,problem);return
            except BaseException:
                snapshot['path']=old
                release_old()
                if release_target:release_target()
                raise
            self.stamps[id(snapshot)]=result['original_mtime']
            self.origins.setdefault(result['path'],set()).add(result['source'])
            if result['source']==result['path']:
                finish(result,None);return
            # Preparation has closed its reader. This binding's original lease
            # is still held; no other reader may lose its bytes during cleanup.
            if self.leases.get(result['source'],0)>1:
                self.cleanup_blocked.add(result['source']);finish(None,StageResourceError());return
            self.retiring.add(result['source'])
            def cleaned(removed,problem):
                self.retiring.remove(result['source'])
                if problem or not removed:
                    self.cleanup_blocked.add(result['source']);finish(None,problem or StageResourceError())
                else:
                    self.cleanup_blocked.discard(result['source']);finish(result,None)
            self._job(remove_unreferenced,(owner.root/account/'outbox',self.durable_refs[account],result['source']),cleaned)
        self._job(prepare_binding,(owner.root/account/'outbox',account,dict(snapshot)),prepared,owner_id=snapshot['id'])

    def age(self,snapshot):
        if id(snapshot) not in self.stamps:raise StageResourceError()
        return self.stamps[id(snapshot)]

    def retire(self,account,snapshot):
        self.stamps.pop(id(snapshot),None)
        path=str(snapshot['path'])
        self.retired.setdefault(path,[account,[]])

    def committed(self,account,state):
        # Published only AFTER atomic_json succeeds. Every deletion claims its
        # path on Qt before its worker is queued; new binds/readers cannot use
        # that retiring path. No worker read handle can block journal replace.
        self.durable_refs[account]=frozenset(row.get('path') for row in state.get('resumable_uploads',{}).values() if isinstance(row,dict))
        if account and account not in self.scanned:
            self.scanned.add(account)
            def scanned(result,error):
                if error:
                    self.owner.status_changed.emit('同步缓存资源检查未完成，缓存已保留。');return
                rows,invalid=result
                for target,source in rows:
                    self.origins.setdefault(target,set()).add(source)
                    refs=self.durable_refs[account]
                    if target not in refs and source not in refs:
                        self.retired.setdefault(target,[account,[]])
                if invalid:self.owner.status_changed.emit('部分同步缓存记录不可用，缓存已保留。')
                self.collect()
            self._job(scan_bindings,(self.owner.root/account/'outbox',),scanned)
        self.collect()

    def collect(self):
        owner=self.owner
        for path,(account,waiters) in tuple(self.retired.items()):
            if (self.leases.get(path) or path in self.retiring
                    or any(self.leases.get(p) for p in self.origins.get(path,()))):continue
            if account not in self.durable_refs or path in self.durable_refs[account]:continue
            # Do not queue a delete while this process still owns a journal ref.
            if account==owner.account_id and any(row.get('path')==path for row in (owner.state or {}).get('resumable_uploads',{}).values()):continue
            self.retiring.add(path)
            def done(result,error,path=path):
                self.retiring.remove(path)
                _,callbacks=self.retired.pop(path)
                if result:
                    for source in self.origins.pop(path,set()):self.cleanup_blocked.discard(source)
                if error:
                    # Preserve bytes on uncertain ownership/IO. A later explicit
                    # retry can collect them; no guessed unlink or busy timer.
                    owner.status_changed.emit('同步缓存清理未完成，缓存已保留。')
                for callback in callbacks:callback()
            self._job(remove_unreferenced,(owner.root/account/'outbox',self.durable_refs[account],path),done)
