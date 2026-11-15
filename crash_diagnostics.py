"""Bounded, asynchronous UI diagnostics. Never reads clipboard or file contents."""
from collections import deque
from datetime import datetime,timezone
from pathlib import Path
import faulthandler,json,os,queue,re,shutil,sys,threading,time,traceback,zipfile
from PySide6.QtCore import QObject,QEvent,QTimer,Qt,Signal,qInstallMessageHandler,qVersion
from PySide6.QtWidgets import QApplication,QWidget
def _stamp():return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def _redact(text):
    text=str(text)
    for key in ('USERPROFILE','APPDATA','LOCALAPPDATA','HOME'):
        value=os.environ.get(key)
        if value:text=re.sub(re.escape(value),'<'+key.lower()+'>',text,flags=re.I)
    text=re.sub(r'https?://[^\s\"\']+','<url>',text)
    text=re.sub(r'(?i)(authorization|bearer|token|password|secret)([\s:=]+)[^\s,;]+',r'\1\2<redacted>',text)
    return text

def process_snapshot():
    result=dict(threads=threading.active_count())
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD),*[(name,ctypes.c_size_t) for name in
                ('peak_rss','rss','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]]
        kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=wintypes.HANDLE
        process=kernel.GetCurrentProcess();c=Counters();c.cb=ctypes.sizeof(c)
        psapi=ctypes.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(process,ctypes.byref(c),c.cb):result['rss_bytes']=c.rss
        handles=wintypes.DWORD();kernel.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        if kernel.GetProcessHandleCount(process,ctypes.byref(handles)):result['handles']=handles.value
        user=ctypes.WinDLL('user32');user.GetGuiResources.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        result.update(gdi_handles=user.GetGuiResources(process,0),user_handles=user.GetGuiResources(process,1),
                      remote_session=bool(user.GetSystemMetrics(0x1000)))
    return result


def _driver_environment():
    if sys.platform!='win32':return dict(gpu_driver='not_collected')
    import winreg
    adapters=[]
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}') as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name=winreg.EnumKey(root,index)
                if not name.isdecimal():continue
                try:
                    with winreg.OpenKey(root,name) as key:
                        adapters.append(dict(name=winreg.QueryValueEx(key,'DriverDesc')[0],
                                             version=winreg.QueryValueEx(key,'DriverVersion')[0]))
                except OSError:continue  # Optional registry values, not application errors.
    except OSError:return dict(gpu_driver='unavailable')
    return dict(gpu_drivers=adapters)

class CrashDiagnostics(QObject):
    ready=Signal()
    exported=Signal(str,str)
    def __init__(self,app,base,version):
        super().__init__(app);self.app=app;self.base=Path(base)/'diagnostics';self.version=version
        self.events=deque(maxlen=200);self.paints=deque(maxlen=60);self._last_paint=None
        self._lock=threading.Lock();self._revision=0;self._saved_revision=-1
        self._commands=queue.Queue(maxsize=256);self._wake=threading.Event()
        self._stop=threading.Event();self._initialized=threading.Event();self._closing=False
        self._watchdog_timeout=0.;self._watchdog_beat=time.monotonic();self._watchdog_dumped=False
        self.stack_stream=None;self.stall_stream=None;self.directory=None
        self._previous_exception=sys.excepthook;self._previous_thread=threading.excepthook
        self._previous_qt=None;self._installed=False
        self.environment=dict(version=version,python=sys.version.split()[0],qt=qVersion(),platform=sys.platform,
            qt_platform=app.platformName(),screens=[dict(dpi=round(s.logicalDotsPerInch(),2),scale=s.devicePixelRatio(),
                width=s.geometry().width(),height=s.geometry().height()) for s in app.screens()])
        self.ready.connect(self._install,Qt.QueuedConnection)
        self._worker=threading.Thread(target=self._run,name='FlickDock-crash-diagnostics',daemon=True);self._worker.start()
        app.installEventFilter(self);app.aboutToQuit.connect(self.close)

    def note(self,name):
        if self._closing:return
        with self._lock:
            self.events.append(dict(event=str(name),timestamp=_stamp()));self._revision+=1
        self._wake.set()

    def configure(self,*,theme,motion):
        self.environment.update(theme=str(theme),animations=bool(motion))
        self.note('appearance.configured')

    def _send(self,kind,value):
        if self._closing:return
        try:self._commands.put_nowait((kind,value))
        except queue.Full:
            self.note('diagnostics.queue_full')
        self._wake.set()

    def _install(self):
        if self._closing or self.stack_stream is None:return
        faulthandler.enable(self.stack_stream,all_threads=True)
        sys.excepthook=self._exception;threading.excepthook=self._thread_exception
        self._previous_qt=qInstallMessageHandler(self._qt_message);self._installed=True
        self.note('diagnostics.ready')

    def _exception(self,kind,value,tb):
        self.note('python.unhandled_exception')
        self._send('stack',''.join(traceback.format_exception(kind,value,tb)))
        self._previous_exception(kind,value,tb)

    def _thread_exception(self,args):
        self.note('python.thread_exception')
        self._send('stack',''.join(traceback.format_exception(args.exc_type,args.exc_value,args.exc_traceback)))
        self._previous_thread(args)

    def _qt_message(self,kind,context,message):
        self._send('qt',dict(timestamp=_stamp(),kind=str(kind),message=_redact(message)))
        if self._previous_qt is not None:self._previous_qt(kind,context,message)

    def eventFilter(self,watched,event):
        if self._closing:return False
        kind=event.type()
        names={QEvent.Show:'window.show',QEvent.Hide:'window.hide',QEvent.WindowStateChange:'window.state',
               QEvent.DevicePixelRatioChange:'window.dpi'}
        if kind not in (*names,QEvent.Paint):return False
        if not isinstance(watched,QWidget) or not watched.isWindow():return False
        if kind in names:self.note(names[kind])
        elif watched.isVisible() and not watched.isMinimized():
            now=time.perf_counter();region=event.region().boundingRect()
            frame=dict(timestamp=_stamp(),interval_ms=None if self._last_paint is None else round((now-self._last_paint)*1000,3),
                region=[region.x(),region.y(),region.width(),region.height()],
                translucent=watched.testAttribute(Qt.WA_TranslucentBackground),
                effect=watched.graphicsEffect() is not None,measurement='top_level_paint_arrival')
            self._last_paint=now
            with self._lock:self.paints.append(frame);self._revision+=1
        return False

    def export(self,path):self._send('export',str(path))

    def watchdog_heartbeat(self,timeout):
        if self._closing:return
        self._watchdog_timeout=float(timeout);self._watchdog_beat=time.monotonic();self._watchdog_dumped=False

    def stop_watchdog(self):
        self._watchdog_timeout=0.

    def _check_watchdog(self):
        # This call runs on a Python worker with the GIL, never on the GUI or
        # faulthandler's unsynchronized native timer thread. A native operation
        # holding the GIL can delay it; the persisted last actions still remain.
        timeout=self._watchdog_timeout
        if timeout and not self._watchdog_dumped and time.monotonic()-self._watchdog_beat>=timeout:
            self._watchdog_dumped=True
            if os.fstat(self.stall_stream.fileno()).st_size<1024*1024:
                self.stall_stream.write(('Timeout: GUI heartbeat absent for '+str(timeout)+' seconds\n').encode('ascii'))
                faulthandler.dump_traceback(file=self.stall_stream,all_threads=True)

    def _snapshot(self,force=False):
        with self._lock:
            if not force and self._saved_revision==self._revision:return
            events=list(self.events);paints=list(self.paints);revision=self._revision
        (self.directory/'breadcrumbs.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in events),encoding='utf8')
        (self.directory/'last-paint.json').write_text(json.dumps(paints,indent=2),encoding='utf8')
        env=dict(self.environment,**process_snapshot())
        (self.directory/'env.json').write_text(json.dumps(env,indent=2),encoding='utf8')
        self._saved_revision=revision

    def _bound_stack(self):
        # Preserve the pre-opened descriptor used by faulthandler. Never replace
        # or close it while the diagnostic hooks are installed.
        stream=self.stack_stream
        if os.fstat(stream.fileno()).st_size>2*1024*1024:
            stream.seek(-1024*1024,os.SEEK_END);tail=stream.read()
            stream.seek(0);stream.truncate();stream.write(b'[Earlier stacks trimmed]\n'+tail)

    def _append(self,path,text,limit=2*1024*1024):
        if path.exists() and path.stat().st_size+len(text.encode('utf8'))>limit:
            previous=path.with_suffix(path.suffix+'.previous');previous.unlink(missing_ok=True);path.replace(previous)
        with path.open('a',encoding='utf8') as stream:stream.write(text)

    def _bundle(self,target,*,include_current=True):
        """Archive only known bounded logs; never include user content files."""
        names=('python-stack.txt','qt-messages.txt','breadcrumbs.jsonl','env.json','last-paint.json')
        sessions=sorted((path for path in self.base.glob('crash-*')
                         if path.is_dir() and not path.is_symlink() and path!=self.directory
                         and path.resolve().parent==self.base.resolve()),key=lambda path:path.name)[-7:]
        def add(archive,path,name):
            if not path.is_file() or path.is_symlink():return
            # Stack and Qt logs are already bounded at write time. Also cap
            # files from an interrupted older version before reading them.
            with path.open('rb') as stream:
                size=os.fstat(stream.fileno()).st_size
                if size>2*1024*1024:stream.seek(-2*1024*1024,os.SEEK_END)
                data=stream.read(2*1024*1024)
            archive.writestr(name,_redact(data.decode('utf8','replace')).encode('utf8'))
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            if include_current:
                for name in names:add(archive,self.directory/name,name)
            for session in sessions:
                for name in names:add(archive,session/name,'previous/'+session.name+'/'+name)
            for name in ('ui-stall.log','ui-stall-previous.log'):add(archive,self.base/name,name)

    def _prepare_previous_incident(self,previous):
        banner=b'FlickDock Python/native crash stacks; no locals or clipboard content\n'
        stalls=(self.base/'ui-stall.log',self.base/'ui-stall-previous.log')
        incident=any(path.is_file() and not path.is_symlink() and path.stat().st_size>0 for path in stalls)
        for session in previous:
            stack=session/'python-stack.txt'
            if stack.is_file() and not stack.is_symlink() and stack.stat().st_size>len(banner):
                incident=True;break
        if not incident:return
        self._archive_incident(include_current=False)

    def _archive_incident(self,*,include_current):
        target=self.base/'latest-incident.zip';temporary=self.base/'latest-incident.zip.part'
        try:
            self._bundle(temporary,include_current=include_current)
            os.replace(temporary,target)
        finally:
            if temporary.exists():temporary.unlink()

    def _run(self):
        try:
            self.environment.update(_driver_environment())
            self.base.mkdir(parents=True,exist_ok=True,mode=0o700)
            previous=sorted((path for path in self.base.glob('crash-*') if path.is_dir() and not path.is_symlink()),key=lambda p:p.name)
            for path in previous[:-7]:
                if path.is_dir() and not path.is_symlink() and path.resolve().parent==self.base.resolve():shutil.rmtree(path)
            self.directory=self.base/('crash-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            self.directory.mkdir()
            stall=self.base/'ui-stall.log'
            if stall.exists() and stall.stat().st_size>1024*1024:
                old=self.base/'ui-stall-previous.log';old.unlink(missing_ok=True);stall.replace(old)
            self.stack_stream=(self.directory/'python-stack.txt').open('a+b',buffering=0)
            self.stall_stream=stall.open('ab',buffering=0)
            self.stack_stream.write(b'FlickDock Python/native crash stacks; no locals or clipboard content\n')
            (self.directory/'qt-messages.txt').touch()
            try:self._prepare_previous_incident(previous[-7:])
            except OSError as error:
                self._append(self.directory/'qt-messages.txt',json.dumps(dict(timestamp=_stamp(),
                    kind='diagnostics.previous_export_failed',message=type(error).__name__))+'\n')
            self._initialized.set();self.ready.emit()
            stall_size=self.stall_stream.tell()
            next_stall_check=time.monotonic()+1
            while not self._stop.is_set() or not self._commands.empty():
                self._wake.wait(.25);self._wake.clear()
                while True:
                    try:kind,value=self._commands.get_nowait()
                    except queue.Empty:break
                    if kind=='stack':self.stack_stream.write(_redact(value).encode('utf8','replace'))
                    elif kind=='qt':self._append(self.directory/'qt-messages.txt',json.dumps(value,ensure_ascii=False)+'\n')
                    elif kind=='export':
                        try:
                            self._bound_stack();self._snapshot(force=True)
                            self._bundle(value)
                            self.exported.emit(value,'')
                        except OSError as error:self.exported.emit('',type(error).__name__)
                self._check_watchdog();self._bound_stack();self._snapshot()
                # If Qt stops responding, the C watchdog writes independently
                # of the event loop. Package it on this worker when possible;
                # next-start recovery still works if native code holds the GIL.
                if time.monotonic()>=next_stall_check:
                    next_stall_check=time.monotonic()+1
                    current_size=os.fstat(self.stall_stream.fileno()).st_size
                    if current_size>stall_size:
                        stall_size=current_size
                        try:self._archive_incident(include_current=True)
                        except OSError as error:
                            self._append(self.directory/'qt-messages.txt',json.dumps(dict(timestamp=_stamp(),
                                kind='diagnostics.stall_export_failed',message=type(error).__name__))+'\n')
        except OSError as error:
            self._initialized.set();self.exported.emit('',type(error).__name__)

    def close(self):
        if self._closing:return
        self.stop_watchdog()
        self.note('application.quit');self._closing=True;self.app.removeEventFilter(self)
        if self._installed:
            qInstallMessageHandler(self._previous_qt);sys.excepthook=self._previous_exception
            threading.excepthook=self._previous_thread;faulthandler.disable();self._installed=False
        faulthandler.cancel_dump_traceback_later()
        self._stop.set();self._wake.set()
        # The worker writes only bounded diagnostic snapshots and owns no UI.
        # Joining is deferred to process shutdown; no synchronous GUI fsync.

    def wait_closed(self,seconds=2):
        self.close();self._worker.join(seconds)
        if not self._worker.is_alive():
            for stream in (self.stack_stream,self.stall_stream):
                if stream is not None:stream.close()
        return not self._worker.is_alive()
