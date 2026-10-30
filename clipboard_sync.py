"""Silent, account-wide latest clipboard. Reading never consumes the content."""
from __future__ import annotations
import base64,hashlib,json,os,shutil,struct,sys,tempfile,threading,time,zipfile
from pathlib import Path,PurePosixPath
from uuid import uuid4
from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,Signal,QMimeData,QUrl,QBuffer,QIODevice,Qt
from PySide6.QtGui import QImage,QPixmap,QImageReader
from PySide6.QtWidgets import QApplication
from platform_native import native_input_available

MAX_BYTES=20*1024*1024
MAX_ARCHIVE_BYTES=MAX_BYTES+1024*1024 # metadata overhead, not additional user file bytes
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
    if mime.hasText() and mime.text():return ('text',dict(text=mime.text(),html=mime.html() if mime.hasHtml() else ''))
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


def package(value,directory,cancel=None):
    """Worker-only file/image encoding. ZIP uses no compression or base64."""
    cancel=cancel or threading.Event();path=Path(directory)/'content.bin'
    if value is None:return None
    kind,data=value
    if kind=='text':
        text=data['text'].encode('utf8');html=data.get('html','').encode('utf8')
        if len(text)+len(html)>=MAX_BYTES:return None
        # No JSON escaping inflation for control characters, line endings or
        # near-limit text. The original UTF-8 and HTML remain lossless.
        blob=TEXT_MAGIC+struct.pack('!I',len(text))+text+html
        path.write_bytes(blob)
    elif kind=='image':
        if 'raw' in data:
            if len(data['raw'])>=MAX_BYTES:return None
            path.write_bytes(data['raw'])
        else:
            buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not data['image'].save(buffer,'PNG') or buffer.size()>=MAX_BYTES:return None
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
                if total>=MAX_BYTES:return None
                entries.append((child,name))
        written=0
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as archive:
            for child,name in entries:
                if cancel.is_set():return None
                info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
                if name.endswith('/'):archive.writestr(info,b'');continue
                before=child.stat();copied=0
                with child.open('rb') as source,archive.open(info,'w') as target:
                    while chunk:=source.read(256*1024):
                        if cancel.is_set():return None
                        written+=len(chunk)
                        copied+=len(chunk)
                        if written>=MAX_BYTES:return None
                        target.write(chunk)
                after=child.stat()
                if copied!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return None
                if path.stat().st_size>=MAX_ARCHIVE_BYTES:return None
    size=path.stat().st_size
    if not 0<size<(MAX_ARCHIVE_BYTES if kind in ('files','text') else MAX_BYTES):return None
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(kind=kind,path=str(path),size=size,sha256=digest)


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
    view_changed=Signal(dict)

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

    def __init__(self,account,events,base,parent=None,*,enabled=True,clipboard=None):
        super().__init__(parent);self.account=account;self.api=account.api;self.clipboard=clipboard or QApplication.clipboard()
        self._is_native=clipboard is None or clipboard is QApplication.clipboard()
        self._sequence=native_sequence if self._is_native else lambda:None
        self.enabled=enabled;self.closed=False;self._epoch=0;self._generation=0;self._applying=False
        self._clock_anchor=None;self._observed_clock=0;self._local_fence=None;self._local_retry=None
        self._clock_path=Path(base)/'clipboard-clock.json';self._copy_clock=0
        try:self._copy_clock=int(json.loads(self._clock_path.read_text(encoding='utf8'))['sequence'])
        except (OSError,ValueError,KeyError,TypeError):pass
        self._identity=self._session_identity()
        self._pushed_head=None
        self._prepared_receive=None
        self._view_revision=0;self.view=dict(value=snapshot(self.clipboard),phase='local',id='',revision=0)
        self._applied_version=0;self._receiving_version=0
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(2)
        self._ignored=None;self._ignored_sequence=None;self._last_sequence=object();self._last_local_signature=object();self._seen='';self._keys=set();self._jobs={};self._pending=None;self._fetching=False;self._refresh_again=False
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
        self.clipboard.dataChanged.connect(self._schedule_change)
        self._last_local_signature=signature(snapshot(self.clipboard));self._last_sequence=self._sequence()
        account.account_changed.connect(self._account_changed)
        events.clipboard_changed.connect(self.refresh)
        payload=getattr(events,'clipboard_payload',None)
        if payload is not None:payload.connect(self._pushed)
        events.socket.connected.connect(self.refresh)
        if enabled:
            QTimer.singleShot(0,self._cleanup)
            self.poll.start()
            if sys.platform=='darwin' and self._is_native:self._mac_changed(initial=True);self.mac_poll.start()
            QTimer.singleShot(0,self.refresh)

    def _mac_changed(self,initial=False):
        if not self.enabled or self.closed:return
        try:
            from AppKit import NSPasteboard
            count=NSPasteboard.generalPasteboard().changeCount()
            changed=self._mac_count is not None and count!=self._mac_count;self._mac_count=count
            if changed and not initial:self._schedule_change()
        except (ImportError,RuntimeError):pass

    def _active(self):return self.enabled and not self.closed and self.account.signed_in

    def set_enabled(self,enabled):
        enabled=bool(enabled)
        if self.closed or self.enabled==enabled:return
        self.enabled=enabled;self.change_debounce.stop();self._account_changed(force=True)
        if enabled:
            self.poll.start()
            if sys.platform=='darwin' and self._is_native:self._mac_changed(initial=True);self.mac_poll.start()
        else:self.poll.stop();self.mac_poll.stop()

    def _schedule_change(self):
        if not self._active() and not self.closed:
            self._set_view(snapshot(self.clipboard),'local');return
        if self._active() and not self._applying:
            sequence=self._sequence()
            if sequence is not None:
                if sequence==self._last_sequence:return
            elif signature(snapshot(self.clipboard))==self._last_local_signature:return
            from PySide6.QtGui import QCursor
            point=QCursor.pos();self._copy_point=(point.x(),point.y())
            # Stop an in-flight remote read before the native notification's
            # coalescing delay; the user's newer local copy always wins.
            self._copy_notified_mono=time.monotonic();self._generation+=1;self.change_debounce.start()

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

    def _tick(self):self._cleanup();self.refresh()

    def _request(self,method,path,callback,**kwargs):
        epoch=self._epoch;holder=[];completed=False;started=time.monotonic()
        def done(result,error):
            nonlocal completed
            completed=True
            if holder:self._keys.discard(holder[0])
            if not self.closed and epoch==self._epoch:
                self._calibrate(result,started);callback(result,error)
        key=self.api.request(method,path,done,timeout_ms=300000 if path.endswith('/content') else 12000,
                             priority='high',**kwargs)
        holder.append(key)
        if key is not None and not completed:self._keys.add(key)
        return key

    def _session_identity(self):
        account=getattr(self.account,'account',None)
        return ((account or {}).get('id',''),getattr(self.account,'device_id',''),getattr(self.api,'token','')) if self.account.signed_in else None

    def _account_changed(self,*_,force=False):
        identity=self._session_identity()
        if not force and identity==self._identity:
            self.refresh();return
        self._identity=identity;self._ignored=None;self._local_retry=None;self._local_fence=None
        self._pushed_head=None
        self._prepared_receive=None
        self._applied_version=0;self._receiving_version=0
        self._clock_anchor=None;self._observed_clock=0
        # A session switch is not a Copy action. Establish a native baseline so
        # a delayed notification cannot export the previous account's content.
        self._last_local_signature=signature(snapshot(self.clipboard));self._last_sequence=self._sequence()
        if sys.platform=='darwin' and not self.closed:self._mac_changed(initial=True)
        self._epoch+=1;self._generation+=1;self._pending=None;self._seen='';self._fetching=False;self.retry.stop()
        self._priority_flags(waiting=False,receiving=False)
        for key in tuple(self._keys):self.api.cancel(key)
        self._keys.clear()
        for job in self._jobs:job.cancelled.set()
        if not self.closed:self._set_view(snapshot(self.clipboard),'local')
        if self._active():QTimer.singleShot(0,self.refresh)

    def _directory(self):
        path=Path(tempfile.mkdtemp(prefix='clip-',dir=self.cache));created=time.time()
        try:(path/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':created}),encoding='utf8')
        except OSError:
            shutil.rmtree(path,ignore_errors=True);raise
        self._directories.append((path,created));return path

    def _work(self,action,callback):
        job=_Job(action);self._jobs[job]=callback;job.signals.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        self.pool.start(job,1)

    def _finished(self,job,result):
        callback=self._jobs.pop(job,None)
        if callback and not self.closed and not job.cancelled.is_set():callback(result)
        self._cleanup()

    def _changed(self):
        if not self._active() or self._applying:return
        value=snapshot(self.clipboard);sig=signature(value);sequence=self._sequence()
        if value is None and sequence is not None and sequence!=self._last_sequence:
            # Some native owners announce a new sequence before delayed data
            # is readable. Retry briefly without consuming its copy event.
            previous,attempts=getattr(self,'_native_read_retry',(None,0))
            attempts=attempts+1 if previous==sequence else 1
            self._native_read_retry=(sequence,attempts)
            if attempts<=5:self.change_debounce.start(100);return
        else:self._native_read_retry=(None,0)
        if self._ignored is not None and sig==self._ignored and (sequence is None or sequence==self._ignored_sequence):return
        if sequence is not None:
            if sequence==self._last_sequence:return
            self._last_sequence=sequence
        elif sig==self._last_local_signature:return
        # A private MIME marker survives delayed Qt/Cocoa notifications. A new
        # native copy has a different sequence and is allowed to replace it.
        mime=self.clipboard.mimeData()
        if (self._ignored is not None and mime and bytes(mime.data(SYNC_MARKER))==self._seen.encode('ascii')
                and sig==self._ignored and (sequence is None or sequence==self._ignored_sequence)):return
        self._current_directory=None;self.retry.stop()
        self._last_local_signature=sig
        self._local_fence=dict(mono=getattr(self,'_copy_notified_mono',time.monotonic()),at=None,floor=self._observed_clock)
        self._copy_timestamp(self._local_fence)
        self._ignored=None;self._generation+=1;self._epoch+=1;generation=self._generation
        # Superseding a local upload prevents its late completion restoring it.
        for key in tuple(self._keys):self.api.cancel(key)
        self._keys.clear();self._fetching=False
        self._pending=None;self._priority_flags(waiting=False,receiving=False)
        for job in self._jobs:job.cancelled.set()
        self._local_retry=None
        self._prepared_receive=None
        if value is None:self._set_view(None,'local');self._pending=None;return
        self._local_retry=dict(id=str(uuid4()),sequence=None,generation=generation,staged=False,package=None,
                               prepared=False,created_at=time.time(),intent=self._local_fence,value=value,
                               feedback=self._copy_context(value))
        self._set_view(value,'uploading',self._local_retry['id'])
        self._prepare_local()

    def _set_view(self,value,phase,identity=''):
        if value is not self.view.get('value'):self._view_revision+=1
        self.view=dict(value=value,phase=phase,id=identity,revision=self._view_revision)
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
        return dict(id='',point=point,fullscreen=fullscreen)

    def _copy_result(self,pending,ok):
        if self.view.get('id')==pending['id']:
            self._set_view(self.view['value'],'synced' if ok else 'error',pending['id'])
        if pending.get('feedback_result') is ok:return
        pending['feedback_result']=ok
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
        def prepared(result):
            if self._pending is not pending:return
            if result is None:self._copy_result(pending,False);self._pending=None;return
            pending['package']=result;pending['prepared']=True;self._stage(pending)
        value=pending.pop('value');quick=inline_text(value)
        if quick is not None:prepared(quick);return
        try:directory=self._directory()
        except OSError:
            self._copy_result(pending,False)
            pending['value']=value;self._local_retry=pending;self._pending=None;self.retry.start();return
        self._work(lambda cancel:package(value,directory,cancel),prepared)

    def _stage(self,pending):
        if not self._active() or self._pending is not pending:return
        if time.time()-pending['created_at']>=86400:self._pending=None;self.refresh();return
        if self._copy_timestamp(pending['intent']) is None:
            def calibrated(result,error):
                if self._pending is not pending:return
                if error or self._clock_anchor is None:self._copy_result(pending,False);self._priority_flags(waiting=True);self.retry.start()
                else:self._stage(pending)
            self._request('GET','/v1/clipboard',calibrated);return
        def staged(result,error):
            if self._pending is not pending:return
            if error:
                if use_inline and getattr(error,'status',0) in (400,413):
                    pending['inline_fallback']=True;self._stage(pending);return
                self._copy_result(pending,False)
                if getattr(error,'status',0)==409:self._pending=None;self.refresh()
                else:self._priority_flags(waiting=True);self.retry.start()
                return
            ready=result.get('clipboard',{}) if isinstance(result,dict) else {}
            item=pending['package']
            if (ready.get('id')==pending['id'] and ready.get('ready') and
                    ready.get('sha256')==item['sha256'] and ready.get('size')==item['size']):
                self._seen=pending['id'];self._applied_version=max(self._applied_version,head_version(ready))
                self._copy_result(pending,True)
                self._pending=None;self._cleanup()
                if self._refresh_again:QTimer.singleShot(0,self.refresh)
                return
            pending['staged']=True;self._upload(pending)
        data={'id':pending['id'],'protocol':2,'sequence':pending['sequence'],'copied_at':pending['intent']['at']}
        use_inline=bool(pending['package'].get('inline') and not pending.get('inline_fallback'))
        if use_inline:data['inline']=pending['package']['inline']
        self._request('POST','/v1/clipboard',staged,data=data)

    def _upload(self,pending):
        if self._pending is not pending or not pending['staged'] or not pending['prepared'] or pending.get('uploading'):return
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
                if self._pending is not pending:return
                pending['materializing']=False
                if path:item['path']=path;self._upload(pending)
                else:self._priority_flags(waiting=True);self.retry.start()
            self._work(materialize,materialized);return
        pending['uploading']=True
        def uploaded(result,error):
            if self._pending is not pending:return
            pending['uploading']=False
            if error:
                self._copy_result(pending,False)
                if getattr(error,'status',0) in (403,409):self._pending=None;self.refresh()
                else:self._priority_flags(waiting=True);self.retry.start()
                return
            self._seen=pending['id'];self._copy_result(pending,True);self._pending=None;self._cleanup()
            head=result.get('clipboard',{}) if isinstance(result,dict) else {}
            self._applied_version=max(self._applied_version,head_version(head))
            if self._refresh_again:QTimer.singleShot(0,self.refresh)
        self._request('PUT',f'/v1/clipboard/{pending["id"]}/content',uploaded,source=item['path'],
            headers={'X-Clipboard-Kind':item['kind'],'X-Content-SHA256':item['sha256']})

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
        previous=(self._pushed_head or {}).get('clipboard',{})
        if head['version']>previous.get('version',0):self._pushed_head=value
        if self._fetching and not self._pending and head['version']>self._receiving_version:
            # A new committed small copy supersedes a stale body download;
            # retain the last usable native clipboard until this one verifies.
            self._epoch+=1;self._generation+=1
            for key in tuple(self._keys):self.api.cancel(key)
            self._keys.clear()
            for job in self._jobs:job.cancelled.set()
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
            if identity==self._seen:self._finish_refresh();return
            if type(head.get('version')) is int and head['version']<=self._applied_version:self._finish_refresh();return
            if self._local_fence is not None:
                fence=self._copy_timestamp(self._local_fence)
                if fence is None or head.get('copied_at',head.get('at',0))<=fence:self._finish_refresh();return
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
                        if signature(snapshot(self.clipboard))!=self._last_local_signature:
                            self._schedule_change()
                        else:
                            if self._apply(value,directory,identity=identity):
                                self._seen=identity
                                self._applied_version=max(self._applied_version,head_version(head))
                                self._set_view(value,'synced',identity)
                                self._prepared_receive=None
                            else:self._set_view(value,'error',identity);self.retry.start(5000)
                    else:self._refresh_again=True
                    self._finish_refresh()
                self._request('GET','/v1/clipboard',confirmed)
            def downloaded(result,error):
                if error:self._finish_refresh();self.retry.start();return
                if generation!=self._generation:self._finish_refresh();return
                self._work(lambda _:unpack(kind,path,directory),prepared)
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
            self.clipboard.setMimeData(mime)
            actual=signature(snapshot(self.clipboard))
            if kind=='image' and data.get('raw') and data.get('mime'):
                native=self.clipboard.mimeData()
                matches=native is not None and bytes(native.data(data['mime']))==data['raw']
            else:matches=actual==signature(value)
            if not matches:
                native=self.clipboard.mimeData()
                if native is not None and bytes(native.data(SYNC_MARKER))==(identity or self._seen).encode('ascii'):
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
            self.clipboard.clear();self._ignored=None;self._current_directory=None
            self._last_local_signature=signature(snapshot(self.clipboard))
            self._ignored_sequence=self._sequence();self._last_sequence=self._ignored_sequence
            if sys.platform=='darwin':self._mac_changed(initial=True)
        finally:self._applying=previous

    def _cleanup(self):
        # Native applications may still hold the last file clipboard's paths
        # after Paste. Retain local downloads for a day, never delete originals.
        if self.closed:return
        now=time.time();keep=[]
        native=snapshot(self.clipboard)
        native_paths=[Path(path).resolve() for path in native[1]] if native and native[0]=='files' else []
        for directory,created in self._directories:
            if now-created<86400:keep.append((directory,created));continue
            if any(path.is_relative_to(directory.resolve()) for path in native_paths):
                keep.append((directory,created));continue
            if directory==self._current_directory:
                if self._ignored is not None and signature(snapshot(self.clipboard))==self._ignored:
                    keep.append((directory,created));continue
                self._current_directory=None
            if directory.resolve().is_relative_to(self.cache.resolve()) and not directory.is_symlink() and not directory.is_junction():
                self._work(lambda _,path=directory:shutil.rmtree(path,ignore_errors=True),lambda _:None)
        self._directories=keep

    def close(self):
        if self.closed:return
        self.closed=True;self.poll.stop();self.retry.stop();self.mac_poll.stop();self.change_debounce.stop();self._account_changed(force=True)
        try:self.clipboard.dataChanged.disconnect(self._schedule_change)
        except RuntimeError:pass
