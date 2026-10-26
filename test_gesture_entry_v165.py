"""Settings -> production 20 ms poll -> real MIME gate, all input simulated.

The actual Qt platform stays offscreen. Only poll's native branch is selected;
Win32 inputs, registration, native drop-target positioning and registry are
mocked. No cursor is moved and no desktop or native hook is created.
"""
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']='1.25'
from PySide6.QtCore import QEventLoop, QMimeData, QPoint, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


def spin_until(predicate, timeout=3800):
    loop=QEventLoop();timer=QTimer();timer.setInterval(3);deadline=QTimer();deadline.setSingleShot(True)
    timer.timeout.connect(lambda:loop.quit() if predicate() else None);deadline.timeout.connect(loop.quit)
    timer.start();deadline.start(timeout);loop.exec();timer.stop();deadline.stop()
    assert predicate(),'Gesture entry did not complete'


with tempfile.TemporaryDirectory(prefix='flickdock-gesture-entry-v165-') as temporary:
    os.environ['APPDATA']=temporary
    import app_settings,drag_probe,flickdock as subject
    from global_hotkeys import _NoopBackend
    app=QApplication([]);actual_platform=app.platformName;subject.load_font(app)
    assert actual_platform()=='offscreen'
    pointer={'down':False,'pos':QPoint(600,350)}
    with ExitStack() as patches:
        for obj,name,options in (
            (subject,'motion_enabled',dict(return_value=False)),
            (QSystemTrayIcon,'show',{}),(QSystemTrayIcon,'hide',{}),
            (app_settings,'_read_startup_value',{}),(app_settings,'_write_startup_value',{}),
            (subject.QCursor,'pos',dict(side_effect=lambda:QPoint(pointer['pos']))),
            (subject.ctypes.windll.user32,'GetAsyncKeyState',dict(side_effect=lambda key:0x8000 if key==1 and pointer['down'] else 0)),
            (subject.ctypes.windll.user32,'RegisterHotKey',dict(side_effect=AssertionError('Real hotkey registration'))),
            (subject.ctypes.windll.user32,'UnregisterHotKey',dict(side_effect=AssertionError('Real hotkey unregistration'))),
            (subject,'native_window_move_active',dict(return_value=False)),
            (drag_probe,'native_window_move_active',dict(return_value=False)),
            (drag_probe,'_native_windows_enabled',dict(return_value=False))):
            patches.enter_context(patch.object(obj,name,**options))
        manager=subject.ShelfManager(app);manager.timer.stop();shelf=manager.shelf
        assert isinstance(manager.hotkeys.backend,_NoopBackend)
        content=Path(temporary)/'payload.txt';content.write_text('Controlled real MIME payload')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(content))])
        changed=[];manager.settings.changed.connect(lambda:changed.append(manager.shake_detector.config.reversals))
        state={};evidence={'platform':actual_platform(),'settings':[],'real_poll':[]}
        old_notify=manager.drag_gate.on_validated
        def notified(position):
            state['type_ready']=time.perf_counter()-state.get('begin',time.perf_counter())
            old_notify(position)
        manager.drag_gate.on_validated=notified
        original_arm=manager.drag_gate.arm
        def arm(position):
            accepted=original_arm(position)
            if accepted:
                state.setdefault('arms',[]).append(time.perf_counter()-state['begin'])
                if state.get('mime_mode')=='immediate':
                    QTimer.singleShot(0,lambda:manager.drag_gate._content_entered(QPoint(position)) if manager.drag_gate._accepts(mime) else None)
            return accepted
        patches.enter_context(patch.object(manager.drag_gate,'arm',side_effect=arm))
        def configure(value):
            manager.show_settings();dialog=manager.settings_dialog
            dialog.shake_sensitivity_slider.setValue(value);dialog.save()
            assert not dialog.isVisible(),dialog.error_label.text()
            persisted=json.loads(manager.settings.path.read_text(encoding='utf8'))
            expected=6 if value==1 else 3 if value==50 else 2
            assert persisted['shake_sensitivity']==value and manager.shake_detector.config.reversals==expected
            evidence['settings'].append(dict(value=value,persisted=persisted['shake_sensitivity'],
                live_reversals=manager.shake_detector.config.reversals,live_acceleration=manager.shake_detector.config.minimum_acceleration))
        def reset():
            manager.timer.stop();pointer['down']=False
            with patch.object(app,'platformName',return_value='windows'):manager.poll()
            shelf.hide_immediately();manager.auto_shown=None;manager.drag_gate.reset_session();manager.shake_detector.reset()
            pointer['pos']=QPoint(600,350);manager.just_dropped=-100
        def run(value,mode):
            global state
            reset();configure(value);state={'mime_mode':mode,'arms':[],'polls':[]}
            begin=[None]
            manager.timer.timeout.disconnect()
            def tick():
                now=time.perf_counter()
                if begin[0] is None:begin[0]=now;state['begin']=now
                elapsed=now-begin[0]
                pointer['pos']=QPoint(round(600+190*math.sin(2*math.pi*2.5*elapsed)),350)
                pointer['down']=True;state['polls'].append(elapsed)
                manager.poll()
                if shelf.isVisible():state.setdefault('shown',time.perf_counter()-state['begin'])
            manager.timer.timeout.connect(tick)
            with patch.object(app,'platformName',return_value='windows'):
                if mode=='late':
                    delayed=QTimer();delayed.setSingleShot(True)
                    delayed.timeout.connect(lambda:manager.drag_gate._content_entered(QPoint(pointer['pos'])) if manager.drag_gate._accepts(mime) else None)
                    delayed.start(1300)
                manager.timer.start(20)
                try:spin_until(lambda:'shown' in state)
                except AssertionError:
                    print('POLL_DIAGNOSTIC',dict(level=value,polls=len(state['polls']),arms=state['arms'],
                        down=manager.was_down,blocked_inside=manager.drag_started_inside,visible=shelf.isVisible(),
                        config=str(manager.shake_detector.config),turns=list(manager.shake_detector._x.turns),
                        axis_when=manager.shake_detector._x.when,peak=manager.shake_detector._x.peak_speed,
                        gate_active=manager.drag_gate.active,gate_validated=manager.drag_gate.validated))
                    raise
                manager.timer.stop()
            polls=state['polls'];result=dict(level=value,mime_mode=mode,first_arm=round(state['arms'][0],3),
                 type_ready=round(state['type_ready'],3),shown=round(state['shown'],3),poll_count=len(polls),
                 maximum_poll_gap_ms=round(max([right-left for left,right in zip(polls,polls[1:])] or [0])*1000,3))
            evidence['real_poll'].append(result)
            reset();manager.timer.timeout.disconnect();manager.timer.timeout.connect(manager.poll)
            return result
        low,high=run(1,'immediate'),run(100,'immediate')
        assert low['shown']>high['shown']+.4,(low,high)
        delayed_low,delayed_high=run(1,'late'),run(100,'late')
        assert delayed_low['shown']>delayed_high['shown']+.4,(delayed_low,delayed_high)

        # A coarse wall/idle clock must no longer quantise gesture derivatives.
        original_monotonic=time.monotonic
        with patch.object(subject.time,'monotonic',side_effect=lambda:math.floor(original_monotonic()*64)/64):
            coarse_low,coarse_high=run(1,'immediate'),run(100,'immediate')
        assert coarse_low['shown']>coarse_high['shown']+.4
        evidence['coarse_idle_clock_keeps_sensitivity_difference']=True

        # A quick reversal only starts an invisible MIME inspection. Empty
        # payloads and window moves cannot reveal or silently consume a file.
        def no_reveal(window_move):
            global state
            reset();configure(100);state={'begin':time.perf_counter(),'mime_mode':'hold','arms':[]}
            start=time.perf_counter()
            def tick():
                elapsed=time.perf_counter()-start
                pointer['down']=True
                pointer['pos']=QPoint(round(600+190*math.sin(2*math.pi*2.5*elapsed)),350)
                manager.poll()
            manager.timer.timeout.disconnect();manager.timer.timeout.connect(tick)
            with patch.object(app,'platformName',return_value='windows'),patch.object(subject,'native_window_move_active',return_value=window_move):
                manager.timer.start(20)
                loop=QEventLoop();QTimer.singleShot(1000,loop.quit);loop.exec();manager.timer.stop()
            assert not shelf.isVisible()
            assert not manager._probe_drop(mime,QPoint(600,350))
            if window_move:assert not state['arms']
            else:assert state['arms'] and not manager._drag_content_ready
            assert content.read_text()=='Controlled real MIME payload'
            reset();manager.timer.timeout.disconnect();manager.timer.timeout.connect(manager.poll)
        no_reveal(False);no_reveal(True)
        evidence['empty_mime_and_window_move_rejected']=True

        # Accepted gestures use measured direction for an actual reveal spring.
        reset();manager.motion=True;shelf.presentation.motion=True
        manager.shake_detector.trigger_velocity=(1600.0,-900.0)
        manager._content_drag_validated(QPoint(600,350))
        origin=dict(shelf.presentation.values)
        assert origin['dx']==-19 and origin['dy']==11 and origin['alpha']==0
        loop=QEventLoop();QTimer.singleShot(90,loop.quit);loop.exec()
        after=dict(shelf.presentation.values)
        assert after!=origin and after['alpha']>0
        evidence['directional_reveal']={'initial':origin,'after_90ms':after}
        reset();manager.motion=False;shelf.presentation.motion=False

        # A queued valid MIME result from the previous sensitivity must not
        # reveal a shelf after a new sensitivity has been applied.
        reset();configure(100);state={'begin':time.perf_counter(),'mime_mode':'hold','arms':[]}
        pointer['down']=True
        with patch.object(app,'platformName',return_value='windows'):manager.poll()
        assert manager.drag_gate.arm(QPoint(600,350))
        epoch=manager.drag_gate._epoch
        assert manager.drag_gate._accepts(mime)
        manager.drag_gate._content_entered(QPoint(600,350))
        configure(1)
        evidence['pending_setting_change']=dict(epoch_before=epoch,epoch_after=manager.drag_gate._epoch,
                                                active=manager.drag_gate.active,validated=manager.drag_gate.validated,
                                                new_reversals=manager.shake_detector.config.reversals)
        loop=QEventLoop();QTimer.singleShot(30,loop.quit);loop.exec()
        stale_shown=shelf.isVisible()
        evidence['pending_setting_change']['stale_reveal']=stale_shown
        evidence['changed_reversals']=changed
        Path(__file__).with_name('gesture-entry-v165.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(evidence,ensure_ascii=False,indent=2))
        reset();manager.quit()
        assert actual_platform()=='offscreen'
        assert not stale_shown and not manager._drag_content_ready,'A queued probe survived a sensitivity change'
print('GESTURE_ENTRY_V165_OK')
