"""Hosted-QA fixture window lease. Never terminate a process or send input."""
import ctypes,hashlib,ntpath,time
from ctypes import wintypes


class WindowsEditorAPI:
    def __init__(self,guard):
        guard();self.k=ctypes.WinDLL('kernel32',use_last_error=True);self.u=ctypes.WinDLL('user32',use_last_error=True)
        declarations=((self.k,'OpenProcess',[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.HANDLE),
            (self.k,'CloseHandle',[wintypes.HANDLE],wintypes.BOOL),
            (self.k,'GetProcessId',[wintypes.HANDLE],wintypes.DWORD),
            (self.k,'WaitForSingleObject',[wintypes.HANDLE,wintypes.DWORD],wintypes.DWORD),
            (self.k,'QueryFullProcessImageNameW',[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)],wintypes.BOOL),
            (self.k,'GetProcessTimes',[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)],wintypes.BOOL),
            (self.u,'IsWindow',[wintypes.HWND],wintypes.BOOL),
            (self.u,'GetWindowThreadProcessId',[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)],wintypes.DWORD),
            (self.u,'GetWindowTextLengthW',[wintypes.HWND],ctypes.c_int),
            (self.u,'GetWindowTextW',[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int],ctypes.c_int),
            (self.u,'PostMessageW',[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM],wintypes.BOOL))
        for library,name,args,result in declarations:
            fn=getattr(library,name);fn.argtypes=args;fn.restype=result
    def open(self,pid):
        # QUERY_LIMITED_INFORMATION | SYNCHRONIZE only; no TERMINATE right.
        handle=self.k.OpenProcess(0x1000|0x100000,False,pid)
        if not handle:raise OSError('FIXTURE_EDITOR_OPEN')
        return handle
    def close(self,handle):
        if not self.k.CloseHandle(handle):raise OSError('FIXTURE_EDITOR_HANDLE')
    def alive(self,handle):
        value=self.k.WaitForSingleObject(handle,0)
        if value not in (0,258):raise OSError('FIXTURE_EDITOR_WAIT')
        return value==258
    def identity(self,handle):
        pid=int(self.k.GetProcessId(handle));times=[wintypes.FILETIME() for _ in range(4)]
        if not pid or not self.k.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):raise OSError('FIXTURE_EDITOR_IDENTITY')
        size=wintypes.DWORD(32768);buffer=ctypes.create_unicode_buffer(size.value)
        if not self.k.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):raise OSError('FIXTURE_EDITOR_IMAGE')
        return pid,(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime,ntpath.normcase(ntpath.normpath(buffer.value))
    def window_exists(self,hwnd):return bool(self.u.IsWindow(hwnd))
    def window_pid(self,hwnd):
        value=wintypes.DWORD();self.u.GetWindowThreadProcessId(hwnd,ctypes.byref(value));return value.value
    def post_close(self,hwnd):
        if not self.u.PostMessageW(hwnd,0x0010,0,0):raise OSError('FIXTURE_EDITOR_POST')
    def title_stamp(self,hwnd):
        length=int(self.u.GetWindowTextLengthW(hwnd))
        if not 0<length<=32768:raise OSError('FIXTURE_EDITOR_TITLE')
        buffer=ctypes.create_unicode_buffer(length+1)
        if self.u.GetWindowTextW(hwnd,buffer,len(buffer))!=length:raise OSError('FIXTURE_EDITOR_TITLE')
        # Internal only: neither title text nor its digest enters diagnostics.
        return hashlib.sha256(buffer.value.encode('utf-16-le',errors='surrogatepass')).digest()


class FixtureEditorLease:
    def __init__(self,api,handle,pid,hwnd,identity,title,verify,guard):
        self.api=api;self.handle=handle;self.pid=pid;self.hwnd=hwnd;self.identity=identity
        self.title=title;self.verify=verify;self.guard=guard
    @classmethod
    def capture(cls,api,*,pid,hwnd,image,verify,guard):
        handle=None
        try:
            guard()
            if type(pid) is not int or type(hwnd) is not int or pid<=0 or hwnd<=0:raise ValueError()
            if image not in ('notepad.exe','wordpad.exe','write.exe'):raise ValueError()
            handle=api.open(pid);identity=api.identity(handle);title=api.title_stamp(hwnd)
            if (not api.alive(handle) or identity[0]!=pid or ntpath.basename(identity[2])!=image
                    or not api.window_exists(hwnd) or api.window_pid(hwnd)!=pid or verify() is not True
                    or api.title_stamp(hwnd)!=title):raise ValueError()
            return cls(api,handle,pid,hwnd,identity,title,verify,guard)
        except Exception:
            if handle is not None:
                try:api.close(handle)
                except Exception:raise RuntimeError('FIXTURE_EDITOR_CAPTURE_RELEASE_FAILED') from None
            raise RuntimeError('FIXTURE_EDITOR_CAPTURE_REFUSED') from None
    def cleanup(self,*,timeout=7.,clock=time.monotonic,sleep=time.sleep):
        result={'status':'unavailable','close_requested':False,'handle_released':False}
        if self.handle is None:return dict(status='already_released',close_requested=False,handle_released=True)
        try:
            self.guard();deadline=clock()+timeout
            if not self.api.window_exists(self.hwnd):result['status']='already_closed';return result
            if not self.api.alive(self.handle):result['status']='already_exited';return result
            if (self.api.identity(self.handle)!=self.identity or self.api.window_pid(self.hwnd)!=self.pid
                    or self.api.title_stamp(self.hwnd)!=self.title):
                result['status']='identity_changed';return result
            if self.verify() is not True:result['status']='identity_changed';return result
            self.guard()
            if clock()>=deadline:result['status']='timeout';return result
            if (not self.api.alive(self.handle) or self.api.identity(self.handle)!=self.identity
                    or not self.api.window_exists(self.hwnd) or self.api.window_pid(self.hwnd)!=self.pid
                    or self.api.title_stamp(self.hwnd)!=self.title):
                result['status']='identity_changed';return result
            self.api.post_close(self.hwnd);result['close_requested']=True
            while self.api.window_exists(self.hwnd):
                if clock()>=deadline:result['status']='timeout';return result
                sleep(min(.025,max(0,deadline-clock())))
            result['status']='closed';return result
        except Exception:return result
        finally:
            try:self.api.close(self.handle);result['handle_released']=True
            except Exception:result['status']='handle_release_failed'
            self.handle=None
