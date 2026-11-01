"""Injected Cocoa/Qt boundary only; no tray, native mouse or filesystem probe."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from PySide6.QtCore import QObject,QPoint,QRect
from PySide6.QtWidgets import QWidget
from test_html_native_workspace import APP,flush
from mac_status_drop import MacStatusDrop,CocoaStatusDropBackend,local_file_paths,locate_status_button,_native_view_type,_PreviewOrderLease


class Tray(QObject):
    def __init__(self,rect=QRect(-100,0,24,24)):super().__init__();self.rect=rect;self.visible=True
    def isVisible(self):return self.visible
    def geometry(self):return self.rect


class View:
    @classmethod
    def alloc(cls):return cls()
    def initWithFrame_(self,frame):self._parent=None;self.types=[];self.frame=frame;return self
    def setAutoresizingMask_(self,value):self.autoresize=value
    def setAccessibilityElement_(self,value):self.ax=value
    def registerForDraggedTypes_(self,value):self.types=list(value)
    def unregisterDraggedTypes(self):self.types=[]
    def superview(self):return self._parent
    def removeFromSuperview(self):
        if self._parent:self._parent.children.remove(self)
        self._parent=None
    def subviews(self):return []
    def isKindOfClass_(self,cls):return False


class Button:
    def __init__(self):
        self.children=[];self.owner=None;self.target=object();self.action='native-menu';self.menu=object();self.types=['original.type'];self.events=[]
    def bounds(self):return (0,0,24,24)
    def window(self):return self.owner
    def addSubview_(self,view):self.children.append(view);view._parent=self
    def subviews(self):return list(self.children)
    def isKindOfClass_(self,cls):return cls is Button


class Window:
    def __init__(self,button,number=17):self.button=button;button.owner=self;self.number=number;self.delegate=object();self.visible=True
    def frame(self):return NS(size=NS(width=24,height=24))
    def contentView(self):return self.button
    def windowNumber(self):return self.number
    def isVisible(self):return self.visible


def native_environment(windows):
    kit=NS(NSView=View,NSStatusBarButton=Button,NSViewWidthSizable=1,NSViewHeightSizable=2,NSPasteboardTypeFileURL='public.file-url',NSPasteboardURLReadingFileURLsOnlyKey='filesOnly',
           NSApplication=NS(sharedApplication=lambda:NS(windows=lambda:windows)))
    quartz=NS(kCGWindowListOptionIncludingWindow=1,kCGWindowBounds='bounds',kCGWindowNumber='number',
              CGWindowListCopyWindowInfo=lambda _,number:[{'number':number,'bounds':dict(X=-100,Y=0,Width=24,Height=24)}])
    objc=NS(selector=lambda function,signature:function,_C_NSUInteger=b'Q',_C_NSBOOL=b'Z')
    return kit,quartz,objc


class StatusDropTests(unittest.TestCase):
    def test_local_file_urls_are_lexical_and_mixed_or_remote_paths_rejected(self):
        def url(path,host='',file=True):return NS(isFileURL=lambda:file,host=lambda:host,path=lambda:path)
        with patch('pathlib.Path.stat',side_effect=AssertionError('Native drag must not stat')):
            self.assertEqual(local_file_paths([url('/tmp/a b.txt'),url('/tmp/a b.txt'),url('/Volumes/资料/a')]),['/tmp/a b.txt','/Volumes/资料/a'])
            for urls in ([url('/tmp/file','remote')],[url('relative')],[url('/tmp/\0')],[url('/tmp/file',file=False)],[url('/tmp/a')]*1001):self.assertEqual(local_file_paths(urls),[])

    def test_locator_requires_one_own_status_button_at_exact_negative_screen_coordinates(self):
        tray=Tray();button=Button();window=Window(button);kit,quartz,_=native_environment([window])
        self.assertEqual(locate_status_button(tray,kit,quartz),(window,button))
        tray.rect=QRect(-99,240,24,24);self.assertIsNone(locate_status_button(tray,kit,quartz))
        tray.rect=QRect(-100,0,24,24);second=Window(Button(),18);kit.NSApplication=NS(sharedApplication=lambda:NS(windows=lambda:[window,second]))
        self.assertIsNone(locate_status_button(tray,kit,quartz),'Ambiguous matches cannot attach to the first window')

    def test_native_child_preserves_owner_menu_types_delegate_and_forwards_all_mouse_events(self):
        tray=Tray();button=Button();window=Window(button);kit,quartz,objc=native_environment([window])
        before=(button.target,button.action,button.menu,list(button.types),window.delegate)
        with patch('mac_status_drop._view_class',None):
            viewtype=_native_view_type(kit,objc)
            backend=CocoaStatusDropBackend(appkit=kit,quartz=quartz,objc=objc,foundation=NS(NSURL=object),view_factory=viewtype)
            bridge=MacStatusDrop(tray,Mock(),Mock(),Mock(),backend=backend)
            try:
                self.assertTrue(bridge.attached);view=backend.view;self.assertFalse(view.isOpaque());self.assertFalse(view.acceptsFirstResponder());self.assertFalse(view.ax)
                self.assertEqual(view.types,['public.file-url'])
                with patch('mac_status_drop.locate_status_button',side_effect=AssertionError('Healthy tray must not enumerate native windows')):
                    for _ in range(100):self.assertTrue(backend.healthy(tray))
                    tray.rect=QRect(-76,0,24,24);self.assertFalse(backend.healthy(tray));tray.rect=QRect(-100,0,24,24)
                for name in ('mouseDown_','mouseUp_','mouseDragged_','rightMouseDown_','rightMouseUp_','rightMouseDragged_','otherMouseDown_','otherMouseUp_','otherMouseDragged_'):
                    callback=Mock();setattr(button,name,callback);event=object();getattr(view,name)(event);callback.assert_called_once_with(event)
                self.assertEqual((button.target,button.action,button.menu,button.types,window.delegate),before)
                tray.visible=False;bridge.sync();self.assertFalse(bridge.attached);self.assertEqual(button.children,[]);self.assertEqual(view.types,[])
                tray.visible=True;self.assertTrue(bridge.sync());self.assertEqual(len(button.children),1)
            finally:bridge.close();tray.deleteLater();flush()
        self.assertEqual(button.children,[]);self.assertEqual((button.target,button.action,button.menu,button.types,window.delegate),before)

    def controller(self,entered=None,dropped=None):
        tray=Tray();backend=NS(attach=Mock(return_value=True),detach=Mock(),healthy=Mock(return_value=True),paths=Mock(return_value=['/tmp/original']),position=lambda _:QPoint(-88,12))
        enter=entered or Mock();exit=Mock();drop=dropped or Mock(return_value=True)
        bridge=MacStatusDrop(tray,enter,exit,drop,backend=backend);self.addCleanup(tray.deleteLater);self.addCleanup(bridge.close)
        sender=NS(draggingSequenceNumber=lambda:7,draggingPasteboard=lambda:NS(changeCount=lambda:4),draggingSourceOperationMask=lambda:1)
        return bridge,backend,sender,enter,exit,drop

    def test_real_sequence_caches_metadata_accepts_copy_only_and_delivers_original_paths_once(self):
        bridge,backend,sender,enter,exit,drop=self.controller()
        self.assertEqual(bridge._entered(sender),1)
        for _ in range(100):self.assertEqual(bridge._updated(sender),1)
        backend.paths.assert_called_once();enter.assert_called_once_with(QPoint(-88,12));self.assertTrue(bridge._prepare(sender))
        self.assertTrue(bridge._perform(sender));drop.assert_called_once_with(['/tmp/original'],QPoint(-88,12));exit.assert_called_once()
        self.assertFalse(bridge._perform(sender));self.assertEqual(drop.call_count,1)
        sender.draggingSequenceNumber=lambda:8;sender.draggingSourceOperationMask=lambda:16
        self.assertEqual(bridge._entered(sender),0)

    def test_rejected_enter_off_thread_and_closed_lifecycle_cannot_deliver_drop(self):
        bridge,backend,sender,enter,exit,drop=self.controller(entered=Mock(return_value=False))
        self.assertEqual(bridge._entered(sender),0);self.assertFalse(bridge._prepare(sender));self.assertFalse(bridge._perform(sender));drop.assert_not_called()
        with patch.object(bridge,'_main_thread',return_value=False):self.assertEqual(bridge._entered(sender),0)
        bridge._active=True
        with patch.object(bridge,'_main_thread',return_value=False):bridge._ended()
        exit.assert_not_called()
        bridge.close();self.assertFalse(bridge.timer.isActive());self.assertFalse(bridge._perform(sender));self.assertFalse(bridge.sync())

    def test_locator_failure_is_reported_and_rebuild_restores_only_our_child(self):
        bridge,backend,sender,enter,exit,drop=self.controller()
        backend.healthy.return_value=False;backend.attach.return_value=False
        self.assertFalse(bridge.sync());self.assertEqual(bridge.diagnostic()['error'],'status-button-not-found')
        backend.attach.return_value=True;self.assertTrue(bridge.sync());self.assertEqual(bridge.diagnostic()['error'],'')

    def test_preview_lease_reasserts_without_activation_and_hide_cancels_pending_orders(self):
        widget=QWidget();widget.presentation_kind='drag';widget.show();flush();lease=_PreviewOrderLease(widget)
        try:
            with patch('platform_native.raise_status_drop_preview',return_value=True) as raise_,patch('platform_native.lower_shelf_after_reveal',return_value=True) as lower,patch.object(widget,'activateWindow',side_effect=AssertionError('Activation requested')):
                lease.hold(widget);self.assertEqual(raise_.call_count,1);flush();self.assertEqual(raise_.call_count,2)
                widget.hide();flush();count=raise_.call_count;lease.timer.timeout.emit();self.assertEqual(raise_.call_count,count)
                self.assertFalse(lease.timer.isActive());self.assertIsNone(lease.widget);self.assertFalse(widget._native_revealed);lower.assert_called()
        finally:widget.hide();widget.deleteLater();flush()

    def test_status_level_lease_keeps_original_restore_slot_and_never_makes_key_window(self):
        from platform_native import raise_status_drop_preview
        widget=QWidget();widget.show();flush();window=NS(level=lambda:0,collectionBehavior=lambda:4,setLevel_=Mock(),setCollectionBehavior_=Mock(),orderFrontRegardless=Mock(),makeKeyAndOrderFront_=Mock())
        kit=NS(NSFloatingWindowLevel=3,NSStatusWindowLevel=25,NSWindowCollectionBehaviorCanJoinAllSpaces=1,NSWindowCollectionBehaviorFullScreenAuxiliary=2)
        try:
            self.assertTrue(raise_status_drop_preview(widget,platform='darwin',appkit=kit,native_window=window))
            self.assertEqual(widget._flickdock_native_order,(0,4));window.setLevel_.assert_called_once_with(26);window.makeKeyAndOrderFront_.assert_not_called()
            window.level=lambda:26;self.assertTrue(raise_status_drop_preview(widget,platform='darwin',appkit=kit,native_window=window));self.assertEqual(widget._flickdock_native_order,(0,4))
            widget.hide();count=window.orderFrontRegardless.call_count;self.assertFalse(raise_status_drop_preview(widget,platform='darwin',appkit=kit,native_window=window));self.assertEqual(window.orderFrontRegardless.call_count,count)
        finally:widget.hide();widget.deleteLater();flush()

    def test_old_drag_lease_does_not_lower_a_new_dock_reveal(self):
        widget=QWidget();widget.presentation_kind='drag';widget.show();flush();lease=_PreviewOrderLease(widget)
        try:
            with patch('platform_native.raise_status_drop_preview',return_value=True),patch('platform_native.lower_shelf_after_reveal',return_value=True) as lower:
                lease.hold(widget);widget.presentation_kind='dock';lease.timer.timeout.emit()
                self.assertIsNone(lease.widget);self.assertFalse(lease.timer.isActive());lower.assert_not_called()
        finally:widget.hide();widget.deleteLater();flush()

    def test_failed_status_raise_restores_current_level_without_overwriting_reveal_restore_slot(self):
        from platform_native import raise_status_drop_preview
        widget=QWidget();widget._flickdock_native_order=(0,4);widget.show();flush()
        window=NS(level=lambda:3,collectionBehavior=lambda:7,setLevel_=Mock(side_effect=[RuntimeError('unavailable'),None]),setCollectionBehavior_=Mock(),orderFrontRegardless=Mock())
        kit=NS(NSFloatingWindowLevel=3,NSStatusWindowLevel=25,NSWindowCollectionBehaviorCanJoinAllSpaces=1,NSWindowCollectionBehaviorFullScreenAuxiliary=2)
        try:
            self.assertFalse(raise_status_drop_preview(widget,platform='darwin',appkit=kit,native_window=window))
            self.assertEqual(widget._flickdock_native_order,(0,4));self.assertEqual(window.setLevel_.call_args.args,(3,));window.orderFrontRegardless.assert_not_called()
        finally:widget.hide();widget.deleteLater();flush()


if __name__=='__main__':unittest.main()
