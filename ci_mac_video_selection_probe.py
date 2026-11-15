"""Reproduce repeated video selection on an isolated native Cocoa runner.

Uses generated media, a fake shelf manager and a disposable profile. Never
opens real user files, starts global hooks, or installs an application.
"""
import faulthandler
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

if sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_OS') != 'macOS':
    raise RuntimeError('This probe requires a disposable macOS CI runner')
os.environ['QT_QPA_PLATFORM']='cocoa'
root=Path('ci-mac-video-probe').resolve();root.mkdir(exist_ok=True)
stack_file=(root/'python-stack.txt').open('w',encoding='utf8',buffering=1)
faulthandler.enable(file=stack_file,all_threads=True)
faulthandler.dump_traceback_later(5,repeat=True,file=stack_file)
events=[];messages=[];errors=[]
def note(event,**fields):
    events.append(dict(event=event,at=round(time.monotonic(),3),**fields))
    (root/'breadcrumbs.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in events[-200:]),encoding='utf8')
def unhandled(kind,error,tb):
    errors.append(''.join(traceback.format_exception(kind,error,tb)))
    stack_file.write(errors[-1]);stack_file.flush()
sys.excepthook=unhandled

from PySide6.QtCore import QCoreApplication,QEvent,QPoint,Qt,qInstallMessageHandler
from PySide6.QtGui import QColor,QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
def qt_message(kind,context,message):
    messages.append(str(message)[:2000]);(root/'qt-messages.txt').write_text('\n'.join(messages[-500:]),encoding='utf8')
qInstallMessageHandler(qt_message)
import test_html_native_workspace as workspace
os.environ['QT_QPA_PLATFORM']='cocoa'
assert app.platformName()=='cocoa',app.platformName()
fixture=workspace.WorkspaceTests();fixture.setUp();shelf=fixture.shelf
original=fixture.base/'probe-h264.mp4';av1=fixture.base/'probe-av1.mp4'
try:
    import imageio_ffmpeg,subprocess
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    command=[ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=160x90:rate=12',
             '-t','1.5','-c:v','libx264','-pix_fmt','yuv420p','-y',str(original)]
    subprocess.run(command,check=True,capture_output=True,timeout=30)
    command=[ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=96x54:rate=8',
             '-t','1','-c:v','libaom-av1','-cpu-used','8','-crf','45','-pix_fmt','yuv420p','-y',str(av1)]
    subprocess.run(command,check=True,capture_output=True,timeout=30)
    image=fixture.base/'probe.png';picture=QImage(96,96,QImage.Format_RGB32);picture.fill(QColor('#4477aa'));assert picture.save(str(image))
    shelf.manager.motion=True;shelf.geometry_morph.motion=True
    shelf.add_paths('permanent',[str(original),str(av1),str(image)]);workspace.flush()
    shelf.show_contents();workspace.until(lambda:not shelf.geometry_morph.active,seconds=5)
    popup=shelf.popup;popup.resume_content();workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()),seconds=10)
    report=dict(platform=app.platformName(),modes={},video=str(original.name))
    for mode in ('list','icons'):
        popup.change_view_mode(mode);popup.resume_content()
        workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()),seconds=10)
        refs=[str(original),str(av1),fixture.paths[0],str(image)]
        samples=[]
        for index in range(50):
            ref=refs[index%len(refs)];item=popup._item_widgets[ref]
            note('click',mode=mode,index=index,kind=('h264' if ref==str(original) else 'av1' if ref==str(av1) else 'other'))
            start=time.monotonic();QTest.mouseClick(item,Qt.LeftButton,pos=item.rect().center())
            workspace.flush();QTest.qWait(30);workspace.flush()
            elapsed=time.monotonic()-start;samples.append(round(elapsed,3))
            if elapsed>2:raise AssertionError(f'click blocked {elapsed:.2f}s ({mode} #{index})')
            if errors:raise AssertionError('Unhandled Qt/Python error')
            if not shelf.isVisible() or not shelf.expanded or not popup.isVisible():
                raise AssertionError(f'shelf disappeared while selecting ({mode} #{index})')
        # Exercise rapid toggles of the same video and ensure no player survives deselection.
        ref=str(original);item=popup._item_widgets[ref]
        for index in range(20):
            note('video-toggle',mode=mode,index=index)
            QTest.mouseClick(item,Qt.LeftButton,pos=item.rect().center());QTest.qWait(10);workspace.flush()
        popup.selected.clear();popup.primary_selection=None;popup.restore_selected_preview();workspace.flush()
        workspace.until(lambda:popup.preview.media.player is None,seconds=5)
        windows=[widget for widget in app.topLevelWidgets() if widget is shelf and widget.isVisible()]
        if len(windows)!=1:raise AssertionError(f'wrong shelf window count: {len(windows)}')
        report['modes'][mode]=dict(clicks=70,max_click_seconds=max(samples),single_shelf=True,player_released=True)
    shelf.grab().save(str(root/'native-shelf.png'))
    report['ok']=True;(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    note('complete');print(json.dumps(report),flush=True)
except BaseException:
    detail=traceback.format_exc();stack_file.write(detail);stack_file.flush()
    (root/'result.json').write_text(json.dumps(dict(ok=False,error=detail),indent=2),encoding='utf8')
    try:shelf.grab().save(str(root/'native-shelf-failure.png'))
    except RuntimeError:pass
    raise
finally:
    fixture.tearDown();faulthandler.cancel_dump_traceback_later();qInstallMessageHandler(None);stack_file.close()
