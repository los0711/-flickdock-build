"""Observe real native overview/utility handoff, only on disposable CI."""
import json,os,sys,tempfile,time
from pathlib import Path


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS') not in ('Windows','macOS'):
        raise RuntimeError('Native navigation QA requires an ephemeral CI runner')
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
    output=Path('transfer-navigation-native.json').resolve();trace=[];checks=[]
    with tempfile.TemporaryDirectory(prefix='flickdock-navigation-') as directory:
        os.environ['APPDATA']=directory;os.environ['XDG_CONFIG_HOME']=directory
        from PySide6.QtCore import QTimer,Qt,QMimeData,QUrl
        from PySide6.QtGui import QKeySequence
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        import flickdock
        app=QApplication([]);flickdock.load_font(app)
        manager=flickdock.ShelfManager(app,cocoa_smoke=True);manager.timer.start(20)
        manager.shelf.pinned=False
        started=time.monotonic()
        def sample(event='frame'):
            row=dict(ms=round((time.monotonic()-started)*1000),event=event,
                app_state=app.applicationState().name,shelf_visible=manager.shelf.isVisible(),
                shelf_state=manager.shelf.visible_state,expanded=manager.shelf.expanded,
                transfer_visible=bool(manager.lan_dialog and manager.lan_dialog.isVisible()))
            if sys.platform=='darwin':
                import AppKit
                row['dock_policy']=int(AppKit.NSApplication.sharedApplication().activationPolicy())
            trace.append(row)
        timer=QTimer();timer.setInterval(10);timer.timeout.connect(sample);timer.start()
        def wait(predicate,seconds=5):
            end=time.monotonic()+seconds
            while not predicate() and time.monotonic()<end:app.processEvents();time.sleep(.004)
            assert predicate(),'Native navigation did not settle'
        def spin(seconds):
            end=time.monotonic()+seconds
            while time.monotonic()<end:app.processEvents();time.sleep(.004)
        try:
            for _ in range(2):
                manager.show_overview();wait(lambda:manager.shelf.isVisible() and manager.shelf.expanded)
                manager.show_lan_dialog();wait(lambda:manager.lan_dialog.isVisible())
                manager.shelf.hide_immediately();spin(.05);sample('before-back')
                manager.lan_dialog.back_button.click()
                wait(lambda:not manager.lan_dialog.isVisible());spin(.45);sample('after-back')
                assert manager.shelf.isVisible() and manager.shelf.expanded,'Back closed transfer without a visible overview'
                checks.append('back restores visible unpinned overview')
            manager.show_lan_dialog();wait(lambda:manager.lan_dialog.isVisible());dialog=manager.lan_dialog
            original=Path(directory)/'original.bin';original.write_bytes(b'original native paste fixture')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);app.clipboard().setMimeData(mime)
            native=dialog.winId()
            for mode in ('wan','lan','wan'):
                dialog.mode_buttons[mode].click();app.processEvents()
                assert sum(control.isChecked() for control in dialog.mode_buttons.values())==1
                assert dialog.mode_buttons[mode].parentWidget() is dialog.mode_segment
                QTest.keySequence(dialog.panel.drop_zone,QKeySequence(QKeySequence.StandardKey.Paste));app.processEvents()
                assert dialog.panel._queued_paths==[str(original)] and dialog.winId()==native
            checks.append('native segmented modes keep one window and original-file Paste')
            dialog.close();wait(lambda:not dialog.isVisible())
            shelf=manager.shelf;shelf.pinned=True;accepted=[]
            shelf.import_paths('permanent',[str(original)],on_committed=lambda:accepted.append(True))
            wait(lambda:accepted and bool(shelf.files['permanent']))
            rename_ref=shelf.files['permanent'][-1]
            assert Path(rename_ref).read_bytes()==original.read_bytes()
            rename_order=[]
            for expanded in (False,True):
                shelf.hide_immediately();shelf.show_transient()
                if expanded:shelf.show_contents(animate=False)
                wait(lambda:shelf.isVisible() and shelf.presentation.values['alpha']>=.99 and not shelf.presentation._timer.isActive())
                shelf._raise_for_reveal();shelf.rename_ref(rename_ref)
                rename=shelf._rename_dialog
                wait(lambda:rename.isVisible() and shelf.rename_edit.hasFocus())
                if sys.platform=='win32':
                    import ctypes
                    user=ctypes.windll.user32;user.GetTopWindow.restype=ctypes.c_void_p
                    user.GetWindow.argtypes=[ctypes.c_void_p,ctypes.c_uint];user.GetWindow.restype=ctypes.c_void_p
                    order=[];handle=user.GetTopWindow(None)
                    while handle and len(order)<10000:order.append(int(handle));handle=user.GetWindow(handle,2)
                    owner_id,dialog_id=int(shelf.winId()),int(rename.winId())
                    assert dialog_id in order and owner_id in order and order.index(dialog_id)<order.index(owner_id),order
                    rename_order.append(dict(expanded=expanded,dialog_order=order.index(dialog_id),shelf_order=order.index(owner_id)))
                else:
                    import AppKit,objc,ctypes,Quartz
                    native=lambda widget:objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId()))).window()
                    observation={}
                    def native_ready():
                        owner,modal=native(shelf),native(rename)
                        order=[int(window.windowNumber()) for window in AppKit.NSApplication.sharedApplication().orderedWindows()]
                        visible=Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly|Quartz.kCGWindowListExcludeDesktopElements,Quartz.kCGNullWindowID) or []
                        own=[dict(number=int(info[Quartz.kCGWindowNumber]),index=index,alpha=float(info.get(Quartz.kCGWindowAlpha,0)),layer=int(info.get(Quartz.kCGWindowLayer,0)))
                             for index,info in enumerate(visible) if int(info.get(Quartz.kCGWindowOwnerPID,-1))==os.getpid()]
                        observation.update(event='rename-native-order',expanded=expanded,order=order,
                            quartz_windows=own,modal_is_panel=bool(modal.isKindOfClass_(AppKit.NSPanel)),
                            owner=dict(number=int(owner.windowNumber()),visible=bool(owner.isVisible()),level=int(owner.level()),alpha=float(owner.alphaValue())),
                            modal=dict(number=int(modal.windowNumber()),visible=bool(modal.isVisible()),level=int(modal.level()),alpha=float(modal.alphaValue())),
                            shelf_alpha=shelf.presentation.values['alpha'],focus=shelf.rename_edit.hasFocus())
                        observation['owner_info']=next((info for info in own if info['number']==int(owner.windowNumber())),None)
                        observation['modal_info']=next((info for info in own if info['number']==int(modal.windowNumber())),None)
                        return bool(observation['owner_info'] and observation['modal_info'] and observation['owner_info']['alpha']>0 and observation['modal_info']['alpha']>0)
                    # Reusing a Qt dialog can assign a fresh WindowServer ID.
                    # Focus/Qt visibility precede its asynchronous screen commit.
                    try:wait(native_ready)
                    finally:trace.append(observation)
                    owner_info,modal_info=observation['owner_info'],observation['modal_info']
                    # AppKit orderedWindows explicitly excludes NSPanel. Qt
                    # Dialogs are QNSPanel, so use the actual WindowServer list.
                    assert owner_info and modal_info and owner_info['alpha']>0 and modal_info['alpha']>0,observation
                    assert observation['focus'] and modal_info['index']<owner_info['index'] and observation['modal']['level']>=observation['owner']['level'],observation
                    rename_order.append(dict(expanded=expanded,dialog_order=modal_info['index'],shelf_order=owner_info['index'],dialog_level=observation['modal']['level'],shelf_level=observation['owner']['level']))
                rename.reject();wait(lambda:not rename.isVisible());assert original.exists()
            checks.append('rename is natively above compact and overview with input focus; cancel preserves original')
            report=dict(ok=True,checks=checks,trace=trace,platform=sys.platform,real_native_windows=True)
            report['rename_native_order']=rename_order
        except Exception as error:
            report=dict(ok=False,checks=checks,trace=trace,platform=sys.platform,error=str(error));raise
        finally:
            timer.stop();manager.timer.stop();output.write_text(json.dumps(report,indent=2))
            app.clipboard().clear()
            manager.quit();app.processEvents()
    return 0


if __name__=='__main__':raise SystemExit(main())
