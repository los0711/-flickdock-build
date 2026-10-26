"""Current-source cold-expand timing, without desktop/input/registry actions.

Results describe GUI-thread timing, never Windows compositor frame rate.
"""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']='1.25'
import sys, json, tempfile, shutil, time, statistics, hashlib
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

HERE=Path(__file__).parent
ROOT=HERE
sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QTimer, QEventLoop, QRect, QPoint, Qt, qInstallMessageHandler
from PySide6.QtGui import QImage, QColor, QPainter, QPixmapCache
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

case=sys.argv[1] if len(sys.argv)>1 else 'mixed-text'
defer='defer' in sys.argv
disable_shadow='no-shadow' in sys.argv
disable_thumbnails='no-thumbnails' in sys.argv
measure={}; timeline=[]; eventlog=[]; beats=[]; phase=None; start=0; shelf=None
def ms():return (time.perf_counter()-start)*1000
def record(name,before,extra=None):
    if phase:
        entry={'name':name,'at':round((before-start)*1000,3),'duration':round((time.perf_counter()-before)*1000,3),
               'morph':bool(shelf and shelf.geometry_morph.active),'phase':phase}
        if extra is not None:entry['extra']=extra
        timeline.append(entry)
def instrument(cls,name,label=None):
    old=getattr(cls,name)
    def called(self,*args,**kwargs):
        before=time.perf_counter()
        try:return old(self,*args,**kwargs)
        finally:record(label or cls.__name__+'.'+name,before)
    setattr(cls,name,called)
class App(QApplication):
    def notify(self,receiver,event):
        before=time.perf_counter()
        try:return super().notify(receiver,event)
        finally:
            duration=(time.perf_counter()-before)*1000
            if phase and duration>3:
                eventlog.append({'at':round((before-start)*1000,3),'duration':round(duration,3),
                                 'receiver':type(receiver).__name__,'type':event.type().name,'phase':phase,
                                 'morph':bool(shelf and shelf.geometry_morph.active)})
class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1.25
def run(msvalue):
    loop=QEventLoop();QTimer.singleShot(msvalue,loop.quit);loop.exec()
def beat():
    if phase:beats.append((phase,ms(),bool(shelf.geometry_morph.active)))
messages=[]
qInstallMessageHandler(lambda _kind,_context,text:messages.append(text))
with tempfile.TemporaryDirectory(prefix='flickdock-cold-audit-') as tmp:
    os.environ['APPDATA']=tmp
    import flickdock as mod, app_settings, surface_morph as surf, media_preview as media, flickdock_motion as motion, shelf_content
    app=App([])
    sys._MEIPASS=str(ROOT)
    font0=time.perf_counter();mod.load_font(app);font_ms=(time.perf_counter()-font0)*1000
    for cls,name in ((mod.ShelfWindow,'show_contents'),(mod.ShelfWindow,'_prepare_morph'),(mod.ShelfWindow,'_settle_morph'),
                     (mod.ContentsPopup,'__init__'),(mod.ContentsPopup,'rebuild'),(mod.IconItem,'__init__'),
                     (mod.PreviewPane,'show_path'),(mod.PreviewPane,'hideEvent'),(mod.ShelfWindow,'item_pixmap'),
                     (surf.SurfaceSnapshot,'capture_live'),(surf.SurfaceSnapshot,'_shadow'),(surf.SurfaceSnapshot,'paintEvent'),
                     (media.MediaPreview,'set_source'),(media.MediaPreview,'_load_player'),(media.MediaPreview,'_dispose_player'),
                     (media.MediaPreview,'_frame_changed'),(media.MediaPreview,'showEvent'),(media.MediaPreview,'hideEvent'),
                     (mod.ImagePreview,'set_source'),(motion.MotionShadow,'draw')):
        instrument(cls,name)
    with ExitStack() as stack:
        for obj,name,kw in ((mod,'motion_enabled',{'return_value':True}),(QSystemTrayIcon,'show',{}),(QSystemTrayIcon,'hide',{}),
                            (app_settings,'_read_startup_value',{}),(app_settings,'_write_startup_value',{}),
                            (mod.QGuiApplication,'screenAt',{'return_value':Screen()}),(mod.QGuiApplication,'primaryScreen',{'return_value':Screen()}),
                            (mod.ctypes.windll.user32,'GetAsyncKeyState',{'return_value':0}),(mod,'native_window_move_active',{'return_value':False}),
                            (mod.QCursor,'pos',{'return_value':QPoint(0,0)})):
            stack.enter_context(patch.object(obj,name,**kw))
        manager=mod.ShelfManager(app);manager.timer.stop();shelf=manager.shelf;shelf.pinned=True
        manager.settings.update(view_mode='icons')
        refs=[shelf.content.add_text(f'Audit text item {i}: readable preview 文字内容') for i in range(100 if case.startswith('mixed') else 125)]
        video=Path(tmp)/'video.mp4';shutil.copyfile(ROOT/'media-test.mp4',video)
        if case.startswith('mixed'):
            image=QImage(3000,2000,QImage.Format.Format_RGB32);image.fill(QColor('#8fbcac'))
            painter=QPainter(image)
            for i in range(45):
                painter.fillRect(i*73%2800,i*113%1800,180,140,QColor.fromHsv(i*17%360,190,180))
            painter.end()
            for i in range(24):
                path=Path(tmp)/f'photo-{i}.jpg';image.save(str(path),'JPG',90);refs.append(str(path))
            refs.append(str(video))
            if case=='mixed-text':refs.append(refs.pop(0))
            elif case=='mixed-image':refs.append(refs.pop(-2))
        shelf.add_paths('permanent',refs)
        shelf._show_at(QPoint(1400,160),'manual',QPoint());run(750)
        if disable_shadow:
            surf.SurfaceSnapshot._shadow=lambda self,w,h,dpr: mod.QPixmap()
        if disable_thumbnails:
            original_file=shelf_content.file_pixmap
            def fast_file(path,size=90,stack=1,dpr=None):
                if Path(path).suffix.lower() in ('.jpg','.png'):
                    return mod.document_pixmap('image',size,stack,dpr)
                return original_file(path,size,stack,dpr)
            stack.enter_context(patch.object(shelf_content,'file_pixmap',side_effect=fast_file))
        if defer:
            original_shot=QTimer.singleShot
            def shot(msec,callback):
                if getattr(callback,'__name__','')=='batch':
                    def retry():
                        if shelf.geometry_morph.active:original_shot(25,retry)
                        else:callback()
                    return original_shot(msec,retry)
                return original_shot(msec,callback)
            stack.enter_context(patch.object(QTimer,'singleShot',side_effect=shot))
        heartbeat=QTimer();heartbeat.setTimerType(Qt.TimerType.PreciseTimer);heartbeat.setInterval(1);heartbeat.timeout.connect(beat);heartbeat.start()
        start=time.perf_counter()
        for repeat in range(3):
            phase=f'expand-{repeat}';begin=time.perf_counter();shelf.show_contents();call_ms=(time.perf_counter()-begin)*1000
            run(1100)
            first_items=len(shelf.popup.rows_body.findChildren(mod.IconItem))
            for _ in range(20):
                if shelf.popup._build_cursor==len(refs):break
                run(100)
            assert shelf.popup._build_cursor==len(refs),'Remaining items did not become accessible'
            measure[phase]={'call_ms':round(call_ms,3),'items_after_1100ms':first_items,'items':len(shelf.popup.rows_body.findChildren(mod.IconItem)),
                            'media_player':shelf.popup.preview.media.player is not None,
                            'poster_empty':shelf.popup.preview.media.surface.image.isNull()}
            phase=None;shelf.collapse_contents();run(450)
        phase=None;heartbeat.stop();manager.hotkeys.close();shelf.resolver.close();shelf.hide()
    summary={'case':case,'defer':defer,'no_shadow':disable_shadow,'no_thumbnails':disable_thumbnails,'font_load_ms':round(font_ms,3),'measure':measure,
             'dpr':shelf.devicePixelRatioF(),'sha256':{name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in ('flickdock.py','surface_morph.py','media_preview.py')}}
    for name in measure:
        rows=[e for e in timeline if e['phase']==name];b=[(t,a) for p,t,a in beats if p==name]
        gaps=[b[i][0]-b[i-1][0] for i in range(1,len(b))]
        animation_gaps=[b[i][0]-b[i-1][0] for i in range(1,len(b)) if b[i][1] or b[i-1][1]]
        summary['measure'][name].update({'heartbeat_max_ms':round(max(gaps or [0]),3),
                         'animation_heartbeat_max_ms':round(max(animation_gaps or [0]),3),
                         'slow_events':sorted([e for e in eventlog if e['phase']==name],key=lambda e:e['duration'],reverse=True)[:8],
                         'functions':{key:{'calls':len([e for e in rows if e['name']==key]),
                                          'max_ms':round(max(e['duration'] for e in rows if e['name']==key),3),
                                          'sum_ms':round(sum(e['duration'] for e in rows if e['name']==key),3),
                                          'morph_calls':len([e for e in rows if e['name']==key and e['morph']])}
                                      for key in sorted(set(e['name'] for e in rows))}})
    result={'summary':summary,'timeline':timeline,'events':eventlog,'messages':messages[:40]}
    outfile=HERE/('cold-expand-after-v162-'+case+('-defer' if defer else '')+('-no-shadow' if disable_shadow else '')+('-no-thumbnails' if disable_thumbnails else '')+'.json')
    outfile.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
