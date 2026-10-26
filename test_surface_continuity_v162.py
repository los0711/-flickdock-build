"""Actual first-frame shadow/appearance handoff at 125% DPI, offscreen."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']='1.25'
from PySide6.QtCore import QPoint,QRect,QRectF
from PySide6.QtWidgets import QApplication

def alpha_row(widget,global_y,left,right):
    image=widget.grab().toImage();dpr=image.devicePixelRatioF()
    row=[]
    for x in range(left,right):
        px=round((x-widget.x())*dpr);py=round((global_y-widget.y())*dpr)
        row.append(image.pixelColor(px,py).alpha() if 0<=px<image.width() and 0<=py<image.height() else 0)
    return row

with tempfile.TemporaryDirectory(prefix='flickdock-continuity-') as tmp:
    os.environ['APPDATA']=tmp
    import flickdock as subject
    from app_settings import SettingsStore
    app=QApplication([]);subject.load_font(app)
    manager=SimpleNamespace(motion=True,settings=SettingsStore(Path(tmp)),save=lambda:None,
                            update_handles=lambda:None,activate_shelf=lambda _:None,
                            _quitting=False,outgoing_drag=False)
    shelf=subject.ShelfWindow(manager,1)
    shelf.files['permanent']=[shelf.content.add_text('Visible preview')];shelf.refresh()
    shelf.geometry_morph.jump(QRect(1400,160,422,301));shelf.show()
    screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080))
    # Freeze a partially revealed surface. Expanding must preserve the same
    # scale and offset on its first snapshot and on its interactive controls.
    shelf.presentation.jump(dict(sx=.96,sy=.93,dx=0,dy=8,alpha=1))
    app.processEvents()
    before=alpha_row(shelf,300,1390,1460)
    with patch.object(subject.QGuiApplication,'screenAt',return_value=screen):shelf.show_contents()
    shelf.presentation.stop();shelf.geometry_morph.animation.pause()
    morph=shelf.geometry_morph;snapshot=shelf._surface_snapshot
    morph._frame(0);app.processEvents()
    after=alpha_row(shelf,300,1390,1460)
    delta=max(abs(a-b) for a,b in zip(before,after))
    # The native source and snapshot round fractional-DPR pixel origins
    # differently. Compare the silhouette to one DIP, and soft-shadow alpha
    # separately; a single antialiased body-edge pixel is not a geometry jump.
    before_edge=next(i for i,a in enumerate(before) if a>=200)
    after_edge=next(i for i,a in enumerate(after) if a>=200)
    edge_delta=abs(before_edge-after_edge)
    shadow_delta=max(abs(a-b) for a,b in zip(before,after) if a<80 and b<80)
    assert edge_delta<=1 and shadow_delta<=5,(edge_delta,shadow_delta,before,after)
    rect=shelf.visual_geometry();transform=snapshot.presentation_transform(rect)
    for button,offset in ((shelf.pin_button,176),(shelf.expand_button,132),(shelf.menu_button,88),(shelf.close_button,44)):
        center=transform.map(QRectF(rect.x()+rect.width()-14-offset-shelf.x(),rect.y()+21-shelf.y(),36,36).center())
        assert abs(button.x()+18-center.x())<=.51 and abs(button.y()+18-center.y())<=.51
        assert button._presentation_scale==(.96,.93)
    widths=[];heights=[]
    for ms in range(231):
        morph._frame(morph.animation.easingCurve().valueForProgress(ms/230))
        widths.append(morph.current.width());heights.append(morph.current.height())
    assert widths==sorted(widths) and heights==sorted(heights)
    assert min(widths)==422 and max(widths)==816
    morph.complete();shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
    shelf.collapse_contents();morph.animation.pause()
    widths=[]
    for ms in range(231):
        morph._frame(morph.animation.easingCurve().valueForProgress(ms/230));widths.append(morph.current.width())
    assert widths==sorted(widths,reverse=True) and min(widths)==422 and max(widths)==816
    morph.complete()
    assert not snapshot.active and shelf.pin_button._presentation_scale==(1,1)
    result={'platform':app.platformName(),'dpr':shelf.devicePixelRatioF(),
            'first_frame_edge_delta_dip':edge_delta,'shadow_alpha_max_delta':shadow_delta,
            'expand_overshoot_px':0,'collapse_undershoot_px':0}
    Path(__file__).with_name('surface-continuity-v162.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('SURFACE_CONTINUITY_V162_OK',json.dumps(result))
    shelf.image_cache.close();shelf.resolver.close();shelf.hide()
