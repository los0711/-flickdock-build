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
import time
import weakref
from PySide6.QtCore import QObject,QEvent,Signal,Qt,QCoreApplication
from shiboken6 import isValid

_automation=None


def _prepare_automation():
    # First import on the GUI entry thread: pythoncom initializes its importing
    # thread automatically. Each daemon then owns exactly its explicit COM ref.
    # No Shell object/query is created here, and dynamic Dispatch never makepys.
    global _automation
    if _automation is None:
        import pythoncom
        from win32com.client.dynamic import Dispatch
        _automation=(pythoncom,Dispatch)
    return _automation


def _resolved(path):
    path=os.path.realpath(os.fspath(path))
    if path.startswith('\\\\?\\UNC\\'):path='\\\\'+path[8:]
    elif path.startswith('\\\\?\\'):path=path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def _select_loaded_view(path,cancel,deadline,client):
    if cancel.is_set():return False
    if time.monotonic()>=deadline:raise TimeoutError('文件定位已超过 6 秒。')
    pythoncom,dispatch=client;folder=_resolved(ntpath.dirname(path));target=_resolved(path)
    shell=document=item=selected=window=matches=None;failure=None;last_error='';called=False
    try:
        shell=dispatch('Shell.Application')
        while time.monotonic()<deadline and not cancel.is_set():
            if not called:
                matches=[]
                try:
                    for index,window in enumerate(shell.Windows()):
                        if index>=64:raise OSError('Windows Shell 窗口数量超过定位上限。')
                        document=window.Document
                        if _resolved(document.Folder.Self.Path)==folder:matches.append(document)
                    if len(matches)>1:raise OSError('存在多个相同目录窗口，无法确认文件定位目标。')
                    document=matches[0] if matches else None
                    item=document.Folder.ParseName(ntpath.basename(path)) if document is not None else None
                except (AttributeError,pythoncom.com_error) as problem:
                    code=getattr(problem,'hresult',None)
                    last_error=('HRESULT 0x'+format(int(code)&0xffffffff,'08X')) if code is not None else str(problem)
                    document=item=None
                if item is not None:
                    if _resolved(item.Path)!=target:raise OSError('Windows Shell 返回的文件与请求位置不一致。')
                    if cancel.is_set():return False
                    # Producer action, once: select, deselect others, show, focus.
                    called=True;document.SelectItem(item,0x1D)
            if called:
                selected=document.SelectedItems();count=int(selected.Count)
                if count==1 and _resolved(selected.Item(0).Path)==target:return True
            pythoncom.PumpWaitingMessages()
            cancel.wait(min(.025,max(0,deadline-time.monotonic())))
        if cancel.is_set():return False
        raise TimeoutError('未能在 6 秒内确认文件选中。'+(' '+last_error if last_error else ''))
    except pythoncom.com_error as problem:
        code=int(problem.hresult)&0xffffffff
        failure=OSError('Windows Shell 文件定位失败 (HRESULT 0x'+format(code,'08X')+')')
    finally:
        # COM proxies stay on this STA and are released before CoUninitialize,
        # including failure paths. No automation proxy crosses the Qt signal.
        shell=document=item=selected=window=matches=None
    if failure is not None:raise failure


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
    client=_prepare_automation();deadline=time.monotonic()+6
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
        return _select_loaded_view(path,cancel,deadline,client)
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
    try:_prepare_automation()
    except (ImportError,OSError) as problem:
        if done is not None:done(OSError('文件定位组件无法加载：'+str(problem)))
        return
    lane=getattr(owner,'_windows_reveal_lane',None)
    if lane is None:lane=_RevealLane(owner);owner._windows_reveal_lane=lane
    lane.request(os.fspath(path),done)


def cancel_reveal(owner):
    lane=getattr(owner,'_windows_reveal_lane',None)
    if lane is not None:lane.close()
