"""Windows Shell item selection, bounded per-owner background requests.

SHParseDisplayName may block on Shell providers, so no native call is made on
the GUI entry path. A successful HRESULT means request accepted, not visual
proof that Explorer selected an item; hosted GUI QA checks that separately.
"""
import ctypes
import ntpath
import os
import sys
import threading
import weakref
from PySide6.QtCore import QObject,QEvent,Signal,Qt,QCoreApplication
from shiboken6 import isValid


def _load_api():
    if sys.platform!='win32':raise OSError('Windows Shell selection is only available on Windows')
    shell=ctypes.WinDLL('shell32',use_last_error=True);ole=ctypes.WinDLL('ole32',use_last_error=True)
    hresult=ctypes.c_int32;dword=ctypes.c_uint32;pointer=ctypes.c_void_p
    ole.CoInitializeEx.argtypes=[pointer,dword];ole.CoInitializeEx.restype=hresult
    ole.CoUninitialize.argtypes=[];ole.CoUninitialize.restype=None
    ole.CoTaskMemFree.argtypes=[pointer];ole.CoTaskMemFree.restype=None
    shell.SHParseDisplayName.argtypes=[ctypes.c_wchar_p,pointer,ctypes.POINTER(pointer),dword,ctypes.POINTER(dword)]
    shell.SHParseDisplayName.restype=hresult
    shell.SHOpenFolderAndSelectItems.argtypes=[pointer,ctypes.c_uint32,ctypes.POINTER(pointer),dword]
    shell.SHOpenFolderAndSelectItems.restype=hresult
    return shell,ole


def _checked(result,operation):
    code=int(result)&0xffffffff
    if code&0x80000000:raise OSError(operation+' 失败 (HRESULT 0x'+format(code,'08X')+')')
    return code


def select_file(path,cancel):
    """Worker-only absolute item PIDL selection. Allocations/COM are paired."""
    path=os.fspath(path)
    if not isinstance(path,str) or '\0' in path or not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]:
        raise OSError('文件位置必须是有效的 Windows 绝对路径。')
    if cancel.is_set():return False
    shell,ole=_load_api();pidl=ctypes.c_void_p()
    initialized=False
    code=int(ole.CoInitializeEx(None,2))&0xffffffff
    if code==0x80010106:pass  # Already initialized in a different apartment.
    else:
        _checked(code,'CoInitializeEx');initialized=True  # S_FALSE also balances.
    try:
        _checked(shell.SHParseDisplayName(path,None,ctypes.byref(pidl),0,None),'SHParseDisplayName')
        if not pidl.value:raise OSError('Windows Shell 未返回文件位置。')
        if cancel.is_set():return False
        # cidl=0: pidl identifies the absolute item, not its parent directory.
        _checked(shell.SHOpenFolderAndSelectItems(pidl,0,None,0),'SHOpenFolderAndSelectItems')
        return True
    finally:
        if pidl.value:ole.CoTaskMemFree(pidl)
        if initialized:ole.CoUninitialize()


class _OwnerWatch(QObject):
    def __init__(self,owner,lane):
        super().__init__(owner);self.lane=weakref.ref(lane);owner.installEventFilter(self)
    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.Close):
            lane=self.lane()
            if lane is not None:lane.close()
        return False


class _ResultRelay(QObject):
    done=Signal(object,object,object)


class _RevealLane:
    def __init__(self,owner):
        self.owner=weakref.ref(owner);self.active=None;self.pending=None;self.epoch=0;self.closed=False
        self.watch=_OwnerWatch(owner,self);owner.destroyed.connect(self.close)
    def request(self,path,done):
        self.closed=False;self.epoch+=1
        if self.active is not None:
            if self.active['path']==path and not self.active['cancel'].is_set():
                # Identical clicks share the actual in-flight selection.
                self.active.update(epoch=self.epoch,done=done);self.pending=None;return
            self.active['cancel'].set();self.active['done']=None
        self.pending=dict(path=path,done=done,epoch=self.epoch)
        if self.active is None:self._start()
    def _start(self):
        if self.active is not None:return
        owner=self.owner()
        if (self.closed or owner is None or not isValid(owner)
                or hasattr(owner,'isVisible') and not owner.isVisible()):self.pending=None;return
        record=self.pending;self.pending=None
        if record is None:return
        relay=_ResultRelay();record.update(cancel=threading.Event(),relay=relay);self.active=record
        relay.done.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        def work():
            try:result,error=select_file(record['path'],record['cancel']),None
            except Exception as problem:result,error=None,problem
            # Shell namespace extensions/UNC may hang. This per-owner daemon
            # never belongs to Qt's shared pool, which waits at application exit.
            if not QCoreApplication.closingDown():relay.done.emit(record,result,error)
        worker=threading.Thread(target=work,daemon=True,name='FlickDock-Shell-reveal')
        record['worker']=worker;worker.start()
    def _finished(self,record,result,error):
        if record is not self.active:return
        self.active=None
        record['relay'].done.disconnect(self._finished);record['relay'].deleteLater()
        owner=self.owner();done=record['done'];record['done']=None
        if (not self.closed and owner is not None and isValid(owner) and record['epoch']==self.epoch
                and not record['cancel'].is_set() and done is not None):
            done(error)
        if self.active is None and self.pending is not None:self._start()
    def close(self,*_):
        self.closed=True;self.epoch+=1;self.pending=None
        if self.active is not None:self.active['cancel'].set();self.active['done']=None


def request_reveal(owner,path,done=None):
    """At most one active job and one latest pending request per Qt owner."""
    if not isValid(owner) or hasattr(owner,'isVisible') and not owner.isVisible():return
    lane=getattr(owner,'_windows_reveal_lane',None)
    if lane is None:lane=_RevealLane(owner);owner._windows_reveal_lane=lane
    lane.request(os.fspath(path),done)


def cancel_reveal(owner):
    lane=getattr(owner,'_windows_reveal_lane',None)
    if lane is not None:lane.close()
