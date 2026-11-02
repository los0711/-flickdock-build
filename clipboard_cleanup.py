"""One owned filesystem operation, isolated from UI and copy worker pools."""
from __future__ import annotations
import json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QObject,QTimer
from clipboard_cleanup_worker import VERSION,BATCH_SIZE,MAX_MESSAGE_BYTES,DEADLINE_MS,diagnostic,no_links


def native_spawn(command,**options):
    from win_owned_process import spawn_owned
    return spawn_owned(command,**options)


def spawn(command,**options):
    if os.name=='nt':return native_spawn(command,**options)
    return subprocess.Popen(command,**options,start_new_session=True)


class Operation:
    def __init__(self,command,request,callback,process_factory,deadline_ms):
        self.token=uuid4().hex;self.command=command;self.request=dict(request,version=VERSION,token=self.token)
        self.callback=callback;self.factory=process_factory;self.deadline_ms=deadline_ms
        self.lock=threading.Lock();self.cancelled=threading.Event();self.done=threading.Event();self.process=None
        self.result=None;self.reaped=False;self.reason=None;self.deadline=threading.Timer(deadline_ms/1000,self.cancel,args=('DEADLINE',));self.deadline.daemon=True
        self.thread=threading.Thread(target=self.run,name='FlickDock-clipboard-cleanup',daemon=True)
    def start(self):self.deadline.start();self.thread.start()
    def cancel(self,reason='CANCELLED'):
        with self.lock:
            if self.done.is_set():return
            self.reason=self.reason or reason;self.cancelled.set();process=self.process
        if process is not None:
            try:process.kill()
            except OSError as error:
                with self.lock:self.result=dict(status='failed',code='TERMINATE_FAILED',diagnostic=diagnostic(error))
    def run(self):
        process=None;root=None;response=None;cleanup_error=None
        try:
            if self.cancelled.is_set():return
            base=Path(tempfile.gettempdir()).resolve(strict=True)/'FlickDock-cleanup-v1'
            no_links(base.parent);base.mkdir(exist_ok=True);no_links(base)
            root=base/self.token;root.mkdir(mode=0o700);request_path=root/'request.json'
            with request_path.open('x',encoding='ascii') as target:json.dump(self.request,target,separators=(',',':'))
            if self.cancelled.is_set():return
            process=self.factory([*self.command,'--request',str(request_path),'--token',self.token,'--parent-pid',str(os.getpid()),'--deadline-ms',str(self.deadline_ms)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            with self.lock:self.process=process;cancelled=self.cancelled.is_set()
            if cancelled:process.kill()
            else:
                # A destructive helper cannot act until its process owner is
                # published and cancellation can address that exact process.
                permit=root/'permit'
                with permit.open('xb') as target:target.write((self.token+':'+self.request['mode']).encode('ascii'))
                if self.cancelled.is_set():process.kill()
            chunks=[];size=0
            while True:
                chunk=process.stdout.read(8192)
                if not chunk:break
                size+=len(chunk)
                if size>MAX_MESSAGE_BYTES:self.cancel('OUTPUT_LIMIT');chunks.clear();continue
                chunks.append(chunk)
            code=process.wait(timeout=1 if self.cancelled.is_set() else None)
            if not self.cancelled.is_set():
                if code!=0:response=dict(status='failed',code='PROCESS_FAILED')
                else:
                    response=json.loads(b''.join(chunks))
                    if (not isinstance(response,dict) or type(response.get('version'))is not int or response.get('version')!=VERSION or response.get('token')!=self.token
                            or response.get('status')not in ('ready','failed')):raise ValueError('Invalid cleanup result')
                    if (response['status']=='ready' and response.get('mode')!=self.request['mode']
                            or response['status']=='failed' and response.get('code')not in ('UNSAFE','STALE','UNSUPPORTED','ACCESS_DENIED','IO')):raise ValueError('Invalid cleanup result purpose')
        except (OSError,ValueError,TypeError,subprocess.TimeoutExpired) as error:
            response=dict(status='failed',code='PROCESS_FAILED',diagnostic=diagnostic(error))
            if process is None:
                process=getattr(error,'_helper_owner',None)
                with self.lock:self.process=process
        finally:
            try:
                if process is not None:
                    try:
                        if process.poll()is None:process.kill()
                        process.wait(timeout=1)
                        if process.stdout is not None:process.stdout.close()
                        close=getattr(process,'close',None)
                        if close is not None:close()
                        self.reaped=True
                    except (OSError,subprocess.TimeoutExpired) as error:cleanup_error=dict(status='failed',code='REAP_FAILED',diagnostic=diagnostic(error))
                else:self.reaped=True
                if root is not None and self.reaped:
                    try:
                        no_links(root);(root/'permit').unlink(missing_ok=True);(root/'request.json').unlink(missing_ok=True);root.rmdir()
                    except (OSError,ValueError) as error:cleanup_error=dict(status='failed',code='CAP_CLEANUP_FAILED',diagnostic=diagnostic(error))
                with self.lock:
                    if cleanup_error:self.result=cleanup_error
                    elif self.reason:self.result=dict(status='deferred',code=self.reason)
                    else:self.result=self.result or response or dict(status='failed',code='PROCESS_FAILED')
            except Exception as error:
                self.reaped=False;self.result=dict(status='failed',code='REAP_FAILED',diagnostic=diagnostic(error))
            finally:self.deadline.cancel();self.done.set()


class ClipboardCleanup(QObject):
    """A single active capability; caller coalesces requests as one dirty bit."""
    def __init__(self,parent=None,*,command=None,process_factory=None,deadline_ms=DEADLINE_MS):
        super().__init__(parent);self.command=command;self.process_factory=process_factory or spawn;self.deadline_ms=deadline_ms
        self.active=None;self.quarantined=None;self.closed=False
        self.clock=QTimer(self);self.clock.setInterval(5);self.clock.timeout.connect(self.poll)
    def argv(self):
        if self.command is not None:return list(self.command)
        if getattr(sys,'frozen',False):return [sys.executable,'--clipboard-cleaner']
        return [sys.executable,str(Path(__file__).with_name('clipboard_cleanup_worker.py'))]
    def submit(self,request,callback):
        if self.closed or self.active is not None or self.quarantined is not None:return None
        operation=Operation(self.argv(),request,callback,self.process_factory,self.deadline_ms);self.active=operation;operation.start();self.clock.start();return operation.token
    def cancel(self):
        if self.active is not None:self.active.cancel()
    def poll(self):
        operation=self.active
        if operation is None:self.clock.stop();return
        if not operation.done.is_set():return
        self.active=None
        if not operation.reaped:self.quarantined=operation
        try:
            if not self.closed:operation.callback(operation.result)
        finally:
            operation.callback=None
            if self.active is None:self.clock.stop()
    def close(self):
        self.closed=True;self.cancel()
