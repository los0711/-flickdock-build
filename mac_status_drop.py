"""A reversible file-drag receiver inside this app's existing Cocoa tray button.

No global monitors, class swizzling, extra status item or replacement Qt menu.
AppKit loads only on a real Cocoa host; tests inject its small native boundary.
"""
import sys,weakref
from PySide6.QtCore import QObject,QEvent,QPoint,QRect,QThread,QTimer
from PySide6.QtGui import QCursor,QGuiApplication
from shiboken6 import isValid

_view_class=None


def local_file_paths(urls):
    """Read URL metadata only. Never stat, resolve, open or follow a path here."""
    if not urls or len(urls)>1000:return []
    result=[];seen=set()
    for url in urls:
        if not url.isFileURL() or str(url.host() or '').lower() not in ('','localhost'):return []
        path=str(url.path() or '')
        if not path.startswith('/') or '\0' in path or len(path)>32768:return []
        if path not in seen:result.append(path);seen.add(path)
    return result


def _children(view):
    if view is None:return []
    return list(view.subviews() or [])


def locate_status_button(tray,appkit,quartz):
    """Only this application's windows, exact Qt/Quartz global rectangle match.

    Quartz window bounds use the same top-left global point convention as
    pointer_over_shelf. There is no guessed primary-screen-height conversion.
    """
    target=QRect(tray.geometry())
    if not tray.isVisible() or target.isEmpty():return None
    matches=[]
    for window in appkit.NSApplication.sharedApplication().windows():
        frame=window.frame();width=float(frame.size.width);height=float(frame.size.height)
        if abs(width-target.width())>2 or abs(height-target.height())>2:continue
        info=quartz.CGWindowListCopyWindowInfo(quartz.kCGWindowListOptionIncludingWindow,int(window.windowNumber())) or []
        bounds=next((item.get(quartz.kCGWindowBounds) for item in info if int(item.get(quartz.kCGWindowNumber,-1))==int(window.windowNumber())),None)
        if not bounds:continue
        rect=QRect(round(bounds['X']),round(bounds['Y']),round(bounds['Width']),round(bounds['Height']))
        if any(abs(a-b)>2 for a,b in zip(rect.getRect(),target.getRect())):continue
        pending=[window.contentView()];visited=0
        while pending and visited<64:
            view=pending.pop();visited+=1
            if view is None:continue
            if view.isKindOfClass_(appkit.NSStatusBarButton):matches.append((window,view))
            pending.extend(_children(view))
    return matches[0] if len(matches)==1 else None


def _bridge(view):
    reference=getattr(view,'bridge',None);return reference() if reference else None


def _forward(view,selector,event):
    button=getattr(view,'original_button',None)
    if button is not None:getattr(button,selector)(event)


def _native_view_type(appkit,objc):
    global _view_class
    if _view_class is not None:return _view_class
    class FlickDockStatusDropView(appkit.NSView):
        def isOpaque(self):return False
        def acceptsFirstResponder(self):return False
        def acceptsFirstMouse_(self,event):return True
        def mouseDown_(self,event):_forward(self,'mouseDown_',event)
        def mouseUp_(self,event):_forward(self,'mouseUp_',event)
        def mouseDragged_(self,event):_forward(self,'mouseDragged_',event)
        def rightMouseDown_(self,event):_forward(self,'rightMouseDown_',event)
        def rightMouseUp_(self,event):_forward(self,'rightMouseUp_',event)
        def rightMouseDragged_(self,event):_forward(self,'rightMouseDragged_',event)
        def otherMouseDown_(self,event):_forward(self,'otherMouseDown_',event)
        def otherMouseUp_(self,event):_forward(self,'otherMouseUp_',event)
        def otherMouseDragged_(self,event):_forward(self,'otherMouseDragged_',event)
        def draggingEntered_(self,sender):
            bridge=_bridge(self);return bridge._entered(sender) if bridge is not None else 0
        draggingEntered_=objc.selector(draggingEntered_,signature=objc._C_NSUInteger+b'@:@')
        def draggingUpdated_(self,sender):
            bridge=_bridge(self);return bridge._updated(sender) if bridge is not None else 0
        draggingUpdated_=objc.selector(draggingUpdated_,signature=objc._C_NSUInteger+b'@:@')
        def draggingExited_(self,sender):
            bridge=_bridge(self)
            if bridge is not None:bridge._ended()
        def prepareForDragOperation_(self,sender):
            bridge=_bridge(self);return bool(bridge is not None and bridge._prepare(sender))
        prepareForDragOperation_=objc.selector(prepareForDragOperation_,signature=objc._C_NSBOOL+b'@:@')
        def performDragOperation_(self,sender):
            bridge=_bridge(self);return bool(bridge is not None and bridge._perform(sender))
        performDragOperation_=objc.selector(performDragOperation_,signature=objc._C_NSBOOL+b'@:@')
        def concludeDragOperation_(self,sender):
            bridge=_bridge(self)
            if bridge is not None:bridge._ended()
        def draggingEnded_(self,sender):
            bridge=_bridge(self)
            if bridge is not None:bridge._ended()
    _view_class=FlickDockStatusDropView;return _view_class


class CocoaStatusDropBackend:
    def __init__(self,*,appkit=None,quartz=None,objc=None,foundation=None,view_factory=None):
        if appkit is None:import AppKit as appkit
        if quartz is None:import Quartz as quartz
        if objc is None:import objc
        if foundation is None:import Foundation as foundation
        self.appkit=appkit;self.quartz=quartz;self.objc=objc;self.foundation=foundation;self.view_factory=view_factory
        self.window=self.button=self.view=None;self.tray_rect=QRect()

    def attach(self,tray,bridge):
        match=locate_status_button(tray,self.appkit,self.quartz)
        if match is None:return False
        window,button=match
        view_type=self.view_factory or _native_view_type(self.appkit,self.objc)
        view=view_type.alloc().initWithFrame_(button.bounds());view.bridge=weakref.ref(bridge);view.original_button=button
        view.setAutoresizingMask_(self.appkit.NSViewWidthSizable|self.appkit.NSViewHeightSizable)
        view.setAccessibilityElement_(False);view.registerForDraggedTypes_([self.appkit.NSPasteboardTypeFileURL])
        button.addSubview_(view)
        self.window,self.button,self.view=window,button,view;self.tray_rect=QRect(tray.geometry());return True

    def healthy(self,tray):
        if self.view is None or self.view.superview()!=self.button:return False
        if self.button.window()!=self.window or not self.window.isVisible():return False
        return bool(tray.isVisible() and QRect(tray.geometry())==self.tray_rect)

    def detach(self):
        view,self.view=self.view,None
        if view is not None:
            view.bridge=None;view.unregisterDraggedTypes();view.removeFromSuperview();view.original_button=None
        self.window=self.button=None;self.tray_rect=QRect()

    def paths(self,sender):
        board=sender.draggingPasteboard()
        if self.appkit.NSPasteboardTypeFileURL not in board.types():return []
        urls=board.readObjectsForClasses_options_([self.foundation.NSURL],{self.appkit.NSPasteboardURLReadingFileURLsOnlyKey:True})
        return local_file_paths(urls)

    def position(self,sender):return QCursor.pos()


class _PreviewOrderLease(QObject):
    def __init__(self,parent):
        super().__init__(parent);self.widget=None;self.stage=0
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._again)

    def hold(self,widget):
        self.clear()
        if widget is None or not isValid(widget) or not widget.isVisible():return
        self.widget=widget;widget.installEventFilter(self);self.stage=0;self._raise();self.timer.start(0)

    def _raise(self):
        from platform_native import raise_status_drop_preview
        widget=self.widget
        if widget is None or not isValid(widget) or not widget.isVisible() or getattr(widget,'presentation_kind','drag')!='drag':return False
        if raise_status_drop_preview(widget):widget._native_revealed=True
        return True

    def _again(self):
        if not self._raise():self.clear();return
        self.stage+=1
        if self.stage==1:self.timer.start(80)

    def clear(self):
        self.timer.stop();widget,self.widget=self.widget,None
        if widget is not None and isValid(widget):
            widget.removeEventFilter(self)
            from platform_native import lower_shelf_after_reveal
            # A new dock/manual intent has already taken ownership of the
            # shared restore slot. An old drag timer must not lower that reveal.
            if getattr(widget,'presentation_kind','drag')=='drag' and lower_shelf_after_reveal(widget):widget._native_revealed=False

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.Close):self.clear()
        return False


class MacStatusDrop(QObject):
    def __init__(self,tray,entered,exited,dropped,parent=None,*,backend=None):
        super().__init__(parent or tray);self.tray=tray;self.entered=entered;self.exited=exited;self.dropped=dropped
        self.closed=False;self.attached=False;self.last_error='';self._key=None;self._consumed=None;self._paths=[];self._active=False
        self.backend=backend
        if backend is None and sys.platform=='darwin' and QGuiApplication.platformName()=='cocoa':
            try:self.backend=CocoaStatusDropBackend()
            except (ImportError,OSError,RuntimeError):self.last_error='native-framework-unavailable'
        self.lease=_PreviewOrderLease(self)
        self.timer=QTimer(self);self.timer.setInterval(750);self.timer.timeout.connect(self.sync)
        tray.destroyed.connect(self.close)
        if self.backend is not None:self.sync();self.timer.start()

    def _main_thread(self):return QThread.currentThread() is self.thread()

    def sync(self):
        if self.closed or self.backend is None:return False
        try:
            if not self.tray.isVisible():
                self._ended();self.backend.detach();self.attached=False;return False
            if self.attached and self.backend.healthy(self.tray):return True
            self._ended();self.backend.detach();self.attached=bool(self.backend.attach(self.tray,self))
            self.last_error='' if self.attached else 'status-button-not-found';return self.attached
        except Exception as error:
            self.attached=False;self.last_error='native-attach-'+type(error).__name__;return False

    def _session_key(self,sender):
        return (int(sender.draggingSequenceNumber()),int(sender.draggingPasteboard().changeCount()))

    def _valid(self,sender):
        if self.closed or not self.attached or not self._main_thread():return False
        if not int(sender.draggingSourceOperationMask())&1:return False
        key=self._session_key(sender)
        if key!=self._key:
            self._key=key;self._paths=self.backend.paths(sender)
        return bool(self._paths and self._consumed!=key)

    def _entered(self,sender):
        try:
            if not self._valid(sender):return 0
            if not self._active:
                if self.entered(self.backend.position(sender)) is False:return 0
                self._active=True
            return 1
        except Exception as error:self.last_error='drag-enter-'+type(error).__name__;return 0

    def _updated(self,sender):return self._entered(sender)

    def _prepare(self,sender):
        try:return self._active and self._valid(sender)
        except Exception as error:self.last_error='drag-prepare-'+type(error).__name__;return False

    def _perform(self,sender):
        try:
            if not self._prepare(sender):return False
            self._consumed=self._key
            return bool(self.dropped(list(self._paths),self.backend.position(sender)))
        except Exception as error:self.last_error='drag-drop-'+type(error).__name__;return False
        finally:self._ended()

    def _ended(self):
        active,self._active=self._active,False;self._paths=[];self._key=None
        if active and not self.closed and self._main_thread():
            try:self.exited()
            except Exception as error:self.last_error='drag-exit-'+type(error).__name__

    def keep_preview_on_top(self,widget):self.lease.hold(widget)

    def diagnostic(self):return dict(attached=self.attached,closed=self.closed,error=self.last_error)

    def close(self,*_):
        if self.closed:return
        self._ended();self.closed=True;self.timer.stop();self.lease.clear()
        if self.backend is not None:
            try:self.backend.detach()
            except Exception as error:self.last_error='native-detach-'+type(error).__name__
        self.attached=False
