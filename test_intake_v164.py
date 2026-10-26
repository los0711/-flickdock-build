"""Intake callback/landing regressions; isolated files and offscreen Qt only."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']='1.25'
from PySide6.QtCore import QObject,QMimeData,QPoint,QRect,QTimer,QUrl,Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def wait_for(predicate,limit=1800):
    started=time.monotonic()
    while not predicate() and (time.monotonic()-started)*1000<limit:
        app.processEvents();QTest.qWait(5)
    assert predicate(),'intake/animation completion timed out'


with tempfile.TemporaryDirectory(prefix='flickdock-intake-v164-') as temporary:
    os.environ['APPDATA']=str(Path(temporary)/'profile')
    import flickdock as subject
    from app_settings import SettingsStore
    from content_stack import StackTile
    from flickdock_motion import ContentTile
    from shelf_content import ContentRepository
    app=QApplication([]);subject.load_font(app)
    assert app.platformName()=='offscreen'
    evidence={}

    # Reproduces import_paths.done's second callback-free visual pulse. A
    # feedback request cannot silently cancel the already accepted intake.
    callbacks=[]
    tile=ContentTile();tile.pulse('drop',lambda:callbacks.append('accepted'))
    tile.pulse('drop')
    tile.set_hover(True);tile.set_pressed(True);tile.set_pressed(False)
    wait_for(lambda:callbacks)
    assert callbacks==['accepted'],callbacks
    QTest.qWait(50);app.processEvents();assert callbacks==['accepted']
    evidence['callback_free_refresh_keeps_intake']=True

    # Spring.animate owns a replaceable visual callback. Intake completion has
    # a separate lifetime and survives that callback being replaced directly.
    independent=[]
    tile.pulse('drop',lambda:independent.append(1))
    tile.spring.animate({'hover':0},finished=lambda:None)
    wait_for(lambda:independent)
    assert independent==[1]
    evidence['visual_retarget_keeps_intake']=True

    # An explicitly new accepted intake supersedes the previous completion;
    # callback-free metadata refreshes preserve the newest one.
    replacement=[]
    tile.pulse('drop',lambda:replacement.append('old'))
    tile.pulse('drop',lambda:replacement.append('new'))
    tile.pulse('drop')
    wait_for(lambda:replacement)
    assert replacement==['new'],replacement
    tile.pulse('drop',lambda:replacement.append('cancelled'));tile.cancel_pulse()
    QTest.qWait(700);app.processEvents();assert replacement==['new']

    repo=ContentRepository();refs=[repo.add_text('First item'),repo.add_text('Second item')]
    stack=StackTile();stack.resize(170,140);stack.show()
    stack.set_stack(repo,refs)
    stack.landing.pause();stack.landing.setCurrentTime(80)
    arrival=stack.arrival;moment=stack.landing.currentTime()
    stack.set_stack(repo,refs)
    assert stack.arrival==arrival and stack.landing.currentTime()==moment
    landed=[];stack.pulse('drop',lambda:landed.append(1))
    stack.spring._started_at=time.perf_counter()-1;stack.spring._tick()
    assert not landed,'tuck completed before the incoming card landed'
    stack.landing.setCurrentTime(stack.landing.duration())
    assert landed==[1] and stack.arrival==1
    evidence['same_refs_refresh_keeps_landing']=True
    evidence['completion_waits_for_landing']=True

    reduced=[]
    still=StackTile(motion=False);still.set_stack(repo,refs)
    still.pulse('drop',lambda:reduced.append(1));wait_for(lambda:reduced)
    assert still.arrival==1 and still.spring.values['sx']==1 and still.spring.values['sy']==1
    evidence['reduced_motion_completes']=True

    # Exercise the actual MIME -> async-copy -> stack refresh -> tuck entry.
    # The controlled worker only delays its completion signal; it does not
    # fake the public intake path or rely on desktop input/registry/hotkeys.
    class ControlledCopy(QObject):
        finished=Signal(list);failed=Signal(str);cancelled=Signal()
        jobs=[]
        def __init__(self,paths,destination,parent=None):
            super().__init__(parent);self.paths=paths;self.destination=Path(destination)
            self.jobs.append(self)
        def start(self):pass
        def cancel(self):self.cancelled.emit()
        def wait(self,*args):return True

    settings=SettingsStore(Path(temporary)/'settings')
    settings.update(capture_mode='copy',capture_dir=str(Path(temporary)/'captured'))
    manager=SimpleNamespace(motion=True,settings=settings,save=lambda:None,
                            update_handles=lambda:None,activate_shelf=lambda _:None,
                            _quitting=False,outgoing_drag=False,just_dropped=0,
                            edge=SimpleNamespace(geometry=lambda:QRect(1910,100,10,160)))
    shelf=subject.ShelfWindow(manager,1)
    shelf._show_at(QPoint(1400,160),'manual',QPoint())
    shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
    source=Path(temporary)/'incoming.txt';source.write_text('accepted copy fixture',encoding='utf8')
    mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))])
    with patch.object(subject,'CopyFilesJob',ControlledCopy):
        assert shelf.receive_mime('permanent',mime)
        job=ControlledCopy.jobs[-1]
        copied=Path(temporary)/'captured'/'owned-incoming.txt';copied.parent.mkdir(parents=True,exist_ok=True)
        copied.write_text(source.read_text(encoding='utf8'),encoding='utf8')
        job.finished.emit([str(copied)])
        assert shelf.files['permanent']==[str(copied)]
        wait_for(lambda:shelf.visible_state in ('hiding','hidden'))
        wait_for(lambda:shelf.visible_state=='hidden')
    assert not shelf.incoming_drop and source.is_file() and copied.is_file()
    evidence['actual_copy_intake_tucks']=True
    evidence['platform']=app.platformName();evidence['dpr']=shelf.devicePixelRatioF()
    Path(__file__).with_name('intake-v164.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
    print('INTAKE_V164_OK',json.dumps(evidence))
    shelf.image_cache.close();shelf.resolver.close();shelf.hide()
    stack.close();tile.close();still.close()
