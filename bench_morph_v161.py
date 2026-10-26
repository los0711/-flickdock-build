"""Real shelf entry point benchmark; isolated profile, offscreen, no input/audio."""
import os,json,sys,tempfile,time,statistics
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_SCALE_FACTOR']='1.25'
from PySide6.QtCore import QObject,QEvent,QPoint,QRect,QTimer,Qt,QAbstractAnimation
from PySide6.QtWidgets import QApplication,QSystemTrayIcon
from PySide6.QtTest import QTest

class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1.25
class Monitor(QObject):
    def __init__(self,app):super().__init__();self.active=False;self.events={};self.beats=[];self.shadow=[];self._last=time.perf_counter();self.timer=QTimer();self.timer.setTimerType(Qt.TimerType.PreciseTimer);self.timer.setInterval(4);self.timer.timeout.connect(self.tick);self.timer.start();app.installEventFilter(self)
    def tick(self):
        now=time.perf_counter()
        if self.active:self.beats.append((now-self._last)*1000)
        self._last=now
    def eventFilter(self,obj,e):
        if self.active and e.type() in (QEvent.Type.LayoutRequest,QEvent.Type.Resize,QEvent.Type.Paint,QEvent.Type.Move):
            name=e.type().name;self.events[name]=self.events.get(name,0)+1
        return False
    def reset(self):self.events={};self.beats=[];self.shadow=[];self._last=time.perf_counter();self.active=True

def wait_until(app,predicate,limit=2500):
    start=time.perf_counter()
    while not predicate() and (time.perf_counter()-start)*1000<limit:app.processEvents();QTest.qWait(2)
    app.processEvents();assert predicate(),'transition did not settle'
def percentile(values,p):
    return round(sorted(values)[min(len(values)-1,int(len(values)*p))],3) if values else 0

with tempfile.TemporaryDirectory(prefix='flickdock-morph-') as tmp:
    os.environ['APPDATA']=tmp
    import flickdock as mod,app_settings
    app=QApplication([]);mod.load_font(app);monitor=Monitor(app)
    draw=mod.MotionShadow.draw
    def measured_draw(self,p):
        s=time.perf_counter();draw(self,p)
        if monitor.active:monitor.shadow.append((time.perf_counter()-s)*1000)
    with patch.object(mod,'motion_enabled',return_value=True),patch.object(QSystemTrayIcon,'show'),patch.object(QSystemTrayIcon,'hide'),patch.object(app_settings,'_read_startup_value'),patch.object(app_settings,'_write_startup_value'),patch.object(mod.MotionShadow,'draw',measured_draw),patch.object(mod.QGuiApplication,'screenAt',return_value=Screen()),patch.object(mod.QGuiApplication,'primaryScreen',return_value=Screen()):
        manager=mod.ShelfManager(app);manager.timer.stop();shelf=manager.shelf;shelf.pinned=True
        manager.settings.update(view_mode='icons')
        refs=[shelf.content.add_text(f'125-item transition benchmark {i}: native text with a readable preview') for i in range(125)]
        shelf.add_paths('permanent',refs)
        shelf._show_at(QPoint(1400,160),'manual',QPoint());QTest.qWait(700);app.processEvents()
        results=[]
        for index in range(3):
            for action in ['expand','collapse']:
                monitor.reset();start=time.perf_counter();(shelf.show_contents if action=='expand' else shelf.collapse_contents)();entry=(time.perf_counter()-start)*1000
                wait_until(app,lambda:shelf.geometry_morph.animation.state()==QAbstractAnimation.State.Stopped)
                elapsed=(time.perf_counter()-start)*1000;monitor.active=False
                results.append({'case':action+str(index),'entry_ms':round(entry,3),'total_ms':round(elapsed,3),'heartbeat_p95_ms':percentile(monitor.beats,.95),'heartbeat_max_ms':round(max(monitor.beats,default=0),3),'shadow_p95_ms':percentile(monitor.shadow,.95),'shadow_calls':len(monitor.shadow),'events':monitor.events})
        monitor.reset();start=time.perf_counter();shelf.show_contents()
        for delay,action in [(35,shelf.collapse_contents),(70,shelf.show_contents),(105,shelf.collapse_contents),(140,shelf.show_contents)]:QTimer.singleShot(delay,action)
        QTest.qWait(900);monitor.active=False
        assert shelf.expanded and shelf.geometry_morph.animation.state()==QAbstractAnimation.State.Stopped
        results.append({'case':'rapid_interrupt','total_ms':round((time.perf_counter()-start)*1000,3),'heartbeat_p95_ms':percentile(monitor.beats,.95),'heartbeat_max_ms':round(max(monitor.beats,default=0),3),'shadow_p95_ms':percentile(monitor.shadow,.95),'shadow_calls':len(monitor.shadow),'events':monitor.events})
        path=Path(__file__).parent/('morph-'+(sys.argv[1] if len(sys.argv)>1 else 'measurement')+'.json');path.write_text(json.dumps(results,indent=2));print(path.name,json.dumps(results))
        shelf.hide_immediately();manager.quit()
