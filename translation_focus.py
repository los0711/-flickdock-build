"""Read focused editor geometry for notice placement, without reading its content."""
import ctypes
from functools import cache
import sys

import logging

from PySide6.QtCore import QObject, QPoint, QRect, QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit

from platform_native import native_input_available
from sync_files import DiskWork


def qt_input_rect(widget):
    if isinstance(widget,(QLineEdit,QPlainTextEdit,QTextEdit)) and not widget.isReadOnly() and widget.isVisible():
        return QRect(widget.mapToGlobal(QPoint()),widget.size())
    return QRect()


def avoid_input(point, area, bubble, obstacle, margin=12):
    """Choose a notice position above the input, then try the lower corners."""
    obstacle=obstacle.intersected(area)
    if obstacle.isEmpty():return point
    padded=obstacle.adjusted(-6,-6,6,6)
    if not bubble.translated(point).intersects(padded):return point
    choices=(QPoint(point.x(),padded.top()-bubble.bottom()-margin),
             QPoint(area.left()+margin-bubble.left(),point.y()),
             QPoint(area.right()-margin-bubble.right(),point.y()),
             QPoint(point.x(),area.top()+margin-bubble.top()))
    for candidate in choices:
        placed=bubble.translated(candidate)
        if area.contains(placed) and not placed.intersects(padded):return candidate
    return point


def _windows_rect(screens):
    from ctypes import wintypes
    from drag_probe import _GuiThreadInfo
    user=ctypes.WinDLL('user32')
    user.GetGUIThreadInfo.argtypes=[wintypes.DWORD,ctypes.POINTER(_GuiThreadInfo)]
    user.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.ClientToScreen.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
    info=_GuiThreadInfo();info.cbSize=ctypes.sizeof(info)
    if not user.GetGUIThreadInfo(0,ctypes.byref(info)) or not info.hwndFocus:return QRect()
    name=ctypes.create_unicode_buffer(128)
    user.GetClassNameW(info.hwndFocus,name,len(name))
    rect=wintypes.RECT()
    control=name.value.lower()
    if (control=='edit' or control.startswith('richedit')) and user.GetWindowRect(info.hwndFocus,ctypes.byref(rect)):
        x,y,w,h=rect.left,rect.top,rect.right-rect.left,rect.bottom-rect.top
    elif info.hwndCaret:
        caret=info.rcCaret
        pt=wintypes.POINT(caret.left,caret.top)
        if not user.ClientToScreen(info.hwndCaret,ctypes.byref(pt)):return QRect()
        x,y,w,h=pt.x-88,pt.y-14,176,max(18,caret.bottom-caret.top)+28
    else:return QRect()
    center=QPoint(x+w//2,y+h//2)
    for sx,sy,sw,sh,dpr in screens:
        if QRect(sx,sy,round(sw*dpr),round(sh*dpr)).contains(center):
            return QRect(sx+round((x-sx)/dpr),sy+round((y-sy)/dpr),round(w/dpr),round(h/dpr))
    return QRect(x,y,w,h)


@cache
def _mac_api():
    p=ctypes.c_void_p
    ax=ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
    cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ax.AXIsProcessTrusted.restype=ctypes.c_bool
    ax.AXUIElementCreateSystemWide.restype=p
    ax.AXUIElementSetMessagingTimeout.argtypes=[p,ctypes.c_float]
    ax.AXUIElementCopyAttributeValue.argtypes=[p,p,ctypes.POINTER(p)]
    ax.AXValueGetValue.argtypes=[p,ctypes.c_int,p];ax.AXValueGetValue.restype=ctypes.c_bool
    cf.CFStringCreateWithCString.argtypes=[p,ctypes.c_char_p,ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype=p
    cf.CFEqual.argtypes=[p,p];cf.CFEqual.restype=ctypes.c_bool
    cf.CFRelease.argtypes=[p];cf.CFRelease.restype=None
    return ax,cf


def _mac_rect():
    ax,cf=_mac_api()
    if not ax.AXIsProcessTrusted():return QRect()
    allocated=[]
    def string(value):
        ref=cf.CFStringCreateWithCString(None,value,0x08000100)
        if ref:allocated.append(ref)
        return ref
    def attr(element,name):
        key=string(name);value=ctypes.c_void_p()
        if key and not ax.AXUIElementCopyAttributeValue(element,key,ctypes.byref(value)) and value.value:
            allocated.append(value.value)
            return value.value
        return None
    try:
        root=ax.AXUIElementCreateSystemWide()
        if not root:return QRect()
        allocated.append(root)
        ax.AXUIElementSetMessagingTimeout(root,.045)
        focused=attr(root,b'AXFocusedUIElement')
        if not focused:return QRect()
        role=attr(focused,b'AXRole')
        roles=(b'AXTextField',b'AXTextArea',b'AXSearchField',b'AXComboBox')
        if not role or not any(cf.CFEqual(role,string(name)) for name in roles):return QRect()
        point=attr(focused,b'AXPosition');size=attr(focused,b'AXSize')
        pair=ctypes.c_double*2;xy=pair();wh=pair()
        if point and size and ax.AXValueGetValue(point,1,xy) and ax.AXValueGetValue(size,2,wh):
            return QRect(round(xy[0]),round(xy[1]),round(wh[0]),round(wh[1]))
        return QRect()
    finally:
        for ref in reversed(allocated):cf.CFRelease(ref)


def native_input_rect(screens,cancel):
    if cancel.is_set():return QRect()
    if sys.platform=='win32':return _windows_rect(screens)
    if sys.platform=='darwin':return _mac_rect()
    return QRect()


class FocusedEditorProbe(QObject):
    changed=Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner
        self.rect=QRect()
        self._job=None
        self._pool=QThreadPool(self);self._pool.setMaxThreadCount(1)
        self._timer=QTimer(self);self._timer.setInterval(280)
        self._timer.timeout.connect(self._poll)

    def prepare(self):
        self.rect=qt_input_rect(QApplication.focusWidget())

    def start(self):
        if native_input_available() and not self._timer.isActive():
            self._timer.start();self._poll()

    def stop(self):
        self._timer.stop()
        if self._job is not None:self._job.cancel.set()

    def _poll(self):
        if not self.owner.isVisible() or self._job is not None:return
        rect=qt_input_rect(QApplication.focusWidget())
        if not rect.isEmpty():
            self._set_rect(rect);return
        screens=[(*screen.geometry().getRect(),screen.devicePixelRatio()) for screen in QApplication.screens()]
        self._job=DiskWork(native_input_rect,screens)
        self._job.signals.done.connect(self._arrived,Qt.QueuedConnection)
        self._pool.start(self._job)

    def _arrived(self,rect,error):
        job,self._job=self._job,None
        if not self.owner.isVisible() or not self._timer.isActive() or job.cancel.is_set():return
        if error is not None:
            logging.getLogger(__name__).debug('Focused editor geometry unavailable: %s',type(error).__name__)
        elif rect is not None:
            self._set_rect(rect)

    def _set_rect(self,rect):
        if rect==self.rect:return
        self.rect=rect
        self.changed.emit()
