"""Trace press/release and pre-animation paints, without desktop interaction.

The controlled case forces the native geometry flush window synchronously.
It demonstrates a vulnerable Qt state, not a Windows DWM reproduction.
"""
import json
import math
import os
import sys
from fractions import Fraction
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']=os.environ.get('FLICKDOCK_TEST_DPR','1.25')
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QRect,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

state=SimpleNamespace(owner=None,phase='setup',armed=False,events=[],faults=[],inside_prepare=0,capture=0)
prototype='--prototype-reserve-transaction' in sys.argv


def geometry(rect):return [rect.x(),rect.y(),rect.width(),rect.height()]


def status():
    owner=state.owner
    if owner is None:return {}
    snapshot=getattr(owner,'_surface_snapshot',None)
    effect=owner.surface_effect
    composed=bool(snapshot is not None and snapshot.active and effect._surface_snapshot is snapshot and effect.isEnabled())
    card=QRect(owner.card.mapToGlobal(QPoint()),owner.card.size())
    actual=QRect(snapshot.rect_global) if composed else card.adjusted(-14,-7,14,20)
    expected=owner.visual_geometry()
    return dict(phase=state.phase,updates=owner.updatesEnabled(),capture_depth=state.capture,
                composed=composed,host=geometry(owner.geometry()),card=geometry(card),
                expected=geometry(expected),rendered=geometry(actual),
                morph_active=owner.geometry_morph.active,preparing=state.inside_prepare>0)


class App(QApplication):
    def notify(self,receiver,event):
        track=state.armed and state.owner is not None and receiver is state.owner
        kind=event.type()
        if track and kind in (QEvent.Type.UpdateRequest,QEvent.Type.Paint,QEvent.Type.Move,QEvent.Type.Resize):
            row=dict(event=kind.name,**status());state.events.append(row)
            # Explicit card.render sends Paint to the card, not the native
            # owner. Owner Paint is therefore still a real backing-store paint
            # even when it arrives reentrantly during endpoint capture.
            if kind==QEvent.Type.Paint and row['rendered']!=row['expected']:
                state.faults.append(row)
        return super().notify(receiver,event)


with tempfile.TemporaryDirectory(prefix='flickdock-expand-flash-v165-') as profile:
    os.environ['APPDATA']=profile
    import flickdock as subject
    from app_settings import SettingsStore
    from surface_morph import SurfaceSnapshot
    from flickdock_motion import Spring
    app=App([]);subject.load_font(app)
    assert app.platformName()=='offscreen'
    screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080))
    results=[]
    original_prepare=subject.ShelfWindow._prepare_morph
    original_capture=SurfaceSnapshot.capture_live
    original_source=subject.ShelfWindow._capture_morph_source
    original_show_contents=subject.ShelfWindow.show_contents

    def prepare(owner,*args,**kwargs):
        previous=state.phase;state.phase='prepare';state.inside_prepare+=1
        try:return original_prepare(owner,*args,**kwargs)
        finally:state.inside_prepare-=1;state.phase=previous

    def capture(snapshot,*args,**kwargs):
        previous=state.phase;state.phase='capture_live';state.capture+=1
        try:return original_capture(snapshot,*args,**kwargs)
        finally:state.capture-=1;state.phase=previous

    def source(owner,*args,**kwargs):
        previous=state.phase;state.phase='capture_source'
        try:return original_source(owner,*args,**kwargs)
        finally:state.phase=previous

    def show_contents(owner,*args,**kwargs):
        if not prototype:return original_show_contents(owner,*args,**kwargs)
        enabled=owner.updatesEnabled();owner.setUpdatesEnabled(False)
        try:return original_show_contents(owner,*args,**kwargs)
        finally:owner.setUpdatesEnabled(enabled)

    with patch.object(subject.ShelfWindow,'_prepare_morph',prepare), \
         patch.object(subject.ShelfWindow,'_capture_morph_source',source), \
         patch.object(subject.ShelfWindow,'show_contents',show_contents), \
         patch.object(SurfaceSnapshot,'capture_live',capture), \
         patch.object(subject.QGuiApplication,'screenAt',return_value=screen):
        for controlled in (False,True):
            state.armed=False;state.events=[];state.faults=[]
            settings=SettingsStore(Path(profile)/('controlled' if controlled else 'natural'))
            manager=SimpleNamespace(motion=True,settings=settings,save=lambda:None,
                                    update_handles=lambda:None,activate_shelf=lambda _:None,
                                    _quitting=False,outgoing_drag=False)
            shelf=subject.ShelfWindow(manager,1);state.owner=shelf;shelf.pinned=True
            shelf.files['permanent']=[shelf.content.add_text('A visible compact preview')];shelf.refresh()
            original_show=shelf.show
            def first_show():
                if prototype:
                    # Model the proposed reservation while the window is
                    # still hidden, before its first native visible frame.
                    visual=shelf.visual_geometry();area=screen.availableGeometry()
                    width=min(subject.EXPANDED_W,area.width()-38);height=min(subject.EXPANDED_H,area.height()-38)
                    right=visual.x()+visual.width()
                    x=max(area.left()+4,min(right-width-28,area.right()-width-20))
                    y=max(area.top()+4,min(visual.y(),area.bottom()-height-30))
                    host=visual.united(QRect(x,y,width+28,height+27)).adjusted(-16,-12,16,12)
                    step=Fraction(shelf.devicePixelRatioF()).limit_denominator(8).denominator
                    x=shelf.x()+math.floor((host.x()-shelf.x())/step)*step
                    y=shelf.y()+math.floor((host.y()-shelf.y())/step)*step
                    shelf.setGeometry(QRect(x,y,host.right()-x+1,host.bottom()-y+1))
                    shelf.card.move(visual.x()-shelf.x()+14,visual.y()-shelf.y()+7)
                    shelf._layout_header(shelf.card.width())
                return original_show()
            with patch.object(shelf,'show',first_show):shelf._show_at(QPoint(1400,160),'manual',QPoint())
            shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
            shelf.zones['permanent'].icon.landing.setCurrentTime(260)
            app.processEvents()
            original_geometry=shelf.setGeometry
            original_card_geometry=shelf.card.setGeometry

            def flush_window(label):
                if not state.armed or not state.inside_prepare:return
                previous=state.phase;state.phase=label
                state.events.append(dict(event='geometry_checkpoint',**status()))
                if controlled:
                    # Model a platform's synchronous paint while setGeometry
                    # rebases a visible native window. No timers/input run.
                    shelf.repaint()
                    QCoreApplication.sendPostedEvents(shelf,QEvent.Type.UpdateRequest)
                state.phase=previous

            def native_geometry(*args):
                result=original_geometry(*args)
                flush_window('after_native_rebase')
                return result

            def card_geometry(*args):
                result=original_card_geometry(*args)
                flush_window('after_target_layout')
                return result

            with patch.object(shelf,'setGeometry',native_geometry),patch.object(shelf.card,'setGeometry',card_geometry):
                native_before=QRect(shelf.geometry())
                state.phase='mouse_press';state.armed=True
                QTest.mousePress(shelf.expand_button,Qt.MouseButton.LeftButton,delay=0)
                state.phase='mouse_release'
                QTest.mouseRelease(shelf.expand_button,Qt.MouseButton.LeftButton,delay=0)
                state.phase='first_update'
                # Observe actual UpdateRequest/Paint before pausing animation.
                QCoreApplication.sendPostedEvents(shelf,QEvent.Type.UpdateRequest)
                app.processEvents()
                state.armed=False
                assert shelf.geometry()==native_before,'Expansion rebased a visible native host'
            snapshot=shelf._surface_snapshot
            source_alpha=snapshot.source.toImage().pixelColor(snapshot.source.width()//2,snapshot.source.height()//2).alpha()
            target_alpha=snapshot.target.toImage().pixelColor(snapshot.target.width()//2,snapshot.target.height()//2).alpha()
            assert source_alpha>=200 and target_alpha>=200,'update transaction captured a blank endpoint'
            result=dict(controlled_native_flush=controlled,events=list(state.events),faults=list(state.faults),
                        source_alpha=source_alpha,target_alpha=target_alpha)
            results.append(result)
            shelf.geometry_morph.animation.pause()
            shelf.geometry_morph.complete()
            for spring in shelf.findChildren(Spring):spring.stop()
            shelf.image_cache.close();shelf.resolver.close();shelf.hide();state.owner=None
    evidence=dict(platform=app.platformName(),dpr=float(os.environ['QT_SCALE_FACTOR']),prototype=prototype,cases=results,
                  scope='Qt offscreen event trace and controlled native-flush window; no DWM test')
    output=Path(__file__).with_name(('expand-flash-prototype-v165-dpr-' if prototype else 'expand-flash-v165-dpr-')+os.environ['QT_SCALE_FACTOR']+'.json')
    output.write_text(json.dumps(evidence,indent=2),encoding='utf8')
    failures=[row for result in results for row in result['faults']]
    print('EXPAND_FLASH_V165',json.dumps(dict(dpr=evidence['dpr'],
          cases=[dict(controlled=case['controlled_native_flush'],paints=sum(row['event']=='Paint' for row in case['events']),
                      update_requests=sum(row['event']=='UpdateRequest' for row in case['events']),
                      faults=case['faults']) for case in results])))
    assert not failures,'visible pre-animation paint used stale or endpoint geometry; see '+str(output)
