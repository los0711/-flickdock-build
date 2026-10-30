"""Real-clock Qt morph integration, with no animation seeking or desktop UI.

Checks native/live-tree stability and responsive timer delivery. This measures
offscreen GUI work; it does not measure the Windows compositor's frame rate.
"""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']='1.25'

from contextlib import ExitStack
import json
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, QTimer, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


def rect(value):
    return (value.x(),value.y(),value.width(),value.height())


class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1.25


phase=None;shelf=None;current=None;in_prepare=0
segments=[];events=[];calls=[];beats=[];violations=[];function_costs=[];entrance_frames=[]
started=time.perf_counter()
def moment():return (time.perf_counter()-started)*1000


class App(QApplication):
    def notify(self, receiver, event):
        before=time.perf_counter()
        try:return super().notify(receiver,event)
        finally:
            elapsed=(time.perf_counter()-before)*1000
            if phase and elapsed>=5:
                events.append(dict(phase=phase,at=round(moment()-elapsed,3),duration_ms=round(elapsed,3),
                                   receiver=type(receiver).__name__,event=event.type().name,
                                   morph=bool(shelf and shelf.geometry_morph.active),preparing=bool(in_prepare)))


def spin_until(predicate,timeout=4000):
    loop=QEventLoop();check=QTimer();check.setInterval(3);deadline=QTimer();deadline.setSingleShot(True)
    error=[]
    def poll():
        try:
            if predicate():loop.quit()
        except Exception as exception:error.append(exception);loop.quit()
    check.timeout.connect(poll);deadline.timeout.connect(loop.quit)
    check.start();deadline.start(timeout);loop.exec();check.stop();deadline.stop()
    if error:raise error[0]
    assert predicate(),'real Qt clock timed out'


def sample():
    if phase and shelf is not None and shelf.presentation._timer.isActive():
        values=dict(shelf.presentation.values)
        if not entrance_frames or entrance_frames[-1]['phase']!=phase or entrance_frames[-1]['values']!=values:
            entrance_frames.append(dict(phase=phase,at=round(moment(),3),values=values,
                                        host=rect(shelf.geometry()),visual=rect(shelf.visual_geometry())))
    if current is None or shelf is None or not shelf.geometry_morph.active or in_prepare:return
    visual=rect(shelf.visual_geometry())
    host,card=rect(shelf.geometry()),rect(shelf.card.geometry())
    if host!=current['host'] or card!=current['card']:
        violations.append(dict(segment=current['id'],at=moment(),host=host,card=card,
                               expected_host=current['host'],expected_card=current['card']))
    if not current['frames'] or current['frames'][-1]['visual']!=visual:
        current['frames'].append(dict(at=round(moment(),3),visual=visual))


def beat():
    beats.append(dict(at=moment(),phase=phase,morph=bool(shelf and shelf.geometry_morph.active),
                      entrance=bool(shelf and shelf.presentation._timer.isActive())))


def instrument(cls,name):
    old=getattr(cls,name)
    def timed(self,*args,**kwargs):
        before=time.perf_counter()
        try:return old(self,*args,**kwargs)
        finally:
            if phase:
                function_costs.append(dict(phase=phase,name=cls.__name__+'.'+name,
                    at=round((before-started)*1000,3),duration_ms=round((time.perf_counter()-before)*1000,3),
                    morph=bool(shelf and shelf.geometry_morph.active),preparing=bool(in_prepare)))
    return patch.object(cls,name,timed)


warnings=[]
qInstallMessageHandler(lambda kind,context,text:warnings.append(text))
with tempfile.TemporaryDirectory(prefix='flickdock-clock-v164-') as temporary:
    os.environ['APPDATA']=str(Path(temporary)/'profile')
    import flickdock as subject, app_settings
    import flickdock_motion, surface_morph
    from global_hotkeys import _NoopBackend
    app=App([]);subject.load_font(app)
    assert app.platformName()=='offscreen'
    with ExitStack() as patches:
        for obj,name,options in (
            (subject,'motion_enabled',dict(return_value=True)),
            (QSystemTrayIcon,'show',{}),(QSystemTrayIcon,'hide',{}),
            (app_settings,'_read_startup_value',{}),(app_settings,'_write_startup_value',{}),
            (subject.QCursor,'pos',dict(return_value=QPoint(1450,350))),
            (subject.QGuiApplication,'screenAt',dict(return_value=Screen())),
            (subject.QGuiApplication,'primaryScreen',dict(return_value=Screen())),
            (subject.ctypes.windll.user32,'GetAsyncKeyState',dict(return_value=0)),
            (subject.ctypes.windll.user32,'GetForegroundWindow',dict(return_value=0)),
            (subject.ctypes.windll.user32,'RegisterHotKey',dict(side_effect=AssertionError('Native hotkey registration'))),
            (subject.ctypes.windll.user32,'UnregisterHotKey',dict(side_effect=AssertionError('Native hotkey unregistration'))),
            (subject,'native_window_move_active',dict(return_value=False))):
            patches.enter_context(patch.object(obj,name,**options))
        original_prepare=subject.ShelfWindow._prepare_morph
        def prepare(self,origin,target):
            global current,in_prepare
            before=time.perf_counter();in_prepare+=1
            if current is not None and 'ended_at' not in current:current['ended_at']=moment()
            try:return original_prepare(self,origin,target)
            finally:
                in_prepare-=1
                current=dict(id=len(segments),phase=phase,prepared_at=moment(),
                             prepare_ms=round((time.perf_counter()-before)*1000,3),
                             host=rect(self.geometry()),card=rect(self.card.geometry()),
                             origin=rect(origin),target=rect(target),frames=[])
                segments.append(current)
                sample()
        patches.enter_context(patch.object(subject.ShelfWindow,'_prepare_morph',prepare))
        original_settle=subject.ShelfWindow._settle_morph
        def settle(self,endpoint):
            global current
            result=original_settle(self,endpoint)
            if current is not None and 'ended_at' not in current:
                current['ended_at']=moment();current['settled']=True
                current['settled_host']=rect(self.geometry());current['settled_card']=rect(self.card.geometry())
            return result
        patches.enter_context(patch.object(subject.ShelfWindow,'_settle_morph',settle))
        for cls,name in ((subject.IconItem,'__init__'),(subject.ContentsPopup,'rebuild'),
                         (subject.PreviewPane,'show_path'),(subject.ImagePreview,'set_source'),
                         (surface_morph.SurfaceSnapshot,'capture_live'),
                         (surface_morph.SurfaceSnapshot,'capture_current'),
                         (flickdock_motion.MotionShadow,'draw')):
            patches.enter_context(instrument(cls,name))
        manager=subject.ShelfManager(app);manager.timer.stop();shelf=manager.shelf;shelf.pinned=True
        assert isinstance(manager.hotkeys.backend,_NoopBackend)
        manager.settings.update(view_mode='icons')
        refs=[shelf.content.add_text(f'Clock test {index}: text 文字内容') for index in range(100)]
        image=QImage(3000,2000,QImage.Format.Format_RGB32);image.fill(QColor('#78a895'))
        painter=QPainter(image)
        for index in range(40):painter.fillRect(index*73%2800,index*109%1800,180,140,QColor.fromHsv(index*17%360,180,180))
        painter.end();images=[]
        for index in range(25):
            path=Path(temporary)/f'photo-{index}.jpg';assert image.save(str(path),'JPG',90)
            images.append(str(path));refs.append(str(path))
        shelf.add_paths('permanent',refs)
        heartbeat=QTimer();heartbeat.setTimerType(Qt.TimerType.PreciseTimer);heartbeat.setInterval(20);heartbeat.timeout.connect(beat);heartbeat.start()
        sampler=QTimer();sampler.setTimerType(Qt.TimerType.PreciseTimer);sampler.setInterval(1);sampler.timeout.connect(sample);sampler.start()
        shelf.geometry_morph.animation.valueChanged.connect(lambda _:sample())
        spin_until(lambda:len(beats)>=9)
        baseline_gaps=[right['at']-left['at'] for left,right in zip(beats,beats[1:])]
        heartbeat_budget=max(100.0,min(180.0,4*max(baseline_gaps)))
        results=[]
        def complete():
            return not shelf.geometry_morph.active and not shelf._transition_preparing
        def content_complete():
            return (complete() and shelf.popup is not None and
                    shelf.popup._build_cursor==len(shelf.popup._build_refs))
        def invoke(action,label):
            before=time.perf_counter();action()
            calls.append(dict(phase=phase,label=label,at=round((before-started)*1000,3),
                              duration_ms=round((time.perf_counter()-before)*1000,3)))
        def run(action,name,*,preview=False):
            global phase
            phase=name;begin=moment();beat()
            invoke(action,name)
            spin_until(lambda:content_complete() and (not preview or shelf.popup.preview.image.image is not None))
            # Include the settled view and the first deferred work callbacks.
            spin_until(lambda:moment()-begin>=650)
            beat();results.append(dict(name=name,begin=begin,end=moment(),
                                      items=len(shelf.popup.visible_refs()),
                                      image_ready=shelf.popup.preview.image.image is not None))
            phase=None
        run(manager.show_overview,'cold_overview',preview=True)
        run(manager.show_shortcut,'shortcut_collapse')
        run(manager.show_overview,'warm_overview',preview=True)
        run(shelf.collapse_contents,'warm_collapse')
        shelf.image_cache.clear();shelf.popup.preview.image.clear_source();shelf.popup.preview.current_path=None
        shelf.popup.selected={images[0]};shelf.popup.primary_selection=images[0]
        run(manager.show_overview,'cold_image_overview',preview=True)
        run(manager.show_shortcut,'image_shortcut_collapse')
        phase='rapid_reversals';begin=moment();beat()
        invoke(manager.show_overview,'rapid_expand_0')
        pending=[]
        for index in range(1,7):
            timer=QTimer();timer.setSingleShot(True)
            action=manager.show_shortcut if index%2 else manager.show_overview
            timer.timeout.connect(lambda action=action,index=index:invoke(action,'rapid_'+str(index)))
            timer.start(145*index);pending.append(timer)
        spin_until(lambda:moment()-begin>=145*6+650 and content_complete())
        beat();results.append(dict(name=phase,begin=begin,end=moment(),items=len(shelf.popup.visible_refs())))
        phase=None;sampler.stop();heartbeat.stop()
        evidence=dict(platform=app.platformName(),dpr=shelf.devicePixelRatioF(),items=len(refs),
                      scope='Real QEventLoop, no animation seeking; offscreen, no DWM claim',
                      baseline_heartbeat_max_ms=round(max(baseline_gaps),3),
                      adaptive_heartbeat_budget_ms=round(heartbeat_budget,3),operations=[],
                      segments=segments,entrance_frames=entrance_frames,calls=calls,slow_events=events,functions=function_costs,violations=violations)
        failures=[]
        for result in results:
            relevant=[segment for segment in segments if segment['phase']==result['name']]
            visible=[segment for segment in relevant if segment['ended_at']-segment['prepared_at']>=35]
            for segment in visible:
                frames=segment['frames']
                segment['unique_visuals']=len(frames)
                segment['max_visual_gap_ms']=round(max([right['at']-left['at'] for left,right in zip(frames,frames[1:])] or [0]),3)
                if len(frames)<5:failures.append(f"{result['name']} segment {segment['id']} has {len(frames)} visual frames")
                if segment.get('settled') and (segment['settled_host']!=segment['host'] or segment['settled_card']!=segment['card']):
                    failures.append(f"segment {segment['id']} changed host/card at completion")
            phase_beats=[entry for entry in beats if result['begin']<=entry['at']<=result['end']]
            gaps=[right['at']-left['at'] for left,right in zip(phase_beats,phase_beats[1:])]
            active_gaps=[right['at']-left['at'] for left,right in zip(phase_beats,phase_beats[1:])
                         if left['morph'] or right['morph'] or left['entrance'] or right['entrance']]
            summary=dict(name=result['name'],max_heartbeat_gap_ms=round(max(gaps or [0]),3),
                         max_animation_heartbeat_gap_ms=round(max(active_gaps or [0]),3),
                         prepares=len(relevant),cancelled_before_tick=len(relevant)-len(visible),
                         min_unique_visuals=min([len(segment['frames']) for segment in visible] or [0]),
                         max_prepare_ms=max([segment['prepare_ms'] for segment in relevant] or [0]),
                         max_entry_call_ms=max([entry['duration_ms'] for entry in calls if entry['phase']==result['name']] or [0]),
                         **{key:value for key,value in result.items() if key not in ('name','begin','end')})
            evidence['operations'].append(summary)
            if result['name']=='cold_overview':
                # The shortcut now reveals the final overview using the same
                # alpha/scale spring as the preview, without a geometry morph.
                frames=[frame for frame in entrance_frames if frame['phase']=='cold_overview']
                summary['entrance_frames']=len(frames)
                if relevant:failures.append('cold overview unexpectedly expanded from a compact shelf')
                if len(frames)<5:failures.append('cold overview showed no real-clock entrance')
                if frames and any(frame['host']!=frames[0]['host'] or frame['visual']!=frames[0]['visual'] or
                                  frame['values']['dx']!=0 for frame in frames):
                    failures.append('cold overview entrance moved sideways or resized the native surface')
            elif not visible:failures.append(result['name']+' showed no real-clock transition')
            if max(active_gaps or [0])>heartbeat_budget:failures.append(result['name']+' blocked the 20 ms heartbeat')
        constructed=[entry for entry in function_costs if entry['name']=='IconItem.__init__' and entry['morph'] and not entry['preparing']]
        evidence['icons_constructed_during_animation']=len(constructed)
        if constructed:failures.append('content widgets constructed during animation')
        if violations:failures.append('native/live card geometry changed during animation')
        painter_errors=[warning for warning in warnings if 'QPainter' in warning or 'paint device' in warning]
        if painter_errors:failures.append('invalid painter lifecycle')
        evidence['painter_errors']=painter_errors;evidence['failures']=failures
        Path(__file__).with_name('morph-clock-v164.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({key:value for key,value in evidence.items() if key not in ('segments','entrance_frames','calls','slow_events','functions','violations')},ensure_ascii=False,indent=2))
        manager.quit()
        assert not failures,failures
print('MORPH_CLOCK_V164_OK')
