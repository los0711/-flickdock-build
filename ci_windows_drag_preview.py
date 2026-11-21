"""Host a genuine cross-process Windows OLE drag on a disposable CI desktop."""
import ctypes
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

from PySide6.QtCore import QEvent, QEventLoop, QMimeData, QObject, QPoint, QTimer, QUrl, Qt
from PySide6.QtGui import QCursor, QDrag
from PySide6.QtWidgets import QApplication, QWidget


def source(root):
    app=QApplication([])

    class Source(QWidget):
        def mouseMoveEvent(self,event):
            if not event.buttons()&Qt.LeftButton:return
            mime=QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(root/'ole-fixture.txt'))])
            drag=QDrag(self);drag.setMimeData(mime)
            (root/'started').touch()
            action=drag.exec(Qt.CopyAction)
            (root/'result').write_text(action.name,encoding='utf-8')

    window=Source()
    window.setGeometry(70,95,250,160)
    window.show();window.raise_();window.activateWindow()
    (root/'source-point').write_text(json.dumps(window.geometry().center().toTuple()),encoding='utf-8')
    return app.exec()


class DragEvents(QObject):
    def __init__(self,shelf):
        super().__init__(shelf)
        self.shelf=shelf
        self.samples=[]

    def eventFilter(self,widget,event):
        kind=event.type()
        if kind in (QEvent.DragEnter,QEvent.DragMove,QEvent.Drop):
            if getattr(widget,'window',lambda:None)() is self.shelf:
                self.samples.append((kind.name,event.source() is None))
        return False


def timing(samples):
    if len(samples)<2:return {'frames':len(samples)}
    gaps=[(next_-previous)*1000 for previous,next_ in zip(samples,samples[1:])]
    return dict(frames=len(samples),paint_hz=round(1000./statistics.mean(gaps),2),
                p50_ms=round(statistics.median(gaps),2),
                p95_ms=round(sorted(gaps)[math.ceil(len(gaps)*.95)-1],2),
                max_ms=round(max(gaps),2))


def wait_ms(milliseconds):
    # Let the native dispatcher sleep until its nearest deadline. qWait's
    # polling sleeps introduce their own cadence into short frame tests.
    loop=QEventLoop()
    timer=QTimer()
    timer.setSingleShot(True);timer.setTimerType(Qt.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(milliseconds)
    loop.exec()


def run(root,population):
    import flickdock
    from display_triggers import display_id
    from flickdock_motion import MotionShadow
    from shelf_ui_common import load_font

    app=QApplication([]);load_font(app)
    manager=flickdock.ShelfManager(app,cocoa_smoke=True)
    shelf=manager.shelf
    screen=manager.edge.display
    manager.settings.update(dock_edge='right',dock_thickness=14,dock_position=48,
        dock_length=105,keep_empty_handle=True,trigger_visible=True,trigger_view='compact',
        capture_mode='reference',sync_upload_enabled=False,sync_receive_enabled=False,
        magnetic_edges={'enabled':True,'displays':{display_id(screen):{
            'right':{'enabled':True,'width':160,'length':100,'position':50}
        }}})
    manager.update_handles();manager.timer.start(20)
    if population:
        fixtures=root/'shelf-fixtures';fixtures.mkdir()
        paths=[]
        for index in range(population):
            path=fixtures/f'fixture-{index:03}.txt'
            path.write_text('Disposable shelf fixture',encoding='utf-8')
            paths.append(str(path))
        shelf.add_paths('permanent',paths)
    trace=DragEvents(shelf);app.installEventFilter(trace)
    visual_paints=[]
    draw=MotionShadow.draw
    def record(effect,painter):
        if effect is shelf.surface_effect and shelf.presentation._timer.isActive():
            visual_paints.append(time.perf_counter())
        return draw(effect,painter)
    MotionShadow.draw=record
    screen_point=manager.edge.file_preview_region().center()
    fixture=root/'ole-fixture.txt'
    fixture.write_text('Disposable OLE drag data',encoding='utf-8')
    process=subprocess.Popen([sys.executable,__file__,'source',str(root)])
    mouse=ctypes.WinDLL('user32').mouse_event
    pressed=False
    report={'refresh_hz':screen.refreshRate(),'geometry':screen.geometry().getRect(),
            'dpr':screen.devicePixelRatio(),'populated_items':population,
            'measurement':'Native QGraphicsEffect paint, not compositor presents'}

    def wait_for(condition,label):
        deadline=time.monotonic()+6
        while not condition():
            if time.monotonic()>deadline:raise AssertionError(label)
            wait_ms(12)

    def glide(start,end,steps):
        for i in range(1,steps+1):
            QCursor.setPos(start+(end-start)*float(i/steps))
            wait_ms(10)

    try:
        wait_for(lambda:(root/'source-point').is_file(),'OLE source window')
        start=QPoint(*json.loads((root/'source-point').read_text('utf-8')))
        QCursor.setPos(start);wait_ms(200)
        mouse(2,0,0,0,0);pressed=True
        glide(start,start+QPoint(55,0),8)
        wait_for(lambda:(root/'started').is_file(),'native source drag')
        approach=screen_point-QPoint(176,0)
        glide(QCursor.pos(),approach,16)
        glide(approach,screen_point,10)
        wait_for(shelf.isVisible,'trigger preview never opened')
        assert shelf.presentation_kind=='dock'
        wait_ms(450)
        report['reveal']=timing(visual_paints)
        global_center=shelf.card.mapToGlobal(shelf.card.rect().center())
        glide(screen_point,global_center,18)
        for dx,dy in ((35,19),(-42,21),(18,-35),(0,0)):
            glide(QCursor.pos(),global_center+QPoint(dx,dy),10)
        mouse(4,0,0,0,0);pressed=False
        wait_for(lambda:(root/'result').is_file(),'OLE drop returned no result')
        wait_for(lambda:shelf.files['permanent'].count(str(fixture))==1,'fixture did not reach the actual shelf')
        report['drag_events']=len([name for name,external in trace.samples if name=='DragMove' and external])
        report['copy_action']=(root/'result').read_text('utf-8')
        assert report['drag_events'] and report['copy_action']=='CopyAction'
        assert fixture.read_text('utf-8')=='Disposable OLE drag data'
        report['ok']=True
    finally:
        if pressed:mouse(4,0,0,0,0)
        if process.poll() is None:process.terminate()
        process.wait(timeout=5)
        MotionShadow.draw=draw
        app.removeEventFilter(trace)
        report['native_events']=trace.samples[-30:]
        report.setdefault('reveal',timing(visual_paints))
        (root/'drag-preview.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report),flush=True)
        manager.quit()
    return 0


if __name__=='__main__':
    assert sys.platform=='win32' and os.environ.get('GITHUB_ACTIONS')=='true'
    assert os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    directory=Path(sys.argv[2]).resolve()
    assert directory.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
    directory.mkdir(parents=True,exist_ok=True)
    population=int(sys.argv[3]) if len(sys.argv)>3 else 0
    raise SystemExit(source(directory) if sys.argv[1]=='source' else run(directory,population))
