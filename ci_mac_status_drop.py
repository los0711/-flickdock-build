"""CI-only Cocoa tray attach/menu/destination-method proof, never Finder drag.

Run in a separate macOS CI process with a 25-second outer timeout. A real Qt
tray/button and actual NSMenu tracking are required; locator failures fail.
Only app-local synthetic NSEvents and a private named NSPasteboard are used.
"""
import json,os,sys,tempfile,time
from pathlib import Path


def verify(output):
    if sys.platform!='darwin' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('This proof is restricted to a disposable macOS CI runner')
    os.environ['QT_QPA_PLATFORM']='cocoa'
    import AppKit,Foundation,objc,ctypes
    from PySide6.QtCore import Qt,QPoint
    from PySide6.QtGui import QIcon,QPixmap
    from PySide6.QtWidgets import QApplication,QSystemTrayIcon,QMenu,QDialog
    from PySide6.QtTest import QTest
    from mac_status_drop import MacStatusDrop,locate_status_button
    from flickdock import ShelfManager
    from types import SimpleNamespace
    app=QApplication.instance() or QApplication(['FlickDock native status drop proof']);app.setQuitOnLastWindowClosed(False)
    pixmap=QPixmap(18,18);pixmap.fill(Qt.black)
    tray=QSystemTrayIcon(QIcon(pixmap),app);menu=QMenu();shelf_calls=[];activations=[];menu_opens=[];native_menus=[];timers=[]
    menu.addAction('打开总览',lambda:shelf_calls.append('explicit-overview'))
    menu.aboutToShow.connect(lambda:menu_opens.append(True));tray.setContextMenu(menu)
    manager=SimpleNamespace(tray=tray,show_first=lambda:shelf_calls.append('unexpected-preview'),_quitting=False)
    def activated(reason):
        activations.append(int(reason.value));ShelfManager.tray_activated(manager,reason)
    tray.activated.connect(activated);tray.show();bridge=None;observer=None;board=None;preview=None
    result={'ok':False,'proof':'native Qt tray/button attach + native menu tracking + direct Cocoa dragging destination methods; no real Finder drag','qt_platform':app.platformName()}
    try:
        deadline=time.monotonic()+4;match=None
        while time.monotonic()<deadline:
            app.processEvents();match=locate_status_button(tray,AppKit,__import__('Quartz'))
            if match:break
            time.sleep(.02)
        if not match:raise AssertionError('Real Qt tray NSStatusBarButton not found; cannot skip native attachment proof')
        window,button=match
        def identity(value):return int(objc.pyobjc_id(value)) if value is not None else None
        def state():
            return dict(target=identity(button.target()),action=str(button.action()),menu=identity(button.menu()),delegate=identity(window.delegate()),types=list(button.registeredDraggedTypes()))
        before=state();entered=[];exited=[];dropped=[]
        bridge=MacStatusDrop(tray,lambda point:entered.append(point.toTuple()),lambda:exited.append(True),lambda paths,point:dropped.append(paths) or True,parent=app)
        if not bridge.attached:raise AssertionError(bridge.diagnostic())
        view=bridge.backend.view
        if state()!=before:raise AssertionError('Qt target/action/menu/delegate/types changed during attachment')
        if view.superview()!=button or view.acceptsFirstResponder() or view.isOpaque():raise AssertionError('Receiver is not a transparent nonresponder inside the exact native button')
        def began(notification):
            native_menu=notification.object();native_menus.append(identity(native_menu))
            timer=Foundation.NSTimer.timerWithTimeInterval_repeats_block_(.08,False,lambda _:native_menu.cancelTracking())
            timers.append(timer);Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(timer,AppKit.NSEventTrackingRunLoopMode)
        center=Foundation.NSNotificationCenter.defaultCenter()
        observer=center.addObserverForName_object_queue_usingBlock_(AppKit.NSMenuDidBeginTrackingNotification,None,None,began)
        rect=button.bounds();location=button.convertPoint_toView_((rect.size.width/2,rect.size.height/2),None)
        def event(kind):
            return AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                kind,location,0,Foundation.NSProcessInfo.processInfo().systemUptime(),window.windowNumber(),None,1,1,1.)
        # This event is queued only to this CI application's AppKit loop. It
        # prevents a native button mouseDown track from waiting for real input.
        AppKit.NSApplication.sharedApplication().postEvent_atStart_(event(AppKit.NSEventTypeLeftMouseUp),False)
        view.mouseDown_(event(AppKit.NSEventTypeLeftMouseDown));app.processEvents()
        if not menu_opens or not native_menus:raise AssertionError('Forwarded mouseDown did not actually begin tracking the existing native Qt menu')
        if shelf_calls:raise AssertionError('Tray click opened a shelf instead of only the menu')
        class FlickDockCIDragInfo(AppKit.NSObject):
            def draggingPasteboard(self):return self.board
            def draggingSequenceNumber(self):return 173
            draggingSequenceNumber=objc.selector(draggingSequenceNumber,signature=objc._C_NSInteger+b'@:')
            def draggingSourceOperationMask(self):return AppKit.NSDragOperationCopy
            draggingSourceOperationMask=objc.selector(draggingSourceOperationMask,signature=objc._C_NSUInteger+b'@:')
        with tempfile.TemporaryDirectory(prefix='flickdock-native-status-') as temp:
            source=Path(temp)/'原始文件.txt';source.write_bytes(b'original immutable file bytes')
            board=AppKit.NSPasteboard.pasteboardWithUniqueName();board.clearContents()
            if not board.writeObjects_([Foundation.NSURL.fileURLWithPath_(str(source))]):raise AssertionError('Private named file-URL pasteboard write failed')
            info=FlickDockCIDragInfo.alloc().init();info.board=board
            operation=int(view.draggingEntered_(info));prepared=bool(view.prepareForDragOperation_(info));accepted=bool(view.performDragOperation_(info));duplicate=bool(view.performDragOperation_(info));view.concludeDragOperation_(info)
            if operation!=int(AppKit.NSDragOperationCopy) or not prepared or not accepted or duplicate:raise AssertionError('Native destination methods did not accept exactly one copy')
            if dropped!=[[str(source)]] or source.read_bytes()!=b'original immutable file bytes':raise AssertionError('Native destination method changed paths/bytes or delivered twice')
        preview=QDialog();preview.setWindowFlags(Qt.Window|Qt.FramelessWindowHint)
        preview.setAttribute(Qt.WA_ShowWithoutActivating);preview.presentation_kind='drag';preview.resize(180,100);preview.show();app.processEvents()
        native_view=objc.objc_object(c_void_p=ctypes.c_void_p(int(preview.winId())));native_window=native_view.window()
        original_order=(int(native_window.level()),int(native_window.collectionBehavior()))
        bridge.keep_preview_on_top(preview);app.processEvents()
        raised=int(native_window.level())
        if raised<int(AppKit.NSStatusWindowLevel)+1:raise AssertionError('Real Cocoa preview did not acquire the required native top level')
        native_window.orderBack_(None);QTest.qWait(100);app.processEvents();reasserted=int(native_window.level())
        if reasserted<int(AppKit.NSStatusWindowLevel)+1:raise AssertionError('Real Cocoa preview lost its native level after source-order reassertion')
        preview.hide();app.processEvents();restored_order=(int(native_window.level()),int(native_window.collectionBehavior()))
        if restored_order!=original_order:raise AssertionError(('Real Cocoa preview did not restore original ordering',original_order,restored_order))
        result['native_preview_order']=dict(original=original_order,raised=raised,reasserted=reasserted,restored=restored_order,focus='not measured; app-local CI window, no activation/key-window API used')
        bridge.close();app.processEvents()
        if view.superview() is not None or list(view.registeredDraggedTypes()):raise AssertionError('Receiver registration was not removed')
        if state()!=before:raise AssertionError('Qt native owner state was not preserved after detach')
        result.update(ok=True,tray_rect=tray.geometry().getRect(),button_class=str(button.className()),menu_about_to_show=len(menu_opens),native_menu_tracking=len(native_menus),activation_reasons=activations,shelf_calls=shelf_calls,entered=len(entered),exited=len(exited),dropped_batches=len(dropped),owner_unchanged=True,detached=True)
    except Exception as error:
        result['error']=type(error).__name__+': '+str(error)
        if bridge is not None:result['bridge']=bridge.diagnostic()
    finally:
        if observer is not None:Foundation.NSNotificationCenter.defaultCenter().removeObserver_(observer)
        for timer in timers:timer.invalidate()
        if bridge is not None:bridge.close()
        if preview is not None:preview.hide();preview.deleteLater()
        if board is not None:board.releaseGlobally()
        tray.hide();menu.close();app.processEvents()
        Path(output).parent.mkdir(parents=True,exist_ok=True);Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if not result['ok']:raise AssertionError(result)
    return result


if __name__=='__main__':
    print(json.dumps(verify(sys.argv[1]),ensure_ascii=False))
