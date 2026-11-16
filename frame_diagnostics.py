"""Opt-in frame measurements; no filenames or frame pixels are recorded."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from queue import Full, Queue
import statistics
import threading
import time
import weakref

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from shiboken6 import isValid


FRAME_BATCH_SIZE = 60
DEFAULT_REFRESH_HZ = 60.0
MIN_REFRESH_HZ = 24.0
MAX_REFRESH_HZ = 360.0
DROP_INTERVAL_MULTIPLIER = 1.5
WRITER_QUEUE_LIMIT = 64


def normalize_refresh_rate(value):
    """Return the actual screen rate, or None when Qt cannot report it."""
    try:rate=float(value)
    except (TypeError,ValueError):return None
    return rate if math.isfinite(rate) and MIN_REFRESH_HZ<=rate<=MAX_REFRESH_HZ else None


def _distribution(values):
    if not values:return {'p50':None,'p95':None,'max':None}
    ordered=sorted(values)
    return {'p50':round(statistics.median(ordered),3),
            'p95':round(ordered[max(0,math.ceil(len(ordered)*.95)-1)],3),
            'max':round(ordered[-1],3)}


def _inferred_cap(refresh_hz,intervals,paint,count,platform):
    if platform in ('offscreen','minimal','minimalegl'):return None
    target=1000./(refresh_hz or DEFAULT_REFRESH_HZ)
    return (60 if refresh_hz and refresh_hz>=90 and count>=20
            and intervals['p50'] is not None and 15.5<=intervals['p50']<=17.9
            and intervals['p95']<=19 and paint['p95'] is not None
            and paint['p95']<target*.75 else None)


class _AsyncWriter:
    """Serialize batches off the GUI thread; a full queue never blocks paint."""
    def __init__(self,base):
        self.base=Path(base)/'diagnostics'
        self.queue=Queue(WRITER_QUEUE_LIMIT)
        self.dropped=0
        self.closed=False
        self.worker=threading.Thread(target=self._run,name='FlickDock-frame-diagnostics',daemon=True)
        self.worker.start()

    def submit(self,record):
        if self.closed:return
        try:self.queue.put_nowait(record)
        except Full:self.dropped+=1

    def close(self):
        if self.closed:return
        self.closed=True
        # The worker also exits once the bounded queue drains; no GUI wait.

    def wait_closed(self,timeout=2):
        self.close();self.worker.join(timeout)
        return not self.worker.is_alive()

    def _run(self):
        from queue import Empty
        while True:
            try:record=self.queue.get(timeout=.25)
            except Empty:
                if self.closed:return
                continue
            try:
                self.base.mkdir(parents=True,exist_ok=True)
                path=self.base/('frames-'+datetime.now().strftime('%Y-%m-%d')+'.jsonl')
                with path.open('a',encoding='utf-8',newline='\n') as stream:
                    stream.write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n')
            except OSError:
                logging.exception('Unable to write frame diagnostics')
            finally:self.queue.task_done()


class FrameTrace:
    def __init__(self,window,*,base=None,writer=None,clock=None):
        self._window=weakref.ref(window)
        self._clock=clock or time.perf_counter
        if writer is None:
            from platform_native import data_directory
            writer=_AsyncWriter(data_directory() if base is None else base)
        self._writer=writer
        self._handle=None;self._screen=None
        self.screen_name='';self.refresh_hz=None;self.screen_changed_at=None
        self.phase='other';self._last_update=None;self._frames=0;self._dispatched=0;self._capped=False
        self._intervals=[];self._paint=[];self._frame_paint=[]
        self._closed=False
        self.set_screen(window.screen())

    def bind_handle(self,handle):
        if handle is self._handle:return
        if self._handle is not None and isValid(self._handle):
            self._handle.screenChanged.disconnect(self.set_screen)
        self._handle=handle
        if handle is not None:
            handle.screenChanged.connect(self.set_screen)
            self.set_screen(handle.screen())

    def set_screen(self,screen):
        if screen is self._screen:
            self._read_screen();return
        self.flush()
        if self._screen is not None and isValid(self._screen):
            self._screen.refreshRateChanged.disconnect(self._screen_rate_changed)
        self._screen=screen
        self.screen_changed_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        if screen is not None:screen.refreshRateChanged.connect(self._screen_rate_changed)
        self._last_update=None
        self._read_screen()

    def _screen_rate_changed(self,_value):
        self.flush();self._last_update=None;self._read_screen()

    def _read_screen(self):
        screen=self._screen
        if screen is None or not isValid(screen):
            self.screen_name='';self.refresh_hz=None;return
        self.screen_name=screen.name()
        self.refresh_hz=normalize_refresh_rate(screen.refreshRate())

    def begin_phase(self,phase):
        if phase==self.phase:return
        self.flush();self.phase=phase;self._last_update=None

    def end_phase(self):
        self.flush();self.phase='other';self._last_update=None

    def update_request(self,phase=None,*,capped=False):
        if self._closed:return
        if phase is not None:self.begin_phase(phase)
        if capped!=self._capped:
            self.flush();self._last_update=None;self._capped=capped
        now=self._clock()
        if self._last_update is not None:
            self._intervals.append(max(0.,(now-self._last_update)*1000))
        self._last_update=now;self._frames+=1

    def dispatched(self):
        if self._closed:return
        self._dispatched+=1
        if self._frames>=FRAME_BATCH_SIZE:self.flush(keep_last=True)

    def skipped(self):
        if not self._closed and self._frames>=FRAME_BATCH_SIZE:self.flush(keep_last=True)

    def paint(self,kind,elapsed_ms):
        if self._closed:return
        target=self._paint if kind=='snapshot' else self._frame_paint
        target.append(max(0.,float(elapsed_ms)))
        if len(target)>=FRAME_BATCH_SIZE and not self._frames:self.flush()

    def flush(self,*,keep_last=False):
        if not self._frames and not self._paint and not self._frame_paint:return None
        window=self._window()
        if window is None or not isValid(window):return None
        target=1000./(self.refresh_hz or DEFAULT_REFRESH_HZ)
        intervals=_distribution(self._intervals)
        paints=_distribution(self._paint)
        frame_paints=_distribution(self._frame_paint)
        missing=sum(max(1,round(value/target)-1) for value in self._intervals
                    if value>DROP_INTERVAL_MULTIPLIER*target)
        total=len(self._intervals)+missing
        mean=statistics.fmean(self._intervals) if self._intervals else None
        measured_paint=paints if self._paint else frame_paints
        # An observed 60 Hz plateau is only evidence of a platform cap when
        # the measured paint cost leaves ample room for the faster display.
        platform=QGuiApplication.platformName()
        cap=_inferred_cap(self.refresh_hz,intervals,measured_paint,len(self._intervals),platform)
        record=dict(timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                    phase=self.phase,frames=self._frames,paint_ms=measured_paint,
                    snapshot_paint_ms=paints,paint_frame_ms=frame_paints,update_interval_ms=intervals,
                    fps=round(1000/mean,2) if mean and mean>0 else None,
                    dispatched_frames=self._dispatched,
                    dispatch_fps=round((self._dispatched/self._frames)*1000/mean,2) if self._frames and mean and mean>0 else None,
                    dispatch_cap_hz=60 if self._capped else None,
                    dpr=window.devicePixelRatioF(),window_size=[window.width(),window.height()],
                    translucent=window.testAttribute(Qt.WA_TranslucentBackground),
                    preview_playing=bool(getattr(window,'preview_playing',False)),
                    refresh_hz=self.refresh_hz,target_interval_ms=round(target,3),
                    actual_interval_ms=round(mean,3) if mean is not None else None,
                    dropped_frames=missing,drop_rate=round(missing/total,4) if total else None,
                    screen_name=self.screen_name,screen_changed_at=self.screen_changed_at,
                    platform_cap_hz=cap,platform_cap_inferred=cap is not None,
                    qt_platform=platform,timing_source='offscreen' if platform in ('offscreen','minimal','minimalegl') else 'native',
                    diagnostic_records_dropped=getattr(self._writer,'dropped',0))
        self._writer.submit(record)
        self._frames=0;self._dispatched=0;self._intervals.clear();self._paint.clear();self._frame_paint.clear()
        if not keep_last:self._last_update=None
        return record

    def close(self,*_):
        if self._closed:return
        self.flush();self._closed=True
        if self._handle is not None and isValid(self._handle):
            self._handle.screenChanged.disconnect(self.set_screen)
        if self._screen is not None and isValid(self._screen):
            self._screen.refreshRateChanged.disconnect(self._screen_rate_changed)
        self._handle=None;self._screen=None;self._writer.close()

    def wait_closed(self,timeout=2):
        self.close()
        return self._writer.wait_closed(timeout)


def for_window(window):
    if os.environ.get('FLICKDOCK_FRAME_TRACE','').lower() not in ('1','true','yes','on'):
        return None
    trace=getattr(window,'_flickdock_frame_trace',None)
    if trace is None:
        trace=FrameTrace(window);window._flickdock_frame_trace=trace
        window.destroyed.connect(trace.close)
    return trace
