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
# Match runtime_macos.py, before importing any Qt Multimedia module.
os.environ['QT_MEDIA_BACKEND']=os.environ.get('FLICKDOCK_PROBE_BACKEND','darwin')
os.environ['QT_MAC_SET_RAISE_PROCESS']='0'
attempt=os.environ.get('FLICKDOCK_PROBE_ATTEMPT','1')
if attempt not in tuple(str(i) for i in range(1,9)):raise ValueError('Invalid isolated probe attempt')
root=(Path('ci-mac-video-probe')/('sink-' if os.environ.get('FLICKDOCK_PROBE_SINK_ONLY')=='1' else 'native-')/attempt).resolve()
root.mkdir(parents=True,exist_ok=True)
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
original_video=os.environ.get('FLICKDOCK_PROBE_ORIGINAL_VIDEO')=='1'
if original_video:
    import media_preview
    media_preview._use_composited_video=lambda:False
original_thumbnail=os.environ.get('FLICKDOCK_PROBE_ORIGINAL_THUMBNAIL','0')
if original_thumbnail in ('1','2'):
    # Exact thumbnail implementation from the user's earlier 1.8.37 source.
    import reference_widgets
    source=Path(__file__).with_name('ci_original_thumbnail.py').read_text(encoding='utf8')
    scope=dict(reference_widgets.__dict__)
    exec(compile(source,'ci_original_thumbnail.py','exec'),scope)
    old_thumbnail=scope['thumbnail']
    if original_thumbnail=='1':reference_widgets.thumbnail=old_thumbnail
    else:
        current_thumbnail=reference_widgets.thumbnail
        def video_thumbnail(shelf,ref,size=40):
            return (old_thumbnail if shelf.content.category(ref)=='video' else current_thumbnail)(shelf,ref,size)
        reference_widgets.thumbnail=video_thumbnail
no_media=os.environ.get('FLICKDOCK_PROBE_NO_MEDIA')=='1'
if no_media:
    # Diagnostic control only: select the same items with decoder creation
    # suppressed to distinguish compositor failures from media lifetime.
    from media_preview import MediaPreview
    MediaPreview._load_player=lambda self,**kwargs:False
if os.environ.get('FLICKDOCK_PROBE_SINK_ONLY')=='1':
    # Diagnostic A/B: keep the same native decoder and first-frame path,
    # but replace QVideoWidget with an ordinary QWidget plus QVideoSink.
    from PySide6.QtMultimedia import QVideoSink
    from PySide6.QtWidgets import QWidget
    from media_preview import _VideoSurface
    class ProbeVideoSurface(QWidget):
        def __init__(self,parent):super().__init__(parent);self.sink=QVideoSink(self)
        def videoSink(self):return self.sink
    def attach_without_native_video_widget(self):
        assert self.native is None
        self.native=ProbeVideoSurface(self)
        self.native.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.native.setGeometry(self.rect());self.native.hide()
        return self.native
    _VideoSurface.attach_video=attach_without_native_video_widget
fixture=workspace.WorkspaceTests();fixture.setUp();shelf=fixture.shelf
original=fixture.base/'probe-h264.mp4';av1=fixture.base/'probe-av1.mp4'
try:
    import imageio_ffmpeg,subprocess
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    command=[ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=1280x720:rate=30',
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
    report=dict(platform=app.platformName(),media_backend=os.environ['QT_MEDIA_BACKEND'],sink_only=os.environ.get('FLICKDOCK_PROBE_SINK_ONLY')=='1',modes={},video=str(original.name))
    # Exercise actual decoding and playback, not just the first poster. The
    # generated test video is silent and the fixture uses a disposable profile.
    if not no_media:
        note('playback-start')
        popup.select_item(str(original));workspace.flush()
        media=popup.preview.media
        workspace.until(lambda:media._poster_sent or media._failed,seconds=12)
        if media._failed or not media._poster_sent:raise AssertionError('H.264 poster did not load')
        if original_video:
            if media.surface.native is None:raise AssertionError('Original video widget was not created')
        elif media.surface.native is not None or media.surface.frame_sink is None:
            raise AssertionError('Cocoa preview created a native video child')
        observed=[]
        def record_frame(frame):
            if frame.isValid():observed.append(frame.startTime())
        media.video_sink.videoFrameChanged.connect(record_frame)
        media.toggle_playback()
        workspace.until(lambda:len(observed)>=5 or media._failed,seconds=8)
        if media._failed or len(observed)<5:raise AssertionError('Video playback did not produce frames')
        screenshot_a=media.surface.grab().toImage()
        seen=len(observed)
        workspace.until(lambda:len(observed)>=seen+3 or media._failed,seconds=6)
        screenshot_b=media.surface.grab().toImage()
        if not original_video and screenshot_a==screenshot_b:raise AssertionError('Video preview did not visibly advance')
        report['playback']=dict(frames=len(observed),visible_frames_differ=None if original_video else True,native_child=original_video)
        popup.selected.clear();popup.primary_selection=None;popup.restore_selected_preview();workspace.flush()
        workspace.until(lambda:media.player is None,seconds=5)
        note('playback-complete')
    for mode in ('list','icons'):
        popup.change_view_mode(mode);popup.resume_content()
        workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()),seconds=10)
        refs=[str(original),str(av1),fixture.paths[0],str(image)]
        samples=[]
        for index in range(50):
            ref=refs[index%len(refs)];item=popup._item_widgets[ref]
            note('click',mode=mode,index=index,kind=('h264' if ref==str(original) else 'av1' if ref==str(av1) else 'other'))
            before=set(popup.selected);expected=before-{ref} if ref in before else {ref}
            point=item.mapTo(shelf,item.rect().center())
            start=time.monotonic();QTest.mouseClick(shelf.windowHandle(),Qt.LeftButton,pos=point)
            workspace.flush();QTest.qWait(30);workspace.flush()
            elapsed=time.monotonic()-start;samples.append(round(elapsed,3))
            if elapsed>2:raise AssertionError(f'click blocked {elapsed:.2f}s ({mode} #{index})')
            if errors:raise AssertionError('Unhandled Qt/Python error')
            if popup.selected!=expected:
                raise AssertionError(f'Native click was intercepted ({mode} #{index}): expected {len(expected)} selected, got {len(popup.selected)}')
            if not shelf.isVisible() or not shelf.expanded or not popup.isVisible():
                raise AssertionError(f'shelf disappeared while selecting ({mode} #{index})')
        # Exercise rapid toggles of the same video and ensure no player survives deselection.
        ref=str(original);item=popup._item_widgets[ref]
        for index in range(20):
            note('video-toggle',mode=mode,index=index)
            before=ref in popup.selected;point=item.mapTo(shelf,item.rect().center())
            QTest.mouseClick(shelf.windowHandle(),Qt.LeftButton,pos=point);QTest.qWait(10);workspace.flush()
            if (ref in popup.selected)==before:raise AssertionError(f'Video toggle intercepted ({mode} #{index})')
        popup.selected.clear();popup.primary_selection=None;popup.restore_selected_preview();workspace.flush()
        workspace.until(lambda:popup.preview.media.player is None,seconds=5)
        windows=[widget for widget in app.topLevelWidgets() if widget is shelf and widget.isVisible()]
        if len(windows)!=1:raise AssertionError(f'wrong shelf window count: {len(windows)}')
        report['modes'][mode]=dict(clicks=70,max_click_seconds=max(samples),single_shelf=True,player_released=True)
    # A selected video must also survive the real overview/compact transition.
    popup.select_item(str(original));workspace.flush()
    if not no_media:workspace.until(lambda:popup.preview.media._poster_sent or popup.preview.media._failed,seconds=12)
    note('collapse-with-video')
    shelf.collapse_contents();workspace.until(lambda:not shelf.geometry_morph.active,seconds=8)
    if shelf.expanded or not shelf.isVisible():raise AssertionError('Compact shelf did not remain visible')
    shelf.show_contents();workspace.until(lambda:not shelf.geometry_morph.active,seconds=8)
    popup.resume_content();workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()),seconds=10)
    from shelf_window import ShelfWindow
    windows=[widget for widget in app.topLevelWidgets() if isinstance(widget,ShelfWindow) and widget.isVisible()]
    if len(windows)!=1 or windows[0] is not shelf:raise AssertionError('Duplicate shelf windows after video transition')
    report['video_collapse_reopen']='one-shelf'
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
