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

from PySide6.QtCore import QObject, QThreadPool, QThread, Qt, QTimer
from account_store import atomic_json
from sync_files import DiskWork, MAX_FILE_BYTES, StageCleanupError
from selection_archive import _source_reader, _is_link

_NAMESPACE = UUID('a9428b39-1dbc-4276-b2cc-5d8d9f9341e8')
_BLOB = re.compile(r'[0-9a-f]{32}(?:\.owned)?\.blob\Z')
_OWNED_BLOB = re.compile(r'[0-9a-f]{32}\.owned\.blob\Z')
_RECORD_LIMIT = 16384


class StageResourceError(ValueError):
    """A resource binding cannot safely be used or guessed from body mtime."""
    def __init__(self):
        super().__init__('同步缓存资源记录不可用，已暂停此项并保留缓存，请检查后重试。')


class MaterializationRegistrationUncertain(StageResourceError):
    """Publication may have happened; its ownership cannot yet be verified."""
    def __init__(self,target):
        super().__init__()
        self.target=str(target)


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


def _materialization_record(outbox,path):
    return Path(outbox)/'materializations-v1'/(Path(path).name[:-len('.owned.blob')]+'.json')


def register_materialization(outbox,path):
    """Publish ownership before this exact generation may create any body."""
    outbox=Path(outbox);path=Path(path);validate_owned_outbox(outbox)
    if not _OWNED_BLOB.fullmatch(path.name) or not _regular(path,outbox):raise StageResourceError()
    record_path=_materialization_record(outbox,path)
    if linked_path(record_path.parent):raise StageResourceError()
    record_path.parent.mkdir(exist_ok=True)
    if record_path.exists() or path.exists():raise StageResourceError()
    record=dict(version=1,account=outbox.parent.name,source=path.name,operation=uuid4().hex)
    record['record_sha256']=_record_digest(record)
    try:atomic_json(record_path,record)
    except OSError as publication_error:
        # replace() can succeed before atomic_json's final temporary unlink
        # fails. Only this attempt's exact nonce/record proves publication;
        # a rejected or different prior owner never grants cleanup authority.
        try:published=_read_record(record_path)
        except StageResourceError as verification_error:
            if isinstance(verification_error.__cause__,FileNotFoundError):raise publication_error
            raise MaterializationRegistrationUncertain(path) from publication_error
        if published==record:raise StageCleanupError(path) from publication_error
        raise MaterializationRegistrationUncertain(path) from publication_error


def _materialization_metadata(outbox,path):
    outbox=Path(outbox);path=Path(path);validate_owned_outbox(outbox)
    if (not _OWNED_BLOB.fullmatch(path.name)
            or os.path.normcase(os.path.abspath(path.parent))!=os.path.normcase(os.path.abspath(outbox))):raise StageResourceError()
    record_path=_materialization_record(outbox,path)
    if linked_path(record_path.parent):raise StageResourceError()
    record=_read_record(record_path)
    if (not isinstance(record,dict) or set(record)!={'version','account','source','operation','record_sha256'}
            or type(record.get('version')) is not int or record['version']!=1 or record.get('account')!=outbox.parent.name or record.get('source')!=path.name
            or not isinstance(record.get('operation'),str) or not re.fullmatch(r'[0-9a-f]{32}',record['operation'])
            or record.get('record_sha256')!=_record_digest(record)):raise StageResourceError()
    return record_path


def checked_materialization(outbox,path):
    outbox=Path(outbox);path=Path(path)
    if not _OWNED_BLOB.fullmatch(path.name) or not _regular(path,outbox):raise StageResourceError()
    record_path=_materialization_record(outbox,path)
    if linked_path(record_path.parent):raise StageResourceError()
    if not path.exists() and not record_path.exists():return None
    return _materialization_metadata(outbox,path)


def _remove_ordinary(outbox,path):
    # Legacy ordinary snapshots keep their former owned-path contract. Only
    # the new explicit purpose can require or grant this new intent authority.
    record=checked_materialization(outbox,path) if _OWNED_BLOB.fullmatch(path.name) else None
    path.unlink(missing_ok=True)
    if record is not None:record.unlink(missing_ok=True)


def scan_materializations(outbox,cancel):
    outbox=Path(outbox);validate_owned_outbox(outbox);directory=outbox/'materializations-v1'
    if linked_path(directory):raise StageResourceError()
    sources=set();invalid=0
    for index,record in enumerate(directory.glob('*.json')):
        if cancel.is_set():raise InterruptedError()
        if index>=4096:raise StageResourceError()
        if not re.fullmatch(r'[0-9a-f]{32}\.json',record.name):invalid+=1;continue
        source=outbox/(record.stem+'.owned.blob')
        try:
            checked=_materialization_metadata(outbox,source)
            if checked is None:raise StageResourceError()
        except (OSError,ValueError,TypeError,KeyError):invalid+=1;continue
        # Even if body unlink succeeded, intent unlink is still a terminal
        # resource operation. Its failure must not become zero after restart.
        sources.add(str(source))
    # Only this explicit new managed purpose is inventoried; legacy unknown
    # ordinary files are neither swept nor guessed into deletion authority.
    for index,source in enumerate(outbox.glob('*.owned.blob')):
        if cancel.is_set():raise InterruptedError()
        if index>=4096:raise StageResourceError()
        if not _OWNED_BLOB.fullmatch(source.name):continue
        if str(source) not in sources:invalid+=1
    return sources,invalid


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
    if _OWNED_BLOB.fullmatch(source.name):checked_materialization(outbox,source)
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


def restore_materializations(outbox, durable_refs, cancel):
    """Inspect only journal/validated-record owned sources, on the disk pool."""
    try:validate_owned_outbox(Path(outbox))
    except StageResourceError:
        # No traversal of an invalid admission root. Each real stage still
        # takes its own worker admission path, which reports the task error
        # before it can read/copy anything through this root.
        if durable_refs:raise
        return [],0,set(),set()
    rows,invalid=scan_bindings(outbox,cancel)
    sources,ordinary_invalid=scan_materializations(outbox,cancel);invalid+=ordinary_invalid;unsafe=set()
    candidates={source for target,source in rows};candidates.update(sources)
    candidates.update(path for path in durable_refs if path and not canonical_path(path))
    for value in candidates:
        if cancel.is_set():raise InterruptedError()
        path=Path(value)
        try:
            if not owned_stage_path(path,outbox):raise StageResourceError()
            if path.exists():sources.add(str(path))
        except (OSError,StageResourceError):
            # A validated record identifies this one source generation, but
            # its bytes cannot be traversed/reclaimed. Charge its place;
            # never guess that an unreadable or reparse source disappeared.
            sources.add(str(path));unsafe.add(str(path))
    return rows,invalid,sources,unsafe


def inventory_materializations(root, current_account, committed_refs, cancel):
    """Read resource records only: no live journal handles or deletion grant."""
    root=Path(root)
    if linked_path(root):raise StageResourceError()
    accounts=[];total=0
    for index,directory in enumerate(root.iterdir()):
        if cancel.is_set():raise InterruptedError()
        if index>=4096:raise StageResourceError()
        account=directory.name
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',account):continue
        if linked_path(directory):raise StageResourceError()
        if not directory.is_dir():continue
        outbox=directory/'outbox'
        try:validate_owned_outbox(outbox)
        except StageResourceError:
            if account==current_account and not committed_refs.get(account):continue
            raise
        rows,invalid,sources,unsafe=restore_materializations(outbox,committed_refs.get(account,frozenset()),cancel)
        total+=len(rows)+invalid
        if invalid or total>4096:raise StageResourceError()
        accounts.append((account,rows,sources,unsafe))
    return accounts


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
            if _OWNED_BLOB.fullmatch(original.name):checked_materialization(outbox,original)
            originals.append(original)
            record=_read_record(record_path)
            for suffix in ('.partial','.repair'):
                temporary=directory/(path.stem+'-'+record['operation']+suffix)
                if linked_path(temporary):raise StageResourceError()
                temporaries.append(temporary)
        for original in originals:_remove_ordinary(outbox,original)
        path.unlink(missing_ok=True)
        for temporary in temporaries:temporary.unlink(missing_ok=True)
        for record_path in records:record_path.unlink(missing_ok=True)
    else:_remove_ordinary(outbox,path)
    return True


class ResourceCleanupWork(DiskWork):
    """Already-authorized resource retirement; business cancellation is separate."""


class ShelfStageStore(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner
        self.pool=QThreadPool(owner);self.pool.setMaxThreadCount(1)
        self.leases={};self.retired={};self.retiring=set();self.stamps={}
        self.durable_refs={};self.origins={};self.scanned=set()
        self.materializations={};self.restoring=set();self.restore_errors=set()
        self.inventory='new'

    @property
    def cleanup_blocked(self):
        return {row['path'] for row in self.materializations.values()
                if row['phase']=='failed' and row['path'] is not None}

    def budget_full(self,account):
        """Only ordinary copies occupy admission; canonical files do not."""
        if not account:return False
        if (self.inventory in ('pending','error') or self.inventory=='new' and account in self.durable_refs
                or self.restoring or self.restore_errors):return True
        capacity=1+self.owner.small_lane.capacity
        return len(self.materializations)>=capacity

    def retry_restore(self,account,state):
        for row in tuple(self.materializations.values()):
            if row['account']==account and row['phase']=='failed' and row['path'] is not None:
                self.retired.setdefault(row['path'],[account,[]])
        self.collect()
        if self.inventory=='error':self.inventory='new'
        if account not in self.restore_errors and not any(
                row['account']==account and row['phase']=='unresolved' for row in self.materializations.values()):return
        if account in self.restoring:return
        self.scanned.discard(account)
        self.committed(account,state)

    def reserve(self,account):
        assert not self.budget_full(account),'Ordinary materialization admitted without capacity'
        reservation=object()
        path=str(self.owner.root/account/'outbox'/(uuid4().hex+'.owned.blob'))
        self.materializations[reservation]=dict(account=account,path=path,phase='staging')
        return reservation

    def staged(self,reservation,result,error):
        row=self.materializations[reservation]
        if isinstance(error,MaterializationRegistrationUncertain):
            assert error.target==row['path'],'Uncertain publication belongs to another generation'
            row['phase']='unresolved'
            self.restore_errors.add(row['account'])
        elif isinstance(error,StageCleanupError):
            assert error.target==row['path'],'Failure belongs to another materialization generation'
            row['phase']='failed'
            self.retire(row['account'],dict(path=row['path']))
            self.collect()
        elif isinstance(result,dict) and result.get('path'):
            assert str(result['path'])==row['path'],'Worker returned another materialization generation'
            row['phase']='ordinary'
        else:
            self.materializations.pop(reservation)
            self._wake()

    def _remember_source(self,account,path,*,failed=False):
        path=str(path)
        existing=next((row for row in self.materializations.values()
                       if row['account']==account and row['path']==path),None)
        if existing is None:
            existing=dict(account=account,path=path,phase='ordinary')
            self.materializations[object()]=existing
        if failed:existing['phase']='failed'

    def deleted(self,account,path):
        # UUID paths and the actual deletion terminal identify this generation;
        # cancelling a newer item with the same item ID cannot release it.
        for key,row in tuple(self.materializations.items()):
            if row['account']==account and row['path']==str(path):self.materializations.pop(key)
        self._wake()

    def discard_unbound(self,reservation):
        # The stage just completed, and its business callback has never run:
        # this exact StageUUID has never entered a durable journal. Retain its
        # resource ownership until protected GC really closes/deletes it.
        row=self.materializations[reservation];account=row['account'];path=row['path']
        assert path is not None and path not in self.retiring
        self.retiring.add(path)
        def removed(result,error):
            self.retiring.remove(path)
            if result:self.deleted(account,path)
            else:
                row['phase']='failed'
                self.owner.status_changed.emit('同步缓存清理未完成，缓存已保留。')
        self._job(remove_unreferenced,(self.owner.root/account/'outbox',frozenset(),path),removed)

    def discard_snapshot(self,snapshot):
        reservation=next((key for key,row in self.materializations.items()
                          if row['account']==self.owner.account_id and row['path']==str(snapshot['path'])),None)
        assert reservation is not None,'A created stage lost its materialization owner'
        self.discard_unbound(reservation)

    def _wake(self):
        owner=self.owner
        if not owner.closed and owner.account_id and owner.upload_enabled:
            owner.debounce.start(1)
            # An upload-tail cycle intentionally remains running while other
            # PUTs are held. Its normal tick refuses reentry; the existing
            # independent continuation owns that explicit idle-main boundary.
            QTimer.singleShot(0,owner,owner.small_lane._continue)

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
                self._remember_source(account,result['source'],failed=True);finish(None,StageResourceError());return
            self.retiring.add(result['source'])
            def cleaned(removed,problem):
                self.retiring.remove(result['source'])
                if problem or not removed:
                    self._remember_source(account,result['source'],failed=True);finish(None,problem or StageResourceError())
                else:
                    self.deleted(account,result['source']);finish(result,None)
            self._job(remove_unreferenced,(owner.root/account/'outbox',self.durable_refs[account],result['source']),cleaned)
        self._job(prepare_binding,(owner.root/account/'outbox',account,dict(snapshot)),prepared,owner_id=snapshot['id'])

    def age(self,snapshot):
        if id(snapshot) not in self.stamps:raise StageResourceError()
        return self.stamps[id(snapshot)]

    def retire(self,account,snapshot):
        self.stamps.pop(id(snapshot),None)
        path=str(snapshot['path'])
        self.retired.setdefault(path,[account,[]])

    def _start_inventory(self):
        account=self.owner.account_id
        if account and not self.owner.closed and self.inventory=='new':
            self.inventory='pending'
            def inventoried(result,error):
                if error:
                    if isinstance(error,InterruptedError):
                        self.inventory='new';self._start_inventory();return
                    self.inventory='error'
                    self.owner.status_changed.emit('同步缓存资源库存未完成，容量待确认，缓存已保留。')
                    return
                for saved_account,rows,sources,unsafe in result:
                    for target,source in rows:self.origins.setdefault(target,set()).add(source)
                    for source in sources:self._remember_source(saved_account,source)
                    for source in unsafe:
                        row=next(row for row in self.materializations.values() if row['account']==saved_account and row['path']==source)
                        row['phase']='unresolved'
                    if unsafe:self.owner.status_changed.emit('部分同步缓存资源不可用，缓存已保留。')
                self.inventory='ready';self._wake()
            self._job(inventory_materializations,(self.owner.root,account,dict(self.durable_refs)),inventoried)

    def committed(self,account,state):
        # Published only AFTER atomic_json succeeds. Every deletion claims its
        # path on Qt before its worker is queued; new binds/readers cannot use
        # that retiring path. No worker read handle can block journal replace.
        self.durable_refs[account]=frozenset(row.get('path') for row in state.get('resumable_uploads',{}).values() if isinstance(row,dict))
        self._start_inventory()
        if account and account not in self.scanned:
            self.scanned.add(account);self.restoring.add(account)
            refs=self.durable_refs[account]
            def scanned(result,error):
                self.restoring.discard(account)
                if error:
                    if isinstance(error,InterruptedError):self.scanned.discard(account)
                    else:self.restore_errors.add(account)
                    self.owner.status_changed.emit('同步缓存资源检查未完成，缓存已保留。');return
                rows,invalid,sources,unsafe=result
                self.restore_errors.discard(account)
                for key,row in tuple(self.materializations.items()):
                    if row['account']==account and row['phase']=='unresolved':self.materializations.pop(key)
                for source in sources:
                    self._remember_source(account,source)
                    if source not in self.durable_refs[account]:self.retired.setdefault(source,[account,[]])
                for source in unsafe:
                    row=next(row for row in self.materializations.values() if row['account']==account and row['path']==source)
                    row['phase']='unresolved'
                # Invalid sidecars are unknown physical owners, not permission
                # to scan/unlink their bytes. Conservatively charge each one
                # a place. An unparseable record also leaves total ownership
                # uncertain; an explicit retry must complete that inventory.
                for unused in range(invalid):
                    self.materializations[object()]=dict(account=account,path=None,phase='unresolved')
                if invalid:self.restore_errors.add(account)
                for target,source in rows:
                    self.origins.setdefault(target,set()).add(source)
                    refs=self.durable_refs[account]
                    if target not in refs and source not in refs:
                        self.retired.setdefault(target,[account,[]])
                if invalid or unsafe:self.owner.status_changed.emit('部分同步缓存记录不可用，缓存已保留。')
                self.collect();self._wake()
            self._job(restore_materializations,(self.owner.root/account/'outbox',refs),scanned)
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
            def done(result,error,path=path,account=account):
                self.retiring.remove(path)
                _,callbacks=self.retired.pop(path)
                if result:
                    for source in self.origins.pop(path,set()):self.deleted(account,source)
                    self.deleted(account,path)
                if error:
                    # Preserve bytes on uncertain ownership/IO. A later explicit
                    # retry can collect them; no guessed unlink or busy timer.
                    owner.status_changed.emit('同步缓存清理未完成，缓存已保留。')
                    if not canonical_path(path) and not any(row['path']==path and row['phase']=='unresolved' for row in self.materializations.values()):
                        self._remember_source(account,path,failed=True)
                for callback in callbacks:callback()
            self._job(remove_unreferenced,(owner.root/account/'outbox',self.durable_refs[account],path),done)
