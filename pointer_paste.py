"""Route a paste only while the pointer is over a visible shelf.

The native boundary never stores keystrokes or reads the clipboard. It only
recognizes the paste chord and queues the already selected target to Qt.
Offscreen verification installs no native hooks or system hotkeys.
"""
from __future__ import annotations

import ctypes
import sys
from PySide6.QtCore import QObject, QEvent, Qt, Signal, QTimer
from PySide6.QtGui import QKeySequence
from platform_native import native_input_available


def pointer_over_shelf(widget,point,*,platform=None,user32=None,quartz=None,window_number=None):
    """Reject an occluded shelf; its Qt visibility flag alone is insufficient."""
    platform=platform or sys.platform
    if user32 is None and quartz is None and not native_input_available():return True
    try:
        if platform=='win32':
            class Point(ctypes.Structure):_fields_=[('x',ctypes.c_long),('y',ctypes.c_long)]
            api=user32 or ctypes.WinDLL('user32',use_last_error=True)
            for name,args,result in (('WindowFromPoint',[Point],ctypes.c_void_p),
                    ('GetAncestor',[ctypes.c_void_p,ctypes.c_uint],ctypes.c_void_p),
                    ('GetCursorPos',[ctypes.POINTER(Point)],ctypes.c_int)):
                fn=getattr(api,name)
                if isinstance(fn,ctypes._CFuncPtr):fn.argtypes=args;fn.restype=result
            native_point=Point()
            if not api.GetCursorPos(ctypes.byref(native_point)):return False
            hit=api.WindowFromPoint(native_point)
            return bool(hit and api.GetAncestor(hit,2)==api.GetAncestor(int(widget.winId()),2))
        if platform=='darwin':
            if quartz is None:import Quartz as quartz
            if window_number is None:
                import objc
                view=objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
                window_number=int(view.window().windowNumber())
            windows=quartz.CGWindowListCopyWindowInfo(quartz.kCGWindowListOptionOnScreenOnly|quartz.kCGWindowListExcludeDesktopElements,quartz.kCGNullWindowID) or []
            for item in windows:
                bounds=item.get(quartz.kCGWindowBounds,{})
                if (item.get(quartz.kCGWindowAlpha,1)>0 and bounds.get('X',0)<=point.x()<bounds.get('X',0)+bounds.get('Width',0)
                        and bounds.get('Y',0)<=point.y()<bounds.get('Y',0)+bounds.get('Height',0)):
                    return item.get(quartz.kCGWindowNumber)==window_number
    except (OSError,ImportError,AttributeError,RuntimeError,ValueError,TypeError):return False
    return False


class _KeyboardInput(ctypes.Structure):
    _fields_=[('key',ctypes.c_uint32),('scan',ctypes.c_uint32),
              ('flags',ctypes.c_uint32),('time',ctypes.c_uint32),('extra',ctypes.c_size_t)]


class WinPointerPaste:
    """WH_KEYBOARD_LL runs on the installing Qt thread, before normal delivery."""
    def __init__(self, accept, *, user32=None, kernel32=None):
        self.accept=accept;self.available=False;self.permission_required=False
        self._closed=False;self._held=set();self._hook=None
        self.api=user32 or ctypes.WinDLL('user32',use_last_error=True)
        kernel32=kernel32 or ctypes.WinDLL('kernel32',use_last_error=True)
        callback_type=getattr(ctypes,'WINFUNCTYPE',ctypes.CFUNCTYPE)(ctypes.c_ssize_t,ctypes.c_int,ctypes.c_size_t,ctypes.c_ssize_t)
        self._callback=callback_type(self._event)
        for name,args,result in (
            ('SetWindowsHookExW',[ctypes.c_int,callback_type,ctypes.c_void_p,ctypes.c_uint32],ctypes.c_void_p),
            ('CallNextHookEx',[ctypes.c_void_p,ctypes.c_int,ctypes.c_size_t,ctypes.c_ssize_t],ctypes.c_ssize_t),
            ('UnhookWindowsHookEx',[ctypes.c_void_p],ctypes.c_int),
            ('GetAsyncKeyState',[ctypes.c_int],ctypes.c_short)):
            fn=getattr(self.api,name)
            if isinstance(fn,ctypes._CFuncPtr):fn.argtypes=args;fn.restype=result
        if isinstance(kernel32.GetModuleHandleW,ctypes._CFuncPtr):
            kernel32.GetModuleHandleW.argtypes=[ctypes.c_wchar_p];kernel32.GetModuleHandleW.restype=ctypes.c_void_p
        self._hook=self.api.SetWindowsHookExW(13,self._callback,kernel32.GetModuleHandleW(None),0)
        self.available=bool(self._hook)

    def _event(self,code,message,address):
        if code>=0 and not self._closed and address:
            try:
                key=_KeyboardInput.from_address(address).key
                pressed=message in (0x100,0x104);released=message in (0x101,0x105)
                if key in self._held:
                    if released:self._held.discard(key)
                    if pressed or released:return 1
                if pressed and key in (0x56,0x2D):
                    down=lambda vk:bool(self.api.GetAsyncKeyState(vk)&0x8000)
                    ctrl,shift,alt,meta=down(0x11),down(0x10),down(0x12),down(0x5B) or down(0x5C)
                    paste=(key==0x56 and ctrl and not shift or key==0x2D and shift and not ctrl) and not alt and not meta
                    if paste and self.accept():self._held.add(key);return 1
            except (OSError,RuntimeError,ValueError,TypeError):pass
        return self.api.CallNextHookEx(self._hook,code,message,address)

    def close(self):
        if self._closed:return
        self._closed=True
        if self._hook:self.api.UnhookWindowsHookEx(self._hook)
        self._hook=None;self._held.clear();self.available=False


class MacPointerPaste:
    """A modifying session event tap needs the user's Accessibility permission."""
    def __init__(self,accept,*,quartz=None):
        if quartz is None:
            import Quartz as quartz
        self.api=quartz;self.accept=accept;self.available=False
        self.permission_required=True;self._held=False;self._closed=False
        self._tap=None;self._source=None;self._callback=self._event
        mask=(1<<quartz.kCGEventKeyDown)|(1<<quartz.kCGEventKeyUp)
        self._tap=quartz.CGEventTapCreate(quartz.kCGSessionEventTap,quartz.kCGHeadInsertEventTap,
            quartz.kCGEventTapOptionDefault,mask,self._callback,None)
        if self._tap is None:return
        self._source=quartz.CFMachPortCreateRunLoopSource(None,self._tap,0)
        if self._source is None:self.close();return
        quartz.CFRunLoopAddSource(quartz.CFRunLoopGetMain(),self._source,quartz.kCFRunLoopCommonModes)
        quartz.CGEventTapEnable(self._tap,True)
        self.available=True;self.permission_required=False

    def _event(self,proxy,kind,event,context):
        q=self.api
        if self._closed:return event
        if kind in (q.kCGEventTapDisabledByTimeout,q.kCGEventTapDisabledByUserInput):
            self._held=False
            if self._tap:q.CGEventTapEnable(self._tap,True)
            return event
        try:
            if q.CGEventGetIntegerValueField(event,q.kCGKeyboardEventKeycode)!=9:return event
            if self._held:
                if kind==q.kCGEventKeyUp:self._held=False
                return None
            flags=q.CGEventGetFlags(event)
            modifiers=q.kCGEventFlagMaskCommand|q.kCGEventFlagMaskControl|q.kCGEventFlagMaskAlternate|q.kCGEventFlagMaskShift
            if kind==q.kCGEventKeyDown and flags&modifiers==q.kCGEventFlagMaskCommand and self.accept():
                self._held=True;return None
        except (RuntimeError,ValueError,TypeError):pass
        return event

    def close(self):
        if self._closed:return
        self._closed=True;self.available=False;self._held=False
        if self._tap:self.api.CGEventTapEnable(self._tap,False)
        if self._source:self.api.CFRunLoopRemoveSource(self.api.CFRunLoopGetMain(),self._source,self.api.kCFRunLoopCommonModes)
        if self._tap:self.api.CFMachPortInvalidate(self._tap)
        self._source=None;self._tap=None


class PointerPasteController(QObject):
    requested=Signal(object)
    permission_changed=Signal()

    def __init__(self,app,target,receive,*,native=True,backend_factory=None):
        super().__init__(app);self.app=app;self.target=target;self.receive=receive
        self._closed=False;self.backend=None;self._factory=backend_factory
        self._native=bool(native and native_input_available())
        self.requested.connect(self._deliver,Qt.ConnectionType.QueuedConnection)
        app.installEventFilter(self)
        self.retry=QTimer(self);self.retry.setInterval(5000);self.retry.timeout.connect(self.retry_native)
        self.retry_native()
        if self._native and sys.platform=='darwin':self.retry.start()

    @property
    def permission_required(self):return bool(self.backend and self.backend.permission_required)

    def retry_native(self):
        if self._closed or self.backend and self.backend.available:return
        if self._factory is None and not self._native:return
        old=self.permission_required
        if self.backend:self.backend.close()
        try:
            factory=self._factory or (MacPointerPaste if sys.platform=='darwin' else WinPointerPaste)
            self.backend=factory(self._accept_native)
        except (OSError,ImportError,RuntimeError):self.backend=None
        if old!=self.permission_required:self.permission_changed.emit()

    def _accept_native(self):
        if self._closed:return False
        target=self.target()
        if target is None:return False
        self.requested.emit(target);return True

    def _deliver(self,target):
        if not self._closed:self.receive(target)

    def eventFilter(self,watched,event):
        if self._closed or event.type() not in (QEvent.Type.ShortcutOverride,QEvent.Type.KeyPress):return False
        if not event.matches(QKeySequence.StandardKey.Paste):return False
        target=self.target()
        if target is None:return False
        event.accept()
        if event.type()==QEvent.Type.KeyPress and not event.isAutoRepeat():self.receive(target)
        return True

    def request_permission(self):
        if sys.platform=='darwin':
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'))

    def close(self):
        if self._closed:return
        self._closed=True;self.retry.stop();self.app.removeEventFilter(self)
        if self.backend:self.backend.close()
