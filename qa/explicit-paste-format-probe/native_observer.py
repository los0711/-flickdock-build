"""HOSTED-ONLY read-only Win format/DROPFILES observer. No Qt or app import."""
from __future__ import annotations
import argparse,ctypes,os,sys
from probe_contract import BoundaryError,GETDATA_EVENT,MAX_FRAME,checked_observer_owner,encode_frame,parse_hdrop,require_hosted,result,safe_error,scope_record

class WinReadAPI:
    def __init__(self):
        require_hosted()
        from ctypes import wintypes as w
        self.u=ctypes.WinDLL('user32',use_last_error=True);self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        signatures=((self.u,'GetClipboardSequenceNumber',[],w.DWORD),(self.u,'IsClipboardFormatAvailable',[w.UINT],w.BOOL),
          (self.u,'GetClipboardOwner',[],w.HWND),(self.u,'GetWindowThreadProcessId',[w.HWND,ctypes.POINTER(w.DWORD)],w.DWORD),
          (self.u,'OpenClipboard',[w.HWND],w.BOOL),(self.u,'CloseClipboard',[],w.BOOL),
          (self.u,'EnumClipboardFormats',[w.UINT],w.UINT),(self.u,'GetClipboardFormatNameW',[w.UINT,w.LPWSTR,ctypes.c_int],ctypes.c_int),
          (self.u,'GetClipboardData',[w.UINT],w.HANDLE),(self.k,'GlobalSize',[w.HGLOBAL],ctypes.c_size_t),
          (self.k,'GlobalLock',[w.HGLOBAL],ctypes.c_void_p),(self.k,'GlobalUnlock',[w.HGLOBAL],w.BOOL),(self.k,'GetACP',[],w.UINT))
        for lib,name,args,ret in signatures:
            fn=getattr(lib,name);fn.argtypes=args;fn.restype=ret
    def owner(self):
        from ctypes import wintypes as w
        hwnd=self.u.GetClipboardOwner();pid=w.DWORD()
        if hwnd:self.u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        return int(pid.value)
    def sequence(self):return int(self.u.GetClipboardSequenceNumber())
    def hdrop_available(self):return bool(self.u.IsClipboardFormatAvailable(15))
    def open(self):return bool(self.u.OpenClipboard(None))
    def close(self):
        if not self.u.CloseClipboard():raise BoundaryError('CLOSE_FAILED')
    def formats(self):
        names={1:'CF_TEXT',2:'CF_BITMAP',7:'CF_OEMTEXT',8:'CF_DIB',13:'CF_UNICODETEXT',15:'CF_HDROP',16:'CF_LOCALE',17:'CF_DIBV5'}
        rows=[];last=0;seen=set()
        while True:
            ctypes.set_last_error(0);current=int(self.u.EnumClipboardFormats(last))
            if not current:
                if ctypes.get_last_error():raise BoundaryError('ENUM_FAILED')
                return rows
            if current in seen or len(rows)>=256:raise BoundaryError('FORMAT_ENUM_LIMIT')
            seen.add(current);last=current;name=names.get(current)
            if name is None:
                buffer=ctypes.create_unicode_buffer(256);size=self.u.GetClipboardFormatNameW(current,buffer,len(buffer))
                name=buffer.value if size else 'STANDARD_'+str(current)
            rows.append(dict(id=current,name=name))
    def copy_hdrop(self):
        # Fixed tiny event cannot carry user data. Parent drains its owned pipe.
        os.write(sys.stdout.fileno(),GETDATA_EVENT)
        handle=self.u.GetClipboardData(15)
        if not handle:raise BoundaryError('GET_DATA_FAILED')
        size=int(self.k.GlobalSize(handle))
        if not 20<=size<=MAX_FRAME:raise BoundaryError('QA_REFERENCE_BLOCK_LIMIT')
        address=self.k.GlobalLock(handle)
        if not address:raise BoundaryError('GLOBAL_LOCK_FAILED')
        try:return ctypes.string_at(address,size)
        finally:
            ctypes.set_last_error(0)
            if not self.k.GlobalUnlock(handle) and ctypes.get_last_error():raise BoundaryError('GLOBAL_UNLOCK_FAILED')
    def ansi_codec(self):return 'cp'+str(self.k.GetACP())

def observe(api,phase,expected_owner):
    """Fake-injectable complete read path; all parsing happens after Close."""
    before=api.sequence();owner=api.owner();available=api.hdrop_available();opened=False;raw=None;formats=[]
    if owner!=expected_owner:return result('unavailable',code='FOREIGN_OR_UNKNOWN_OWNER',owner_pid=owner,sequence_before=before)
    try:
        if not api.open():return result('unavailable',code='BUSY',owner_pid=owner,sequence_before=before)
        opened=True
        if api.owner()!=expected_owner:return result('unavailable',code='OWNER_CHANGED',sequence_before=before)
        formats=api.formats()
        if phase=='paths' and available:raw=api.copy_hdrop()
    finally:
        if opened:api.close()
    after=api.sequence()
    # Explicit observational receipt: changed counter is recorded, not a cloud baseline verdict.
    paths=parse_hdrop(raw,ansi_codec=api.ansi_codec()) if raw is not None else None
    return result(owner_pid=owner,owner_pid_after=api.owner(),sequence_before=before,sequence_after=after,hdrop_available=available,
                  native_formats=formats,paths=paths,phase=phase)

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--scope',required=True);parser.add_argument('--nonce',required=True)
    parser.add_argument('--expected-owner',required=True,type=int);parser.add_argument('--phase',required=True,choices=('metadata','paths'))
    args=parser.parse_args(argv);scope,workspace,record=scope_record(args.scope,args.nonce,role='child')
    checked_observer_owner(scope,args.nonce,record,args.expected_owner)
    try:value=observe(WinReadAPI(),args.phase,args.expected_owner)
    except Exception as cause:value=result('error',code=type(cause).__name__,diagnostic=safe_error(cause))
    sys.stdout.buffer.write(encode_frame(value));sys.stdout.buffer.flush();return 0
if __name__=='__main__':raise SystemExit(main())
