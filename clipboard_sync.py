"""Silent, account-wide latest clipboard. Reading never consumes the content."""
from __future__ import annotations
import base64,hashlib,json,os,shutil,struct,sys,tempfile,threading,time,zipfile
from pathlib import Path,PurePosixPath
from uuid import UUID,uuid4
from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,Signal,QMimeData,QUrl,QBuffer,QIODevice,Qt
from PySide6.QtGui import QImage,QPixmap,QImageReader
from PySide6.QtWidgets import QApplication
from platform_native import native_input_available,copy_activity
from clipboard_identity import signature,content_keys
from sync_scheduler import SyncScheduler
from virtual_clipboard import describe as describe_virtual,capture as capture_virtual,VirtualClipboardError

MAX_BYTES=20*1024*1024
MAX_ARCHIVE_BYTES=MAX_BYTES+1024*1024 # metadata overhead, not additional user file bytes
SYNC_MARKER='application/x-flickdock-cloud-clipboard'
TEXT_MAGIC=b'FDCLIP2\0'
NATIVE_READ_RETRY_INITIAL_MS=100
NATIVE_READ_RETRY_MAX_MS=1000
CLIPBOARD_PRIORITY_LEASE_MS=350
TEXT_COPY_COALESCE_MS=8
BINARY_COPY_COALESCE_MS=60
MAC_CLIPBOARD_OBSERVE_MS=50
COPY_FORMAT_SETTLE_SECONDS=1.
REPEAT_COPY_INPUT_MAX_AGE_MS=3000
COPY_REPLAY_HISTORY_ENTRIES=64
COPY_CONFIRMATION_TIMEOUT_MS=12000


class ClipboardSizeLimit(ValueError):
    """The local content exceeds policy, rather than a transport failing."""


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
        if len(raw)<offset or len(raw)-offset>=MAX_BYTES:raise ValueError('clipboard limit')
        size=struct.unpack('!I',raw[len(TEXT_MAGIC):offset])[0]
        if size>len(raw)-offset:raise ValueError('invalid text')
        data=dict(text=raw[offset:offset+size].decode('utf8'),html=raw[offset+size:].decode('utf8'))
    else:data=json.loads(raw.decode('utf8'))
    if not isinstance(data,dict) or not isinstance(data.get('text'),str) or not isinstance(data.get('html',''),str):raise ValueError('invalid text')
    return ('text',data)


def unpack_inline(head):
    if head.get('kind')!='text' or not 0<head.get('size',0)<=32768:raise ValueError('clipboard limit')
    raw=base64.b64decode(head.get('inline',''),validate=True)
    if len(raw)!=head['size'] or hashlib.sha256(raw).hexdigest()!=head.get('sha256'):raise ValueError('clipboard integrity')
    return text_value(raw)


def same_cloud_content(item,head):
    if head.get('kind')!=item['kind']:return False
    if head.get('size')==item['size'] and head.get('sha256')==item['sha256']:return True
    inline=item.get('inline')
    if item['kind']!='text' or not isinstance(inline,dict) or not isinstance(head.get('inline'),str):return False
    # Clipboard bridges can strip HTML or normalize CR/LF without creating a
    # new text copy. Verify both encoded bodies before comparing their text;
    # the native original keeps its richer formats and is never rewritten.
    local=unpack_inline(dict(item,inline=inline['body']))
    remote=unpack_inline(head)
    return content_keys(local)==content_keys(remote)

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


def snapshot(clipboard):
    return _snapshot_mime(clipboard.mimeData())


def _snapshot_mime(mime):
    if mime is None:return None
    if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
        from virtual_clipboard import remote_placeholder
        urls=[url for url in mime.urls() if not (url.isLocalFile() and remote_placeholder(url.toLocalFile()))]
        if not urls:raise VirtualClipboardError('REMOTE_FILE_PLACEHOLDER')
        if len(urls)!=1 or not all(url.isLocalFile() for url in urls):return None
        return ('files',[url.toLocalFile() for url in urls])
    if mime.hasImage():
        for format in ('image/png','image/jpeg','image/webp','image/bmp','image/tiff'):
            raw=bytes(mime.data(format))
            if raw:return ('image',dict(raw=raw,mime=format))
        image=mime.imageData()
        if isinstance(image,QPixmap):image=image.toImage()
        if isinstance(image,QImage) and not image.isNull():return ('image',dict(image=QImage(image)))
    if mime.hasText() and mime.text():return ('text',dict(text=mime.text(),html=mime.html() if mime.hasHtml() else ''))
    return None


def native_format_count():
    if sys.platform=='win32':
        import ctypes
        user32=ctypes.WinDLL('user32',use_last_error=True);ctypes.set_last_error(0)
        count=user32.CountClipboardFormats();error=ctypes.get_last_error()
        if not count and error:raise OSError(error,'Cannot inspect native clipboard formats')
        return count
    if sys.platform=='darwin':
        from AppKit import NSPasteboard
        return len(NSPasteboard.generalPasteboard().types() or [])
    return None


def package(value,directory,cancel=None):
    """Worker-only file/image encoding. ZIP uses no compression or base64."""
    cancel=cancel or threading.Event();path=Path(directory)/'content.bin'
    if value is None:return None
    kind,data=value;raw_file=None
    if kind=='text':
        text=data['text'].encode('utf8');html=data.get('html','').encode('utf8')
        if len(text)+len(html)>=MAX_BYTES:raise ClipboardSizeLimit('剪贴板内容超过 20 MB 同步上限。')
        # No JSON escaping inflation for control characters, line endings or
        # near-limit text. The original UTF-8 and HTML remain lossless.
        blob=TEXT_MAGIC+struct.pack('!I',len(text))+text+html
        path.write_bytes(blob)
    elif kind=='image':
        if 'raw' in data:
            if len(data['raw'])>=MAX_BYTES:raise ClipboardSizeLimit('图片超过 20 MB 同步上限。')
            path.write_bytes(data['raw'])
        else:
            buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not data['image'].save(buffer,'PNG'):return None
            if buffer.size()>=MAX_BYTES:raise ClipboardSizeLimit('图片超过 20 MB 同步上限。')
            path.write_bytes(bytes(buffer.data()))
    else:
        entries=[];total=0;roots=set()
        if len(data)!=1 or not Path(data[0]).is_file():return None
        for item in data:
            source=Path(item)
            from virtual_clipboard import remote_placeholder
            if remote_placeholder(source.name):raise VirtualClipboardError('REMOTE_FILE_PLACEHOLDER')
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
                if total>=MAX_BYTES:raise ClipboardSizeLimit('文件超过 20 MB 同步上限。')
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
                        if written>=MAX_BYTES:raise ClipboardSizeLimit('文件超过 20 MB 同步上限。')
                        target.write(chunk);raw_hash.update(chunk)
                after=child.stat()
                if copied!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return None
                if path.stat().st_size>=MAX_ARCHIVE_BYTES:return None
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
    if not 0<size<(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES):return None
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(kind=kind,path=str(path),size=size,sha256=digest,**({'raw_file':raw_file} if raw_file and raw_file.get('offset') else {}))


def unpack(kind,path,directory):
    path=Path(path)
    if path.stat().st_size>=(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES):raise ValueError('clipboard limit')
    if kind=='text':
        return text_value(path.read_bytes())
    if kind=='image':
        image=QImage(str(path))
        if image.isNull():raise ValueError('invalid image')
        format=bytes(QImageReader.imageFormat(str(path))).decode('ascii')
        return (kind,dict(image=image,raw=path.read_bytes(),mime='image/'+format))
    if kind!='files':raise ValueError('invalid kind')
    from sync_files import safe_name
    root=Path(directory)/'files';roots=[];seen=set();total=0;names={};siblings={}
    with zipfile.ZipFile(path) as archive:
        members=archive.infolist()
        if len(members)>4096:raise ValueError('too many entries')
        from virtual_clipboard import remote_placeholder
        if any(remote_placeholder(item.filename) for item in members):raise VirtualClipboardError('REMOTE_FILE_PLACEHOLDER')
        root.mkdir(exist_ok=False)
        for item in members:
            name=item.filename;parts=PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '\\' in name or ':' in name or '..' in parts
                    or any(part in ('','.','..') or part.rstrip(' .')!=part for part in parts)
                    or item.flag_bits&1 or (item.external_attr>>16)&0o170000==0o120000):raise ValueError('unsafe archive')
            folded=name.rstrip('/').casefold()
            if folded in seen:raise ValueError('duplicate name')
            seen.add(folded);total+=item.file_size
            if total>=MAX_BYTES:raise ValueError('clipboard limit')
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
        super().__init__();self.action=action;self.error=None;self.cancelled=threading.Event();self.signals=_Signals()
    def run(self):
        result=None
        try:
            if not self.cancelled.is_set():result=self.action(self.cancelled)
        except (OSError,ValueError,RuntimeError,zipfile.BadZipFile) as error:self.error=error
        self.signals.finished.emit(self,result)


class ClipboardSync(QObject):
    activity_changed=Signal(bool)
    copy_result=Signal(bool,dict)
    copy_started=Signal()
    copy_observed=Signal(object)
    copy_repeated=Signal(object)
    # Keep original image bytes as a shared Python object, rather than copying
    # a potentially 20 MB value through QVariantMap for each status update.
    view_changed=Signal(object)
    native_access_changed=Signal()

    @property
    def _pending(self):return getattr(self,'_pending_value',None)

    @_pending.setter
    def _pending(self,value):
        before=self._priority_active();self._pending_value=value;self._priority_sending=False
        if before!=self._priority_active():self.activity_changed.emit(self._priority_active())

    def _priority_active(self):
        return bool(getattr(self,'_priority_granted',False) and self._priority_work())

    def _priority_work(self):
        return bool(self._pending is not None and getattr(self,'_priority_sending',False) and not getattr(self,'_priority_waiting',False)
                    or getattr(self,'_content_fetching',False))

    def _priority_expired(self):
        before=self._priority_active();self._priority_granted=False
        if before:self.activity_changed.emit(False)

    def _priority_flags(self,*,waiting=None,receiving=None,sending=None):
        before=self._priority_active()
        if waiting is not None:self._priority_waiting=waiting
        if receiving is not None:self._content_fetching=receiving
        if sending is not None:self._priority_sending=sending
        self._priority_granted=self._priority_work()
        if self._priority_granted:self._priority_timer.start(CLIPBOARD_PRIORITY_LEASE_MS)
        else:self._priority_timer.stop()
        self._update_schedule()
        if before!=self._priority_active():self.activity_changed.emit(self._priority_active())

    def __init__(self,account,events,base,parent=None,*,enabled=True,clipboard=None,scheduler=None):
        super().__init__(parent);self.account=account;self.api=account.api;self.clipboard=clipboard or QApplication.clipboard()
        self._priority_granted=False;self._content_fetching=False;self._priority_timer=QTimer(self);self._priority_timer.setSingleShot(True)
        self._priority_timer.timeout.connect(self._priority_expired)
        self._is_native=clipboard is None or clipboard is QApplication.clipboard()
        self._native_reader=None
        self._native_baseline=None
        self._copy_input_stamp=None
        self._notified_activity=None;self._content_read=None;self._content_read_failed=None
        self._copy_history=[];self._current_content_keys=None
        self._sequence=native_sequence if self._is_native else lambda:None
        self.enabled=enabled;self._local_observation=False;self.closed=False;self._epoch=0;self._generation=0;self._applying=False
        self._native_access=self._native_accessible()
        self._clock_anchor=None;self._local_clock_anchor=(time.monotonic(),round(time.time()*1000))
        self._observed_clock=0;self._local_fence=None;self._local_retry=None
        self._clock_path=Path(base)/'clipboard-clock.json';self._copy_clock=0
        try:self._copy_clock=int(json.loads(self._clock_path.read_text(encoding='utf8'))['sequence'])
        except (OSError,ValueError,KeyError,TypeError):pass
        self._identity=self._session_identity()
        self._pushed_head=None;self._known_cloud_head=None
        self._prepared_receive=None;self._rejected_head=None
        self._native_receive_interrupted=False
        self._snapshot_available=True;self._cloud_progress=None
        self._virtual_capture=None;self._virtual_value=None;self._virtual_retry=None;self._native_format_issue=''
        self._view_revision=0;self.view=dict(value=self._snapshot(),phase='local',id='',revision=0,permission_required=not self._native_access,deliveries={},native_read_pending=not self._snapshot_available)
        from clipboard_delivery import ClipboardDeliveries
        self.deliveries=ClipboardDeliveries(self,base)
        self._applied_version=0;self._receiving_version=0
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(2)
        self._ignored=None;self._last_sequence=object();self._last_local_signature=object();self._seen='';self._keys=set();self._jobs={};self._pending=None;self._fetching=False;self._refresh_again=False
        self.scheduler=scheduler if scheduler is not None else SyncScheduler(self,connected=False)
        self._owns_scheduler=scheduler is None;self._metadata_probe=None
        self._retry_on_reconnect=False;self._resume_dependent_jobs=True
        self.schedule=self.scheduler.register('clipboard',self._scheduled_check)
        self.cache=Path(base)/'clipboard-cache';self.cache.mkdir(parents=True,exist_ok=True)
        if self._is_native and sys.platform=='win32' and native_input_available():
            from native_clipboard_reader import NativeClipboardReader
            self._native_reader=NativeClipboardReader(self.cache,lambda:self._sequence(),self)
            self._native_reader.ready.connect(self._native_read_ready)
            self._native_reader.failed.connect(self._native_read_failed)
        self._directories=[];self._current_directory=None
        for directory in self.cache.glob('clip-*'):
            if not directory.is_dir() or directory.is_symlink() or directory.is_junction():continue
            try:
                lease=json.loads((directory/'.lease.json').read_text(encoding='utf8'))
                if lease.get('flickdock_clipboard')==1 and isinstance(lease.get('created_at'),(int,float)):
                    self._directories.append((directory,lease['created_at']))
            except (OSError,ValueError,AttributeError):pass
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.setInterval(5000);self.retry.timeout.connect(self._retry)
        self.mac_poll=QTimer(self);self.mac_poll.setInterval(MAC_CLIPBOARD_OBSERVE_MS);self.mac_poll.timeout.connect(self._mac_changed);self._mac_count=None
        self.change_debounce=QTimer(self);self.change_debounce.setSingleShot(True);self.change_debounce.setInterval(BINARY_COPY_COALESCE_MS)
        self.change_debounce.timeout.connect(self._changed)
        self.clipboard.dataChanged.connect(self._schedule_change)
        self._last_local_signature=signature(self._snapshot())
        self._last_sequence=self._sequence() if self._snapshot_available else object()
        if self._native_reader is not None and not self._snapshot_available:
            activity=self._read_copy_activity()
            self._native_baseline=(self._sequence(),activity[0] if activity is not None else None)
        account.account_changed.connect(self._account_changed)
        events.clipboard_changed.connect(self.request_refresh)
        payload=getattr(events,'clipboard_payload',None)
        if payload is not None:payload.connect(self._pushed)
        delivery=getattr(events,'clipboard_delivery',None)
        if delivery is not None:delivery.connect(self.deliveries.merge)
        from shared_clipboard_files import ClipboardFileJobs
        self.file_jobs=ClipboardFileJobs(self,base)
        self.scheduler.connection_changed.connect(self._connection_changed)
        self.scheduler.bind_socket(events.socket)
        self._update_schedule()
        if enabled:
            if self._native_reader is not None and not self._snapshot_available:self.change_debounce.start(NATIVE_READ_RETRY_INITIAL_MS)
            QTimer.singleShot(0,self._cleanup)
            if sys.platform=='darwin' and self._is_native:self._mac_changed(initial=True);self.mac_poll.start()
            self.schedule.request()

    def _mac_changed(self,initial=False):
        if not (self.enabled or self._local_observation) or self.closed:return
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
        self._snapshot_available=False
        self._native_format_issue=''
        if not self._native_accessible():return None
        try:
            if self._is_native and sys.platform=='win32' and native_input_available() and not self.clipboard.ownsClipboard():
                if self._native_reader is not None:
                    self._snapshot_available,value=self._native_reader.read(self._sequence())
                    self._native_format_issue=self._native_reader.issue
                    return value
                return None
            mime=self.clipboard.mimeData()
            if mime is None:return None
            self._native_format_issue=''
            if self._is_native:
                # Remote-control apps may add virtual-file descriptors beside
                # an ordinary local file copy. Use the ready local payload
                # before asking that app to render a delayed remote stream.
                from virtual_clipboard import only_remote_placeholders
                if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()) and not only_remote_placeholders(mime):
                    value=_snapshot_mime(mime)
                    if value is None:
                        self._native_format_issue='VIRTUAL_FILE_SINGLE_ONLY';return None
                    self._snapshot_available=True;return value
                descriptor=describe_virtual(mime)
                if descriptor is not None:
                    sequence=self._sequence();key=sequence,descriptor.sha256
                    if self._virtual_value is not None and self._virtual_value[0]==key:
                        self._snapshot_available=True;return self._virtual_value[1]
                    if not self.closed and hasattr(self,'cache') and sequence is not None:self._capture_virtual(descriptor,key)
                    return None
            value=_snapshot_mime(mime)
            if value is None and self._is_native and native_format_count():
                self._native_format_issue='NATIVE_FORMAT_NOT_READY';return None
            self._snapshot_available=True
            return value
        except VirtualClipboardError as error:
            self._native_format_issue=error.code;return None
        except (OSError,RuntimeError,ImportError) as error:
            from network_diagnostics import record_event
            record_event(self._clock_path.parent,'cloud','clipboard-read',type(error).__name__.upper())
            return None

    def _native_read_ready(self,sequence,value,directory):
        if self.closed:return
        if directory is not None:self._directories.append((directory,time.time()))
        if self._prepared_receive is not None:self._apply_received()
        baseline,self._native_baseline=self._native_baseline,None
        if baseline is not None:
            activity=self._read_copy_activity()
            stamp=activity[0] if activity is not None else None
            # Startup/account restoration is an observation, not permission to
            # export the previous session's delayed clipboard as a fresh copy.
            if sequence==baseline[0] or stamp is None or baseline[1] is None or stamp==baseline[1]:
                self._last_sequence=sequence;self._last_local_signature=signature(value)
                if self._ignored!=signature(value):self._set_view(value,'local')
                if self._native_receive_interrupted and self._active():
                    self._native_receive_interrupted=False;self.request_refresh()
                return
        self.change_debounce.start(0)

    def _native_read_failed(self,code):
        if self.closed:return
        from network_diagnostics import record_event
        record_event(self._clock_path.parent,'cloud','clipboard-read',code)
        self._native_format_issue=code
        if code=='REMOTE_FILE_PLACEHOLDER':
            self._observe_control_file(self._sequence());return
        self._set_view(self.view['value'],self.view['phase'],self.view.get('id',''))
        self.change_debounce.start(0 if code=='NATIVE_READ_REPLACED' else NATIVE_READ_RETRY_MAX_MS)

    def _observe_control_file(self,sequence):
        self._last_sequence=sequence;self.change_debounce.stop()
        task=self._pending or self._local_retry
        if task is not None and task['id']==self.view.get('id'):task['generation']=self._generation
        self._set_view(self.view['value'],self.view['phase'],self.view.get('id',''))
        if self._native_receive_interrupted and self._pending is None and self._local_retry is None:
            self._native_receive_interrupted=False;self.request_refresh()

    def _reject_control_head(self,head):
        self._rejected_head=(head['id'],head.get('sha256'))
        from network_diagnostics import record_event
        record_event(self._clock_path.parent,'remote','clipboard_skip','REMOTE_FILE_PLACEHOLDER')

    def _capture_virtual(self,descriptor,key):
        if self._virtual_capture is not None:
            if self._virtual_capture[0]==key:return
            self._virtual_capture[1].cancelled.set()
        if self._virtual_retry is not None and self._virtual_retry[0]==key and time.monotonic()<self._virtual_retry[1]:return
        try:directory=self._directory()
        except OSError as error:
            self._native_format_issue=type(error).__name__.upper();return
        epoch=self._epoch
        previous=self.view['value'];previous_signature=signature(previous)
        def prepare(cancel):
            result=capture_virtual(directory,descriptor,key[0],cancel)
            if result['ok'] and previous and previous[0]=='files' and len(previous[1])==1:
                source=Path(previous[1][0])
                if source.name==descriptor.name and signature(previous)==previous_signature:
                    from selection_archive import _source_reader
                    try:
                        with _source_reader(source) as stream:
                            digest=hashlib.sha256()
                            while data:=stream.read(1024*1024):
                                if cancel.is_set():raise InterruptedError()
                                digest.update(data)
                        result['equivalent']=digest.hexdigest()==result['sha256'] and signature(previous)==previous_signature
                    except InterruptedError:raise
                    except OSError:
                        from network_diagnostics import record_event
                        record_event(self._clock_path.parent,'cloud','clipboard-read','PREVIOUS_FILE_UNAVAILABLE')
                        result['equivalent']=False
            return result
        def done(result):
            if self._virtual_capture is None or self._virtual_capture[0]!=key:return
            self._virtual_capture=None
            if epoch!=self._epoch or key[0]!=self._sequence():return
            if result is None or not result['ok']:
                code=result['code'] if result else 'VIRTUAL_FILE_READ_FAILED'
                from network_diagnostics import record_event
                record_event(self._clock_path.parent,'cloud','clipboard-read',code)
                self._virtual_retry=key,time.monotonic()+5;self._native_format_issue=code
                return
            equivalent=result.get('equivalent') and signature(previous)==previous_signature
            if equivalent:
                Path(result['path']).unlink();value=previous
            else:value=('files',[result['path']])
            self._virtual_value=key,value;self._virtual_retry=None
            if self._active():self.change_debounce.start(0)
            else:self._set_view(self._virtual_value[1],'local')
        job=self._work(prepare,done)
        self._virtual_capture=key,job

    def recheck_native_access(self,*,force=False):
        allowed=self._native_accessible(force=force)
        if allowed!=self._native_access and not self.closed:
            self._native_access=allowed;self._account_changed(force=True);self.native_access_changed.emit()
        return allowed

    def _active(self):return self.enabled and not self.closed and self.account.signed_in and self._native_accessible()

    def set_enabled(self,enabled):
        enabled=bool(enabled)
        if self.closed or self.enabled==enabled:return
        self.enabled=enabled;self.change_debounce.stop();self._account_changed(force=True)
        if enabled:
            if sys.platform=='darwin' and self._is_native:self._mac_changed(initial=True);self.mac_poll.start()
        elif not self._local_observation:self.mac_poll.stop()

    def set_local_observation(self,enabled):
        """Reuse the native observer for translation without enabling cloud sync."""
        self._local_observation=bool(enabled)
        if self.closed:return
        if sys.platform=='darwin' and self._is_native:
            if self.enabled or enabled:
                self._mac_changed(initial=True);self.mac_poll.start()
            else:self.mac_poll.stop()

    def _schedule_change(self):
        if self._is_native and not self.closed:
            if self._applying:return
            sequence=self._sequence()
            if sequence is not None and sequence==self._last_sequence:return
            # Capture input/owner provenance before a delayed reader finishes;
            # mouse movement during that read is not a new Copy action.
            if self._notified_activity is None or self._notified_activity[0]!=sequence:
                self._notified_activity=(sequence,self._read_copy_activity())
            if self._virtual_capture is not None and self._virtual_capture[0][0]!=sequence:
                self._virtual_capture[1].cancelled.set()
            if self._active():
                # Clipboard owners (including remote-control software) can still
                # be publishing delayed formats inside dataChanged. Fence old
                # receives now, and read only after the notification unwinds.
                self._native_receive_interrupted|=self._fetching or self._prepared_receive is not None
                self._copy_notified_mono=time.monotonic();self._generation+=1
                self.change_debounce.start(TEXT_COPY_COALESCE_MS)
            else:self.change_debounce.start(TEXT_COPY_COALESCE_MS)
            return
        if not self._active() and not self.closed:
            value=self._snapshot()
            if self._snapshot_available and not self._applying:self.copy_observed.emit(value)
            self._set_view(value if self._snapshot_available else self.view.get('value'),'local');return
        if self._active() and not self._applying:
            sequence=self._sequence()
            if sequence is not None and sequence==self._last_sequence:return
            value=self._snapshot();sig=signature(value)
            # UI observers need real repeated copies too. This signal never
            # changes the content fence or schedules a duplicate cloud upload.
            if self._snapshot_available:self.copy_observed.emit(value)
            if (self._snapshot_available and (value is not None or sequence is None) and sig==self._last_local_signature
                    and self.view.get('phase') not in ('error','superseded')):
                self._last_sequence=sequence;self._repeat_copy_feedback(value);return
            if self._snapshot_available:self._last_copy_observation=time.monotonic()
            from PySide6.QtGui import QCursor
            point=QCursor.pos();self._copy_point=(point.x(),point.y())
            # Stop an in-flight remote read before the native notification's
            # coalescing delay; the user's newer local copy always wins.
            self._native_receive_interrupted|=self._fetching or self._prepared_receive is not None
            self._copy_notified_mono=time.monotonic();self._generation+=1
            self.change_debounce.start(TEXT_COPY_COALESCE_MS if value is not None and value[0]=='text' else BINARY_COPY_COALESCE_MS)

    def _read_copy_activity(self):
        if not self._is_native:return None
        try:return copy_activity()
        except (OSError,RuntimeError,ImportError):
            from network_diagnostics import record_event
            record_event(self._clock_path.parent,'cloud','copy-source','INPUT_METADATA_UNAVAILABLE')
            return None

    def _repeat_copy_feedback(self,value,*,user_intent=True,explicit=False):
        if not user_intent:return
        now=time.monotonic();previous=getattr(self,'_last_copy_observation',now)
        self._last_copy_observation=now
        # Native format conversion bursts are one observation. A later real
        # copy can show the confirmed state without re-uploading identical bytes.
        if (value is not None and (explicit or now-previous>=COPY_FORMAT_SETTLE_SECONDS) and self.view.get('id')
                and signature(value)==signature(self.view.get('value'))):
            self.copy_repeated.emit(dict(self.view,value=value))

    def _remember_content(self,value):
        keys=content_keys(value)
        self._current_content_keys=keys
        self._remember_keys(keys)

    def _remember_keys(self,keys):
        if not keys:return
        self._copy_history=[row for row in self._copy_history if not row.intersection(keys)]
        self._copy_history.append(keys)
        del self._copy_history[:-COPY_REPLAY_HISTORY_ENTRIES]

    def _content_ready(self,value,sequence):
        previous=self.view.get('value')
        if content_keys(value) is not None and self._current_content_keys is not None:return True
        key=(sequence,signature(value),signature(previous))
        if self._content_read_failed==key:return True
        if self._content_read is not None:
            if self._content_read[0]==key:return False
            self._content_read[1].cancelled.set()
        epoch=self._epoch
        def read(cancel):
            content_keys(value,read_files=True,cancel=cancel)
            content_keys(previous,read_files=True,cancel=cancel)
            return True
        def ready(result):
            if self._content_read is None or self._content_read[0]!=key:return
            self._content_read=None
            if epoch!=self._epoch or sequence!=self._sequence():return
            if result is not True:self._content_read_failed=key
            elif self._current_content_keys is None:self._remember_content(previous)
            self.change_debounce.start(0)
        job=self._work(read,ready);self._content_read=(key,job)
        return False

    def _consume_replay(self,value,sequence):
        self._last_sequence=sequence;self._last_local_signature=signature(value)
        task=self._pending or self._local_retry
        if task is not None and task['id']==self.view.get('id'):task['generation']=self._generation
        current=self.view.get('value')
        keys=content_keys(value);current_keys=self._current_content_keys
        if current is not None and keys and current_keys and not keys.intersection(current_keys):
            # A known old bridge echo must not replace a newer local copy.
            # Restore only a positively identified replay, never unknown data.
            fence=self._local_fence
            restored=self._apply(current,self._current_directory,identity=self.view.get('id'))
            self._local_fence=fence
            if not restored:
                from network_diagnostics import record_event
                record_event(self._clock_path.parent,'cloud','clipboard-replay','RESTORE_UNCONFIRMED')
        if self._native_receive_interrupted and self._pending is None and self._local_retry is None:
            self._native_receive_interrupted=False;self.request_refresh()

    def _copy_timestamp(self,intent):
        if intent['at'] is None:
            # Keep a copy's order immutable. An unavailable cloud clock must
            # never gate a copy while the cloud clock is unavailable. The
            # monotonic anchor also prevents wall-clock changes during a copy.
            mono,server=self._clock_anchor or self._local_clock_anchor
            intent['at']=max(round(server+(intent['mono']-mono)*1000),intent['floor']+1)
        return intent['at']

    def _calibrate(self,result,started):
        if not isinstance(result,dict):return
        server=result.get('server_time');finished=time.monotonic()
        if type(server) is int and finished-started<10:
            self._clock_anchor=((started+finished)/2,server)
            if self._local_fence:self._copy_timestamp(self._local_fence)
        head=result.get('clipboard')
        self._observe_cloud_head(head)
        if isinstance(head,dict) and type(head.get('copied_at')) is int:
            self._observed_clock=max(self._observed_clock,head['copied_at'])

    def _observe_cloud_head(self,head):
        if not isinstance(head,dict) or not (head.get('ready') or head.get('expired')):return
        version=head_version(head)
        if not version or not isinstance(head.get('id'),str):return
        known=self._known_cloud_head
        if known is None or version>known[0]:self._known_cloud_head=(version,head['id'],head.get('sha256'))

    def _cloud_receive_valid(self,head,generation):
        if generation!=self._generation or not self._active() or head_version(head)<=self._applied_version:return False
        if self._known_cloud_head!=(head_version(head),head['id'],head['sha256']):return False
        if self._local_fence is not None and not self._head_after_local_copy(head,self._local_fence):return False
        return True

    def _head_after_local_copy(self,head,intent):
        local=self._copy_timestamp(intent);remote=head.get('copied_at',head.get('at',0))
        if local is None or type(remote) is not int:return False
        if remote!=local:return remote>local
        # Match the server's same-millisecond device ordering, while keeping
        # this device's own in-flight publication behind its native-copy fence.
        device=head.get('device')
        return isinstance(device,str) and device>self.account.device_id

    def _accept_received(self,head,value,directory,*,valid,applied):
        from virtual_clipboard import remote_placeholder
        if value[0]=='files' and any(remote_placeholder(ref) for ref in value[1]):
            self._reject_control_head(head);return
        self._prepared_receive=dict(head=dict(head),value=value,directory=directory,valid=valid,applied=applied,attempts=0)
        self._apply_received()

    def _apply_received(self):
        pending=self._prepared_receive
        if pending is None:return
        if not pending['valid']():self._prepared_receive=None;return
        value=pending['value'];actual=signature(self._snapshot())
        if self._snapshot_available and actual not in (self._last_local_signature,signature(value)):
            self._prepared_receive=None;self._schedule_change();return
        readable=self._snapshot_available or self._native_format_issue=='REMOTE_FILE_PLACEHOLDER'
        if not readable or not self._apply(value,pending['directory'],identity=pending['head']['id']):
            from network_diagnostics import record_event
            record_event(self._clock_path.parent,'remote','clipboard_apply','NATIVE_WRITE_UNCONFIRMED')
            pending['attempts']+=1
            self.retry.start(min(NATIVE_READ_RETRY_MAX_MS,NATIVE_READ_RETRY_INITIAL_MS*2**min(pending['attempts']-1,4)))
            self._set_view(self.view['value'],self.view['phase'],self.view['id']);return
        head=pending['head'];self._prepared_receive=None;self.retry.stop()
        self.file_jobs.supersede();self._epoch+=1;self._generation+=1;self._pending=None;self._local_retry=None
        self._cancel_replaced_work();self._fetching=False
        self._priority_flags(waiting=False,receiving=False)
        self._seen=head['id'];self._applied_version=max(self._applied_version,head_version(head))
        self._remember_content(value)
        self._observed_clock=max(self._observed_clock,head.get('copied_at',head.get('at',0)))
        known=self._known_cloud_head
        backed_up=bool(known and known[1:]==(head['id'],head['sha256']))
        self._set_view(value,'synced',head['id'],route='cloud',backed_up=backed_up)
        from network_diagnostics import record_event
        record_event(self._clock_path.parent,'remote','clipboard_apply','NATIVE_WRITE_VERIFIED')
        pending['applied']()

    def _next_sequence(self):
        self._copy_clock=max(self._copy_clock+1,time.time_ns()//1000)
        self._clock_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self._clock_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'sequence':self._copy_clock}),encoding='utf8')
        os.replace(temporary,self._clock_path)
        return self._copy_clock

    def _update_schedule(self):
        active=self._pending is not None or self._local_retry is not None or self._content_fetching or self._prepared_receive is not None
        finishing=self._prepared_receive is not None or bool(self._cloud_progress and self._cloud_progress['phase']=='verifying')
        self.schedule.configure(enabled=self._active(),transferring=active,finishing=finishing)

    def request_refresh(self):self.schedule.request()

    def _connection_changed(self,online):
        self._resume_dependent_jobs=True
        if not online:
            if self._prepared_receive is None:self.retry.stop()
            self.file_jobs.retry.stop();self.deliveries.retry.stop()
            self._retry_on_reconnect=self._pending is not None or self._local_retry is not None

    def _scheduled_check(self):
        self.file_jobs.expire()
        if self._resume_dependent_jobs:
            self._resume_dependent_jobs=False;self.file_jobs._retry();self.deliveries.flush()
        if self._retry_on_reconnect:
            self._retry_on_reconnect=False
            if self._pending is not None or self._local_retry is not None:self.retry.start(0)
        if self._pending is not None and self._pending.get('replacement_deadline'):
            self._resolve_replaced(self._pending,self._pending['replacement_error']);return
        if self._fetching:
            # The content stream owns its bytes. A progress check reads only
            # the latest version so a missed newer hint can replace it safely.
            if self._content_fetching and self._metadata_probe is None:
                marker=object();self._metadata_probe=marker;generation=self._generation
                def checked(result,error):
                    if self._metadata_probe is not marker:return
                    self._metadata_probe=None
                    if error or generation!=self._generation:return
                    head=result.get('clipboard') if isinstance(result,dict) else None
                    if isinstance(head,dict) and head_version(head)>self._receiving_version:self._pushed(result)
                self._request('GET','/v1/clipboard',checked)
            return
        self._cleanup();self.refresh()

    def _valid_task(self,pending):
        if pending.get('_policy_rejected'):return False
        return (self._active() and self._pending is pending or
                getattr(self,'file_jobs',None) is not None and self.file_jobs.valid(pending))

    def _clear_task(self,pending):
        if self._pending is pending:self._pending=None
        self._update_schedule()

    def _cancel_replaced_work(self):
        self._metadata_probe=None
        retained=[task for task in self.file_jobs.tasks.values() if self.file_jobs.held(task)]
        keys=set().union(*(task.get('_keys',set()) for task in retained)) if retained else set()
        for task in self.file_jobs.tasks.values():
            if task.get('releases'):keys.update(task.get('_release_keys',()))
        for key in tuple(self._keys-keys):self.api.cancel(key)
        self._keys.intersection_update(keys)
        for job in self._jobs:
            if not getattr(job,'_shared_probe',False) and not any(task.get('_job') is job for task in retained):job.cancelled.set()
        if self._content_read is not None and self._content_read[1].cancelled.is_set():self._content_read=None
        if self._virtual_capture is not None and self._virtual_capture[1].cancelled.is_set():self._virtual_capture=None

    def _task_error(self,pending,error):
        from network_diagnostics import record_event
        record_event(self._clock_path.parent,'cloud','clipboard_submit',str(getattr(error,'code','') or type(error).__name__),
                     getattr(error,'status',0),details={'phase':'content' if pending.get('staged') else 'metadata'})
        if getattr(error,'code','')=='CLIPBOARD_TRANSIENT_FILE':
            self._unavailable_task(pending);return
        if self.view.get('id')==pending['id'] and pending.get('generation',self._generation)==self._generation:
            previous=self._cloud_progress or {}
            self._set_cloud_progress(pending['id'],previous.get('received',0),previous.get('total',0),'error')
        self._copy_result(pending,False)
        if getattr(self,'file_jobs',None) is not None:self.file_jobs.fail(pending,error)
        if self._pending is pending and pending.get('generation',self._generation)==self._generation:self._priority_flags(waiting=True);self.retry.start()

    def _unavailable_task(self,pending):
        # An earlier upload reply must not resurrect a policy-rejected copy.
        pending['_policy_rejected']=True
        for key in tuple(pending.get('_keys',())):
            self.api.cancel(key);self._keys.discard(key)
        job=pending.get('_job')
        if job is not None:job.cancelled.set()
        self.file_jobs.update(pending,'unavailable')
        if self.view.get('id')==pending['id']:self._cloud_progress=None
        self._copy_result(pending,False,phase='unavailable')
        if self._local_retry is pending:self._local_retry=None
        self._clear_task(pending)
        if self._pending is None and self._local_retry is None:
            self.retry.stop();self._priority_flags(waiting=False)

    def _expire_confirmation(self,pending):
        if self._pending is not pending:return
        from network_diagnostics import record_event
        record_event(self._clock_path.parent,'cloud','clipboard-conflict','CONFIRMATION_TIMEOUT')
        self._set_cloud_progress(pending['id'],0,0,'error')
        self._copy_result(pending,False,phase='error')
        self.file_jobs.update(pending,'unavailable');self._clear_task(pending)
        self.retry.stop();self._priority_flags(waiting=False,sending=False)

    def _resolve_replaced(self,pending,error,*,snapshot=None):
        if self._pending is not pending and not self.file_jobs.held(pending):return
        if pending.get('resolving_replacement') and snapshot is None:return
        deadline=pending.get('replacement_deadline')
        if deadline and snapshot is None and time.monotonic()>=deadline:
            self._expire_confirmation(pending);return
        if deadline:self.retry.stop()
        pending['resolving_replacement']=True
        def resolved(result,failure):
            pending.pop('resolving_replacement',None)
            if not self._valid_task(pending):return
            if self._pending is not pending and not self.file_jobs.held(pending):return
            if failure:self._task_error(pending,failure);return
            head=result.get('clipboard') if isinstance(result,dict) else None
            item=pending['package'];held=self.file_jobs.held(pending)
            try:
                matches=bool(not held and self._pending is pending and isinstance(head,dict) and head.get('ready') and same_cloud_content(item,head))
            except (ValueError,TypeError,UnicodeError) as invalid:
                self._task_error(pending,invalid);return
            if matches:
                try:canonical=str(UUID(head.get('id','')))
                except (ValueError,TypeError,AttributeError):self._task_error(pending,ValueError('云端剪贴板标识无效。'));return
                if canonical!=head['id'] or not head_version(head):self._task_error(pending,ValueError('云端剪贴板版本无效。'));return
                if head.get('sha256')!=item['sha256']:
                    from network_diagnostics import record_event
                    record_event(self._clock_path.parent,'cloud','clipboard-conflict','EQUIVALENT_TEXT_FORMAT')
                self._task_ready(pending,head,equivalent=True)
                if self._snapshot_available and signature(self._snapshot())==signature(self.view['value']):
                    self.deliveries.received(head,'cloud')
                return
            newer=bool(isinstance(head,dict) and head.get('ready') and self._head_after_local_copy(head,pending['intent']))
            if not held and self._pending is pending and not newer:
                # Reservations replace upload order before they replace the
                # committed body. The previous ready head proves no new copy.
                end=pending.setdefault('replacement_deadline',time.monotonic()+COPY_CONFIRMATION_TIMEOUT_MS/1000)
                pending['replacement_error']=error
                if time.monotonic()>=end:self._expire_confirmation(pending);return
                self.retry.stop();self._priority_flags(waiting=True,sending=False)
                self._set_cloud_progress(pending['id'],0,0,'verifying');return
            from network_diagnostics import record_event
            record_event(self._clock_path.parent,'cloud','clipboard-conflict','DIFFERENT_CLIPBOARD_CONTENT')
            self._set_cloud_progress(pending['id'],0,0,'superseded')
            self._copy_result(pending,False,phase='superseded')
            self._clear_task(pending)
            if held:self.file_jobs.fail(pending,error)
            else:self.file_jobs.update(pending,'unavailable')
            if isinstance(head,dict) and head.get('ready'):self._pushed_head={'clipboard':head}
            self.refresh()
        if snapshot is not None:resolved(snapshot,None)
        else:self._request('GET','/v1/clipboard',resolved,_task=pending)

    def _task_ready(self,pending,head,*,equivalent=False):
        if not self._valid_task(pending):return
        item=pending['package']
        try:
            content_verified=isinstance(head,dict) and (same_cloud_content(item,head) if equivalent else
                head.get('sha256')==item['sha256'] and head.get('size')==item['size'])
        except (ValueError,TypeError,UnicodeError) as invalid:
            self._task_error(pending,invalid);return
        if (not isinstance(head,dict) or (not equivalent and head.get('id')!=pending['id']) or not head.get('ready')
                or not content_verified):
            self._task_error(pending,ValueError('剪贴板提交校验失败。'));return
        if self._pending is pending:
            self._seen=head['id'];self._applied_version=max(self._applied_version,head_version(head))
            if self.view.get('id')==pending['id']:self._set_view(self.view['value'],'synced',pending['id'],backed_up=True)
            self._copy_result(pending,True);self._pending=None
            if equivalent:self._set_view(self.view['value'],'synced',head['id'],backed_up=True,copy_id=pending['id'])
            self.deliveries.committed(head)
        raw=item.get('raw_file')
        if raw and pending.get('shared_files'):self.file_jobs.update(pending,'ready',raw['size'])
        elif raw:self.file_jobs.update(pending,'unavailable')
        self._cleanup()
        if self._refresh_again:QTimer.singleShot(0,self.refresh)

    def _request(self,method,path,callback,_task=None,**kwargs):
        epoch=self._epoch;identity=self._identity;holder=[];completed=False;started=time.monotonic()
        def done(result,error):
            nonlocal completed
            completed=True
            if holder:self._keys.discard(holder[0])
            if holder and _task is not None:_task.setdefault('_keys',set()).discard(holder[0])
            if holder and _task is not None:_task.setdefault('_release_keys',set()).discard(holder[0])
            if not self.closed and identity==self._identity and (epoch==self._epoch or _task is not None and self._valid_task(_task)):
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
        return ((account or {}).get('id',''),getattr(self.account,'device_id',''),getattr(self.api,'token','')) if self.account.signed_in else None

    def _account_changed(self,*_,force=False):
        identity=self._session_identity()
        if not force and identity==self._identity:
            self.request_refresh();return
        self.schedule.configure(enabled=False)
        self.deliveries.reset()
        files=getattr(self,'file_jobs',None)
        if files is not None:files.save()
        self._identity=identity;self._ignored=None;self._local_retry=None;self._local_fence=None
        self._copy_history=[];self._current_content_keys=None;self._notified_activity=None;self._content_read_failed=None
        if self._content_read is not None:self._content_read[1].cancelled.set();self._content_read=None
        self._pushed_head=None;self._known_cloud_head=None;self._cloud_progress=None
        self._prepared_receive=None
        self._native_receive_interrupted=False
        self._applied_version=0;self._receiving_version=0
        self._clock_anchor=None;self._local_clock_anchor=(time.monotonic(),round(time.time()*1000));self._observed_clock=0
        # A session switch is not a Copy action. Establish a native baseline so
        # a delayed notification cannot export the previous account's content.
        self._last_local_signature=signature(self._snapshot());self._last_sequence=self._sequence()
        self._native_baseline=None
        if self._native_reader is not None and not self._snapshot_available:
            activity=self._read_copy_activity()
            self._native_baseline=(self._last_sequence,activity[0] if activity is not None else None)
        if sys.platform=='darwin' and not self.closed:self._mac_changed(initial=True)
        self._epoch+=1;self._generation+=1;self._pending=None;self._seen='';self._rejected_head=None;self._fetching=False;self.retry.stop()
        self._priority_flags(waiting=False,receiving=False)
        for key in tuple(self._keys):self.api.cancel(key)
        self._keys.clear()
        for job in self._jobs:job.cancelled.set()
        self._virtual_capture=None
        if files is not None:
            # The old account's journal was saved before switching identity.
            files.journal=None;files.tasks={};files.reset(load=not self.closed)
        if not self.closed:
            value=self._snapshot();self._set_view(value if self._snapshot_available else self.view['value'],'local')
        self._update_schedule()
        if self._active():self.schedule.request()

    def _directory(self):
        path=Path(tempfile.mkdtemp(prefix='clip-',dir=self.cache));created=time.time()
        try:(path/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':created}),encoding='utf8')
        except OSError:
            shutil.rmtree(path,ignore_errors=True);raise
        self._directories.append((path,created));return path

    def _work(self,action,callback):
        job=_Job(action);self._jobs[job]=callback;job.signals.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        self.pool.start(job,1)
        return job

    def _finished(self,job,result):
        callback=self._jobs.pop(job,None)
        if job.error is not None and not job.cancelled.is_set():
            from network_diagnostics import record_event
            record_event(self._clock_path.parent,'cloud','clipboard-work',type(job.error).__name__,
                         details={'errno':getattr(job.error,'errno',None)})
        if callback and not self.closed and not job.cancelled.is_set():callback(result)
        self._cleanup()

    def _changed(self):
        if self.closed or self._applying:return
        if not self._active():
            value=self._snapshot()
            if self._snapshot_available:
                self.copy_observed.emit(value);self._set_view(value,'local');self._last_sequence=self._sequence()
            return
        sequence=self._sequence()
        # Repeated native notifications for one copy must not ask another app
        # to render the same delayed clipboard again or repeat file/image work.
        if sequence is not None and sequence==self._last_sequence:return
        value=self._snapshot();sig=signature(value)
        if not self._snapshot_available:
            if self._native_format_issue=='REMOTE_FILE_PLACEHOLDER':
                self._observe_control_file(sequence);return
            previous,attempts=getattr(self,'_native_read_retry',(None,0))
            attempts=min(attempts+1,5) if previous==sequence else 1
            self._native_read_retry=(sequence,attempts)
            self._set_view(self.view['value'],self.view['phase'],self.view.get('id',''))
            self.change_debounce.start(min(NATIVE_READ_RETRY_MAX_MS,NATIVE_READ_RETRY_INITIAL_MS*2**(attempts-1)))
            return
        if value is None and sequence is not None and sequence!=self._last_sequence:
            # Some native owners announce a new sequence before delayed data
            # is readable. Retry briefly without consuming its copy event.
            previous,attempts=getattr(self,'_native_read_retry',(None,0))
            attempts=attempts+1 if previous==sequence else 1
            self._native_read_retry=(sequence,attempts)
            if attempts<=5:self.change_debounce.start(100);return
        else:self._native_read_retry=(None,0)
        if self._is_native and not self._content_ready(value,sequence):return
        notified=self._notified_activity
        activity=notified[1] if notified is not None and notified[0]==sequence else self._read_copy_activity()
        self._notified_activity=None
        repeat_intent=True
        if activity is not None:
            stamp,age,foreground=activity
            repeat_intent=stamp!=self._copy_input_stamp and 0<=age<=REPEAT_COPY_INPUT_MAX_AGE_MS and foreground is not False
            self._copy_input_stamp=stamp
        keys=content_keys(value)
        if (self._is_native and activity is not None and not repeat_intent and keys
                and any(keys.intersection(row) for row in self._copy_history)):
            self._consume_replay(value,sequence);return
        if self._is_native and (sig!=self._last_local_signature or repeat_intent):self.copy_observed.emit(value)
        retry_copy=repeat_intent and self.view.get('phase') in ('error','superseded') and not self.view.get('backed_up')
        if sig==self._last_local_signature and not retry_copy and (not repeat_intent or sig==signature(self.view.get('value'))):
            self._last_sequence=sequence
            self._repeat_copy_feedback(value,user_intent=repeat_intent,explicit=activity is not None and repeat_intent)
            task=self._pending or self._local_retry
            if task is not None and task['id']==self.view['id']:task['generation']=self._generation
            self._set_view(self.view['value'],self.view['phase'],self.view.get('id',''))
            # The unreadable notification fenced an in-flight receive before
            # we could tell it was a duplicate. Resume that receive now instead
            # of leaving it cancelled until the next 30-second poll.
            if self._native_receive_interrupted and self._pending is None and self._local_retry is None:
                self._native_receive_interrupted=False;self.request_refresh()
            return
        if sequence is not None:
            if sequence==self._last_sequence:return
            self._last_sequence=sequence
        self._native_receive_interrupted=False
        self._remember_content(value)
        self._current_directory=None;self.retry.stop()
        self._last_local_signature=sig
        self._local_fence=dict(mono=getattr(self,'_copy_notified_mono',time.monotonic()),at=None,floor=self._observed_clock)
        self._copy_timestamp(self._local_fence)
        self._ignored=None;self._generation+=1;self._epoch+=1;generation=self._generation
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
                               feedback=self._copy_context(value))
        self.file_jobs.register(self._local_retry,value)
        self._set_view(value,'uploading',self._local_retry['id'])
        self._prepare_local()

    def _set_view(self,value,phase,identity='',*,route=None,backed_up=None,copy_id=None):
        previous=self.view.get('value')
        if value is not previous:
            if signature(value)==signature(previous):value=previous
            else:self._view_revision+=1
        same=bool(identity and identity==self.view.get('id'))
        delivery=route if route is not None else self.view.get('route','') if same else ''
        backup=backed_up if backed_up is not None else phase=='synced' or same and self.view.get('backed_up',False)
        task=self._pending or self._local_retry
        if copy_id is None:
            copy_id=(task['id'] if identity and task is not None and task['id']==identity
                     else self.view.get('copy_id','') if same else '')
        # A deduplicated upload can adopt another immutable cloud ID. Its local
        # Copy action remains the same event for feedback and presentation.
        local=bool(copy_id)
        same_copy=bool(copy_id and copy_id==self.view.get('copy_id'))
        if identity and phase=='uploading' and not same:
            self._cloud_progress=dict(id=identity,phase='preparing',received=0,total=0)
        progress=dict(self._cloud_progress) if self._cloud_progress and self._cloud_progress['id']==identity else None
        if backup:progress=dict(phase='complete',received=1,total=1)
        state=dict(value=value,phase=phase,id=identity,revision=self._view_revision,permission_required=not self._native_accessible(),route=delivery,backed_up=backup,
                   deliveries=self.deliveries.summary(identity),native_read_pending=not self._snapshot_available,
                   cloud_transfer=progress,local_copy=local,copy_id=copy_id,
                   receive_pending=self._prepared_receive is not None,
                   native_format_issue=self._native_format_issue,
                   point=task.get('feedback',{}).get('point') if local and task is not None else self.view.get('point') if same or same_copy else None)
        shared=self.file_jobs.tasks.get(identity)
        if shared is not None:
            raw=(shared.get('package') or {}).get('raw_file') or {}
            state.update(received=shared.get('_file_received',0),total=raw.get('size',shared['source_stat'][2]))
        self._update_schedule()
        if state==self.view:return
        self.view=state
        self.view_changed.emit(dict(self.view))

    def _set_cloud_progress(self,identity,received,total,phase):
        if self.view.get('id')!=identity:return
        self._cloud_progress=dict(id=identity,received=max(0,received),total=max(0,total),phase=phase)
        self._set_view(self.view['value'],self.view['phase'],identity)

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

    def _copy_result(self,pending,ok,*,phase=None):
        # A retained old file can finish for the shelf while a newer clipboard
        # is active. Its completion must not emit stale clipboard feedback.
        if (self.view.get('id')!=pending['id']
                or pending.get('generation',self._generation)!=self._generation):return
        if self.view.get('id')==pending['id']:
            self._set_view(self.view['value'],phase or ('synced' if ok else 'error'),pending['id'])
        if pending.get('feedback_result') is ok:return
        pending['feedback_result']=ok
        context=dict(pending.get('feedback',{}),id=pending['id'],route=self.view['route'],
                     delivered=any(row['success'] for row in self.view['deliveries'].values()),backed_up=self.view['backed_up'],phase=self.view['phase'],
                     deliveries=self.view['deliveries'],cloud_transfer=self.view['cloud_transfer'],local_copy=self.view['local_copy'])
        self.copy_result.emit(ok,context)

    def _prepare_local(self):
        pending=self._local_retry
        if not pending or not self._active():return
        if not self.scheduler.connected:self._retry_on_reconnect=True;self.schedule.request();return
        try:
            if pending['sequence'] is None:pending['sequence']=self._next_sequence()
        except OSError:
            self._copy_result(pending,False);self._pending=None;self.retry.start();return
        self._local_retry=None;self._pending=pending
        def prepared(result):
            if not self._valid_task(pending):return
            if isinstance(result,VirtualClipboardError) and result.code=='REMOTE_FILE_PLACEHOLDER':
                self._unavailable_task(pending);return
            if isinstance(result,ClipboardSizeLimit):
                self.file_jobs.update(pending,'unavailable')
                self._copy_result(pending,False,phase='size_limit');self._clear_task(pending);return
            if result is None:
                self.file_jobs.update(pending,'unavailable');self._copy_result(pending,False);self._clear_task(pending);return
            keys=result.pop('_content_keys',None)
            if keys is not None and self.view.get('id')==pending['id']:
                self._current_content_keys=keys;self._remember_keys(keys)
            pending['package']=result;pending['prepared']=True;self.file_jobs.prepared(pending)
            self._set_cloud_progress(pending['id'],0,result['size'],'uploading');self._stage(pending)
        value=pending.pop('value');quick=inline_text(value)
        if quick is not None:prepared(quick);return
        try:directory=self._directory()
        except OSError:
            self._copy_result(pending,False)
            pending['value']=value;self._local_retry=pending;self._pending=None;self.retry.start();return
        def prepare(cancel):
            try:
                result=package(value,directory,cancel)
                if result is not None:
                    keys=content_keys(value,read_files=True,cancel=cancel)
                    raw=result.get('raw_file')
                    if raw and ('bytes',raw['size'],bytes.fromhex(raw['sha256'])) not in keys:
                        raise OSError('Clipboard file changed after packaging')
                    result['_content_keys']=keys
                return result
            except ClipboardSizeLimit as limit:return limit
            except VirtualClipboardError as error:return error
        pending['_job']=self._work(prepare,prepared)

    def _stage(self,pending):
        if not self._valid_task(pending):return
        if not self.scheduler.connected:self._retry_on_reconnect=True;self.schedule.request();return
        if time.time()-pending['created_at']>=86400:self._clear_task(pending);self.refresh();return
        self._copy_timestamp(pending['intent'])
        def staged(result,error):
            if not self._valid_task(pending):return
            if error:
                if getattr(error,'code','')=='CLIPBOARD_REPLACED':self._resolve_replaced(pending,error);return
                if use_inline and getattr(error,'status',0) in (400,413):
                    pending['inline_fallback']=True;self._stage(pending);return
                self._task_error(pending,error)
                if getattr(error,'status',0)==409 and not self.file_jobs.held(pending):self._clear_task(pending);self.refresh()
                return
            ready=result.get('clipboard',{}) if isinstance(result,dict) else {}
            pending['shared_files']=bool(isinstance(result,dict) and result.get('shared_files'))
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
        if not self.scheduler.connected:self._retry_on_reconnect=True;self.schedule.request();return
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
            self._work(materialize,materialized);return
        pending['uploading']=True
        self._priority_flags(waiting=False,sending=True)
        self.file_jobs.update(pending,'uploading')
        def uploaded(result,error):
            if not self._valid_task(pending):return
            pending['uploading']=False
            if error:
                if getattr(error,'code','')=='CLIPBOARD_REPLACED':self._resolve_replaced(pending,error);return
                self._task_error(pending,error)
                if getattr(error,'status',0) in (403,409) and not self.file_jobs.held(pending):self._clear_task(pending);self.refresh()
                return
            head=result.get('clipboard',{}) if isinstance(result,dict) else {}
            self._task_ready(pending,head)
        headers={'X-Clipboard-Kind':item['kind'],'X-Content-SHA256':item['sha256']};options={}
        raw_file=item.get('raw_file') if pending.get('shared_files') else None
        if raw_file:
            descriptor={key:raw_file[key] for key in ('name','size','sha256','prefix','suffix')}
            headers['X-Clipboard-Raw-File']=json.dumps(descriptor,separators=(',',':'),ensure_ascii=True)
            options.update(source_offset=raw_file['offset'],source_size=raw_file['size'])
        def progress(sent,total):
            if not self._valid_task(pending):return
            if sent>pending.get('_last_priority_bytes',0):
                pending['_last_priority_bytes']=sent;self._priority_flags()
            count=min(raw_file['size'],max(0,sent)) if raw_file else sent
            self.file_jobs.update(pending,'verifying' if sent>=total else 'uploading',count)
            self._set_cloud_progress(pending['id'],sent,total,'verifying' if total>0 and sent>=total else 'uploading')
            if total>0 and sent>=total:self._priority_flags(waiting=True,sending=False)
        self._request('PUT',f'/v1/clipboard/{pending["id"]}/content',uploaded,source=item['path'],headers=headers,
                      progress=progress,_task=pending,**options)

    def _retry(self):
        if not self._active():return
        if self._prepared_receive is not None:
            identity=self._prepared_receive['head']['id'];self._apply_received()
            if self._prepared_receive is not None or self._seen==identity:return
        if not self.scheduler.connected:
            self._retry_on_reconnect=self._pending is not None or self._local_retry is not None
            self.schedule.request();return
        if self._local_retry:self._prepare_local();return
        if self._pending:
            if self._pending.get('replacement_deadline'):
                self._resolve_replaced(self._pending,self._pending['replacement_error']);return
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
        self._observe_cloud_head(head)
        previous=(self._pushed_head or {}).get('clipboard',{})
        if head['version']>previous.get('version',0):self._pushed_head=value
        if self._fetching and not self._pending and head['version']>self._receiving_version:
            # A different committed copy supersedes a stale body download;
            # retain the last usable native clipboard until this one verifies.
            self._epoch+=1;self._generation+=1
            self._cancel_replaced_work()
            self._fetching=False;self._priority_flags(receiving=False)
        self.refresh()

    def refresh(self):
        if not self._active():return
        if not self.scheduler.connected:self.schedule.request();return
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
            pending=self._pending
            if generation==self._generation and pending is not None and pending.get('replacement_deadline'):
                self._resolve_replaced(pending,pending['replacement_error'],snapshot={'clipboard':head})
                self._finish_refresh();return
            if not isinstance(head,dict) or generation!=self._generation:self._finish_refresh();return
            identity=head.get('id','')
            if not isinstance(identity,str) or len(identity)!=36 or any(c not in '0123456789abcdef-' for c in identity):self._finish_refresh();return
            if self._rejected_head==(identity,head.get('sha256')):self._finish_refresh();return
            if identity==self._seen:
                self.deliveries.committed(head,result.get('delivery') if isinstance(result,dict) else None)
                self._finish_refresh();return
            if type(head.get('version')) is int and head['version']<=self._applied_version:self._finish_refresh();return
            if self._local_fence is not None and not self._head_after_local_copy(head,self._local_fence):self._finish_refresh();return
            if not head.get('ready'):
                # Pending/failed/oversize copies never erase a usable native
                # clipboard. Expiry is owned by the cache lease, not polling.
                if head.get('expired') and type(head.get('version')) is int:
                    self._applied_version=max(self._applied_version,head['version'])
                self._finish_refresh();return
            kind,size,sha=head.get('kind'),head.get('size'),head.get('sha256')
            if kind not in ('text','image','files') or type(size) is not int or not 0<size<(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES) or not isinstance(sha,str) or len(sha)!=64:self._finish_refresh();return
            is_inline=kind=='text' and isinstance(head.get('inline'),str) and size<=32768
            cached=self._prepared_receive
            if cached is not None and (cached['head']['id'],cached['head']['sha256'])==(identity,sha):
                self.deliveries.committed(head,result.get('delivery') if isinstance(result,dict) else None)
                self._apply_received();self._finish_refresh();return
            try:directory=self.cache if is_inline else self._directory()
            except OSError:
                self._finish_refresh();self.retry.start();return
            path=directory/'download.bin';self._fetching=True
            self._receiving_version=head_version(head)
            self._priority_flags(receiving=True)
            def prepared(value):
                if generation!=self._generation:self._finish_refresh();return
                self._priority_flags(receiving=False)
                if isinstance(value,VirtualClipboardError) and value.code=='REMOTE_FILE_PLACEHOLDER':
                    self._reject_control_head(head);self._finish_refresh();return
                if value is None:self._finish_refresh();self.retry.start();return
                # The authenticated head already commits this immutable body.
                # The observed version, native-copy generation and local intent
                # are rechecked at apply time; another GET adds an RTT without
                # making the following native write atomic with a remote copy.
                def applied():
                    self.deliveries.received(head,'cloud')
                    self.deliveries.committed(head,result.get('delivery') if isinstance(result,dict) else None)
                self._accept_received(head,value,directory,valid=lambda:self._cloud_receive_valid(head,generation),applied=applied)
                self._finish_refresh()
            def downloaded(result,error):
                if generation!=self._generation:self._finish_refresh();return
                self._priority_flags(receiving=False)
                if error:self._finish_refresh();self.retry.start();return
                def decode(_):
                    try:
                        value=unpack(kind,path,directory)
                        content_keys(value,read_files=True,cancel=_)
                        return value
                    except VirtualClipboardError as error:return error
                self._work(decode,prepared)
            if is_inline:
                try:value=unpack_inline(head)
                except (ValueError,TypeError,UnicodeError):
                    self._finish_refresh();self.retry.start();return
                prepared(value)
            else:
                def cloud_body():
                    last_received=0
                    def progress(received,total):
                        nonlocal last_received
                        if generation==self._generation and received>last_received:
                            last_received=received;self._priority_flags()
                    self._request('GET',f'/v1/clipboard/{identity}/content',downloaded,destination=path,expected={'size':size,'sha256':sha},progress=progress)
                cloud_body()
        if self._pushed_head:
            pushed,self._pushed_head=self._pushed_head,None;latest(pushed,None)
        else:self._request('GET','/v1/clipboard',latest)

    def _finish_refresh(self):
        self._fetching=False
        self._priority_flags(receiving=False)
        if self._refresh_again:
            self._refresh_again=False;self.schedule.request()

    def _apply(self,value,directory,*,identity=None):
        if not self._native_accessible(force=True):return False
        kind,data=value
        from virtual_clipboard import remote_placeholder
        if kind=='files' and any(remote_placeholder(ref) for ref in data):return False
        actual=signature(self._snapshot())
        if actual==signature(value):
            self._adopt_native(actual,directory,identity)
            return True
        mime=QMimeData()
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
            self.clipboard.setMimeData(mime)
            actual=signature(self._snapshot())
            if kind=='image' and data.get('raw') and data.get('mime'):
                native=self.clipboard.mimeData()
                matches=native is not None and bytes(native.data(data['mime']))==data['raw']
            else:matches=actual==signature(value)
            if not matches:
                native=self.clipboard.mimeData()
                if native is not None and bytes(native.data(SYNC_MARKER))==(identity or self._seen).encode('ascii'):
                    # Our delayed native formats are not a new Copy intent.
                    # Delivery is still unconfirmed until a readable retry.
                    observed=actual if self._snapshot_available else signature(value)
                    self._ignored=observed;self._last_local_signature=observed;self._last_sequence=self._sequence()
                return False
            self._adopt_native(actual,directory,identity)
            return True
        except (OSError,RuntimeError):return False
        finally:self._applying=False

    def _adopt_native(self,actual,directory,identity):
        # A verified receive replaces startup observation, including its delayed result.
        self._native_baseline=None
        self._last_copy_observation=time.monotonic()
        activity=self._read_copy_activity()
        if activity is not None:self._copy_input_stamp=activity[0]
        self._local_fence=None;self._ignored=actual
        self._last_sequence=self._sequence()
        self._last_local_signature=actual;self._current_directory=directory
        if sys.platform=='darwin':self._mac_changed(initial=True)

    def _clear_remote_clipboard(self):
        previous=self._applying;self._applying=True
        try:
            self.clipboard.clear();self._ignored=None;self._current_directory=None
            self._last_local_signature=signature(self._snapshot())
            self._last_sequence=self._sequence()
            if sys.platform=='darwin':self._mac_changed(initial=True)
        finally:self._applying=previous

    def _cleanup(self):
        self._update_schedule()
        # Native applications may still hold the last file clipboard's paths
        # after Paste. Retain local downloads for a day, never delete originals.
        if self.closed:return
        # An unreadable pasteboard is not an empty one. Its native file URLs
        # must stay usable across a denied permission, including after restart.
        if not self._native_accessible(force=True):return
        now=time.time();keep=[]
        try:native=self._snapshot()
        except (OSError,RuntimeError):return
        if not self._snapshot_available:return
        native_paths=[Path(path).resolve() for path in native[1]] if native and native[0]=='files' else []
        for directory,created in self._directories:
            if directory in self.file_jobs.protected_directories():keep.append((directory,created));continue
            if now-created<86400:keep.append((directory,created));continue
            if any(path.is_relative_to(directory.resolve()) for path in native_paths):
                keep.append((directory,created));continue
            if directory==self._current_directory:
                if self._ignored is not None and signature(self._snapshot())==self._ignored:
                    keep.append((directory,created));continue
                self._current_directory=None
            if directory.resolve().is_relative_to(self.cache.resolve()) and not directory.is_symlink() and not directory.is_junction():
                self._work(lambda _,path=directory:shutil.rmtree(path,ignore_errors=True),lambda _:None)
        self._directories=keep

    def close(self):
        if self.closed:return
        self.closed=True;self._priority_timer.stop();self.retry.stop();self.mac_poll.stop();self.change_debounce.stop()
        if self._native_reader is not None:self._native_reader.close()
        self._account_changed(force=True)
        self.schedule.close()
        self.scheduler.connection_changed.disconnect(self._connection_changed)
        if self._owns_scheduler:self.scheduler.close()
        try:self.clipboard.dataChanged.disconnect(self._schedule_change)
        except RuntimeError:pass
