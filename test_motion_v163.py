"""Terminal-frame regression through ShelfWindow; no desktop windows or input.

Run FLICKDOCK_TEST_DPR=1/1.25/1.5/2 in fresh processes. This checks Qt raster
continuity, native geometry calls and lifecycle state; it cannot test DWM.
"""
from array import array
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']=os.environ.get('FLICKDOCK_TEST_DPR','1.25')
from PySide6.QtCore import QPoint,QRect,QRectF,qInstallMessageHandler
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication


def difference(before,after):
    assert before.size()==after.size(),(before.size(),after.size())
    before_argb=before.convertToFormat(QImage.Format.Format_ARGB32)
    after_argb=after.convertToFormat(QImage.Format.Format_ARGB32)
    a=array('I',before_argb.constBits().tobytes())
    b=array('I',after_argb.constBits().tobytes())
    changed=0; maximum=0; alpha_max=0
    for left,right in zip(a,b):
        if left==right:continue
        changed+=1
        deltas=[abs(((left>>shift)&255)-((right>>shift)&255)) for shift in (0,8,16,24)]
        maximum=max(maximum,*deltas);alpha_max=max(alpha_max,deltas[-1])
    return dict(changed_pixels=changed,max_channel_delta=maximum,max_alpha_delta=alpha_max)


def image(shelf):
    return shelf.grab().toImage()


with tempfile.TemporaryDirectory(prefix='flickdock-v163-terminal-') as profile:
    os.environ['APPDATA']=profile
    import flickdock as subject
    from app_settings import SettingsStore
    from flickdock_motion import Spring
    warnings=[]
    qInstallMessageHandler(lambda kind,context,text:warnings.append(text))
    app=QApplication([])
    assert app.platformName()=='offscreen'
    subject.load_font(app)
    manager=SimpleNamespace(motion=True,settings=SettingsStore(Path(profile)),save=lambda:None,
                            update_handles=lambda:None,activate_shelf=lambda _:None,
                            _quitting=False,outgoing_drag=False)
    manager.settings.update(view_mode='icons')
    shelf=subject.ShelfWindow(manager,1)
    shelf.pinned=True
    shelf.files['permanent']=[shelf.content.add_text('静态文本 / terminal frame evidence')]
    shelf.refresh()
    # Isolate surface completion from the separately tested incoming-card
    # landing animation, whose clock would otherwise advance between grabs.
    from content_stack import StackTile
    for tile in shelf.findChildren(StackTile):tile.landing.stop();tile._arrive(1.0)
    shelf._show_at(QPoint(1400,160),'manual',QPoint(0,14))
    shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
    app.processEvents()
    screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080))
    results=[]

    def land(action,name,pose=None):
        with patch.object(subject.QGuiApplication,'screenAt',return_value=screen):action()
        morph=shelf.geometry_morph;morph.animation.pause()
        if pose is None:pose=dict(sx=1,sy=1,dx=0,dy=0,alpha=1)
        shelf.presentation.jump(pose)
        snapshot=shelf._surface_snapshot
        assert not shelf.card.isHidden(),'live source was hidden during the transition'
        assert snapshot.isHidden(),'snapshot became a second presentation widget'
        assert shelf.surface_effect._surface_snapshot is snapshot
        for spring in shelf.findChildren(Spring):spring.stop()
        morph._frame(1)
        app.processEvents()
        before=image(shelf);host=QRect(shelf.geometry());card=shelf.card.mapToGlobal(QPoint())
        # Completion is the precise boundary reported as a final twitch.
        morph.complete()
        app.processEvents()
        after=image(shelf)
        assert shelf.geometry()==host,'completion moved or resized the native host'
        assert shelf.card.mapToGlobal(QPoint())==card,'completion shifted the content origin'
        diff=difference(before,after)
        result=dict(name=name,host=[host.x(),host.y(),host.width(),host.height()],**diff)
        results.append(result)
        if diff['changed_pixels']:
            before.save(str(Path(__file__).with_name('motion-before-v163.png')))
            after.save(str(Path(__file__).with_name('motion-after-v163.png')))
        assert diff['changed_pixels']==0,result
        assert not snapshot.active and shelf.surface_effect._surface_snapshot is None
        assert not shelf.card.isHidden()

    land(shelf.show_contents,'expand_identity')
    land(shelf.collapse_contents,'collapse_identity')
    # A reveal Spring may still have a nonidentity pose when the geometry
    # animation ends. Both paths must keep that same pose and source renderer.
    land(shelf.show_contents,'expand_during_reveal',dict(sx=.986,sy=.978,dx=3,dy=2,alpha=1))
    land(shelf.collapse_contents,'collapse_during_reveal',dict(sx=.994,sy=.991,dx=1,dy=1,alpha=1))

    for instant in (0,1,17,57,103,199,229):
        with patch.object(subject.QGuiApplication,'screenAt',return_value=screen):shelf.show_contents()
        shelf.presentation.stop();shelf.geometry_morph.animation.pause()
        shelf.geometry_morph.animation.setCurrentTime(instant)
        before=QRect(shelf.visual_geometry())
        shelf.collapse_contents()
        assert shelf.visual_geometry()==before,('retarget teleported',instant)
        shelf.geometry_morph.animation.pause()
        shelf.geometry_morph.complete()
    callbacks=[]
    shelf.geometry_morph.animate(shelf.visual_geometry().translated(-8,0),lambda:callbacks.append(1))
    shelf.geometry_morph.complete();shelf.geometry_morph.complete()
    assert callbacks==[1]
    assert not shelf._surface_snapshot.active
    shelf.setProperty('flickdockConfirmationActive',True)
    original=(shelf.expanded,QRect(shelf.visual_geometry()))
    shelf.toggle_contents();shelf.show_contents();shelf.collapse_contents()
    assert original==(shelf.expanded,QRect(shelf.visual_geometry()))
    shelf.setProperty('flickdockConfirmationActive',False)

    # Measure the concrete remaining risk of live source rendering. This is a
    # forced offscreen full-window render, not real display frame timing.
    shelf.files['permanent'] += [shelf.content.add_text(f'Readable content item {i}') for i in range(124)]
    shelf.refresh()
    with patch.object(subject.QGuiApplication,'screenAt',return_value=screen):shelf.show_contents()
    shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
    shelf.geometry_morph.animation.pause()
    costs=[]
    for moment in range(0,231,16):
        shelf.geometry_morph.animation.setCurrentTime(moment)
        started=time.perf_counter();image(shelf);costs.append((time.perf_counter()-started)*1000)
    shelf.geometry_morph.complete()
    assert len(shelf.surface_effect._shadow_cache)<=3
    assert not [message for message in warnings if 'QPainter' in message or 'paint device' in message],warnings
    evidence=dict(platform=app.platformName(),dpr=shelf.devicePixelRatioF(),terminal_frames=results,
                  rapid_reversals=7,overlay_completion_callbacks=len(callbacks),
                  full_offscreen_grab_median_ms=round(statistics.median(costs),3),
                  full_offscreen_grab_max_ms=round(max(costs),3),
                  shared_shadow_textures=len(shelf.surface_effect._shadow_cache),
                  scope='Qt offscreen; no DWM, desktop input, hotkeys, registry or network')
    output=Path(__file__).with_name('motion-v163-dpr-'+os.environ['QT_SCALE_FACTOR']+'.json')
    output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8')
    print('MOTION_V163_OK',json.dumps(evidence,ensure_ascii=False))
    shelf.image_cache.close();shelf.resolver.close();shelf.hide()
