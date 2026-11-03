"""HOSTED-ONLY owned delayed CF_HDROP fixture. Never Explorer/virtual-files QA.

This deliberate renderer blocks forever; only its protected Job is terminated.
The independent observer must reach GetClipboardData, deadline-kill and reap.
"""
from __future__ import annotations
import argparse,ctypes,json,os,sys
from probe_contract import encode_frame,result,safe_error,scope_record

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--scope',required=True);parser.add_argument('--nonce',required=True)
    args=parser.parse_args(argv);scope,workspace,record=scope_record(args.scope,args.nonce,role='child')
    from ctypes import wintypes as w
    user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    PROC=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
    class WindowClass(ctypes.Structure):
        _fields_=[('style',w.UINT),('proc',PROC),('clsExtra',ctypes.c_int),('wndExtra',ctypes.c_int),
                  ('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('background',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
    signatures=((kernel,'GetModuleHandleW',[w.LPCWSTR],w.HMODULE),(kernel,'Sleep',[w.DWORD],None),
      (user,'RegisterClassW',[ctypes.POINTER(WindowClass)],w.ATOM),
      (user,'CreateWindowExW',[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,ctypes.c_void_p],w.HWND),
      (user,'DefWindowProcW',[w.HWND,w.UINT,w.WPARAM,w.LPARAM],ctypes.c_ssize_t),
      (user,'GetMessageW',[ctypes.POINTER(w.MSG),w.HWND,w.UINT,w.UINT],w.BOOL),
      (user,'TranslateMessage',[ctypes.POINTER(w.MSG)],w.BOOL),(user,'DispatchMessageW',[ctypes.POINTER(w.MSG)],ctypes.c_ssize_t),
      (user,'OpenClipboard',[w.HWND],w.BOOL),(user,'EmptyClipboard',[],w.BOOL),(user,'SetClipboardData',[w.UINT,w.HANDLE],w.HANDLE),
      (user,'CloseClipboard',[],w.BOOL),(user,'IsClipboardFormatAvailable',[w.UINT],w.BOOL))
    for lib,name,a,r in signatures:fn=getattr(lib,name);fn.argtypes=a;fn.restype=r
    @PROC
    def procedure(hwnd,message,wp,lp):
        if message==0x305 and wp==15:  # WM_RENDERFORMAT, deliberate owned non-response.
            kernel.Sleep(0xffffffff)
            return 0
        return user.DefWindowProcW(hwnd,message,wp,lp)
    instance=kernel.GetModuleHandleW(None);name='FlickDockExplicitPasteProbe_'+args.nonce[:24]
    wc=WindowClass();wc.proc=procedure;wc.instance=instance;wc.name=name
    if not user.RegisterClassW(ctypes.byref(wc)):raise OSError('REGISTER_FIXTURE_WINDOW_FAILED')
    hwnd=user.CreateWindowExW(0,name,'Owned QA clipboard renderer',0,0,0,1,1,None,None,instance,None)
    if not hwnd:raise OSError('CREATE_FIXTURE_WINDOW_FAILED')
    if not user.OpenClipboard(hwnd):raise OSError('FIXTURE_CLIPBOARD_BUSY')
    try:
        if not user.EmptyClipboard():raise OSError('FIXTURE_EMPTY_FAILED')
        # A NULL return is expected for delayed rendering; availability verifies it.
        user.SetClipboardData(15,None)
        if not user.IsClipboardFormatAvailable(15):raise OSError('FIXTURE_OFFER_FAILED')
    finally:
        if not user.CloseClipboard():raise OSError('FIXTURE_CLOSE_FAILED')
    ready=scope/'blocked-provider-ready.partial'
    ready.write_text(json.dumps({'pid':os.getpid(),'nonce':args.nonce,'controller_pid':os.getppid()}),encoding='utf8')
    ready.replace(scope/'blocked-provider-ready.json')
    message=w.MSG()
    while True:
        code=user.GetMessageW(ctypes.byref(message),None,0,0)
        if code==-1:raise OSError('FIXTURE_MESSAGE_FAILED')
        if code==0:return 0
        user.TranslateMessage(ctypes.byref(message));user.DispatchMessageW(ctypes.byref(message))
if __name__=='__main__':
    try:exit_code=main()
    except Exception as cause:
        sys.stdout.buffer.write(encode_frame(result('error',diagnostic=safe_error(cause))));sys.stdout.buffer.flush();exit_code=1
    raise SystemExit(exit_code)
