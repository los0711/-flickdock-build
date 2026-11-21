"""Frame diagnostics are opt-in, bounded and independent of real displays."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QRect,QRectF,Qt
from PySide6.QtGui import QImage,QPainter,QPixmap
from PySide6.QtWidgets import QApplication,QWidget

from frame_diagnostics import FrameTrace,_AsyncWriter,_inferred_cap,for_window,normalize_refresh_rate
from surface_morph import SurfaceSnapshot

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class _Sink:
    def __init__(self):self.records=[];self.closed=False
    def submit(self,value):self.records.append(value)
    def close(self):self.closed=True
    def wait_closed(self,timeout=2):self.close();return True


class FrameDiagnosticsTests(unittest.TestCase):
    def setUp(self):self.window=QWidget();self.window.resize(320,200)
    def tearDown(self):self.window.close();self.window.deleteLater();APP.processEvents()

    def test_trace_is_disabled_by_default(self):
        with patch.dict(os.environ,{'FLICKDOCK_FRAME_TRACE':'0'}):
            self.assertIsNone(for_window(self.window))
            self.assertFalse(hasattr(self.window,'_flickdock_frame_trace'))

    def test_refresh_validation_and_aggregate_drop_counts(self):
        for value in (None,0,23,361,float('nan'),float('inf'),'invalid'):
            self.assertIsNone(normalize_refresh_rate(value))
        for value in (24,60,90,120,144,165,240,360):
            self.assertEqual(normalize_refresh_rate(value),float(value))
        now=[0.];sink=_Sink();trace=FrameTrace(self.window,writer=sink,clock=lambda:now[0])
        try:
            trace.refresh_hz=120.
            trace.begin_phase('expand')
            for index in range(60):
                now[0]+=3/120 if index==30 else 1/120
                trace.paint('frame',2.)
                trace.update_request('expand');trace.dispatched()
            self.assertEqual(len(sink.records),1)
            record=sink.records[0]
            self.assertEqual(record['phase'],'expand')
            self.assertEqual(record['frames'],60)
            self.assertEqual(record['paint_frame_ms']['p95'],2.)
            self.assertAlmostEqual(record['update_interval_ms']['p50'],1000/120,places=2)
            self.assertEqual(record['dropped_frames'],2)
            self.assertGreater(record['drop_rate'],0)
            self.assertEqual(record['window_size'],[320,200])
            self.assertEqual(record['target_interval_ms'],8.333)
        finally:trace.close()

    def test_phase_and_screen_changes_flush_without_counting_idle_time(self):
        now=[1.];sink=_Sink();trace=FrameTrace(self.window,writer=sink,clock=lambda:now[0])
        try:
            trace.update_request('expand');now[0]+=.02;trace.update_request('expand')
            now[0]+=10;trace.update_request('collapse')
            self.assertEqual(len(sink.records),1)
            self.assertEqual(sink.records[0]['phase'],'expand')
            self.assertEqual(sink.records[0]['actual_interval_ms'],20.)
            trace.set_screen(None)
            self.assertIsNone(trace.refresh_hz)
            trace.set_screen(APP.primaryScreen())
            self.assertIsNotNone(trace.screen_changed_at)
            trace.end_phase()
            self.assertEqual(sink.records[-1]['phase'],'collapse')
            self.assertIsNone(sink.records[-1]['actual_interval_ms'])
        finally:trace.close()

    def test_cap_is_only_inferred_with_fast_paint_on_a_fast_screen(self):
        now=[0.];sink=_Sink();trace=FrameTrace(self.window,writer=sink,clock=lambda:now[0])
        try:
            trace.refresh_hz=144.
            trace.begin_phase('expand')
            for _ in range(60):
                now[0]+=1/60;trace.paint('frame',1.);trace.update_request('expand');trace.dispatched()
            record=sink.records[-1]
            self.assertIsNone(record['platform_cap_hz'])
            self.assertEqual(_inferred_cap(144.,record['update_interval_ms'],record['paint_ms'],59,'windows'),60)
            for _ in range(60):
                now[0]+=1/60;trace.paint('frame',12.);trace.update_request('expand');trace.dispatched()
            self.assertIsNone(sink.records[-1]['platform_cap_hz'])
        finally:trace.close()

    def test_writer_persists_batches_without_blocking_callers(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-frame-trace-') as root:
            writer=_AsyncWriter(root);writer.submit({'phase':'expand','fps':60})
            self.assertTrue(writer.wait_closed())
            files=list((Path(root)/'diagnostics').glob('frames-*.jsonl'))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text(encoding='utf-8'))['phase'],'expand')

    def test_snapshot_paint_records_real_elapsed_time(self):
        sink=_Sink()
        with patch.dict(os.environ,{'FLICKDOCK_FRAME_TRACE':'1'}),patch('frame_diagnostics._AsyncWriter',return_value=sink):
            snapshot=SurfaceSnapshot(self.window)
            source=QPixmap(100,100);source.fill(Qt.red)
            target=QPixmap(200,150);target.fill(Qt.blue)
            snapshot.begin(source,target,QRect(0,0,200,150),present=False)
            snapshot.mix=.5
            image=QImage(200,150,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
            painter=QPainter(image)
            try:snapshot.paint_frame(painter,QRectF(0,0,200,150))
            finally:painter.end()
            trace=self.window._flickdock_frame_trace;record=trace.flush()
            self.assertIsNotNone(record['paint_frame_ms']['p95'])
            self.assertGreaterEqual(record['paint_frame_ms']['p95'],0)
            snapshot.finish();trace.close()


if __name__=='__main__':unittest.main()
