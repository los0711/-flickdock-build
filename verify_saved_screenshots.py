"""Native saved-file and authorization checks, restricted to disposable macOS CI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

from PySide6.QtCore import QObject,QPoint,Qt,Signal
from PySide6.QtGui import QColor,QImage
from PySide6.QtWidgets import QApplication,QWidget

from clipboard_history_runtime import ClipboardHistory
from clipboard_history_store import ClipboardHistoryStore
from mac_permissions import MacPermissionBackend
from mac_permissions_ui import MacPermissionsDialog


def pause(milliseconds):
    deadline=time.monotonic()+milliseconds/1000
    while time.monotonic()<deadline:
        QApplication.processEvents()
        time.sleep(.005)


def wait_for(predicate,label,seconds=12):
    deadline=time.monotonic()+seconds
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError(label)
        pause(20)


def native_drag(point,delta):
    import Quartz
    def post(kind,at):
        event=Quartz.CGEventCreateMouseEvent(None,kind,(at.x(),at.y()),Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,event)
        pause(24)
    post(Quartz.kCGEventMouseMoved,point)
    post(Quartz.kCGEventLeftMouseDown,point)
    for step in range(1,7):post(Quartz.kCGEventLeftMouseDragged,point+delta*step/6)
    post(Quartz.kCGEventLeftMouseUp,point+delta)


def run(arguments):
    index=arguments.index('--verify-saved-screenshots')
    output=Path(arguments[index+1]).resolve()
    assert (sys.platform=='darwin' and os.environ.get('GITHUB_ACTIONS')=='true'
            and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
            and os.environ.get('QT_QPA_PLATFORM')=='cocoa')
    assert output.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
    root=output.parent/'saved-capture-fixtures'
    root.mkdir(exist_ok=True)
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    report=dict(ok=False,frozen=bool(getattr(sys,'frozen',False)))
    from app_version import APP_VERSION
    report['version']=APP_VERSION

    class Observation(QObject):
        copy_observed=Signal(object)
        def set_local_observation(self,enabled,*,source):return
        def recheck_native_access(self,*,force=False):return

    owner=QWidget()
    manager=SimpleNamespace(app=app,shelf=owner,settings=SimpleNamespace(theme='light',base=root/'profile'),
                            clipboard_sync=Observation(),pointer_paste=None,lan=None)
    runtime=ClipboardHistory(manager,manager.settings.base)
    manager.clipboard_history=runtime
    observed=root/'observed'
    observed.mkdir(exist_ok=True)
    runtime.screenshots.home=observed.resolve()
    dialog=None
    try:
        runtime.set_enabled(True)
        wait_for(lambda:runtime.panel is not None,'History did not load')
        assert runtime.screenshots._stream is not None
        folder=observed/'Captures'
        folder.mkdir(exist_ok=True)
        runtime.set_screenshot_folders([str(folder.resolve())])
        wait_for(lambda:folder.resolve() in runtime.screenshots._folders and not runtime.jobs,
                 'Configured screenshot folder was not activated')
        app.clipboard().setText('Saved screenshot clipboard sentinel')
        pause(120)
        from AppKit import NSPasteboard
        board=NSPasteboard.generalPasteboard()
        change_count=board.changeCount()
        path=observed/'Screenshot 2026-11-21 at 20.31.00.png'
        started=time.monotonic()
        result=subprocess.run(['/usr/sbin/screencapture','-x','-R80,80,160,100',str(path)],
                              capture_output=True,timeout=15)
        assert result.returncode==0,result.stderr.decode(errors='replace')
        wait_for(lambda:runtime.total==1,'Saved system screenshot did not reach history')
        first=runtime.entries[0]['id']
        original=hashlib.sha256(path.read_bytes()).hexdigest()
        report['system_file_only_capture']=True
        report['system_capture_to_history_ms']=round((time.monotonic()-started)*1000)
        assert board.changeCount()==change_count and app.clipboard().text()=='Saved screenshot clipboard sentinel'
        report['capture_left_clipboard_unchanged']=True

        fixture=root/'fixture.bmp'
        image=QImage(47,29,QImage.Format_ARGB32);image.fill(QColor('#43a9bd'))
        assert image.save(str(fixture),'BMP')
        third=folder/'external-tool-418.png'
        # A separate native image-saving process emits real file events, no clipboard writes.
        subprocess.run(['/usr/bin/sips','-s','format','png',str(fixture),'--out',str(third)],
                       capture_output=True,check=True,timeout=15)
        wait_for(lambda:runtime.total==2,'External image save in selected screenshot folder was not captured')
        report['external_file_saver_capture']=True
        assert board.changeCount()==change_count
        key=next(entry['id'] for entry in runtime.entries if entry['id']!=first)
        payload=runtime.store.execute('payload',key)
        actual=QImage.fromData(payload[1])
        assert actual.size()==image.size() and actual.pixelColor(2,2)==image.pixelColor(2,2)
        runtime._saved_screenshot(('image',{'image':actual}))
        wait_for(lambda:not runtime.jobs,'Dedup write did not finish')
        assert runtime.total==2
        report['file_and_clipboard_pixels_deduplicated']=True

        runtime._change('remove',first)
        wait_for(lambda:runtime.total==1,'Delete failed')
        subprocess.run(['/usr/bin/xattr','-w','com.flickdock.qa','unchanged-pixels',str(path)],check=True,timeout=10)
        pause(650)
        assert runtime.total==1,'Unchanged file event restored a deleted history entry'
        runtime.set_enabled(False)
        assert runtime.screenshots._stream is None and not runtime.screenshots._timer.isActive()
        ignored=folder/'disabled-419.png'
        subprocess.run(['/usr/bin/sips','-s','format','png',str(fixture),'--out',str(ignored)],
                       capture_output=True,check=True,timeout=15)
        pause(300)
        runtime.set_enabled(True)
        wait_for(lambda:not runtime.jobs,'History did not reload')
        pause(450)
        assert runtime.total==1
        assert hashlib.sha256(path.read_bytes()).hexdigest()==original
        assert ClipboardHistoryStore(manager.settings.base).execute('page',12,0)[1]==1
        assert runtime.store.execute('payload',key)[1]==payload[1]
        report.update(disable_stops_observation=True,deleted_entry_stays_deleted=True,
                      saved_history_survives_restart=True,original_file_unchanged=True)

        actual_state=MacPermissionBackend().paste()
        from clipboard_history_native import mac_paste_allowed
        assert mac_paste_allowed()==(actual_state=='allowed')
        report['permission_queries_agree']=True

        states=dict(clipboard='allowed',accessibility='denied',input='denied',network='unknown',files='on_demand')
        hidden=[]
        backend=SimpleNamespace(inspect=lambda **_:dict(states),
                                request=lambda kind:hidden.append(not dialog.isVisible()) or True)
        dialog=MacPermissionsDialog(manager,owner,backend=backend)
        dialog.show();dialog.move(45,30);dialog.raise_();dialog.activateWindow()
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        pause(450)
        assert dialog.windowModality()==Qt.NonModal
        assert dialog.windowHandle().transientParent() is None
        import ctypes,objc
        native=objc.objc_object(c_void_p=ctypes.c_void_p(int(dialog.winId()))).window()
        assert native is not None and native.level()==AppKit.NSNormalWindowLevel
        before=dialog.pos()
        point=dialog.title.mapToGlobal(QPoint(40,dialog.title.height()//2))
        native_drag(point,QPoint(82,18))
        wait_for(lambda:(dialog.pos()-before).manhattanLength()>20,'Permission title bar could not drag')
        report['native_permission_title_drags']=True
        dialog.request('accessibility')
        wait_for(lambda:bool(hidden),'Permission setup did not execute')
        assert all(hidden),'Permission dialog covered the OS request'
        states['accessibility']='allowed'
        dialog._returned_to_app()
        pause(180)
        assert dialog.states['accessibility']=='allowed'
        assert dialog.rows['accessibility'][0].text()=='已允许'
        assert dialog.pos()!=before
        report.update(permission_yields_before_request=True,rechecks_grant_on_return=True,
                      nonmodal_normal_level=True,ok=True)
    except Exception:
        report['error']=traceback.format_exc()
        raise
    finally:
        if dialog is not None:dialog.close()
        runtime.close()
        owner.close()
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(run(sys.argv))

