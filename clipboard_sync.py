"""Silent, account-wide latest clipboard. Reading never consumes the content."""
from __future__ import annotations
import base64,hashlib,json,os,shutil,struct,sys,tempfile,threading,time,zipfile
from pathlib import Path,PurePosixPath
from uuid import uuid4
from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,Signal,QMimeData,QUrl,QBuffer,QIODevice,Qt
from PySide6.QtGui import QImage,QPixmap,QImageReader
from PySide6.QtWidgets import QApplication
from platform_native import native_input_available
import clipboard_audit

MAX_BYTES=100*1024*1024
MAX_ARCHIVE_BYTES=MAX_BYTES+1024*1024 # metadata overhead, not additional user file bytes
MULTIPART_THRESHOLD=24*1024*1024
SYNC_MARKER='application/x-flickdock-cloud-clipboard'
TEXT_MAGIC=b'FDCLIP2\0'


def head_version(head):
    version=head.get('version',0) if isinstance(head,dict) else 0
    return version if type(version) is int and 0<=version<=2**53-1 else 0


def inline_text(value):
    if value is None or value[0]!='text':return None
    data=value[1]
    if len(data['text'])+len(data.get('html',''))>32756:return None
    text=data['text'].encode('utf8');html=data.get('html','').encode('utf8')
    blob=TEXT_MAGIC+struct.pack('!I',len(text))+text+html
    if len(blob)>32768:return None
    digest=hashlib.sha256(blob).hexdigest()
    return dict(kind='text',path=None,size=len(blob),sha256=digest,
        inline=dict(kind='text',body=base64.b64encode(blob).decode('ascii'),size=len(blob),sha256=digest))


def text_value(raw):
    if raw.startswith(TEXT_MAGIC):
        offset=len(TEXT_MAGIC)+4
        if len(raw)<offset or len(raw)-offset>MAX_BYTES:raise ValueError('clipboard limit')
        size=struct.unpack('!I',raw[len(TEXT_MAGIC):offset])[0]
        if size>len(raw)-offset:raise ValueError('invalid text')
        data=dict(text=raw[offset:offset+size].decode('utf8'),html=raw[offset+size:].decode('utf8'))
    else:data=json.loads(raw.decode('utf8'))
    if not isinstance(data,dict) or not isinstance(data.get('text'),str) or not isinstance(data.get('html',''),str):raise ValueError('invalid text')
    if not raw.startswith(TEXT_MAGIC) and len(data['text'].encode('utf8'))+len(data.get('html','').encode('utf8'))>MAX_BYTES:raise ValueError('clipboard limit')
    return ('text',data)


def unpack_inline(head):
    if head.get('kind')!='text' or not 0<head.get('size',0)<=32768:raise ValueError('clipboard limit')
    raw=base64.b64decode(head.get('inline',''),validate=True)
    if len(raw)!=head['size'] or hashlib.sha256(raw).hexdigest()!=head.get('sha256'):raise ValueError('clipboard integrity')
    return text_value(raw)

def native_sequence():
    if not native_input_available():return None
    try:
        if sys.platform=='darwin':
            from AppKit import NSPasteboard
            return int(NSPasteboard.generalPasteboard().changeCount())
        if sys.platform=='win32':
            import ctypes
            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except (ImportError,OSError,RuntimeError,AttributeError):pass
    return None


def snapshot(clipboard,*,baseline_state=None):
    mime=clipboard.mimeData()
    if mime is None:return None
    if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
        if len(mime.urls())!=1 or not all(url.isLocalFile() for url in mime.urls()):return None
        return ('files',[url.toLocalFile() for url in mime.urls()])
    if mime.hasImage():
        for format in ('image/png','image/jpeg','image/webp','image/bmp','image/tiff'):
            raw=bytes(mime.data(format))
            if raw:return ('image',dict(raw=raw,mime=format))
        image=mime.imageData()
        if isinstance(image,QPixmap):image=image.toImage()
        if isinstance(image,QImage) and not image.isNull():return ('image',dict(image=QImage(image)))
    if mime.hasText():
        text=mime.text()
        if text:return ('text',dict(text=text,html=mime.html() if mime.hasHtml() else ''))
    if baseline_state is not None:baseline_state['known_empty']=not mime.formats()
    return None


def signature(value):
    if value is None:return None
    kind,data=value
    if kind=='image':
        if 'raw' in data:return (kind,hashlib.sha256(data['raw']).digest())
        image=data['image'].convertToFormat(QImage.Format.Format_RGBA8888)
        return (kind,image.width(),image.height(),hashlib.sha256(image.constBits()).digest())
    if kind=='files':
        versions=[]
        for path in data:
            canonical=os.path.normcase(os.path.abspath(path))
            try:info=Path(path).stat();versions.append((canonical,info.st_size,info.st_mtime_ns))
            except OSError:versions.append((canonical,None,None))
        return (kind,tuple(versions))
    return (kind,data['text'],data.get('html',''))


def package(value,directory,cancel=None,*,diagnostics=None):
    """Worker-only file/image encoding. ZIP uses no compression or base64."""
    cancel=cancel or threading.Event();path=Path(directory)/'content.bin'
    def too_large():
        if diagnostics is not None:diagnostics['policy_skip']='oversize'
        return None
    if value is None:return None
    kind,data=value;raw_file=None
    if kind=='text':
        text=data['text'].encode('utf8');html=data.get('html','').encode('utf8')
        if len(text)+len(html)>MAX_BYTES:return too_large()
        # No JSON escaping inflation for control characters, line endings or
        # near-limit text. The original UTF-8 and HTML remain lossless.
        blob=TEXT_MAGIC+struct.pack('!I',len(text))+text+html
        path.write_bytes(blob)
    elif kind=='image':
        if 'raw' in data:
            if len(data['raw'])>MAX_BYTES:return too_large()
            path.write_bytes(data['raw'])
        else:
            buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not data['image'].save(buffer,'PNG'):return None
            if buffer.size()>MAX_BYTES:return too_large()
            path.write_bytes(bytes(buffer.data()))
    else:
        entries=[];total=0;roots=set()
        if len(data)!=1 or not Path(data[0]).is_file():return None
        for item in data:
            source=Path(item)
            if not source.exists() or source.is_symlink() or source.is_junction():return None
            if not source.name or source.name.casefold() in roots:return None
            roots.add(source.name.casefold())
            candidates=source.rglob('*') if source.is_dir() else [source]
            if source.is_dir():entries.append((source,source.name+'/'))
            for child in candidates:
                if cancel.is_set() or len(entries)>4096:return None
                if child.is_symlink() or child.is_junction():return None
                name=source.name+'/'+child.relative_to(source).as_posix() if source.is_dir() else source.name
                if child.is_dir():entries.append((child,name+'/'));continue
                if not child.is_file():return None
                total+=child.stat().st_size
                if total>MAX_BYTES:return too_large()
                entries.append((child,name))
        written=0
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as archive:
            for child,name in entries:
                if cancel.is_set():return None
                info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
                if name.endswith('/'):archive.writestr(info,b'');continue
                before=child.stat();copied=0;raw_hash=hashlib.sha256()
                with child.open('rb') as source,archive.open(info,'w') as target:
                    while chunk:=source.read(256*1024):
                        if cancel.is_set():return None
                        written+=len(chunk)
                        copied+=len(chunk)
                        if written>MAX_BYTES:return too_large()
                        target.write(chunk);raw_hash.update(chunk)
                after=child.stat()
                if copied!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return None
                if path.stat().st_size>MAX_ARCHIVE_BYTES:return too_large()
                raw_file=dict(name=child.name,size=copied,sha256=raw_hash.hexdigest(),
                    source=str(child.absolute()),stat=[before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns])
        if raw_file and raw_file['size']:
            with zipfile.ZipFile(path) as archive:info=archive.infolist()[0]
            with path.open('rb') as stream:
                header=stream.read(30);name_length,extra_length=struct.unpack('<HH',header[26:30])
                offset=30+name_length+extra_length;stream.seek(0);prefix=stream.read(offset)
                stream.seek(offset+raw_file['size']);suffix=stream.read()
            raw_file.update(offset=offset,prefix=base64.b64encode(prefix).decode('ascii'),suffix=base64.b64encode(suffix).decode('ascii'))
    size=path.stat().st_size
    if size>(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES):return too_large()
    if size<=0:return None
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(kind=kind,path=str(path),size=size,sha256=digest,**({'raw_file':raw_file} if raw_file and raw_file.get('offset') else {}))


def unpack(kind,path,directory):
    path=Path(path)
    if path.stat().st_size>(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES):raise ValueError('clipboard limit')
    if kind=='text':
        return text_value(path.read_bytes())
    if kind=='image':
        image=QImage(str(path))
        if image.isNull():raise ValueError('invalid image')
        format=bytes(QImageReader.imageFormat(str(path))).decode('ascii')
        return (kind,dict(image=image,raw=path.read_bytes(),mime='image/'+format))
    if kind!='files':raise ValueError('invalid kind')
    from sync_files import safe_name
    root=Path(directory)/'files';root.mkdir(exist_ok=False);roots=[];seen=set();total=0;names={};siblings={}
    with zipfile.ZipFile(path) as archive:
        members=archive.infolist()
        if len(members)>4096:raise ValueError('too many entries')
        for item in members:
            name=item.filename;parts=PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '\\' in name or ':' in name or '..' in parts
                    or any(part in ('','.','..') or part.rstrip(' .')!=part for part in parts)
                    or item.flag_bits&1 or (item.external_attr>>16)&0o170000==0o120000):raise ValueError('unsafe archive')
            folded=name.rstrip('/').casefold()
            if folded in seen:raise ValueError('duplicate name')
            seen.add(folded);total+=item.file_size
            if total>MAX_BYTES:raise ValueError('clipboard limit')
            portable=[]
            for index,part in enumerate(parts):
                prefix=parts[:index+1]
                if prefix not in names:
                    name=safe_name(part);used=siblings.setdefault(tuple(portable),{})
                    while len(name.encode('utf8'))>220:name=name[:-1]
                    if name.casefold() in used and used[name.casefold()]!=prefix:
                        suffix=hashlib.sha256('/'.join(prefix).encode('utf8')).hexdigest()[:8]
                        name=Path(name).stem+'-'+suffix+Path(name).suffix
                    used[name.casefold()]=prefix;names[prefix]=name
                portable.append(names[prefix])
            target=root.joinpath(*portable)
            if not target.resolve().is_relative_to(root.resolve()):raise ValueError('unsafe path')
            if portable[0] not in roots:roots.append(portable[0])
            if item.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(item) as source,target.open('xb') as output:
                count=0
                while chunk:=source.read(256*1024):
                    count+=len(chunk)
                    if count>item.file_size:raise ValueError('invalid size')
                    output.write(chunk)
    return (kind,[str(root/name) for name in roots])


class _Signals(QObject):finished=Signal(object,object)


class _Job(QRunnable):
    def __init__(self,action):
        super().__init__();self.action=action;self.cancelled=threading.Event();self.signals=_Signals()
    def run(self):
        result=None
        try:
            if not self.cancelled.is_set():result=self.action(self.cancelled)
        except (OSError,ValueError,RuntimeError,zipfile.BadZipFile):pass
        self.signals.finished.emit(self,result)


class ClipboardSync(QObject):
    activity_changed=Signal(bool)
    copy_result=Signal(bool,dict)
    copy_started=Signal()
    # Keep original image bytes as a shared Python object, rather than copying
    # a potentially 100 MiB value through QVariantMap for each status update.
    view_changed=Signal(object)
    native_access_changed=Signal()

    @property
    def _pending(self):return getattr(self,'_pending_value',None)

    @_pending.setter
    def _pending(self,value):
        before=self._priority_active();self._pending_value=value
        if before!=self._priority_active():self.activity_changed.emit(self._priority_active())

    def _priority_active(self):
        return bool(self._pending is not None and not getattr(self,'_priority_waiting',False)
                    or getattr(self,'_content_fetching',False))

    def _priority_flags(self,*,waiting=None,receiving=None):
        before=self._priority_active()
        if waiting is not None:self._priority_waiting=waiting
        if receiving is not None:self._content_fetching=receiving
        if before!=self._priority_active():self.activity_changed.emit(self._priority_active())

    def __init__(self,account,events,base,parent=None,*,enabled=True,clipboard=None,independent_transport=False):
        super().__init__(parent);self.account=account;self.api=account.api;self.clipboard=clipboard or QApplication.clipboard()
        self._owned_api=False
        if independent_transport:
            from cloud_api import CloudApi
            if isinstance(account.api,CloudApi):
                # Qt's HighPriority only orders requests waiting in one
                # manager. It cannot preempt six occupied HTTP/1 connections.
                # Keep clipboard control/body traffic in its own bounded
                # manager, preserving the same URL/auth/validation protocol.
                self.api=CloudApi(account.api.origin,self,allow_test_http=account.api._allow_test_http)
                self.api.token=account.api.token
                self.api.diagnostics_dir=account.api.diagnostics_dir
                self._owned_api=True
        self._transport_origin=self.api.origin if self._owned_api else None
        self._is_native=clipboard is None or clipboard is QApplication.clipboard()
        self._sequence=native_sequence if self._is_native else lambda:None
        self.enabled=enabled;self.closed=False;self._epoch=0;self._generation=0;self._applying=False
        self._native_access=self._native_accessible()
        self._clock_anchor=None;self._observed_clock=0;self._local_fence=None;self._local_retry=None
        self._suspended_intent=None;self._baseline_pending=None;self._baseline_guard=None;self._needs_security_baseline=not self._native_access
        self._clock_path=Path(base)/'clipboard-clock.json';self._copy_clock=0
        try:self._copy_clock=int(json.loads(self._clock_path.read_text(encoding='utf8'))['sequence'])
        except (OSError,ValueError,KeyError,TypeError):pass
        self._identity=self._session_identity()
        self._pushed_head=None
        self._prepared_receive=None
        self._accepted_signature=None;self._accepted_head='';self._observed_ready_head=''
        self._reading_notification=False
        self._view_revision=0;self.view=dict(value=None,phase='local',id='',revision=0,permission_required=not self._native_access)
        self._applied_version=0;self._receiving_version=0
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(2)
        self._ignored=None;self._owned_marker=b'';self._ignored_sequence=None;self._last_sequence=object();self._last_local_signature=object();self._seen='';self._keys=set();self._jobs={};self._pending=None;self._fetching=False;self._refresh_again=False
        self.cache=Path(base)/'clipboard-cache';self.cache.mkdir(parents=True,exist_ok=True)
        self._directories=[];self._current_directory=None
        for directory in self.cache.glob('clip-*'):
            if not directory.is_dir() or directory.is_symlink() or directory.is_junction():continue
            try:
                lease=json.loads((directory/'.lease.json').read_text(encoding='utf8'))
                if lease.get('flickdock_clipboard')==1 and isinstance(lease.get('created_at'),(int,float)):
                    self._directories.append((directory,lease['created_at']))
            except (OSError,ValueError,AttributeError):pass
        self.poll=QTimer(self);self.poll.setInterval(30000);self.poll.timeout.connect(self._tick)
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.setInterval(5000);self.retry.timeout.connect(self._retry)
        self.mac_poll=QTimer(self);self.mac_poll.setInterval(200);self.mac_poll.timeout.connect(self._mac_changed);self._mac_count=None
        self.change_debounce=QTimer(self);self.change_debounce.setSingleShot(True);self.change_debounce.setInterval(60)
        self.change_debounce.timeout.connect(self._changed)
        self._subscriptions=[]
        if self._owned_api:
            self._subscribe(account.api.origin_changed,self._primary_origin_changed)
            self._subscribe(self.api.unauthorized,self._transport_unauthorized)
            self._subscribe(self.api.origin_changed,self._transport_origin_changed)
        self._subscribe(self.clipboard.dataChanged,self._schedule_change)
        read_started=time.monotonic();notified_before=getattr(self,'_copy_notified_mono',None)
        notice=getattr(self,'_notification_serial',0)
        sequence=self._sequence();baseline=self._snapshot();sig=signature(baseline)
        stable=self._sequence()==sequence and getattr(self,'_notification_serial',0)==notice
        self.view['value']=baseline;self._last_local_signature=sig;self._last_sequence=sequence
        if stable:
            if sig is not None:self._baseline_guard=(self._identity,sig)
        else:
            notified=getattr(self,'_copy_notified_mono',None)
            observed=notified if notified is not None and notified!=notified_before else read_started
            self._copy_notified_mono=observed
            self._baseline_pending=(self._identity,sig,False,observed)
            self.change_debounce.start()
        # An earlier FlickDock process may still own the pasteboard. Starting
        # or switching account is not permission to publish that cloud payload.
        try:mime=self.clipboard.mimeData()
        except (OSError,RuntimeError):mime=None
        if mime is not None:
            marker=bytes(mime.data(SYNC_MARKER))
            try:
                from uuid import UUID
                UUID(marker.decode('ascii'))
            except (ValueError,UnicodeError):pass
            else:
                if stable and self._sequence()==sequence:self._owned_marker=marker;self._ignored=self._last_local_signature
        self._subscribe(account.account_changed,self._account_changed)
        self._subscribe(events.clipboard_changed,self.refresh)
        payload=getattr(events,'clipboard_payload',None)
        if payload is not None:self._subscribe(payload,self._pushed)
        self._subscribe(events.socket.connected,self.refresh)
        from shared_clipboard_files import ClipboardFileJobs
        self.file_jobs=ClipboardFileJobs(self,base)
        if enabled:
            QTimer.singleShot(0,self._cleanup)
            self.poll.start()
            if sys.platform=='darwin' and self._is_native:self._mac_changed(initial=True);self.mac_poll.start()
            QTimer.singleShot(0,self.refresh)

    def _mac_changed(self,initial=False):
        if not self.enabled or self.closed:return
        if not self.recheck_native_access():return
        try:
            from AppKit import NSPasteboard
            count=NSPasteboard.generalPasteboard().changeCount()
            changed=self._mac_count is not None and count!=self._mac_count;self._mac_count=count
            if changed and not initial:self._schedule_change()
        except (ImportError,RuntimeError):pass

    def _native_accessible(self,*,force=False):
        if not self._is_native or sys.platform!='darwin' or not native_input_available():return True
        from mac_permissions import background_clipboard_allowed
        return background_clipboard_allowed(force=force)

    def _snapshot(self):
        self._snapshot_known_empty=False
        if not self._native_accessible():return None
        state={}
        try:
            value=snapshot(self.clipboard,baseline_state=state)
            self._snapshot_known_empty=state.get('known_empty') is True
            return value
        except (OSError,RuntimeError):return None

    def recheck_native_access(self,*,force=False):
        allowed=self._native_accessible(force=force)
        if allowed!=self._native_access and not self.closed:
            self._native_access=allowed;self._account_changed(force=True);self.native_access_changed.emit()
        return allowed

    def _active(self):return self.enabled and not self.closed and self.account.signed_in and self._native_accessible()

    def set_enabled(self,enabled):
        enabled=bool(enabled)
        if self.closed or self.enabled==enabled:return
        if not enabled:
            if (self._local_fence is not None and self._last_local_signature is not None
                    and self._identity==self._session_identity() and self._native_accessible()):
                self._suspended_intent=dict(session=self._identity,signature=self._last_local_signature,
                    intent=dict(self._local_fence))
            else:self._suspended_intent=None
        self.enabled=enabled;self.change_debounce.stop()
        if not enabled:self.file_jobs.suspend()
        self._account_changed(force=True)
        if enabled:
            self.poll.start()
            if sys.platform=='darwin' and self._is_native:self._mac_changed(initial=True);self.mac_poll.start()
        else:self.poll.stop();self.mac_poll.stop()
        # Shared shelf consumers must leave the cancelled clipboard task and
        # use their independently enabled shelf upload, never remain waiting.
        self.file_jobs.changed.emit()

    def _resume_local_intent(self,value,sig):
        proof=self._suspended_intent;self._suspended_intent=None
        if self._baseline_guard and self._baseline_guard!=(self._identity,sig):self._baseline_guard=None
        if (not proof or proof['session']!=self._identity or sig!=proof['signature']
                or value is None or not self._native_accessible()):return
        # This proves only receive ordering, never cloud upload eligibility.
        # Even an oversized/unfinished image must remain normally pasteable.
        self._local_fence=dict(proof['intent'])

    def _subscribe(self,signal,slot):
        signal.connect(slot);self._subscriptions.append((signal,slot))

    def _own_notification(self,sequence,value=None):
        """Ownership is marker + payload, never native sequence alone.

        OLE/Cocoa can advance their counter when rendering delayed formats of
        our write. Marker-free notifications are checked against current content
        separately; removing a private format alone must not cause an upload.
        """
        if not self._owned_marker or self._ignored is None:return False
        try:
            mime=self.clipboard.mimeData()
            if mime is None or bytes(mime.data(SYNC_MARKER))!=self._owned_marker:return False
            if signature(value if value is not None else self._snapshot())!=self._ignored:return False
        except (OSError,RuntimeError):return False
        self._last_sequence=sequence
        clipboard_audit.observe(self.clipboard,module='clipboard_sync',function='_own_notification',
            trigger='native_change',origin='cloud_receive',task_id=self._owned_marker.decode('ascii'),
            operation='suppressed_echo',content_type=self._ignored[0],fingerprint_data=self._ignored)
        return True

    def _schedule_change(self):
        if self.closed:return
        self._notification_serial=getattr(self,'_notification_serial',0)+1
        if not self._active():
            if not self.change_debounce.isActive():self.change_debounce.start()
            return
        if self._active() and not self._applying:
            sequence=self._sequence()
            if sequence is not None and sequence==self._last_sequence:return
            # Native notification is only a hint, not a logical Copy. Keep
            # this synchronous slot free of delayed OLE/pasteboard rendering.
            # The deferred reader verifies sequence + content before changing
            # task identity, status or cloud state.
            from PySide6.QtGui import QCursor
            point=QCursor.pos();self._copy_point=(point.x(),point.y())
            self._copy_notified_mono=time.monotonic()
            if not self.change_debounce.isActive():self.change_debounce.start()

    def _same_local_content(self,sig):
        """A native counter change is not proof of new clipboard content.

        Reuse an active copy or the same verified current cloud value. A
        different known cloud head deliberately invalidates that proof.
        No time window, input hook or network lookup is needed for an echo.
        """
        if sig is None or sig!=self._last_local_signature:return False
        if self._baseline_guard==(self._identity,sig):return True
        pending=self._pending or self._local_retry
        if pending is not None:
            return self._observed_ready_head in (pending.get('basis_head',''),pending['id'])
        return bool(sig==self._accepted_signature and self._accepted_head and
                    self._observed_ready_head==self._accepted_head)

    def _remember_head(self,identity,sig):
        self._baseline_guard=None;self._baseline_pending=None;self._needs_security_baseline=False
        self._accepted_head=identity;self._observed_ready_head=identity
        self._accepted_signature=sig

    def _copy_timestamp(self,intent):
        if intent['at'] is None and self._clock_anchor is not None:
            mono,server=self._clock_anchor
            intent['at']=max(round(server+(intent['mono']-mono)*1000),intent['floor']+1)
        return intent['at']

    def _calibrate(self,result,started):
        if not isinstance(result,dict):return
        server=result.get('server_time');finished=time.monotonic()
        if type(server) is int and finished-started<10:
            self._clock_anchor=((started+finished)/2,server)
            if self._local_fence:self._copy_timestamp(self._local_fence)
        head=result.get('clipboard')
        if isinstance(head,dict) and type(head.get('copied_at')) is int:
            self._observed_clock=max(self._observed_clock,head['copied_at'])

    def _next_sequence(self):
        self._copy_clock=max(self._copy_clock+1,time.time_ns()//1000)
        self._clock_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self._clock_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'sequence':self._copy_clock}),encoding='utf8')
        os.replace(temporary,self._clock_path)
        return self._copy_clock

    def _tick(self):self.file_jobs.expire();self._cleanup();self.refresh()

    def _valid_task(self,pending):
        return (self._active() and (self._pending is pending or
                getattr(self,'file_jobs',None) is not None and
                self.file_jobs.valid(pending) and self.file_jobs.held(pending)))

    def _clear_task(self,pending):
        if self._pending is pending:self._pending=None

    def _cancel_replaced_work(self):
        retained=[task for task in self.file_jobs.tasks.values() if self.file_jobs.held(task)]
        keys=set().union(*(task.get('_keys',set()) for task in retained)) if retained else set()
        for task in self.file_jobs.tasks.values():
            if task.get('releases'):keys.update(task.get('_release_keys',()))
        for key in tuple(self._keys-keys):self.api.cancel(key)
        self._keys.intersection_update(keys)
        for job in self._jobs:
            if not getattr(job,'_shared_probe',False) and not any(task.get('_job') is job for task in retained):job.cancelled.set()

    def _task_error(self,pending,error):
        self._copy_result(pending,False)
        if getattr(self,'file_jobs',None) is not None:self.file_jobs.fail(pending,error)
        if self._pending is pending and pending.get('generation',self._generation)==self._generation:self._priority_flags(waiting=True);self.retry.start()

    def _task_ready(self,pending,head):
        item=pending['package']
        if (not isinstance(head,dict) or head.get('id')!=pending['id'] or not head.get('ready')
                or head.get('sha256')!=item['sha256'] or head.get('size')!=item['size']):
            self._task_error(pending,ValueError('剪贴板提交校验失败。'));return
        if self._pending is pending:
            self._seen=pending['id'];self._applied_version=max(self._applied_version,head_version(head))
            self._remember_head(pending['id'],pending.get('signature',self._last_local_signature))
            self._copy_result(pending,True);self._pending=None
        raw=item.get('raw_file')
        if raw and pending.get('shared_files'):self.file_jobs.update(pending,'ready',raw['size'])
        elif raw:self.file_jobs.update(pending,'unavailable')
        self._cleanup()
        if self._refresh_again:QTimer.singleShot(0,self.refresh)

    def _request(self,method,path,callback,_task=None,**kwargs):
        if not self._active():return None
        if self._owned_api:
            # AccountService assigns credentials before announcing the new
            # session. Do not send an old task under those new credentials.
            if self._session_identity()!=self._identity:return None
            self.api.token=self.account.api.token
        epoch=self._epoch;identity=self._identity;holder=[];completed=False;started=time.monotonic()
        def done(result,error):
            nonlocal completed
            completed=True
            if holder:self._keys.discard(holder[0])
            if holder and _task is not None:_task.setdefault('_keys',set()).discard(holder[0])
            if holder and _task is not None:_task.setdefault('_release_keys',set()).discard(holder[0])
            if self.enabled and not self.closed and identity==self._identity and (epoch==self._epoch or _task is not None and self._valid_task(_task)):
                self._calibrate(result,started);callback(result,error)
        key=self.api.request(method,path,done,timeout_ms=300000 if path.endswith('/content') else 12000,
                             priority='normal' if _task is not None and _task is not self._pending else 'high',**kwargs)
        holder.append(key)
        if key is not None and not completed:
            self._keys.add(key)
            if _task is not None:_task.setdefault('_keys',set()).add(key)
            if _task is not None and path.endswith('/release'):_task.setdefault('_release_keys',set()).add(key)
        return key

    def _session_identity(self):
        account=getattr(self.account,'account',None)
        return ((account or {}).get('id',''),getattr(self.account,'device_id',''),getattr(self.account.api,'token','')) if self.account.signed_in else None

    def _primary_origin_changed(self,origin):
        if not self.closed and self._owned_api:
            self.api.origin=str(origin);self._transport_origin=self.api.origin

    def _transport_origin_changed(self,origin):
        from cloud_api import CLOUD_ORIGINS
        previous=self._transport_origin;origin=str(origin)
        # Only relay the existing transport's real allowlisted fallback, never
        # turn a loosely emitted signal into a new authenticated origin.
        if (not self._owned_api or not self._active() or self._identity!=self._session_identity()
                or self.api.token!=self.account.api.token or previous not in CLOUD_ORIGINS
                or origin!=CLOUD_ORIGINS[(CLOUD_ORIGINS.index(previous)+1)%len(CLOUD_ORIGINS)]
                or self.api.origin!=origin):return
        primary=self.account.api
        if primary.origin!=previous:
            # A newer primary choice wins over a delayed clipboard fallback.
            self._primary_origin_changed(primary.origin);return
        self._transport_origin=origin
        primary.origin=origin
        primary.origin_changed.emit(origin)

    def _transport_unauthorized(self):
        # CloudApi already checks each reply's auth_token against its token.
        # Also bind that token to the still-active primary session: a cancelled
        # old reply may already have received 401 headers during account change.
        if (self._owned_api and self._active() and self._identity==self._session_identity()
                and self.api.token and self.api.token==self.account.api.token):
            self.account.api.unauthorized.emit()

    def _account_changed(self,*_,force=False):
        if self.closed and not force:return
        identity=self._session_identity()
        accessible=self._native_accessible()
        if identity!=self._identity or not accessible:self._needs_security_baseline=True
        security_boundary=identity!=self._identity or self._needs_security_baseline
        if self._owned_api:
            # Update before cancelling keys; CloudApi cancellation can finish a
            # reply synchronously, including a response with old 401 headers.
            self.api.token=self.account.api.token if self._active() else ''
            self._primary_origin_changed(self.account.api.origin)
        if not force and identity==self._identity:
            self.refresh();return
        files=getattr(self,'file_jobs',None)
        if files is not None:files.save()
        if self.closed or security_boundary or not accessible:
            self._suspended_intent=None;self._baseline_guard=None
        # Native write provenance outlives a login session. Clearing it here
        # would republish the previous account's cloud clipboard on a delayed
        # format notification after login/permission/settings changes.
        self._identity=identity;self._local_retry=None;self._local_fence=None;self._baseline_pending=None
        self._pushed_head=None
        self._prepared_receive=None
        self._accepted_signature=None;self._accepted_head='';self._observed_ready_head=''
        self._applied_version=0;self._receiving_version=0
        self._clock_anchor=None;self._observed_clock=0
        # A session switch is not a Copy action. Establish a native baseline so
        # a delayed notification cannot export the previous account's content.
        # Shutdown is not a session switch: an OLE/pasteboard provider or an
        # offline file stat must never hold the process alive just to baseline.
        baseline=self.view.get('value') if not self.closed else None
        stable=True;baseline_sig=None
        if not self.closed and self.enabled:
            read_started=time.monotonic();notified_before=getattr(self,'_copy_notified_mono',None)
            notice=getattr(self,'_notification_serial',0);self._snapshot_known_empty=False
            sequence=self._sequence();baseline=self._snapshot();baseline_sig=signature(baseline)
            known_empty=self._snapshot_known_empty
            stable=self._sequence()==sequence and getattr(self,'_notification_serial',0)==notice
            if stable:self._last_local_signature=baseline_sig;self._last_sequence=sequence
            else:
                self._last_sequence=sequence
                notified=getattr(self,'_copy_notified_mono',None)
                observed=notified if notified is not None and notified!=notified_before else read_started
                self._copy_notified_mono=observed
        if sys.platform=='darwin' and not self.closed:self._mac_changed(initial=True)
        self._epoch+=1;self._generation+=1;self._pending=None;self._seen='';self._fetching=False;self.retry.stop()
        self._priority_flags(waiting=False,receiving=False)
        for key in tuple(self._keys):self.api.cancel(key)
        self._keys.clear()
        for job in self._jobs:job.cancelled.set()
        if files is not None:
            # The old account's journal was saved before switching identity.
            files.journal=None;files.tasks={};files.reset(load=not self.closed and self.enabled)
        if not self.closed and self.enabled:
            if stable:
                self._resume_local_intent(baseline,baseline_sig)
                if security_boundary and baseline_sig is not None:
                    self._baseline_guard=(identity,baseline_sig);self._needs_security_baseline=False
                elif security_boundary and accessible and known_empty:
                    # A stable, format-free native clipboard contains no old
                    # account payload. Its next actual Copy is a new event,
                    # unlike unsupported or delayed formats that remain private.
                    self._baseline_guard=None;self._needs_security_baseline=False
                elif security_boundary:
                    # Disabled/temporarily unreadable sessions have not yet
                    # established a safe baseline. Carry that fact to the next
                    # native event instead of exporting old delayed formats.
                    self._baseline_pending=(identity,None,True,read_started)
            else:
                # Do not pair an old delayed sample with the new native counter.
                # A same-content format notification is still a login baseline;
                # a genuinely different copy is handled by the normal reader.
                self._baseline_pending=(self._identity,baseline_sig,security_boundary,observed)
                self.change_debounce.start()
        if not self.closed:self._set_view(baseline,'local')
        if self._active():QTimer.singleShot(0,self.refresh)

    def _directory(self):
        path=Path(tempfile.mkdtemp(prefix='clip-',dir=self.cache));created=time.time()
        try:(path/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':created}),encoding='utf8')
        except OSError:
            shutil.rmtree(path,ignore_errors=True);raise
        self._directories.append((path,created));return path

    def _work(self,action,callback,*,priority=1):
        job=_Job(action);self._jobs[job]=callback;job.signals.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        self.pool.start(job,priority)
        return job

    def _finished(self,job,result):
        callback=self._jobs.pop(job,None)
        if callback and self.enabled and not self.closed and not job.cancelled.is_set():callback(result)
        self._cleanup()

    def _changed(self):
        if self.closed or self._applying:return
        if not self._active():
            self._set_view(self._snapshot(),'local');return
        if self._reading_notification:
            self.change_debounce.start();return
        sequence=self._sequence()
        # Repeated native notifications for one copy must not ask another app
        # to render the same delayed clipboard again or repeat file/image work.
        if sequence is not None and sequence==self._last_sequence:return
        notice=getattr(self,'_notification_serial',0)
        self._reading_notification=True
        try:value=self._snapshot();sig=signature(value)
        finally:self._reading_notification=False
        if self._sequence()!=sequence or getattr(self,'_notification_serial',0)!=notice:
            # A delayed provider can pump events and change the native value
            # during a read. Do not stamp an old sample with a newer counter.
            self.change_debounce.start();return
        if value is None and sequence is not None and sequence!=self._last_sequence:
            # Some native owners announce a new sequence before delayed data
            # is readable. Retry briefly without consuming its copy event.
            previous,attempts=getattr(self,'_native_read_retry',(None,0))
            attempts=attempts+1 if previous==sequence else 1
            self._native_read_retry=(sequence,attempts)
            if attempts<=5:self.change_debounce.start(100);return
        else:self._native_read_retry=(None,0)
        baseline=self._baseline_pending
        if baseline is not None and baseline[0]==self._identity:
            self._baseline_pending=None
            if sig==baseline[1] or baseline[1] is None and baseline[2]:
                if baseline[2] and sig is None:self._baseline_pending=baseline
                self._last_sequence=sequence;self._last_local_signature=sig
                self._resume_local_intent(value,sig);self._set_view(value,'local')
                if sig is not None:self._baseline_guard=(self._identity,sig)
                if baseline[2] and sig is not None:
                    self._needs_security_baseline=False
                    if baseline[1] is None:
                        # Unknown account-baseline data is never exported. A
                        # real native change during that read still protects
                        # the current payload from an older peer; newer peers
                        # remain ordered by the observed notification time.
                        self._local_fence=dict(mono=max(baseline[3],getattr(self,'_copy_notified_mono',baseline[3])),
                            at=None,floor=self._observed_clock)
                        self._copy_timestamp(self._local_fence)
                if self._prepared_receive:QTimer.singleShot(0,self.refresh)
                return
            self._suspended_intent=None
        if self._own_notification(sequence,value):
            if self._prepared_receive:QTimer.singleShot(0,self.refresh)
            return
        if self._same_local_content(sig):
            if self._baseline_guard==(self._identity,sig) and self._local_fence is None:
                # A security-baseline reassertion cannot safely be exported,
                # but must not let an older in-flight receive erase its MIME.
                # Keep the first observed time; format noise never advances it.
                self._local_fence=dict(mono=getattr(self,'_copy_notified_mono',time.monotonic()),
                    at=None,floor=self._observed_clock)
                self._copy_timestamp(self._local_fence)
            self._last_sequence=self._sequence()
            if self._prepared_receive:QTimer.singleShot(0,self.refresh)
            return
        if sequence is not None:
            if sequence==self._last_sequence:return
            self._last_sequence=sequence
        elif sig==self._last_local_signature:return
        clipboard_audit.observe(self.clipboard,module='clipboard_sync',function='_changed',
            trigger='QClipboard.dataChanged',origin='external_change',operation='observed_change',
            content_type=value[0] if value else 'empty',fingerprint_data=sig or ('empty',))
        self._current_directory=None;self.retry.stop()
        self._last_local_signature=sig
        self._suspended_intent=None;self._baseline_guard=None;self._needs_security_baseline=False
        self._local_fence=dict(mono=getattr(self,'_copy_notified_mono',time.monotonic()),at=None,floor=self._observed_clock)
        self._copy_timestamp(self._local_fence)
        self._ignored=None;self._owned_marker=b'';self._generation+=1;self._epoch+=1;generation=self._generation
        self.copy_started.emit()
        self.file_jobs.supersede()
        # Superseding a local upload prevents its late completion restoring it.
        self._cancel_replaced_work();self._fetching=False
        self._pending=None;self._priority_flags(waiting=False,receiving=False)
        self._local_retry=None
        self._prepared_receive=None
        if value is None:self._set_view(None,'local');self._pending=None;return
        self._local_retry=dict(id=str(uuid4()),sequence=None,generation=generation,staged=False,package=None,
                               prepared=False,created_at=time.time(),intent=self._local_fence,value=value,
                               feedback=self._copy_context(value),signature=sig,basis_head=self._observed_ready_head)
        clipboard_audit.observe(self.clipboard,module='clipboard_sync',function='_changed',trigger='external_change',
            origin='external_change',task_id=self._local_retry['id'],operation='upload_created',
            content_type=value[0],fingerprint_data=sig)
        self.file_jobs.register(self._local_retry,value)
        self._set_view(value,'uploading',self._local_retry['id'])
        self._prepare_local()

    def _set_view(self,value,phase,identity=''):
        if value is not self.view.get('value'):self._view_revision+=1
        self.view=dict(value=value,phase=phase,id=identity,revision=self._view_revision,permission_required=not self._native_accessible())
        self.view_changed.emit(dict(self.view))

    def _copy_context(self,value):
        from PySide6.QtGui import QCursor,QGuiApplication
        from PySide6.QtCore import QPoint
        point=getattr(self,'_copy_point',None) or QCursor.pos().toTuple()
        screen=QGuiApplication.screenAt(QPoint(*point)) or QGuiApplication.primaryScreen()
        fullscreen=False
        if screen is not None:
            if value[0]=='image':
                data=value[1];image=data.get('image')
                if image is not None:size=image.size()
                else:
                    buffer=QBuffer();buffer.setData(data.get('raw',b''));buffer.open(QIODevice.ReadOnly)
                    size=QImageReader(buffer).size()
                rect=screen.geometry();ratio=screen.devicePixelRatio()
                fullscreen=any(abs(size.width()-rect.width()*scale)<=2 and abs(size.height()-rect.height()*scale)<=2 for scale in (1,ratio))
            if not fullscreen and self._is_native:
                from platform_native import foreground_fullscreen
                fullscreen=foreground_fullscreen(screen.geometry())
        return dict(id='',point=point,fullscreen=fullscreen,value=value)

    def _copy_result(self,pending,ok):
        # A retained old file can finish for the shelf while a newer clipboard
        # is active. Its completion must not emit stale clipboard feedback.
        if (not self._active() or self.view.get('id')!=pending['id']
                or pending.get('generation',self._generation)!=self._generation):return
        if self.view.get('id')==pending['id']:
            self._set_view(self.view['value'],'synced' if ok else 'error',pending['id'])
        if pending.get('feedback_result') is ok:return
        pending['feedback_result']=ok
        audit_sig=self._last_local_signature if isinstance(self._last_local_signature,tuple) else ('unknown',)
        clipboard_audit.observe(self.clipboard,module='clipboard_sync',function='_copy_result',
            trigger='upload_callback',origin='external_change',task_id=pending['id'],operation='upload_result',
            result='ok' if ok else 'failed',content_type=audit_sig[0],fingerprint_data=audit_sig)
        context=dict(pending.get('feedback',{}),id=pending['id'])
        self.copy_result.emit(ok,context)

    def _prepare_local(self):
        pending=self._local_retry
        if not pending or not self._active():return
        try:
            if pending['sequence'] is None:pending['sequence']=self._next_sequence()
        except OSError:
            self._copy_result(pending,False);self._pending=None;self.retry.start();return
        self._local_retry=None;self._pending=pending
        diagnostics={}
        def prepared(result):
            if not self._valid_task(pending):return
            if result is None:
                self.file_jobs.update(pending,'unavailable')
                if diagnostics.get('policy_skip')=='oversize':
                    self._baseline_guard=(self._identity,self._last_local_signature)
                    self._set_view(self.view.get('value'),'local',pending['id'])
                else:self._copy_result(pending,False)
                self._clear_task(pending)
                if self._refresh_again:QTimer.singleShot(0,self.refresh)
                return
            pending['package']=result;pending['prepared']=True;self.file_jobs.prepared(pending);self._stage(pending)
        value=pending.pop('value');quick=inline_text(value)
        if quick is not None:prepared(quick);return
        try:directory=self._directory()
        except OSError:
            self._copy_result(pending,False)
            pending['value']=value;self._local_retry=pending;self._pending=None;self.retry.start();return
        pending['_job']=self._work(lambda cancel:package(value,directory,cancel,diagnostics=diagnostics),prepared,priority=2)

    def _stage(self,pending):
        if not self._valid_task(pending):return
        if time.time()-pending['created_at']>=86400:self._clear_task(pending);self.refresh();return
        if self._copy_timestamp(pending['intent']) is None:
            def calibrated(result,error):
                if not self._valid_task(pending):return
                if error or self._clock_anchor is None:self._task_error(pending,error or ValueError('云端时钟校准失败。'))
                else:self._stage(pending)
            self._request('GET','/v1/clipboard',calibrated,_task=pending);return
        def staged(result,error):
            if not self._valid_task(pending):return
            if error:
                if use_inline and getattr(error,'status',0) in (400,413):
                    pending['inline_fallback']=True;self._stage(pending);return
                self._task_error(pending,error)
                if getattr(error,'status',0)==409 and not self.file_jobs.held(pending):self._clear_task(pending);self.refresh()
                return
            ready=result.get('clipboard',{}) if isinstance(result,dict) else {}
            pending['shared_files']=bool(isinstance(result,dict) and result.get('shared_files'))
            pending['multipart_v1']=result.get('multipart_v1') if isinstance(result,dict) else None
            item=pending['package']
            if (ready.get('id')==pending['id'] and ready.get('ready') and
                    ready.get('sha256')==item['sha256'] and ready.get('size')==item['size']):
                self._task_ready(pending,ready)
                return
            pending['staged']=True;self._upload(pending)
        data={'id':pending['id'],'protocol':2,'sequence':pending['sequence'],'copied_at':pending['intent']['at']}
        use_inline=bool(pending['package'].get('inline') and not pending.get('inline_fallback'))
        if use_inline:data['inline']=pending['package']['inline']
        self.file_jobs.save()
        self._request('POST','/v1/clipboard',staged,data=data,_task=pending)

    def _upload(self,pending):
        if not self._valid_task(pending) or not pending['staged'] or not pending['prepared'] or pending.get('uploading'):return
        if time.time()-pending['created_at']>=86400:self._pending=None;self.refresh();return
        item=pending['package']
        if item is None:self._pending=None;return
        if not item.get('path'):
            if pending.get('materializing'):return
            pending['materializing']=True
            try:directory=self._directory()
            except OSError:
                pending['materializing']=False;self._priority_flags(waiting=True);self.retry.start();return
            def materialize(cancel):
                if cancel.is_set():return None
                path=directory/'content.bin';path.write_bytes(base64.b64decode(item['inline']['body'],validate=True))
                return str(path)
            def materialized(path):
                if not self._valid_task(pending):return
                pending['materializing']=False
                if path:item['path']=path;self._upload(pending)
                else:self._priority_flags(waiting=True);self.retry.start()
            self._work(materialize,materialized,priority=2);return
        pending['uploading']=True
        self.file_jobs.update(pending,'uploading')
        def uploaded(result,error):
            if not self._valid_task(pending):return
            pending['uploading']=False
            if error:
                self._task_error(pending,error)
                if getattr(error,'status',0) in (403,409) and not self.file_jobs.held(pending):self._clear_task(pending);self.refresh()
                return
            head=result.get('clipboard',{}) if isinstance(result,dict) else {}
            self._task_ready(pending,head)
        headers={'X-Clipboard-Kind':item['kind'],'X-Content-SHA256':item['sha256']};options={}
        raw_file=item.get('raw_file') if pending.get('shared_files') else None
        if item['size']>MULTIPART_THRESHOLD:
            self._upload_multipart(pending,raw_file,uploaded);return
        if raw_file:
            descriptor={key:raw_file[key] for key in ('name','size','sha256','prefix','suffix')}
            headers['X-Clipboard-Raw-File']=json.dumps(descriptor,separators=(',',':'),ensure_ascii=True)
            options.update(source_offset=raw_file['offset'],source_size=raw_file['size'])
        def progress(sent,total):
            if not self._valid_task(pending):return
            count=min(raw_file['size'],max(0,sent)) if raw_file else sent
            self.file_jobs.update(pending,'verifying' if sent>=total else 'uploading',count)
        self._request('PUT',f'/v1/clipboard/{pending["id"]}/content',uploaded,source=item['path'],headers=headers,
                      progress=progress,_task=pending,**options)

    def _upload_multipart(self,pending,raw_file,uploaded):
        """Bounded authenticated parts; only the verified commit is success."""
        from cloud_api import CloudError
        capability=pending.get('multipart_v1');item=pending['package']
        if (not isinstance(capability,dict) or capability.get('part_size') not in (1024*1024,4*1024*1024)
                or capability.get('threshold')!=MULTIPART_THRESHOLD
                or type(capability.get('max_bytes')) is not int or capability['max_bytes']<MAX_BYTES
                or type(capability.get('max_archive_bytes')) is not int or capability['max_archive_bytes']<MAX_ARCHIVE_BYTES):
            self._stop_upload(pending,CloudError('云端尚未支持大容量剪贴板，请稍后重新复制。',code='CLIPBOARD_MULTIPART_UNAVAILABLE'));return
        token=object();pending['_multipart_token']=token
        body_size=raw_file['size'] if raw_file else item['size']
        base_offset=raw_file['offset'] if raw_file else 0
        path=f'/v1/clipboard/{pending["id"]}/upload'
        data=dict(kind=item['kind'],size=body_size,sha256=item['sha256'])
        if raw_file:data['raw_file']={name:raw_file[name] for name in ('name','size','sha256','prefix','suffix')}
        def active():return self._valid_task(pending) and pending.get('_multipart_token') is token and pending.get('uploading')
        def fail(error):
            if active():
                if getattr(error,'status',0) in (400,413,426) or getattr(error,'code','') in ('CLIPBOARD_INTEGRITY','CLIPBOARD_LIMIT','CLIPBOARD_PART_CONFLICT','CLIPBOARD_MULTIPART_INVALID','CLIPBOARD_SOURCE_CHANGED'):
                    self._stop_upload(pending,error);return
                pending['_multipart_token']=None;uploaded(None,error)
        def initialized(result,error):
            if not active():return
            if error:fail(error);return
            try:
                upload_id=result['upload_id'];part_size=result['part_size'];count=result['part_count'];index=result['next_part']
                valid_id=isinstance(upload_id,str) and str(__import__('uuid').UUID(upload_id))==upload_id
                if (not valid_id or result.get('multipart_v1') is not True or part_size!=capability['part_size']
                        or type(count) is not int or count!=(body_size+part_size-1)//part_size
                        or type(index) is not int or not 0<=index<=count):raise ValueError('invalid multipart status')
            except (KeyError,TypeError,ValueError,AttributeError):
                fail(CloudError('剪贴板分片状态校验失败。',code='CLIPBOARD_MULTIPART_INVALID'));return
            endpoint=path+'/'+upload_id
            def next_part(index):
                if not active():return
                sent=min(body_size,index*part_size)
                self.file_jobs.update(pending,'verifying' if index==count else 'uploading',sent)
                if index==count:
                    def committed(result,error):
                        if not active():return
                        if error:fail(error);return
                        pending['_multipart_token']=None;uploaded(result,None)
                    self._request('POST',endpoint+'/commit',committed,data={},_task=pending);return
                length=min(part_size,body_size-sent);offset=base_offset+sent
                def hash_part(cancel):
                    digest=hashlib.sha256();remaining=length
                    try:
                        with Path(item['path']).open('rb') as stream:
                            if os.fstat(stream.fileno()).st_size!=item['size']:return {'error':'changed'}
                            stream.seek(offset)
                            while remaining:
                                if cancel.is_set():return None
                                chunk=stream.read(min(256*1024,remaining))
                                if not chunk:return {'error':'changed'}
                                digest.update(chunk);remaining-=len(chunk)
                    except FileNotFoundError:return {'error':'changed'}
                    except OSError:return {'error':'io'}
                    return {'digest':digest.hexdigest()}
                def hashed(result):
                    if not active():return
                    if isinstance(result,dict) and result.get('error')=='changed':
                        fail(CloudError('剪贴板上传快照已改变，请重新复制。',code='CLIPBOARD_SOURCE_CHANGED'));return
                    if not isinstance(result,dict) or not result.get('digest'):
                        fail(CloudError('剪贴板缓存暂时无法读取，稍后会重试。',code='CLIPBOARD_CACHE_IO'));return
                    digest=result['digest']
                    def progressed(value,total):
                        if active():self.file_jobs.update(pending,'uploading',sent+min(length,max(0,value)))
                    def accepted(result,error):
                        if not active():return
                        if error:fail(error);return
                        if not isinstance(result,dict) or type(result.get('next_part')) is not int or result['next_part']!=index+1:
                            fail(CloudError('剪贴板分片回执校验失败。',code='CLIPBOARD_MULTIPART_INVALID'));return
                        next_part(index+1)
                    self._request('PUT',endpoint+'/parts/'+str(index),accepted,source=item['path'],
                        source_offset=offset,source_size=length,headers={'X-Content-SHA256':digest},
                        progress=progressed,_task=pending)
                pending['_job']=self._work(hash_part,hashed,priority=2 if self._pending is pending else 1)
            next_part(index)
        self._request('POST',path,initialized,data=data,_task=pending)

    def _stop_upload(self,pending,error):
        """A permanent protocol limit is not a retryable network interruption."""
        if not self._valid_task(pending):return
        pending['uploading']=False;pending['_multipart_token']=None
        pending['_clipboard_terminal']=str(getattr(error,'code','') or 'UPLOAD_UNAVAILABLE')[:64]
        if self._pending is pending and pending.get('signature')==self._last_local_signature:
            self._baseline_guard=(self._identity,self._last_local_signature)
        self.file_jobs.update(pending,'unavailable',error=error)
        self._copy_result(pending,False)
        if self._pending is pending:self.retry.stop()
        self._clear_task(pending)
        if self._refresh_again:QTimer.singleShot(0,self.refresh)

    def _retry(self):
        if not self._active():return
        if self._local_retry:self._prepare_local();return
        if self._pending:
            self._priority_flags(waiting=False)
            if self.view.get('id')==self._pending['id']:self._set_view(self.view['value'],'uploading',self._pending['id'])
            if self._pending['staged']:self._upload(self._pending)
            else:self._stage(self._pending)
        else:self.refresh()

    def _pushed(self,value):
        if not self._active():return
        head=value.get('clipboard') if isinstance(value,dict) else None
        if not isinstance(head,dict) or type(head.get('version')) is not int:return
        if head['version']<=self._applied_version or head.get('id')==self._seen:return
        identity=head.get('id','')
        if (isinstance(identity,str) and len(identity)==36
                and all(c in '0123456789abcdef-' for c in identity)):
            self._observed_ready_head=identity
        previous=(self._pushed_head or {}).get('clipboard',{})
        if head['version']>previous.get('version',0):self._pushed_head=value
        if self._fetching and not self._pending and head['version']>self._receiving_version:
            # A new committed small copy supersedes a stale body download;
            # retain the last usable native clipboard until this one verifies.
            self._epoch+=1;self._generation+=1
            self._cancel_replaced_work()
            self._fetching=False;self._priority_flags(receiving=False)
        self.refresh()

    def refresh(self):
        if not self._active():return
        if self._pending:
            self._refresh_again=True;return
        if self._fetching:self._refresh_again=True;return
        self._refresh_again=False
        self._fetching=True;generation=self._generation
        def latest(result,error):
            if error:self._finish_refresh();self.retry.start();return
            head=result.get('clipboard') if isinstance(result,dict) else None
            pushed=self._pushed_head
            version=head.get('version',0) if isinstance(head,dict) else 0
            if type(version) is not int:version=0
            if pushed and pushed.get('clipboard',{}).get('version',0)>version:
                self._pushed_head=None;head=pushed['clipboard']
            if not isinstance(head,dict) or generation!=self._generation:self._finish_refresh();return
            identity=head.get('id','')
            if not isinstance(identity,str) or len(identity)!=36 or any(c not in '0123456789abcdef-' for c in identity):self._finish_refresh();return
            # Even a pending newer copy changes what "same as current cloud"
            # means. Otherwise a deliberate local recopy of A while B is being
            # uploaded gets swallowed and the older B later overwrites it.
            if head_version(head)>=self._applied_version:self._observed_ready_head=identity
            if identity==self._seen:self._finish_refresh();return
            if type(head.get('version')) is int and head['version']<=self._applied_version:self._finish_refresh();return
            if self._local_fence is not None:
                fence=self._copy_timestamp(self._local_fence)
                if fence is None or head.get('copied_at',head.get('at',0))<=fence:
                    cached=self._prepared_receive
                    if cached and cached[0][0]==identity:self._prepared_receive=None
                    self._finish_refresh();return
            if not head.get('ready'):
                # Pending/failed/oversize copies never erase a usable native
                # clipboard. Expiry is owned by the cache lease, not polling.
                if head.get('expired') and type(head.get('version')) is int:
                    self._applied_version=max(self._applied_version,head['version'])
                self._finish_refresh();return
            kind,size,sha=head.get('kind'),head.get('size'),head.get('sha256')
            if kind not in ('text','image','files') or type(size) is not int or not 0<size<=(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES) or not isinstance(sha,str) or len(sha)!=64:self._finish_refresh();return
            self._observed_ready_head=identity
            is_inline=kind=='text' and isinstance(head.get('inline'),str) and size<=32768
            cached=self._prepared_receive
            if cached and cached[0]!=(identity,kind,size,sha):self._prepared_receive=None;cached=None
            try:directory=cached[2] if cached else self.cache if is_inline else self._directory()
            except OSError:
                self._finish_refresh();self.retry.start();return
            path=directory/'download.bin';self._fetching=True
            self._receiving_version=head_version(head)
            self._priority_flags(receiving=True)
            def prepared(value):
                if generation!=self._generation:self._finish_refresh();return
                if value is None:self._finish_refresh();self.retry.start();return
                self._prepared_receive=((identity,kind,size,sha),value,directory)
                def confirmed(result,error):
                    current=result.get('clipboard') if isinstance(result,dict) else None
                    if error:
                        self._finish_refresh();self.retry.start();return
                    if generation!=self._generation:self._finish_refresh();return
                    if isinstance(current,dict) and current.get('id')==identity and current.get('ready'):
                        if self._local_fence is not None:
                            fence=self._copy_timestamp(self._local_fence)
                            if fence is None or current.get('copied_at',current.get('at',0))<=fence:
                                cached=self._prepared_receive
                                if cached and cached[0][0]==identity:self._prepared_receive=None
                                self._finish_refresh();return
                        sequence=self._sequence()
                        native_value=self._snapshot();native_sig=signature(native_value)
                        if (sequence!=self._last_sequence or self._sequence()!=sequence
                                or native_sig!=self._last_local_signature):
                            self._schedule_change()
                        else:
                            equal=native_sig==signature(value)
                            if equal or self._apply(value,directory,identity=identity):
                                self._seen=identity
                                self._applied_version=max(self._applied_version,head_version(head))
                                self._remember_head(identity,self._last_local_signature)
                                self._set_view(native_value if equal else value,'synced',identity)
                                self._prepared_receive=None
                            else:self._set_view(value,'error',identity);self.retry.start(5000)
                    else:self._refresh_again=True
                    self._finish_refresh()
                self._request('GET','/v1/clipboard',confirmed)
            def downloaded(result,error):
                if error:self._finish_refresh();self.retry.start();return
                if generation!=self._generation:self._finish_refresh();return
                self._work(lambda _:unpack(kind,path,directory),prepared,priority=2)
            if cached:prepared(cached[1])
            elif is_inline:
                try:value=unpack_inline(head)
                except (ValueError,TypeError,UnicodeError):
                    self._finish_refresh();self.retry.start();return
                prepared(value)
            else:self._request('GET',f'/v1/clipboard/{identity}/content',downloaded,destination=path,expected={'size':size,'sha256':sha})
        if self._pushed_head:
            pushed,self._pushed_head=self._pushed_head,None;latest(pushed,None)
        else:self._request('GET','/v1/clipboard',latest)

    def _finish_refresh(self):
        self._fetching=False
        self._priority_flags(receiving=False)
        if self._refresh_again:
            self._refresh_again=False;QTimer.singleShot(0,self.refresh)

    def _apply(self,value,directory,*,identity=None):
        if not self._native_accessible(force=True):return False
        kind,data=value;mime=QMimeData()
        mime.setData(SYNC_MARKER,(identity or self._seen).encode('ascii'))
        if kind=='files':mime.setUrls([QUrl.fromLocalFile(path) for path in data])
        elif kind=='image':
            mime.setImageData(data['image'])
            if data.get('raw') and data.get('mime'):mime.setData(data['mime'],data['raw'])
        else:
            mime.setText(data['text'])
            if data.get('html'):mime.setHtml(data['html'])
        self._applying=True
        try:
            written_marker=(identity or self._seen).encode('ascii')
            expected=signature(value)
            clipboard_audit.set_mime(self.clipboard,mime,module='clipboard_sync',function='_apply',
                trigger='cloud_content_verified',origin='cloud_receive',task_id=identity or self._seen,
                content_type=kind,fingerprint_data=expected)
            actual=signature(self._snapshot())
            native=self.clipboard.mimeData()
            owns_new=native is not None and bytes(native.data(SYNC_MARKER))==written_marker
            # A rejected native write must not erase the provenance of the
            # previous still-owned clipboard. Adopt only the observed marker.
            if owns_new:self._owned_marker=written_marker
            if kind=='image' and data.get('raw') and data.get('mime'):
                matches=native is not None and bytes(native.data(data['mime']))==data['raw']
            else:matches=actual==expected
            if not matches:
                if owns_new:
                    self._ignored=actual;self._last_local_signature=actual;self._ignored_sequence=self._sequence();self._last_sequence=self._ignored_sequence
                return False
            self._local_fence=None;self._ignored=actual
            self._ignored_sequence=self._sequence();self._last_sequence=self._ignored_sequence
            self._last_local_signature=self._ignored
            self._current_directory=directory
            if sys.platform=='darwin':self._mac_changed(initial=True)
            return True
        except (OSError,RuntimeError):return False
        finally:self._applying=False

    def _clear_remote_clipboard(self):
        previous=self._applying;self._applying=True
        try:
            clipboard_audit.clear(self.clipboard,module='clipboard_sync',function='_clear_remote_clipboard',
                trigger='clear_remote',origin='internal')
            self._ignored=None;self._owned_marker=b'';self._current_directory=None
            self._last_local_signature=signature(self._snapshot())
            self._ignored_sequence=self._sequence();self._last_sequence=self._ignored_sequence
            if sys.platform=='darwin':self._mac_changed(initial=True)
        finally:self._applying=previous

    def _cleanup(self):
        # Native applications may still hold the last file clipboard's paths
        # after Paste. Retain local downloads for a day, never delete originals.
        if self.closed:return
        # An unreadable pasteboard is not an empty one. Its native file URLs
        # must stay usable across a denied permission, including after restart.
        if not self._native_accessible(force=True):return
        now=time.time();keep=[]
        protected=self.file_jobs.protected_directories()
        # Most completions have nothing to expire. Never ask another app to
        # render text/images merely because an HTTP or disk job completed.
        if not any(now-created>=86400 and directory not in protected and
                   directory!=self._current_directory for directory,created in self._directories):return
        try:
            mime=self.clipboard.mimeData()
            if mime is None:return
            native_paths=[Path(url.toLocalFile()).resolve() for url in mime.urls() if url.isLocalFile()] if mime.hasUrls() else []
        except (OSError,RuntimeError):return
        for directory,created in self._directories:
            if directory in protected or directory==self._current_directory:keep.append((directory,created));continue
            if now-created<86400:keep.append((directory,created));continue
            if any(path.is_relative_to(directory.resolve()) for path in native_paths):
                keep.append((directory,created));continue
            if directory.resolve().is_relative_to(self.cache.resolve()) and not directory.is_symlink() and not directory.is_junction():
                self._work(lambda _,path=directory:shutil.rmtree(path,ignore_errors=True),lambda _:None)
        self._directories=keep

    def close(self):
        if self.closed:return
        self.closed=True;self.poll.stop();self.retry.stop();self.mac_poll.stop();self.change_debounce.stop();self._account_changed(force=True)
        for signal,slot in self._subscriptions:
            try:signal.disconnect(slot)
            except RuntimeError:pass
        self._subscriptions.clear()
        if self._owned_api:self.api.close()
