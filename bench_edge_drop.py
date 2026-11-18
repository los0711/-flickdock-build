"""Actual Qt paint output and CPU cost, not displayed screen FPS.

No account, native input, private file or clipboard is used. Contact frames
are rendered by production code, not an image-generation approximation.
"""
import argparse,gc,json,os,statistics,time
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QRect,QRectF,Qt,QUrl
from PySide6.QtGui import QColor,QImage,QLinearGradient,QPainter,QPen
from PySide6.QtWidgets import QApplication
from edge_absorb import EdgeAbsorb,target_in_area
from edge_file_drag import EdgeFileDrag
from edge_magnetic_model import DESKTOP_MAGNETIC_CONFIG
from performance_metrics import process_sample,qt_sample


def flush(app):
    app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);gc.collect()


def run(output,icon_file=None,icon_files=None):
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    from shelf_ui_common import load_font
    load_font(app)
    output.mkdir(parents=True,exist_ok=True)
    stack_images=[QImage(str(path)) for path in (icon_files or ())[:4]]
    assert all(not image.isNull() for image in stack_images)
    stack_urls=[QUrl.fromLocalFile('C:/synthetic-file-'+str(index)+'.txt') for index in range(12)]
    area=QRect(0,0,480,400);point=QPoint(479,200)
    target=target_in_area(point,area,'right',160)
    visual=EdgeAbsorb(seed=52,config=DESKTOP_MAGNETIC_CONFIG);visual._follow_target(target);visual._timer.stop()
    for _ in range(60):visual.field.step(1/60)
    visual.release(urls=[QUrl.fromLocalFile('C:/synthetic-fixture.txt')]);visual._timer.stop()
    if icon_file is not None:
        visual._begin_artwork((),QImage(str(icon_file)));visual._timer.stop()
    if stack_images:visual._begin_artwork(stack_urls,images=stack_images);visual._timer.stop()
    frames=[];previous=0
    for moment in (0,105,205,305,405,600):
        visual.field.step((moment-previous)/1000)
        visual.drop_artwork.elapsed_ms=moment
        visual._update_geometry();previous=moment
        layer=QImage(visual.overlay.size()*2,QImage.Format_ARGB32_Premultiplied);layer.setDevicePixelRatio(2)
        layer.fill(Qt.transparent);visual.overlay.render(layer)
        image=QImage(480*2,400*2,QImage.Format_ARGB32_Premultiplied);image.setDevicePixelRatio(2)
        p=QPainter(image);gradient=QLinearGradient(0,0,480,400)
        gradient.setColorAt(0,QColor('#15263d'));gradient.setColorAt(1,QColor('#0e1a2b'))
        p.fillRect(QRectF(0,0,480,400),gradient);p.drawImage(QPoint(visual.overlay.x(),visual.overlay.y()),layer)
        p.setPen(QPen(QColor('#46617e'),.5));p.drawLine(479,0,479,400);p.end()
        image.save(str(output/f'drop-{moment:03d}ms.png'));frames.append((moment,image))
    sheet=QImage(3*480,2*430,QImage.Format_ARGB32_Premultiplied);sheet.fill(QColor('#0c121d'))
    p=QPainter(sheet);p.setRenderHint(QPainter.SmoothPixmapTransform)
    from native_ui_common import reference_font
    font=reference_font();font.setPixelSize(14);p.setFont(font)
    for index,(moment,frame) in enumerate(frames):
        x,y=(index%3)*480,(index//3)*430
        p.drawImage(QRectF(x,y+30,480,400),frame)
        p.setPen(QColor('#d6e3f4'));p.drawText(QRectF(x+16,y+5,450,20),Qt.AlignLeft,str(moment)+' ms')
    p.end();sheet.save(str(output/'actual-genie-frames.png'))
    visual.close();flush(app)
    # Render the full field plus a mid-warp image, a conservative steady-state
    # combination. Source rectangles and document artwork are prepared once.
    visual=EdgeAbsorb(seed=5,config=DESKTOP_MAGNETIC_CONFIG);visual._follow_target(target);visual._timer.stop()
    for _ in range(80):visual.field.step(1/60)
    visual.release();visual._timer.stop();art=visual.drop_artwork
    if icon_file is not None:
        visual._begin_artwork((),QImage(str(icon_file)));visual._timer.stop();art=visual.drop_artwork
    if stack_images:visual._begin_artwork(stack_urls,images=stack_images);visual._timer.stop();art=visual.drop_artwork
    rows=[]
    for dpr in (1,1.5,2):
        size=visual.overlay.size()
        image=QImage(round(size.width()*dpr),round(size.height()*dpr),QImage.Format_ARGB32_Premultiplied);image.setDevicePixelRatio(dpr)
        samples=[]
        for index in range(360):
            art.elapsed_ms=visual.config.drop_settle_ms+visual.config.drop_absorb_ms*(.1+.75*(index%120)/120)
            visual.field.follow(200,0,0,399)
            started=time.perf_counter();visual.field.step(1/60);visual.overlay.render(image)
            samples.append((time.perf_counter()-started)*1000)
        samples.sort();rows.append(dict(dpr=dpr,frames=360,p50_ms=round(statistics.median(samples),4),p95_ms=round(samples[int(len(samples)*.95)-1],4),max_ms=round(max(samples),4)))
    visual.close();del visual,art;flush(app)
    # Warm native-wrapper caches, then include every prepared edge and actual
    # DropArtwork in 50 complete lifetimes. Offscreen is intentionally explicit.
    def cycle(index):
        field=EdgeAbsorb(seed=index,config=DESKTOP_MAGNETIC_CONFIG);gate=EdgeFileDrag(field,lambda mime,pos:False)
        geometry=app.primaryScreen().geometry();point=QPoint(geometry.right()-5,geometry.center().y())
        gate.update_pointer(geometry.center(),False);gate.update_pointer(point,True)
        field.release(point)
        if stack_images:field._begin_artwork(stack_urls,images=stack_images)
        field._timer.stop();gate.close();field.close()
        assert not field._timer.isActive();del gate,field;flush(app)
    for index in range(3):cycle(index)
    before={**process_sample(),'qt':qt_sample(app)}
    for index in range(50):cycle(index)
    after={**process_sample(),'qt':qt_sample(app)}
    assert after['qt']['widgets']<=before['qt']['widgets']
    assert after['qt']['windows']<=before['qt']['windows']
    for key in ('handles','gdi_handles','user_handles','python_threads'):
        assert after[key]<=before[key]+2,(key,before[key],after[key])
    report=dict(ok=True,measurement='Offscreen CPU model + full field + post-drop mesh; not monitor FPS or GPU timing',stack_icons=len(stack_images),glow_size=[DESKTOP_MAGNETIC_CONFIG.glow_width_px,DESKTOP_MAGNETIC_CONFIG.glow_height_px],renderer=rows,lifetime_cycles=50,before=before,after=after)
    (output/'drop-cost.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--icon-file',type=Path);parser.add_argument('--icon-files',type=Path,nargs='+')
    args=parser.parse_args();run(args.output,args.icon_file,args.icon_files)
