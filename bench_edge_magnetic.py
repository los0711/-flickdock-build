"""Offscreen renderer cost and lifetime checks; never a displayed-FPS claim."""
from __future__ import annotations
import argparse,gc,json,os,statistics,time
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QRect
from PySide6.QtGui import QImage,QPainter,QColor
from PySide6.QtWidgets import QApplication
from edge_absorb import EdgeAbsorb,target_in_area
from edge_file_drag import EdgeFileDrag
from performance_metrics import process_sample,qt_sample


def flush(app):
    app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);gc.collect()


def run(output):
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    output.mkdir(parents=True,exist_ok=True)
    visual=EdgeAbsorb(seed=50)
    target=target_in_area(QPoint(1908,530),QRect(0,0,1920,1080),'right',150)
    visual._follow_target(target);visual._timer.stop()
    for _ in range(90):visual.field.step(1/60)
    visual._update_geometry()
    costs=[]
    for dpr in (1,1.5,2):
        size=visual.overlay.size()
        image=QImage(round(size.width()*dpr),round(size.height()*dpr),QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        samples=[]
        for _ in range(360):
            start=time.perf_counter();visual.field.step(1/60);visual.overlay.render(image)
            samples.append((time.perf_counter()-start)*1000)
        image.save(str(output/f'field-dpr-{dpr:g}.png'))
        composite=QImage(image.size(),QImage.Format_ARGB32_Premultiplied)
        composite.setDevicePixelRatio(dpr);composite.fill(QColor('#152238'))
        painter=QPainter(composite);painter.drawImage(0,0,image);painter.end()
        composite.save(str(output/f'field-on-background-dpr-{dpr:g}.png'))
        samples.sort()
        costs.append(dict(dpr=dpr,logical_canvas=[size.width(),size.height()],frames=len(samples),p50_ms=round(statistics.median(samples),4),p95_ms=round(samples[int(len(samples)*.95)-1],4),max_ms=round(max(samples),4)))
    visual.close();del visual;flush(app)
    # Warm up the Qt/tool-window caches before measuring 50 complete lifetimes.
    for _ in range(3):
        field=EdgeAbsorb();gate=EdgeFileDrag(field,lambda mime,pos:False);gate.close();field.close()
        del gate,field;flush(app)
    before={**process_sample(),'qt':qt_sample(app)}
    active_after_close=0
    for index in range(50):
        field=EdgeAbsorb(seed=index);gate=EdgeFileDrag(field,lambda mime,pos:False)
        field._follow_target(target);field._timer.stop()
        for _ in range(30):field.field.step(1/60)
        field.retreat();field.field.step(.3)
        gate.close();field.close()
        active_after_close+=int(field._timer.isActive())
        del gate,field;flush(app)
    after={**process_sample(),'qt':qt_sample(app)}
    report=dict(ok=True,platform=app.platformName(),measurement='CPU model + Qt offscreen raster render; not monitor presentation FPS or GPU timing',renderer=costs,lifetime_cycles=50,active_frames_after_close=active_after_close,before=before,after=after)
    for name in ('gdi_handles','user_handles','python_threads'):
        if name in before and name in after:
            assert after[name]<=before[name]+2,(name,before[name],after[name])
    assert after['qt']['windows']<=before['qt']['windows'],(before['qt'],after['qt'])
    assert active_after_close==0
    (output/'edge-cost.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
