"""Private original-byte clipboard cache; never owns system/user source files.

Content creation/verification/deletion run on the helper/operation worker. A
content reference and its scoped holders retain one lease. Releasing the final
lease only closes its own read-only lock and wakes a coalesced cleanup worker;
there is no per-copy cleanup thread or unbounded in-memory deletion queue.
"""
from __future__ import annotations
import hashlib,json,math,mmap,os,re,shutil,tempfile,threading,time,weakref
from contextlib import contextmanager,ExitStack
from dataclasses import dataclass,field
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

_INSTANCE=uuid4().hex
_TOKEN=re.compile(r'[0-9a-f]{32}\Z')
_NAMES={'text','html','image','native_image'}
MARKER_MAX_BYTES=1024
ORIGINAL_CHUNK_BYTES=262144
FOREIGN_RECOVERY_GRACE_SECONDS=86400
CLEANUP_BATCH_ENTRIES=32
CLEANUP_BATCH_YIELD_SECONDS=.01
_LIVE=weakref.WeakValueDictionary()
_GUARD=threading.RLock();_WAKE=threading.Event();_IDLE=threading.Event();_CLEANER=None;_LAST_CLEANUP_ERROR=None;_LAST_CLEANUP_STATUS=None;_STARTUP_SWEEP=False
_IDLE.set()
class CacheIdentityError(ValueError):pass
class CacheLeaseBusy(OSError):pass

def _record_failure(phase,error):
    global _LAST_CLEANUP_ERROR
    diagnostic={'phase':phase,'exception':type(error).__name__ if type(error).__name__ in ('OSError','PermissionError','ValueError','FileNotFoundError','BlockingIOError') else 'Exception'}
    for name in ('errno','winerror'):
        value=getattr(error,name,None)
        if type(value)is int:diagnostic[name]=value
    _LAST_CLEANUP_ERROR=diagnostic

def cache_base():
    # OS temp roots may contain a system alias (/var on macOS). Resolve only
    # that trusted base; private namespaces and returned capabilities must
    # still pass _no_links without following any of their links.
    return Path(tempfile.gettempdir()).resolve(strict=True)/'FlickDock-native-originals-v1'

def _no_links(path):
    for item in (path,*path.parents):
        if item.is_symlink() or item.is_junction():raise ValueError('Unsafe owned cache root')

class _Lock:
    def __init__(self,path):
        self.file=open(path,'rb')
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            if error.errno in (11,13,36) or getattr(error,'winerror',None) in (32,33,158):raise CacheLeaseBusy(error.errno,'Owned cache lease is active') from None
            raise
        except Exception:self.file.close();raise
    def close(self):
        if self.file is None:return
        file,self.file=self.file,None
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_UN)
        finally:file.close()

def _metadata(root,token):
    _no_links(root)
    if not _TOKEN.fullmatch(token) or root.name!=token:raise ValueError('Invalid owned cache identity')
    valid=[];failure=None
    for path in (root/'owner.json',root.with_suffix('.owner.json')):
        try:
            _no_links(path)
            if path.stat().st_size>MARKER_MAX_BYTES:raise ValueError('Invalid owned cache marker')
            data=json.loads(path.read_text(encoding='ascii'))
            if not isinstance(data,dict) or data.get('version')!=1 or data.get('token')!=token or not _TOKEN.fullmatch(str(data.get('instance',''))):raise ValueError('Invalid owned cache marker')
            created=data.get('created')
            if type(created) not in (int,float) or not math.isfinite(created) or created<=0:raise ValueError('Invalid owned cache time')
            valid.append(data)
        except (OSError,ValueError) as error:failure=error
    if not valid:raise failure or ValueError('Missing owned cache marker')
    if any(data['instance']!=valid[0]['instance'] for data in valid):raise CacheIdentityError('Conflicting owned cache markers')
    return valid[0]

def _sweep():
    global _LAST_CLEANUP_STATUS
    base=cache_base()
    if not base.exists():return
    _no_links(base)
    # Iterate in bounded batches, without allocating a list of all roots or
    # maintaining a queue proportional to the number of previous copies.
    with os.scandir(base) as entries:
        count=0
        for entry in entries:
            count+=1
            if count%CLEANUP_BATCH_ENTRIES==0:time.sleep(CLEANUP_BATCH_YIELD_SECONDS) # Cleanup worker only, never input/UI.
            sidecar=re.fullmatch(r'([0-9a-f]{32})\.owner\.json',entry.name)
            if sidecar:
                token=sidecar[1];path=Path(entry.path)
                with _GUARD:live=token in _LIVE
                if live or (base/token).exists():continue
                try:
                    _no_links(path)
                    if path.is_file() and time.time()-path.stat().st_mtime>=FOREIGN_RECOVERY_GRACE_SECONDS:path.unlink()
                except OSError as error:_record_failure('cleanup_marker',error)
                continue
            if not _TOKEN.fullmatch(entry.name):continue
            root=Path(entry.path);lock=None
            try:
                data=_metadata(root,entry.name)
                with _GUARD:
                    if entry.name in _LIVE:continue
                # Another process may be between publication and adoption.
                # Its existing lock protects active holders; fresh unadopted
                # roots have a conservative crash-recovery grace period.
                if data['instance']!=_INSTANCE and time.time()-float(data.get('created',0))<FOREIGN_RECOVERY_GRACE_SECONDS:continue
                lock=_Lock(root/'lease.lock')
                with _GUARD:
                    if entry.name in _LIVE:continue
                for child in root.iterdir():
                    if child.is_symlink() or child.is_junction() or not child.is_file():raise ValueError('Unsafe owned cache child')
                    if child.name not in {'owner.json','lease.lock',*(n+s for n in _NAMES for s in ('.bin','.partial'))}:raise ValueError('Unexpected owned cache child')
                lock.close();lock=None
                shutil.rmtree(root)
                sidecar=root.with_suffix('.owner.json')
                if sidecar.exists():
                    _no_links(sidecar);sidecar.unlink()
            except CacheLeaseBusy:
                # A legitimate writer/consumer owns this original. This is a
                # normal skip, distinct from invalid identity or delete IO.
                _LAST_CLEANUP_STATUS={'phase':'cleanup_lease','status':'active'};continue
            except (OSError,ValueError,TypeError,json.JSONDecodeError) as error:
                # Failed deletion remains as a marked on-disk cleanup item.
                # Next release/startup sweep retries; no tight retry loop.
                _record_failure('cleanup_root',error);continue
            finally:
                if lock is not None:lock.close()

def _clean():
    global _CLEANER,_LAST_CLEANUP_ERROR
    try:
        while True:
            _WAKE.wait();_WAKE.clear();_IDLE.clear()
            try:_sweep()
            except (OSError,ValueError) as error:
                # External filesystem failures retain durable ownership and a
                # diagnostic. Programmer invariants are not caught here.
                _record_failure('cleanup',error)
            _IDLE.set()
    finally:
        with _GUARD:_CLEANER=None
        _IDLE.set()

def schedule_cleanup():
    global _CLEANER
    with _GUARD:
        _IDLE.clear();_WAKE.set()
        if _CLEANER is None:
            _CLEANER=threading.Thread(target=_clean,name='FlickDock-native-cache-cleanup',daemon=True);_CLEANER.start()

class CacheCapability:
    """Parent-owned operation capability, created off the GUI thread."""
    def __init__(self,*,base=None,cancel=None):
        global _STARTUP_SWEEP
        self.token=uuid4().hex;self.instance=_INSTANCE;self.base=Path(base) if base is not None else cache_base()
        self.root=self.base/self.token;self.lock=None;self.cancel=cancel;self._has_root=False
        with _GUARD:
            _LIVE[self.token]=self;startup=not _STARTUP_SWEEP;_STARTUP_SWEEP=True
        if startup and self.base.exists():schedule_cleanup()
    def arguments(self):return ['--native-cache-root',str(self.root),'--native-cache-token',self.token,'--native-cache-instance',self.instance]
    def adopt(self,descriptor):
        self._has_root=True
        data=_metadata(self.root,self.token)
        if data['instance']!=self.instance:raise ValueError('Wrong owned cache instance')
        if descriptor.get('cache_token')!=self.token:raise ValueError('Wrong owned cache capability')
        parts=descriptor.get('local_parts');kind=descriptor.get('local_kind');size=descriptor.get('logical_size');fingerprint=descriptor.get('fingerprint')
        if kind not in ('text','image') or type(size)is not int or size<0 or not isinstance(parts,list) or not 1<=len(parts)<=2 or not isinstance(fingerprint,str) or not re.fullmatch('[0-9a-f]{64}',fingerprint):raise ValueError('Invalid local original descriptor')
        self.lock=_Lock(self.root/'lease.lock');verified=[];names=set()
        try:
            for part in parts:
                if not isinstance(part,dict):raise ValueError('Invalid local original part')
                name=part.get('name');length=part.get('size');digest=part.get('sha256')
                if name not in _NAMES or name in names or type(length)is not int or length<0 or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Invalid local original part')
                names.add(name);path=self.root/(name+'.bin');_no_links(path)
                if not path.is_file() or path.stat().st_size!=length:raise ValueError('Local original size mismatch')
                sha=hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda:source.read(ORIGINAL_CHUNK_BYTES),b''):
                        if self.cancel is not None and self.cancel.is_set():raise InterruptedError('Owned cache verification cancelled')
                        sha.update(chunk)
                if sha.hexdigest()!=digest:raise ValueError('Local original hash mismatch')
                mime=part.get('mime');encoding=part.get('encoding')
                if name=='image' and mime not in ('image/png','image/jpeg','image/webp','image/bmp','image/tiff'):raise ValueError('Invalid local image format')
                if name=='text' and encoding not in ('utf-16-le','utf-8'):raise ValueError('Invalid local text encoding')
                verified.append(MappingProxyType(dict(name=name,path=str(path),size=length,sha256=digest,mime=mime,encoding=encoding)))
            if (kind=='text' and names-{'text','html'}) or (kind=='image' and names-{'image','native_image'}) or ('text' if kind=='text' else 'image') not in names:raise ValueError('Local original parts mismatch')
            by_name={part['name']:part for part in verified}
            if kind=='image':
                expected=by_name['image']['sha256'];actual_size=by_name['image']['size']
                if 'native_image' in by_name:
                    from win_clipboard_native import dib_layout,_chunks
                    with open(by_name['native_image']['path'],'rb') as source,mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ) as raw:
                        prefix,start,end,mime=dib_layout(raw);sha=hashlib.sha256(prefix)
                        for chunk in _chunks(raw,start,end):
                            if self.cancel is not None and self.cancel.is_set():raise InterruptedError('Original verification cancelled')
                            sha.update(chunk)
                        if sha.hexdigest()!=expected or len(prefix)+end-start!=actual_size or mime!=by_name['image']['mime']:raise ValueError('Original DIB derivative mismatch')
            else:
                from win_clipboard_native import _text_parts
                with ExitStack() as stack:
                    def mapped(name):
                        if name not in by_name:return None
                        source=stack.enter_context(open(by_name[name]['path'],'rb'))
                        return stack.enter_context(mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ)) if by_name[name]['size'] else b''
                    ignored,actual_size,expected,end=_text_parts(mapped('text'),mapped('html'),0,cancel=self.cancel,encoding=by_name['text']['encoding'])
            if expected!=fingerprint or actual_size!=size:raise ValueError('Local original identity mismatch')
            return LocalNativeContent(kind,tuple(verified),(kind,fingerprint),size,'too_large',self)
        except Exception:self.lock.close();self.lock=None;raise
    def retire(self):
        """After confirmed child exit, mark even failed partial initialization.

        Only the operation's unguessable parent capability may repair its own
        marker. No arbitrary returned path or source file gains this authority.
        """
        if self.lock is not None:return
        try:exists=self.root.exists()
        except OSError:self._has_root=True;raise
        if not exists:return
        self._has_root=True
        _no_links(self.root)
        if self.root.parent!=self.base or self.root.name!=self.token:raise ValueError('Invalid retired cache root')
        allowed={'owner.json','lease.lock',*(name+suffix for name in _NAMES for suffix in ('.bin','.partial'))}
        if any(child.name not in allowed or child.is_symlink() or child.is_junction() or not child.is_file() for child in self.root.iterdir()):raise ValueError('Unsafe retired cache contents')
        try:existing=_metadata(self.root,self.token)
        except CacheIdentityError:raise
        except (OSError,ValueError):existing=None
        if existing is not None:
            if existing['instance']!=self.instance:raise CacheIdentityError('Wrong owned cache instance')
            if (self.root/'lease.lock').exists():return
        (self.root/'owner.json').write_text(json.dumps(dict(version=1,token=self.token,instance=self.instance,created=time.time())),encoding='ascii')
        if not (self.root/'lease.lock').exists():(self.root/'lease.lock').write_bytes(b'1')
    def __del__(self):
        global _LAST_CLEANUP_ERROR
        # Read-only lock close has no payload read/write/flush. Directory scans
        # and deletion are exclusively on the single coalesced worker.
        lock,self.lock=getattr(self,'lock',None),None
        try:
            if lock is not None:lock.close()
        except Exception as error:
            _record_failure('close',error)
        finally:
            with _GUARD:_LIVE.pop(getattr(self,'token',''),None)
            if getattr(self,'_has_root',False):schedule_cleanup()

@dataclass(frozen=True)
class LocalNativeContent:
    kind:str
    parts:tuple=field(repr=False)
    fingerprint:tuple
    logical_size:int
    sync_policy:str
    _lease:CacheCapability=field(repr=False,compare=False)
    @contextmanager
    def acquire(self):yield self

class CacheSink:
    """Child writer; call only after CloseClipboard, close before result EOF."""
    def __init__(self,root,token,instance):
        self.root=Path(root);self.token=token;self.instance=instance;self.lock=None;self.parts=[]
        if not _TOKEN.fullmatch(token) or not _TOKEN.fullmatch(instance) or self.root.name!=token or self.root.parent!=cache_base():raise ValueError('Invalid owned cache capability')
    def _prepare(self):
        if self.lock is not None:return
        _no_links(self.root.parent);self.root.parent.mkdir(exist_ok=True)
        marker=json.dumps(dict(version=1,token=self.token,instance=self.instance,created=time.time()))
        # Establish durable ownership before a payload directory can exist.
        # If startup dies before its in-directory marker, the sidecar still
        # allows safe recovery of this exact capability after the grace/lock.
        with self.root.with_suffix('.owner.json').open('x',encoding='ascii') as owner:owner.write(marker)
        self.root.mkdir()
        (self.root/'lease.lock').write_bytes(b'1');self.lock=_Lock(self.root/'lease.lock')
        (self.root/'owner.json').write_text(marker,encoding='ascii')
    def write(self,name,chunks,*,mime=None,encoding=None):
        if name not in _NAMES or any(p['name']==name for p in self.parts):raise ValueError('Invalid owned cache output')
        self._prepare();sha=hashlib.sha256();size=0;partial=self.root/(name+'.partial')
        with partial.open('xb') as target:
            for chunk in chunks:
                target.write(chunk);sha.update(chunk);size+=len(chunk)
        partial.replace(self.root/(name+'.bin'))
        part=dict(name=name,size=size,sha256=sha.hexdigest())
        if mime is not None:part['mime']=mime
        if encoding is not None:part['encoding']=encoding
        self.parts.append(part);return part
    def descriptor(self,kind,logical_size,fingerprint):return dict(local_kind=kind,local_parts=self.parts,logical_size=logical_size,fingerprint=fingerprint,cache_token=self.token)
    def close(self):
        if self.lock is not None:self.lock.close();self.lock=None
