"""Silent, account-wide latest clipboard. Reading never consumes the content."""
from __future__ import annotations
import hashlib,json,os,shutil,sys,tempfile,threading,time,zipfile
from pathlib import Path,PurePosixPath
from uuid import uuid4
from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,Signal,QMimeData,QUrl,QBuffer,QIODevice,Qt
from PySide6.QtGui import QImage,QPixmap,QImageReader
from PySide6.QtWidgets import QApplication
from platform_native import native_input_available

MAX_BYTES=20*1024*1024
MAX_ARCHIVE_BYTES=MAX_BYTES+1024*1024 # metadata overhead, not additional user file bytes

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
        if not all(url.isLocalFile() for url in mime.urls()):return None
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
    if kind=='image':return (kind,hashlib.sha256(data['raw']).digest()) if 'raw' in data else (kind,data['image'].cacheKey())
    if kind=='files':
        versions=[]
        for path in data:
            try:info=Path(path).stat();versions.append((path,info.st_size,info.st_mtime_ns))
            except OSError:versions.append((path,None,None))
        return (kind,tuple(versions))
    return (kind,data['text'],data.get('html',''))


def package(value,directory,cancel=None):
    """Worker-only file/image encoding. ZIP uses no compression or base64."""
    cancel=cancel or threading.Event();path=Path(directory)/'content.bin'
    if value is None:return None
    kind,data=value
    if kind=='text':
        blob=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf8')
        if len(blob)>=MAX_BYTES:return None
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
    if not 0<size<(MAX_ARCHIVE_BYTES if kind=='files' else MAX_BYTES):return None
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(kind=kind,path=str(path),size=size,sha256=digest)


def unpack(kind,path,directory):
    path=Path(path)
    if path.stat().st_size>=(MAX_ARCHIVE_BYTES if kind=='files' else MAX_BYTES):raise ValueError('clipboard limit')
    if kind=='text':
        data=json.loads(path.read_text(encoding='utf8'))
        if not isinstance(data,dict) or not isinstance(data.get('text'),str) or not isinstance(data.get('html',''),str):raise ValueError('invalid text')
        return (kind,data)
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
    def __init__(self,account,events,base,parent=None,*,enabled=True,clipboard=None):
        super().__init__(parent);self.account=account;self.api=account.api;self.clipboard=clipboard or QApplication.clipboard()
        self.enabled=enabled;self.closed=False;self._epoch=0;self._generation=0;self._applying=False
        self._identity=self._session_identity()
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
        self.mac_poll=QTimer(self);self.mac_poll.setInterval(500);self.mac_poll.timeout.connect(self._mac_changed);self._mac_count=None
        self.clipboard.dataChanged.connect(self._changed)
        account.account_changed.connect(self._account_changed)
        events.clipboard_changed.connect(self.refresh)
        events.socket.connected.connect(self.refresh)
        if enabled:
            QTimer.singleShot(0,self._cleanup)
            self.poll.start()
            if sys.platform=='darwin':self._mac_changed(initial=True);self.mac_poll.start()
            QTimer.singleShot(0,self.refresh)

    def _mac_changed(self,initial=False):
        if not self.enabled or self.closed:return
        try:
            from AppKit import NSPasteboard
            count=NSPasteboard.generalPasteboard().changeCount()
            changed=self._mac_count is not None and count!=self._mac_count;self._mac_count=count
            if changed and not initial:self._changed()
        except (ImportError,RuntimeError):pass

    def _active(self):return self.enabled and not self.closed and self.account.signed_in

    def _tick(self):self._cleanup();self.refresh()

    def _request(self,method,path,callback,**kwargs):
        epoch=self._epoch;holder=[];completed=False
        def done(result,error):
            nonlocal completed
            completed=True
            if holder:self._keys.discard(holder[0])
            if not self.closed and epoch==self._epoch:callback(result,error)
        key=self.api.request(method,path,done,timeout_ms=300000 if path.endswith('/content') else 12000,**kwargs)
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
        self._identity=identity;self._ignored=None
        # A session switch is not a Copy action. Establish a native baseline so
        # a delayed notification cannot export the previous account's content.
        self._last_local_signature=signature(snapshot(self.clipboard));self._last_sequence=native_sequence()
        if sys.platform=='darwin' and not self.closed:self._mac_changed(initial=True)
        self._epoch+=1;self._generation+=1;self._pending=None;self._seen='';self._fetching=False;self.retry.stop()
        for key in tuple(self._keys):self.api.cancel(key)
        self._keys.clear()
        for job in self._jobs:job.cancelled.set()
        if self._active():QTimer.singleShot(0,self.refresh)

    def _directory(self):
        path=Path(tempfile.mkdtemp(prefix='clip-',dir=self.cache));created=time.time()
        (path/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':created}),encoding='utf8')
        self._directories.append((path,created));return path

    def _work(self,action,callback):
        job=_Job(action);self._jobs[job]=callback;job.signals.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(job)

    def _finished(self,job,result):
        callback=self._jobs.pop(job,None)
        if callback and not self.closed and not job.cancelled.is_set():callback(result)
        self._cleanup()

    def _changed(self):
        if not self._active() or self._applying:return
        value=snapshot(self.clipboard);sig=signature(value);sequence=native_sequence()
        if self._ignored is not None and sig==self._ignored and (sequence is None or sequence==self._ignored_sequence):return
        if sequence is not None:
            if sequence==self._last_sequence:return
            self._last_sequence=sequence
        elif sig==self._last_local_signature and (value is None or value[0]!='files'):return
        self._current_directory=None
        self._last_local_signature=sig
        self._ignored=None;self._generation+=1;self._epoch+=1;generation=self._generation
        # Superseding a local upload prevents its late completion restoring it.
        for key in tuple(self._keys):self.api.cancel(key)
        self._keys.clear();self._fetching=False
        for job in self._jobs:job.cancelled.set()
        self._pending=dict(id=str(uuid4()),generation=generation,staged=False,package=None,prepared=False,created_at=time.time())
        pending=self._pending;self._stage(pending)
        directory=self._directory()
        def prepared(result):
            if self._pending is not pending:return
            pending['package']=result;pending['prepared']=True;self._upload(pending)
        self._work(lambda cancel:package(value,directory,cancel),prepared)

    def _stage(self,pending):
        if not self._active() or self._pending is not pending:return
        if time.time()-pending['created_at']>=86400:self._pending=None;self.refresh();return
        def staged(result,error):
            if self._pending is not pending:return
            if error:
                if getattr(error,'status',0)==409:self._pending=None;self.refresh()
                else:self.retry.start()
                return
            pending['staged']=True;self._seen=pending['id'];self._upload(pending)
        self._request('POST','/v1/clipboard',staged,data={'id':pending['id']})

    def _upload(self,pending):
        if self._pending is not pending or not pending['staged'] or not pending['prepared']:return
        if time.time()-pending['created_at']>=86400:self._pending=None;self.refresh();return
        item=pending['package']
        if item is None:self._pending=None;return
        def uploaded(result,error):
            if self._pending is not pending:return
            if error:
                if getattr(error,'status',0) in (403,409):self._pending=None;self.refresh()
                else:self.retry.start()
                return
            self._pending=None;self._cleanup()
        self._request('PUT',f'/v1/clipboard/{pending["id"]}/content',uploaded,source=item['path'],
            headers={'X-Clipboard-Kind':item['kind'],'X-Content-SHA256':item['sha256']})

    def _retry(self):
        if not self._active():return
        if self._pending:
            if self._pending['staged']:self._upload(self._pending)
            else:self._stage(self._pending)
        else:self.refresh()

    def refresh(self):
        if not self._active() or self._pending:return
        if self._fetching:self._refresh_again=True;return
        self._fetching=True;generation=self._generation
        def latest(result,error):
            if error:self._finish_refresh();self.retry.start();return
            head=result.get('clipboard') if isinstance(result,dict) else None
            if not isinstance(head,dict) or generation!=self._generation:self._finish_refresh();return
            identity=head.get('id','')
            if not isinstance(identity,str) or len(identity)!=36 or any(c not in '0123456789abcdef-' for c in identity):self._finish_refresh();return
            if identity==self._seen:self._finish_refresh();return
            if head.get('device')==getattr(self.account,'device_id',''):self._seen=identity;self._finish_refresh();return
            if not head.get('ready'):
                if self._ignored is not None and signature(snapshot(self.clipboard))==self._ignored:
                    self._clear_remote_clipboard();self._cleanup()
                self._finish_refresh();return
            kind,size,sha=head.get('kind'),head.get('size'),head.get('sha256')
            if kind not in ('text','image','files') or type(size) is not int or not 0<size<(MAX_ARCHIVE_BYTES if kind=='files' else MAX_BYTES) or not isinstance(sha,str) or len(sha)!=64:self._finish_refresh();return
            directory=self._directory();path=directory/'download.bin';self._fetching=True
            def downloaded(result,error):
                if error:self._finish_refresh();self.retry.start();return
                if generation!=self._generation:self._finish_refresh();return
                def prepared(value):
                    if value is None or generation!=self._generation:self._finish_refresh();return
                    def confirmed(result,error):
                        current=result.get('clipboard') if isinstance(result,dict) else None
                        if error or generation!=self._generation:self._finish_refresh();return
                        if isinstance(current,dict) and current.get('id')==identity and current.get('ready'):
                            self._apply(value,directory);self._seen=identity
                        else:self._refresh_again=True
                        self._finish_refresh()
                    self._request('GET','/v1/clipboard',confirmed)
                self._work(lambda _:unpack(kind,path,directory),prepared)
            self._request('GET',f'/v1/clipboard/{identity}/content',downloaded,destination=path,expected={'size':size,'sha256':sha})
        self._request('GET','/v1/clipboard',latest)

    def _finish_refresh(self):
        self._fetching=False
        if self._refresh_again:
            self._refresh_again=False;QTimer.singleShot(0,self.refresh)

    def _apply(self,value,directory):
        kind,data=value;mime=QMimeData()
        if kind=='files':mime.setUrls([QUrl.fromLocalFile(path) for path in data])
        elif kind=='image':
            mime.setImageData(data['image'])
            if data.get('raw') and data.get('mime'):mime.setData(data['mime'],data['raw'])
        else:
            mime.setText(data['text'])
            if data.get('html'):mime.setHtml(data['html'])
        self._applying=True
        try:
            self.clipboard.setMimeData(mime);self._ignored=signature(snapshot(self.clipboard))
            self._ignored_sequence=native_sequence();self._last_sequence=self._ignored_sequence
            self._last_local_signature=self._ignored
            self._current_directory=directory
            if sys.platform=='darwin':self._mac_changed(initial=True)
        finally:self._applying=False

    def _clear_remote_clipboard(self):
        previous=self._applying;self._applying=True
        try:
            self.clipboard.clear();self._ignored=None;self._current_directory=None
            self._last_local_signature=signature(snapshot(self.clipboard))
            self._ignored_sequence=native_sequence();self._last_sequence=self._ignored_sequence
            if sys.platform=='darwin':self._mac_changed(initial=True)
        finally:self._applying=previous

    def _cleanup(self):
        # Native applications may still hold the last file clipboard's paths
        # after Paste. Retain local downloads for a day, never delete originals.
        if self.closed:return
        now=time.time();keep=[]
        for directory,created in self._directories:
            if now-created<86400:keep.append((directory,created));continue
            if directory==self._current_directory:
                self._current_directory=None
                if self._ignored is not None and signature(snapshot(self.clipboard))==self._ignored:
                    self._clear_remote_clipboard()
            if directory.resolve().is_relative_to(self.cache.resolve()) and not directory.is_symlink() and not directory.is_junction():
                self._work(lambda _,path=directory:shutil.rmtree(path,ignore_errors=True),lambda _:None)
        self._directories=keep

    def close(self):
        if self.closed:return
        self.closed=True;self.poll.stop();self.retry.stop();self.mac_poll.stop();self._account_changed(force=True)
        try:self.clipboard.dataChanged.disconnect(self._changed)
        except RuntimeError:pass
