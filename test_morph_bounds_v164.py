"""Real-entry intermediate silhouette bounds, including a controlled old path.

QT offscreen only; a zero-difference last frame does not establish that the
intermediate frames obey their geometry. No native input, hotkeys or registry.
"""
import json
import math
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']=os.environ.get('FLICKDOCK_TEST_DPR','1.25')
from PySide6.QtCore import QEvent,QPoint,QPointF,QRect,Qt
from PySide6.QtGui import QEnterEvent,QPainter
from PySide6.QtWidgets import QApplication


def legacy_draw(effect,painter):
    """Reproduce the old drawSource bypass without changing source files."""
    bounds=effect.sourceBoundingRect(Qt.CoordinateSystem.LogicalCoordinates)
    snapshot=effect._surface_snapshot
    active=snapshot is not None and snapshot.active
    rect=effect._surface_rect(snapshot) if active else bounds
    center=rect.center()
    painter.save()
    painter.setRenderHints(QPainter.RenderHint.Antialiasing|QPainter.RenderHint.SmoothPixmapTransform)
    painter.translate(center.x()+effect.dx,center.y()+effect.dy)
    painter.scale(effect.sx,effect.sy);painter.translate(-center.x(),-center.y())
    paint_surface_shadow(painter,rect,effect._shadow_cache,effect.blurRadius(),effect.offset(),effect.color())
    if active:snapshot.paint_frame(painter,rect,live_draw=effect.drawSource,target_size=bounds.size())
    else:effect.drawSource(painter)
    painter.restore()


def sample(shelf,progress):
    shelf.geometry_morph._frame(progress)
    app.processEvents()
    image=shelf.grab().toImage();ratio=image.devicePixelRatioF()
    rect=shelf._surface_snapshot.content_rect()
    center_x=round(rect.center().x()*ratio);center_y=round(rect.center().y()*ratio)
    xs=[x for x in range(image.width()) if image.pixelColor(x,center_y).alpha()>=200]
    ys=[y for y in range(image.height()) if image.pixelColor(center_x,y).alpha()>=200]
    assert xs and ys,'surface disappeared'
    actual=[min(xs),min(ys),max(xs),max(ys)]
    expected=[math.floor(rect.left()*ratio),math.floor(rect.top()*ratio),
              math.ceil(rect.right()*ratio)-1,math.ceil(rect.bottom()*ratio)-1]
    errors=[abs(a-b) for a,b in zip(actual,expected)]
    below=round((rect.bottom()+32)*ratio)
    alpha_below=image.pixelColor(center_x,below).alpha() if below<image.height() else 0
    return dict(progress=progress,expected_physical_bounds=expected,actual_physical_bounds=actual,
                max_bound_error_physical_px=max(errors),alpha_beyond_shadow_below=alpha_below)


with tempfile.TemporaryDirectory(prefix='flickdock-bounds-v164-') as profile:
    os.environ['APPDATA']=profile
    import flickdock as subject
    from app_settings import SettingsStore
    from flickdock_motion import MotionShadow
    from surface_morph import paint_surface_shadow
    app=QApplication([]);subject.load_font(app)
    assert app.platformName()=='offscreen'
    manager=SimpleNamespace(motion=True,settings=SettingsStore(Path(profile)),save=lambda:None,
                            update_handles=lambda:None,activate_shelf=lambda _:None,
                            _quitting=False,outgoing_drag=False)
    shelf=subject.ShelfWindow(manager,1);shelf.pinned=True
    shelf.files['permanent']=[shelf.content.add_text('Readable content')];shelf.refresh()
    shelf._show_at(QPoint(1400,160),'manual',QPoint())
    shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1));app.processEvents()
    screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080))
    with patch.object(subject.QGuiApplication,'screenAt',return_value=screen):shelf.show_contents()
    shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
    shelf.geometry_morph.animation.pause()
    # The same assertion must reject the controlled old presentation path.
    class LegacyMotionShadow(MotionShadow):
        def draw(self,painter):legacy_draw(self,painter)
    endpoint=shelf.card.pos()
    # Use a fresh subclass: PySide caches virtual overrides on construction.
    # Replacing a Python method after a QGraphicsEffect exists does not change
    # the C++ paint dispatch and would make this controlled regression vacuous.
    shelf.surface_effect=LegacyMotionShadow(shelf.card,40)
    shelf.card.setGraphicsEffect(shelf.surface_effect)
    shelf.surface_effect.begin_surface_morph(shelf._surface_snapshot)
    origin=shelf.visual_geometry()
    shelf.card.move(origin.x()-shelf.x()+14,origin.y()-shelf.y()+7)
    before=sample(shelf,0)
    assert before['max_bound_error_physical_px']>10,before
    shelf.card.move(endpoint)
    shelf.surface_effect=MotionShadow(shelf.card,40)
    shelf.card.setGraphicsEffect(shelf.surface_effect)
    shelf.surface_effect.begin_surface_morph(shelf._surface_snapshot)
    shelf.surface_effect.update()
    expand=[sample(shelf,p) for p in (0,.1,.3,.5,.9,1)]
    for frame in expand:
        assert frame['max_bound_error_physical_px']<=2,frame
        assert frame['alpha_beyond_shadow_below']<=2,frame
    shelf.geometry_morph.complete();shelf.collapse_contents()
    shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
    shelf.geometry_morph.animation.pause()
    collapse=[sample(shelf,p) for p in (0,.1,.3,.5,.9,1)]
    for frame in collapse:
        assert frame['max_bound_error_physical_px']<=2,frame
        assert frame['alpha_beyond_shadow_below']<=2,frame
    shelf.geometry_morph.complete()
    shelf.zones['permanent'].icon.landing.setCurrentTime(shelf.zones['permanent'].icon.landing.duration())
    def compact_ink():
        image=shelf.grab().toImage();ratio=image.devicePixelRatioF();card=shelf.card.geometry()
        return image.copy(QRect(round(card.x()*ratio),round(card.y()*ratio),
                                round(card.width()*ratio),round(card.height()*ratio)))
    compact_before=compact_ink()
    reversals=[]
    for progress in (.1,.5,.9):
        with patch.object(subject.QGuiApplication,'screenAt',return_value=screen):shelf.show_contents()
        shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
        shelf.geometry_morph.animation.pause()
        previous=sample(shelf,progress);visual=QRect(shelf.visual_geometry())
        ratio=shelf.devicePixelRatioF()
        global_before=[value+round((shelf.x() if i%2==0 else shelf.y())*ratio)
                       for i,value in enumerate(previous['actual_physical_bounds'])]
        shelf.collapse_contents();shelf.presentation.stop();shelf.geometry_morph.animation.pause()
        after=sample(shelf,0)
        global_after=[value+round((shelf.x() if i%2==0 else shelf.y())*ratio)
                      for i,value in enumerate(after['actual_physical_bounds'])]
        error=max(abs(a-b) for a,b in zip(global_before,global_after))
        assert shelf.visual_geometry()==visual
        assert error<=2 and after['max_bound_error_physical_px']<=2,(progress,error,after)
        assert after['alpha_beyond_shadow_below']==0
        shelf.geometry_morph.complete()
        reversals.append(dict(progress=progress,global_silhouette_delta_physical_px=error))
    compact_after=compact_ink()
    assert compact_before==compact_after,'collapsed source retained popup pixels after rapid reversal'
    neutral=[]
    for button in (shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button):
        QApplication.sendEvent(button,QEnterEvent(QPointF(10,10),QPointF(10,10),QPointF(10,10)))
        QApplication.sendEvent(button,QEvent(QEvent.Type.Leave))
        button.spring._started_at=time.perf_counter()-1;button.spring._tick()
        assert not button._hovered and not button._pressed
        assert button.spring.values['hover']==0 and button.spring.values['down']==0
        assert button.spring.values['scale']==1
        image=button.grab().toImage();ratio=image.devicePixelRatioF()
        ink=image.pixelColor(round(5*ratio),round(18*ratio))
        rgb=[ink.red(),ink.green(),ink.blue()]
        assert max(rgb)-min(rgb)<=32,('idle header retained an accent color',rgb)
        neutral.append(rgb)
    evidence=dict(platform=app.platformName(),dpr=shelf.devicePixelRatioF(),
                  controlled_drawSource_before=before,expand=expand,collapse=collapse,
                  rapid_reversals=reversals,collapsed_popup_ghost_pixels=0,
                  idle_header_rgb=neutral,scope='Qt raster bounds, no DWM or desktop interaction')
    output=Path(__file__).with_name('motion-bounds-v164-dpr-'+os.environ['QT_SCALE_FACTOR']+'.json')
    output.write_text(json.dumps(evidence,indent=2),encoding='utf8')
    print('MORPH_BOUNDS_V164_OK',json.dumps(evidence))
    shelf.image_cache.close();shelf.resolver.close();shelf.hide()
