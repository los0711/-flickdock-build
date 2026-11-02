"""An atomically Job-protected Windows helper with only owned stdio inherited.

The job handle is not inherited. Parent death therefore closes its last handle
and KILL_ON_JOB_CLOSE terminates exactly this helper tree, not named processes.
"""
from __future__ import annotations
import ctypes,os,subprocess,sys,threading
from dataclasses import dataclass,field
from win_clipboard_native import native_error,safe_failure

KILL_ON_JOB_CLOSE=0x2000
HANDLE_LIST=0x20002
JOB_LIST=0x2000D

@dataclass
class _Spawn:
    process:object
    thread:object
    pid:int
    stdout:object
    cleanup_error:Exception|None=None
    failed_handles:dict=field(default_factory=dict)

class _Win32:
    def __init__(self,*,api=None,crt=None,kernel=None):
        if api is None or crt is None or kernel is None:
            if sys.platform!='win32':raise OSError('Protected helper requires Windows')
            import _winapi,msvcrt
            api,crt,kernel=_winapi,msvcrt,ctypes.WinDLL('kernel32',use_last_error=True)
        from ctypes import wintypes as w
        self.api=api;self.crt=crt;self.kernel=kernel
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class Io(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_=[('BasicLimitInformation',Basic),('IoInfo',Io),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        self.Extended=Extended
        class Startup(ctypes.Structure):
            _fields_=[('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),
              *[(n,w.DWORD) for n in ('dwX','dwY','dwXSize','dwYSize','dwXCountChars','dwYCountChars','dwFillAttribute','dwFlags')],
              ('wShowWindow',w.WORD),('cbReserved2',w.WORD),('lpReserved2',ctypes.POINTER(w.BYTE)),('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]
        class StartupEx(ctypes.Structure):_fields_=[('StartupInfo',Startup),('lpAttributeList',ctypes.c_void_p)]
        class ProcessInfo(ctypes.Structure):_fields_=[('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]
        self.StartupEx=StartupEx;self.ProcessInfo=ProcessInfo
        for name,args,ret in [('CreateJobObjectW',[ctypes.c_void_p,w.LPCWSTR],w.HANDLE),('SetInformationJobObject',[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD],w.BOOL),('ResumeThread',[w.HANDLE],w.DWORD),
          ('InitializeProcThreadAttributeList',[ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.c_size_t)],w.BOOL),
          ('UpdateProcThreadAttribute',[ctypes.c_void_p,w.DWORD,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p],w.BOOL),
          ('DeleteProcThreadAttributeList',[ctypes.c_void_p],None),
          ('CreateProcessW',[w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,w.DWORD,ctypes.c_void_p,w.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(ProcessInfo)],w.BOOL)]:
            fn=getattr(self.kernel,name);fn.argtypes=args;fn.restype=ret
    def job(self):
        handle=self.kernel.CreateJobObjectW(None,None)
        if not handle:raise native_error('job')
        limits=self.Extended();limits.BasicLimitInformation.LimitFlags=KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error=native_error('job')
            try:self.close(handle)
            except Exception as cleanup:
                # job() has not returned ownership to the constructor yet.
                # Retain a failed close explicitly; never lose or retry the
                # opaque handle when startup is reported as unavailable.
                error._helper_failed_handles={'unconfigured_job':handle}
                error.cleanup_diagnostics=[safe_failure('close',cleanup)]
            raise error
        return handle
    def spawn(self,command,job):
        from ctypes import wintypes as w
        read=write=fd=None;stream=None;info=None
        attributes=None;failure=None;cleanup_errors=[];failed_handles={}
        try:
            read,write=self.api.CreatePipe(None,0);os.set_handle_inheritable(write,True)
            fd=self.crt.open_osfhandle(read,os.O_RDONLY|os.O_BINARY);read=None;stream=os.fdopen(fd,'rb');fd=None
            with open(os.devnull,'rb') as stdin,open(os.devnull,'wb') as stderr:
                ih=self.crt.get_osfhandle(stdin.fileno());eh=self.crt.get_osfhandle(stderr.fileno())
                os.set_handle_inheritable(ih,True);os.set_handle_inheritable(eh,True)
                startup=self.StartupEx();startup.StartupInfo.cb=ctypes.sizeof(startup);startup.StartupInfo.dwFlags=0x100
                startup.StartupInfo.hStdInput=ih;startup.StartupInfo.hStdOutput=write;startup.StartupInfo.hStdError=eh
                size=ctypes.c_size_t();self.kernel.InitializeProcThreadAttributeList(None,2,0,ctypes.byref(size))
                if not 0<size.value<=65536:raise native_error('protect')
                buffer=ctypes.create_string_buffer(size.value)
                if not self.kernel.InitializeProcThreadAttributeList(buffer,2,0,ctypes.byref(size)):raise native_error('protect')
                attributes=buffer
                startup.lpAttributeList=ctypes.cast(attributes,ctypes.c_void_p)
                handles=(w.HANDLE*3)(ih,write,eh);jobs=(w.HANDLE*1)(job)
                for name,values in ((HANDLE_LIST,handles),(JOB_LIST,jobs)):
                    if not self.kernel.UpdateProcThreadAttribute(attributes,0,name,values,ctypes.sizeof(values),None,None):raise native_error('protect')
                process=self.ProcessInfo();line=ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
                # CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT.
                # Job membership is atomic with creation, including parent death
                # before ResumeThread; there is no create-then-assign gap.
                if not self.kernel.CreateProcessW(command[0],line,None,None,True,0x00000004|0x08000000|0x00080000,None,None,ctypes.byref(startup.StartupInfo),ctypes.byref(process)):raise native_error('spawn')
                info=_Spawn(process.hProcess,process.hThread,process.dwProcessId,stream)
        except Exception as error:failure=error
        finally:
            # Do not let one cleanup exception skip the other resources, or
            # override a successful return before child ownership is adopted.
            actions=[]
            if attributes is not None:actions.append(('attributes',None,lambda:self.kernel.DeleteProcThreadAttributeList(attributes)))
            if write is not None:actions.append(('parent_write',write,lambda:self.close(write)))
            if read is not None:actions.append(('parent_read',read,lambda:self.close(read)))
            if info is None and stream is not None:actions.append(('stdout',stream,stream.close))
            if fd is not None:actions.append(('stdout_fd',fd,lambda:os.close(fd)))
            for name,handle,action in actions:
                try:action()
                except Exception as error:
                    cleanup_errors.append(safe_failure('cleanup',error))
                    if handle is not None:failed_handles[name]=handle
            if cleanup_errors:
                error=OSError('Protected helper startup cleanup failed');error.phase='cleanup';error.cleanup_diagnostics=cleanup_errors
                if info is not None:info.cleanup_error=error;info.failed_handles=failed_handles
                else:
                    if failure is None:failure=error
                    failure._helper_failed_handles=failed_handles;failure.cleanup_diagnostics=cleanup_errors
        if failure is not None:raise failure
        return info
    def resume(self,thread):
        if self.kernel.ResumeThread(thread)==0xffffffff:raise native_error('resume')
    def wait(self,process,timeout=None):
        ms=self.api.INFINITE if timeout is None else max(0,int(timeout*1000))
        result=self.api.WaitForSingleObject(process,ms)
        if result==self.api.WAIT_TIMEOUT:raise subprocess.TimeoutExpired('owned-helper',timeout)
        if result!=self.api.WAIT_OBJECT_0:raise native_error('wait')
        return int(self.api.GetExitCodeProcess(process))
    def close(self,handle):self.api.CloseHandle(handle)

class ProtectedProcess:
    """Worker-owned process/pipe; only kill() may cross worker/UI/deadline lanes.

    wait/close remain on the operation worker; no native wait runs on the GUI.
    """
    def __init__(self,command,*,backend=None):
        self._handle_lock=threading.Lock();self._close_errors={};self._failed_handles={};self._close_inflight=set();self.diagnostic=None
        self._api=backend or _Win32();self.args=list(command);self.returncode=None;self._job=None;self._process=None;self._thread=None;self.stdout=None
        spawned=None
        try:
            self._job=self._api.job()
            spawned=self._api.spawn(self.args,self._job);self._process=spawned.process;self._thread=spawned.thread;self.stdout=spawned.stdout;self.pid=spawned.pid
            self._failed_handles.update(spawned.failed_handles)
            self._close_errors.update({name:'Protected helper startup cleanup failed' for name in spawned.failed_handles})
            if spawned.cleanup_error is not None:raise spawned.cleanup_error
            self._api.resume(spawned.thread)
            self._close_handle('_thread')
        except Exception as error:
            # The child is still suspended when protection fails. It never
            # reaches clipboard code and is not left running without a job.
            self._failed_handles.update(getattr(error,'_helper_failed_handles',{}))
            self._close_errors.update({name:'Protected helper startup cleanup failed' for name in self._failed_handles})
            self.diagnostic=safe_failure(getattr(error,'phase','spawn'),error)
            # Cleanup stages are independent. Preserve the owner on failure so
            # the operation can quarantine unresolved handles instead of losing
            # them in an exception from a partially constructed object.
            try:self.kill()
            except Exception as problem:self.diagnostic=safe_failure('close',problem)
            if spawned is not None:
                try:self.returncode=self._api.wait(spawned.process,1.)
                except Exception as problem:self.diagnostic=safe_failure('wait',problem)
            else:self.returncode=0 # No child was created.
            try:
                if self.stdout is not None:self.stdout.close()
            except Exception as problem:self._failed_handles['stdout']=self.stdout;self._close_errors['stdout']='Protected helper pipe close failed';self.diagnostic=safe_failure('close',problem)
            try:self.close()
            except Exception as problem:self.diagnostic=safe_failure('cleanup',problem)
            error._helper_owner=self
            raise
    def poll(self):
        try:return self.wait(0.)
        except subprocess.TimeoutExpired:return None
    def wait(self,timeout=None):
        if self.returncode is None:self.returncode=self._api.wait(self._process,timeout)
        return self.returncode
    def _close_handle(self,name):
        # Claim ownership before entering Win32. No lock spans CloseHandle or
        # wait, and no second caller can close a claimed/recycled handle.
        with self._handle_lock:
            if name in self._close_errors:raise OSError(self._close_errors[name])
            handle=getattr(self,name);setattr(self,name,None)
            if handle is not None:self._close_inflight.add(name)
        if handle is None:return
        try:self._api.close(handle)
        except Exception as error:
            # A backend failure can have ambiguous completion. Retain the
            # ownership evidence, never retry an opaque numeric handle. The
            # operation reports failure/quarantines; parent exit releases any
            # still-open OS handle. This is not successful resource cleanup.
            with self._handle_lock:
                self._failed_handles[name]=handle;self._close_errors[name]='Protected helper handle close failed';self.diagnostic=safe_failure('close',error)
            raise OSError('Protected helper handle close failed') from None
        finally:
            with self._handle_lock:self._close_inflight.discard(name)
    def kill(self):
        # Closing this noninherited job handle also kills any helper children.
        self._close_handle('_job')
    def close(self):
        errors=[]
        for name in ('_job','_thread','_process'):
            try:self._close_handle(name)
            except OSError as error:errors.append(error)
        if errors:raise errors[0]
        with self._handle_lock:
            if self._close_errors:raise OSError('Protected helper resource cleanup failed')
            if self._close_inflight:raise OSError('Protected helper cleanup still pending')

def spawn_owned(command,**options):
    """Standard protected helper creation, no shell, console or extra handles."""
    return ProtectedProcess(command)
