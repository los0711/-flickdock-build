"""Async owned-process clipboard reader; no QClipboard/native read on UI thread."""
from __future__ import annotations
import json,struct,sys,subprocess,threading
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QObject,QTimer
from win_clipboard_native import MAGIC,MAX_BYTES,MAX_HEADER,MAX_WIRE,native_metadata,safe_failure
from win_owned_process import spawn_owned
from win_clipboard_cache import CacheCapability

def unknown(code='READ_UNAVAILABLE',diagnostic=None):
    result=dict(status='unknown',valid=False,value=None,known_empty=False,sequence=None,
                sequence_before=None,sequence_after=None,marker=b'',code=code)
    if diagnostic is not None:result['diagnostic']=diagnostic
    return result

def decode_frame(raw,*,cache=None):
    if not isinstance(raw,bytes) or not len(MAGIC)+4<=len(raw)<=MAX_WIRE or not raw.startswith(MAGIC):raise ValueError('Invalid helper frame')
    n=struct.unpack_from('<I',raw,len(MAGIC))[0];start=len(MAGIC)+4
    if not 0<n<=MAX_HEADER or start+n>len(raw):raise ValueError('Invalid helper header')
    h=json.loads(raw[start:start+n].decode('ascii'))
    if not isinstance(h,dict) or h.get('version')!=1 or h.get('status') not in ('ready','unknown','changed','overlimit') or type(h.get('known_empty')) is not bool:raise ValueError('Invalid helper metadata')
    for key in ('sequence_before','sequence_after'):
        if h.get(key) is not None and (type(h[key]) is not int or not 0<=h[key]<=0xffffffff):raise ValueError('Invalid sequence')
    parts=h.get('parts');kind=h.get('kind')
    wanted={None:(), 'text':('marker','text','html'),'image':('marker','image'),'files':('marker','path','canonical'),'local':('marker',)}
    if kind not in wanted or not isinstance(parts,list) or len(parts)!=len(wanted[kind]):raise ValueError('Invalid helper parts')
    data={};offset=start+n
    for part,name in zip(parts,wanted[kind]):
        if not isinstance(part,dict) or part.get('name')!=name or type(part.get('size')) is not int or not 0<=part['size']<=MAX_BYTES:raise ValueError('Invalid part size')
        end=offset+part['size']
        if end>len(raw):raise ValueError('Truncated helper data')
        data[name]=raw[offset:end];offset=end
    if offset!=len(raw) or len(data.get('marker',b''))>1024:raise ValueError('Invalid trailing data')
    result=dict(status=h['status'],valid=h['status']=='ready' and type(h['sequence_before']) is int and h['sequence_before']>0 and h['sequence_before']==h['sequence_after'],
      known_empty=h['known_empty'],sequence=h['sequence_after'],sequence_before=h['sequence_before'],sequence_after=h['sequence_after'],marker=data.get('marker',b''),value=None,code=h.get('code',''))
    eligible=h.get('sync_eligible',True)
    if type(eligible)is not bool:raise ValueError('Invalid sync eligibility')
    result['sync_eligible']=eligible
    if 'diagnostic' in h:
        d=h['diagnostic']
        if not isinstance(d,dict) or set(d)-{'phase','exception','errno','winerror'}:raise ValueError('Invalid diagnostic')
        # Reuse the fixed vocabulary; arbitrary exception names/messages never
        # cross this boundary, even from a malformed helper frame.
        dummy=OSError();checked=safe_failure(d.get('phase'),dummy)
        if checked['phase']!=d.get('phase') or d.get('exception') not in ('OSError','PermissionError','FileNotFoundError','TimeoutError','BrokenPipeError','ValueError','UnicodeDecodeError','RuntimeError','TooLarge','Unsupported','TimeoutExpired','MemoryError','InterruptedError','Exception'):raise ValueError('Invalid diagnostic')
        if any(type(d[k])is not int or not -0x80000000<=d[k]<=0xffffffff for k in ('errno','winerror') if k in d):raise ValueError('Invalid diagnostic')
        result['diagnostic']=dict(d)
    if not result['valid']:
        if kind is not None or result['known_empty']:raise ValueError('Invalid state has payload/empty proof')
        return result
    if kind is None:
        if not result['known_empty']:raise ValueError('Missing helper value')
    elif result['known_empty']:raise ValueError('Empty state has payload')
    elif kind=='local':
        if eligible or cache is None:raise ValueError('Missing original-content capability')
        content=cache.adopt(h)
        if content.logical_size<=MAX_BYTES:raise ValueError('Invalid local-only size policy')
        result.update(local_content=content,signature=content.fingerprint)
    elif kind=='text':
        if not eligible:raise ValueError('Invalid inline text policy')
        if len(data['text'])+len(data['html'])>MAX_BYTES:raise ValueError('Text policy')
        result['value']=('text',dict(text=data['text'].decode('utf8','strict'),html=data['html'].decode('utf8','strict')))
    elif kind=='image':
        if not eligible:raise ValueError('Invalid inline image policy')
        if h.get('mime') not in ('image/png','image/jpeg','image/webp','image/bmp','image/tiff') or not data['image']:raise ValueError('Invalid image')
        result['value']=('image',dict(raw=data['image'],mime=h['mime']))
    else:
        path=data['path'].decode('utf8','strict');canonical=data['canonical'].decode('utf8','strict');info=h.get('file_stat')
        if not path or '\0' in path or len(path)>32767 or not canonical or '\0' in canonical or not isinstance(info,list) or len(info)!=2 or any(type(x)is not int or x<0 for x in info) or eligible!=(info[0]<=MAX_BYTES):raise ValueError('Invalid file metadata')
        stamp=h.get('source_stat')
        if not isinstance(stamp,list) or len(stamp)!=4 or any(type(x)is not int or x<0 for x in stamp) or stamp[2:]!=info:raise ValueError('Invalid source stat')
        result['value']=('files',[path]);result['signature']=('files',((canonical,*info),));result['source_stat']=stamp
    return result

class _ClipboardReadPurpose:
    """Default FDCR1 binding; process ownership is independent of payload type."""
    @property
    def max_wire(self):return MAX_WIRE
    @staticmethod
    def cache(cancel):return CacheCapability(cancel=cancel)
    @staticmethod
    def arguments(cache):return cache.arguments()
    @staticmethod
    def decode(raw,cache):return decode_frame(raw,cache=cache)
    @staticmethod
    def failure(code='READ_UNAVAILABLE',diagnostic=None):return unknown(code,diagnostic)
    @staticmethod
    def command():
        if getattr(sys,'frozen',False):return [sys.executable,'--clipboard-reader']
        return [sys.executable,str(Path(__file__).with_name('win_clipboard_native.py'))]

class _ReadOperation:
    """One owner for child, pipe, parse and reap. Contains no Qt objects."""
    def __init__(self,token,callback,command,deadline_ms,process_factory,*,purpose=None):
        self.token=token;self.callback=callback;self.command=command
        self.process=None;self.reason=None;self.result=None
        self.purpose=_ClipboardReadPurpose() if purpose is None else purpose
        self.reaped=False;self.done=threading.Event();self.lock=threading.Lock();self.process_factory=process_factory;self.diagnostic=None;self.cancelled=threading.Event()
        self.deadline=threading.Timer(deadline_ms/1000,self.cancel,args=('DEADLINE',));self.deadline.daemon=True
        self.thread=threading.Thread(target=self._run,name='FlickDock-clipboard-reader',daemon=True)
    def start(self):self.deadline.start();self.thread.start()
    def cancel(self,reason):
        with self.lock:
            if self.done.is_set():return
            self.reason=self.reason or reason;process=self.process
            self.cancelled.set()
        if process is not None:
            try:process.kill()
            except OSError as error:
                # Preserve the error until worker-side wait can distinguish an
                # already-exited race from a genuinely unreaped owned process.
                with self.lock:self.reason='TERMINATE_FAILED';self.diagnostic=safe_failure('close',error)
    @staticmethod
    def _reap(process):
        error=None;closed=False;diagnostic=None
        for attempt in range(2):
            try:
                if process.returncode is None:
                    try:process.kill()
                    except OSError as cause:error='TERMINATE_FAILED';diagnostic=safe_failure('close',cause)
                    process.wait(timeout=1.)
                if process.stdout is not None:process.stdout.close()
                close=getattr(process,'close',None)
                if close is not None:close()
                closed=True;break
            except (OSError,subprocess.TimeoutExpired) as cause:error='REAP_FAILED';diagnostic=safe_failure('cleanup',cause)
        return closed,error,diagnostic
    def _run(self):
        process=None;chunks=[];value=None;cache=None
        try:
            with self.lock:cancelled=self.reason is not None
            if cancelled:return
            cache=self.purpose.cache(self.cancelled)
            process=self.process_factory([*self.command,*self.purpose.arguments(cache)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            with self.lock:self.process=process;cancelled=self.reason is not None
            if cancelled:process.kill()
            size=0
            while True:
                block=process.stdout.read(262144)
                if not block:break
                with self.lock:cancelled=self.reason is not None
                if cancelled:chunks.clear();continue
                size+=len(block)
                if size>self.purpose.max_wire:self.cancel('OUTPUT_LIMIT');chunks.clear();continue
                chunks.append(block)
            code=process.wait(timeout=1. if self.reason else None) # Worker only.
            with self.lock:reason=self.reason
            if reason:value=self.purpose.failure(reason)
            elif code!=0:value=self.purpose.failure('PROCESS_FAILED')
            else:
                try:value=self.purpose.decode(b''.join(chunks),cache)
                except (ValueError,UnicodeError,TypeError,KeyError):value=self.purpose.failure('INVALID_FRAME')
        except Exception as error:
            value=self.purpose.failure('START_FAILED' if process is None else 'PROCESS_FAILED',safe_failure(getattr(error,'phase','spawn' if process is None else 'pipe'),error))
            if process is None:
                process=getattr(error,'_helper_owner',None)
                if process is not None:
                    with self.lock:self.process=process
        finally:
            chunks.clear()
            try:
                cleanup=None
                if process is not None:
                    self.reaped,cleanup,reap_diagnostic=self._reap(process)
                    if reap_diagnostic is not None:self.diagnostic=reap_diagnostic
                else:self.reaped=True
                if cache is not None:
                    if self.reaped:
                        try:cache.retire()
                        except (OSError,ValueError) as error:
                            # Local cache IO is not an unreaped OS process.
                            # Reject this sample, preserve cleanup ownership,
                            # but allow the next real clipboard event to run.
                            value=self.purpose.failure('CACHE_UNAVAILABLE',safe_failure('cache',error))
                    else:self._unreaped_cache=cache # Keep authority with quarantined writer.
                with self.lock:self.result=self.purpose.failure(cleanup or self.reason,self.diagnostic or getattr(process,'diagnostic',None)) if cleanup or self.reason else value or self.purpose.failure('READ_UNAVAILABLE')
            except Exception as error:
                self.result=self.purpose.failure('REAP_FAILED',safe_failure('cleanup',error));self.reaped=False
            finally:self.deadline.cancel();self.done.set()

class WindowsClipboardReader(QObject):
    """One active operation and one latest pending callback, entirely async.

    The sole Qt clock runs only while an owned operation exists. It dispatches
    completion and deadlines; it does not read clipboard data or wait for I/O.
    The worker owns process/pipe/decoding/reaping end to end, so no queued Qt
    process/parse callbacks can reference a deleted native process object.
    """
    def __init__(self,parent=None,*,deadline_ms=3000,command=None,process_factory=None,purpose=None):
        super().__init__(parent);self.deadline_ms=max(50,min(int(deadline_ms),10000));self.command=command
        self.process_factory=process_factory or spawn_owned
        self.purpose=_ClipboardReadPurpose() if purpose is None else purpose
        self._active=None;self._pending=None;self._closed=False;self._quarantined=None
        self._clock=QTimer(self);self._clock.setInterval(5);self._clock.timeout.connect(self._poll)
        from PySide6.QtCore import QCoreApplication
        app=QCoreApplication.instance()
        if app is not None:app.aboutToQuit.connect(self.close)
    def _argv(self,purpose=None):
        if self.command is not None:return list(self.command)
        return (self.purpose if purpose is None else purpose).command()
    def _notify(self,request,value):
        callback=request.callback;request.callback=None
        if callback is not None and not self._closed:callback(value,None)
    def read(self,callback,*,replace=False,purpose=None):
        token=uuid4().hex
        purpose=self.purpose if purpose is None else purpose
        if self._closed:
            QTimer.singleShot(0,self,lambda:callback(purpose.failure('CLOSED'),None));return token
        if self._quarantined is not None:
            QTimer.singleShot(0,self,lambda:callback(purpose.failure('REAP_FAILED'),None));return token
        operation=_ReadOperation(token,callback,self._argv(purpose),self.deadline_ms,self.process_factory,purpose=purpose)
        if self._active is None:
            pending,self._pending=self._pending,None
            if pending is not None:QTimer.singleShot(0,self,lambda:self._notify(pending,pending.purpose.failure('SUPERSEDED')))
            self._start(operation)
        else:
            pending,self._pending=self._pending,operation
            if pending is not None:QTimer.singleShot(0,self,lambda:self._notify(pending,pending.purpose.failure('SUPERSEDED')))
            if replace:self._active.cancel('SUPERSEDED')
        return token
    request=read
    def _start(self,operation):
        self._active=operation;operation.start();self._clock.start()
    def dispatch_ready(self):
        """Deliver only a fully verified/reaped result, without waiting for I/O.

        Explicit Paste uses this before a new user intent, so a ready prior
        Paste is not discarded just because its Qt delivery tick is pending.
        Cloud-listener replacement keeps its existing, separate ordering.
        """
        if self._active is not None and self._active.done.is_set():self._poll()
    def _poll(self):
        operation=self._active
        if operation is None:self._clock.stop();return
        if not operation.done.is_set():return
        self._active=None
        if not operation.reaped:self._quarantined=operation
        try:
            self._notify(operation,operation.result)
            if self._quarantined is not None and self._pending is not None:
                pending,self._pending=self._pending,None;self._notify(pending,pending.purpose.failure('REAP_FAILED'))
        finally:
            if self._active is None and self._pending is not None and not self._closed and self._quarantined is None:
                pending,self._pending=self._pending,None;self._start(pending)
            if self._active is None:self._clock.stop()
    def cancel(self,token):
        if self._pending is not None and self._pending.token==token:self._pending.callback=None;self._pending=None
        if self._active is not None and self._active.token==token:
            self._active.callback=None;self._active.cancel('CANCELLED')
    def close(self):
        if self._closed:return
        self._closed=True
        if self._pending is not None:self._pending.callback=None;self._pending=None
        if self._active is not None:self._active.callback=None;self._active.cancel('CLOSED')
        else:self._clock.stop()
