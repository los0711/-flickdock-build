"""Fail-closed product factory plus an explicit, hash-bound CI-only probe ABI.

No DLL is searched in cwd/PATH. This is not an enabled product material until
the independent pixel gate establishes a compatible implementation.
"""
import ctypes,hashlib,os,re,sys
from pathlib import Path


class _Region(ctypes.Structure):
    _fields_=[(key,ctypes.c_float) for key in ('x','y','width','height','radius','alpha')]


class NativeProbeLibrary:
    def __init__(self,path,expected_hash):
        if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('Native DLL probe is restricted to disposable Windows CI')
        candidate=Path(path)
        if not candidate.is_absolute() or not re.fullmatch('[0-9a-f]{64}',expected_hash):raise ValueError('Absolute DLL path and trusted build SHA256 required')
        candidate=candidate.resolve(strict=True)
        if hashlib.sha256(candidate.read_bytes()).hexdigest()!=expected_hash:raise ValueError('Native probe DLL hash differs from build evidence')
        self.library=ctypes.WinDLL(str(candidate),winmode=0x00000100|0x00000800)
        self.handle=None;lib=self.library;hr=ctypes.c_long
        lib.fm_create.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_void_p)];lib.fm_create.restype=hr
        lib.fm_update.argtypes=[ctypes.c_void_p,ctypes.POINTER(_Region),ctypes.c_uint];lib.fm_update.restype=hr
        lib.fm_visible.argtypes=[ctypes.c_void_p,ctypes.c_int];lib.fm_visible.restype=hr
        lib.fm_close.argtypes=[ctypes.c_void_p];lib.fm_close.restype=hr
        lib.fm_stats.argtypes=[ctypes.POINTER(ctypes.c_long),ctypes.c_uint];lib.fm_stats.restype=hr
        lib.fm_policy.argtypes=[ctypes.POINTER(ctypes.c_long),ctypes.c_uint];lib.fm_policy.restype=hr
        lib.fm_error.argtypes=[ctypes.c_wchar_p,ctypes.c_uint];lib.fm_error.restype=hr
        lib.fm_shutdown.argtypes=[];lib.fm_shutdown.restype=hr
        self.path=candidate;self.sha256=expected_hash

    def check(self,code):
        if code<0:
            text=ctypes.create_unicode_buffer(1024);self.library.fm_error(text,len(text))
            raise OSError(f'Composition HRESULT 0x{code&0xffffffff:08x}: '+text.value)

    def policy(self):
        values=(ctypes.c_long*2)();self.check(self.library.fm_policy(values,2))
        return dict(advanced_effects=bool(values[0]),high_contrast=bool(values[1]))

    def attach(self,hwnd,mode):
        if self.handle is not None:raise RuntimeError('Probe context already attached')
        handle=ctypes.c_void_p();self.check(self.library.fm_create(hwnd,mode,0,ctypes.byref(handle)));self.handle=handle

    def regions(self,values):
        array=(_Region*len(values))(*[_Region(*item) for item in values]);self.check(self.library.fm_update(self.handle,array,len(values)))

    def visible(self,value):self.check(self.library.fm_visible(self.handle,bool(value)))

    def stats(self):
        values=(ctypes.c_long*4)();self.check(self.library.fm_stats(values,4))
        return dict(contexts=values[0],windows=values[1],visuals=values[2],dispatchers=values[3])

    def close(self):
        if self.handle is not None:
            handle,self.handle=self.handle,None;self.check(self.library.fm_close(handle))

    def shutdown(self):self.close();self.check(self.library.fm_shutdown())


class UnverifiedWindowsMaterial:
    def capabilities(self):
        from window_material import MaterialCapabilities
        return MaterialCapabilities('windows-composition',False,False,'unverified',verified=False)
    def attach(self,owner,role):return False
    def set_regions(self,regions,revision=0):pass
    def set_theme(self,theme):pass
    def set_visible(self,visible):pass
    def set_policy_callback(self,callback):self.policy_callback=callback
    def close(self):pass


def create_backend():return UnverifiedWindowsMaterial()
