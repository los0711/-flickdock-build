"""Bounded Windows clipboard reads; delayed OLE producers never enter the UI."""
from __future__ import annotations
import ctypes,json,os,re,shutil,stat,sys,time
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QObject,QProcess,QThreadPool,QTimer,Signal,Qt
from sync_files import DiskWork

READ_TIMEOUT_MS=3000
VIRTUAL_READ_TIMEOUT_MS=45000
MAX_PAYLOAD_BYTES=20*1024*1024
MAX_METADATA_BYTES=MAX_PAYLOAD_BYTES+65536
DIRECTORY=re.compile(r'clip-read-[a-f0-9]{32}\Z')


def foreign_clipboard_lock():
    from ctypes import wintypes
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.GetOpenClipboardWindow.restype=wintypes.HWND
    api.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    window=api.GetOpenClipboardWindow()
    if not window:return False
    pid=wintypes.DWORD()
    if not api.GetWindowThreadProcessId(window,ctypes.byref(pid)):raise ctypes.WinError()
    return pid.value!=os.getpid()


def _read_result(directory,cancel):
    path=directory/'result.json'
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size>MAX_METADATA_BYTES:raise ValueError('Invalid clipboard worker result')
    with path.open('rb') as stream:raw=stream.read(MAX_METADATA_BYTES+1)
    if len(raw)>MAX_METADATA_BYTES:raise ValueError('Invalid clipboard worker result')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Invalid clipboard worker result')
    if value.get('ok') is not True:return value
    sequence=value.get('sequence')
    if type(sequence) is not int or not 0<=sequence<=0xffffffff:raise ValueError('Invalid clipboard sequence')
    kind=value.get('kind');data=value.get('data')
    if kind=='text':
        if not isinstance(data,dict) or not isinstance(data.get('text'),str) or not isinstance(data.get('html',''),str):raise ValueError('Invalid clipboard text')
    elif kind=='files':
        if not isinstance(data,list) or not data or len(data)>1 or not all(isinstance(item,str) and Path(item).is_absolute() for item in data):raise ValueError('Invalid clipboard files')
    elif kind=='image':
        from PySide6.QtGui import QImage
        raw=directory/'image.raw'
        if raw.exists():
            if raw.is_symlink() or not stat.S_ISREG(raw.stat().st_mode) or raw.stat().st_size>=MAX_PAYLOAD_BYTES:raise ValueError('Invalid clipboard image bytes')
            encoded=raw.read_bytes();image=QImage.fromData(encoded)
            data={'image':image,'raw':encoded,'mime':value['mime']}
        else:
            path=directory/'image.png'
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size>=MAX_PAYLOAD_BYTES:raise ValueError('Invalid clipboard image bytes')
            image=QImage(str(path));data={'image':image}
        if image.isNull():raise ValueError('Invalid clipboard image')
    elif kind is not None:raise ValueError('Unknown clipboard payload')
    if cancel.is_set():raise InterruptedError()
    return dict(ok=True,sequence=sequence,value=(kind,data) if kind else None)


def _store_image(directory,data):
    # Native producers can expose encoded bytes without an imageData bitmap.
    # Preserve those bytes rather than transcoding a JPEG or PNG for transport.
    raw=data.get('raw')
    if raw:
        if len(raw)>=MAX_PAYLOAD_BYTES:raise ValueError('Clipboard image too large')
        (directory/'image.raw').write_bytes(raw)
    else:
        path=directory/'image.png'
        if not data['image'].save(str(path),'PNG'):raise ValueError('Cannot store clipboard image')
        if path.stat().st_size>=MAX_PAYLOAD_BYTES:raise ValueError('Clipboard image too large')
    return data.get('mime','')


class NativeClipboardReader(QObject):
    ready=Signal(object,object,object)
    failed=Signal(str)

    def __init__(self,cache,sequence,parent=None):
        super().__init__(parent);self.cache=Path(cache).resolve(strict=True);self.sequence=sequence
        self.closed=False;self.process=None;self.loading=False;self.jobs=set()
        self.cached_sequence=None;self.value=None;self.wanted=None;self.issue=''
        self.directory=None;self.error='';self.virtual_wait=False
        self.failure_sequence=None;self.failures=0;self.retry_at=0.;self.rejected_sequence=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._timeout)

    def read(self,sequence):
        if sequence==self.rejected_sequence and sequence is not None:
            self.issue='REMOTE_FILE_PLACEHOLDER';return False,None
        if self.cached_sequence==sequence:return True,self.value
        self.wanted=sequence
        if self.closed or self.process is not None or self.loading:return False,None
        if sequence==self.failure_sequence and time.monotonic()<self.retry_at:return False,None
        if foreign_clipboard_lock():
            self.issue='NATIVE_CLIPBOARD_BUSY';return False,None
        self.issue='NATIVE_FORMAT_NOT_READY'
        self.directory=self.cache/('clip-read-'+uuid4().hex);self.directory.mkdir(mode=0o700)
        (self.directory/'.lease.json').write_text(json.dumps(dict(flickdock_clipboard=1,created_at=time.time())),encoding='utf8')
        job=self.directory/'read.json';job.write_text(json.dumps(dict(sequence=sequence)),encoding='utf8')
        process=QProcess(self);self.process=process;self.error='';self.virtual_wait=False
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.readyReadStandardError.connect(lambda:process.readAllStandardError())
        process.readyReadStandardOutput.connect(lambda:process.readAllStandardOutput())
        process.errorOccurred.connect(lambda error:self._error(process,error))
        process.finished.connect(lambda code,status:self._finished(process,code,status))
        command=self._command(job);process.setProgram(command[0]);process.setArguments(command[1:])
        process.start();self.timer.start(READ_TIMEOUT_MS)
        return False,None

    def _command(self,job):
        if getattr(sys,'frozen',False):return [sys.executable,'--native-clipboard-reader',str(job)]
        return [sys.executable,str(Path(__file__)),str(job)]

    def _timeout(self):
        phase=self.directory/'virtual' if self.directory is not None else None
        if (not self.virtual_wait and phase is not None and phase.is_file()
                and not phase.is_symlink() and phase.stat().st_size==7 and phase.read_bytes()==b'virtual'):
            self.virtual_wait=True;self.timer.start(VIRTUAL_READ_TIMEOUT_MS);return
        if self.process is not None:self.error='NATIVE_READ_TIMEOUT';self.process.kill()

    def _error(self,process,error):
        if process is self.process and error==QProcess.FailedToStart:
            self.error='NATIVE_READ_START_FAILED';self._finished(process,1,QProcess.CrashExit)

    def _discard(self,directory):
        if directory and DIRECTORY.fullmatch(directory.name) and directory.parent==self.cache and not directory.is_symlink():
            shutil.rmtree(directory,ignore_errors=True)

    def _reject(self,code):
        sequence=self.sequence()
        if code=='REMOTE_FILE_PLACEHOLDER':self.rejected_sequence=sequence
        self.failures=self.failures+1 if sequence==self.failure_sequence else 1
        self.failure_sequence=sequence
        self.retry_at=time.monotonic()+min(30.,.25*2**min(self.failures,7))
        self.issue=code;self.failed.emit(code)

    def _finished(self,process,code,status):
        if process is not self.process:return
        self.timer.stop();self.process=None;process.deleteLater()
        directory,self.directory=self.directory,None
        if self.closed or code or status!=QProcess.NormalExit:
            self._discard(directory)
            if not self.closed:
                self._reject(self.error or 'NATIVE_READ_FAILED')
            return
        self.loading=True;job=DiskWork(_read_result,directory);self.jobs.add(job)
        def done(value,error):
            self.jobs.discard(job);self.loading=False
            if self.closed:self._discard(directory);return
            if error or not value.get('ok'):
                self._discard(directory);self._reject((value or {}).get('code','NATIVE_READ_INVALID_REPLY'));return
            if value['sequence']!=self.sequence():
                self._discard(directory);self.failed.emit('NATIVE_READ_REPLACED');return
            self.cached_sequence=value['sequence'];self.value=value['value'];self.issue='';self.failures=0;self.rejected_sequence=None
            retained=self.value and self.value[0]=='files' and any(Path(ref).is_relative_to(directory) for ref in self.value[1])
            self.ready.emit(self.cached_sequence,self.value,directory if retained else None)
            if not retained:self._discard(directory)
        job.signals.done.connect(done,Qt.QueuedConnection);QThreadPool.globalInstance().start(job)

    def close(self):
        self.closed=True;self.timer.stop()
        for job in self.jobs:job.cancel.set()
        if self.process is not None:self.process.kill()


def run_worker(path):
    directory=None;validated=False;stage='JOB'
    try:
        if sys.platform!='win32':raise RuntimeError('Windows clipboard reader only')
        job=Path(path).absolute();directory=job.parent
        if (job.name!='read.json' or not DIRECTORY.fullmatch(directory.name)
                or directory.parent.name!='clipboard-cache' or job.is_symlink() or directory.is_symlink()
                or directory.parent.is_symlink() or job.is_junction() or directory.is_junction()
                or directory.parent.is_junction() or job.resolve(strict=True)!=job
                or job.stat().st_size>128):raise ValueError('Invalid clipboard read job')
        validated=True
        expected=json.loads(job.read_text())['sequence']
        if type(expected) is not int or not 0<=expected<=0xffffffff:raise ValueError('Invalid clipboard sequence')
        stage='INITIALIZE'
        from PySide6.QtGui import QGuiApplication
        from clipboard_sync import native_sequence,_snapshot_mime,native_format_count
        from virtual_clipboard import DESCRIPTOR_MIME,describe,materialize,VirtualClipboardError,only_remote_placeholders
        app=QGuiApplication.instance() or QGuiApplication([]);clipboard=app.clipboard()
        if native_sequence()!=expected:raise VirtualClipboardError('NATIVE_READ_REPLACED')
        stage='CAPTURE'
        for attempt in range(2):
            app.processEvents();before=native_sequence();mime=clipboard.mimeData()
            if mime is None:raise ValueError('Clipboard unavailable')
            if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()) and not only_remote_placeholders(mime):value=_snapshot_mime(mime)
            elif mime.hasFormat(DESCRIPTOR_MIME):
                (directory/'virtual').write_bytes(b'virtual');descriptor=describe(mime)
                files=directory/'files';files.mkdir(exist_ok=True)
                result=materialize(mime,files,{'sequence':before,'descriptor':descriptor.sha256},native_sequence)
                value=('files',[str(files/result['name'])])
            else:value=_snapshot_mime(mime)
            sequence=native_sequence()
            if before==sequence:break
        else:raise VirtualClipboardError('NATIVE_READ_REPLACED')
        if value is None and native_format_count():raise VirtualClipboardError('NATIVE_FORMAT_NOT_READY')
        stage='ENCODE';kind,data=value if value else (None,None)
        if kind=='image':
            output=dict(ok=True,sequence=sequence,kind=kind,mime=_store_image(directory,data));data=None
        else:output=dict(ok=True,sequence=sequence,kind=kind,data=data)
        encoded=json.dumps(output,ensure_ascii=False).encode('utf8')
        if len(encoded)>MAX_METADATA_BYTES:raise ValueError('Clipboard payload too large')
        (directory/'result.json').write_bytes(encoded);return 0
    except Exception as error:
        if validated:
            code=getattr(error,'code','NATIVE_READ_'+stage+'_'+type(error).__name__.upper())
            (directory/'result.json').write_text(json.dumps(dict(ok=False,code=code)),encoding='utf8')
            return 0
        return 1


if __name__=='__main__':raise SystemExit(run_worker(sys.argv[1]))
