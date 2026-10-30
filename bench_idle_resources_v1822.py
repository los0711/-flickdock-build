"""Matched idle observer: no repeated QObject traversal inside CPU interval."""
import argparse,json,time
from pathlib import Path
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,WorkspaceTests,flush,until
from performance_metrics import process_sample,qt_sample,process_topology


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mode',required=True);parser.add_argument('--seconds',type=float,default=30);args=parser.parse_args()
    fixture=WorkspaceTests();fixture.setUp()
    try:
        shelf=fixture.shelf;refs=[]
        for i in range(1000):
            path=fixture.base/f'idle-{i}.txt';path.write_text(str(i));refs.append(str(path))
        shelf.files['permanent'].clear();shelf.add_paths('permanent',refs);shelf.show_contents();flush();popup=shelf.popup;popup.resume_content()
        until(lambda:popup._build_cursor==1000 and popup.retirement.is_idle,seconds=30)
        QTest.qWait(1000);flush()
        before=dict(process=process_sample(),qt=qt_sample(APP),topology=process_topology())
        loop=QEventLoop();timer=QTimer();timer.setInterval(100);heartbeat=dict(last=None,count=0,max_gap_ms=0.)
        def beat():
            now=time.perf_counter()
            if heartbeat['last'] is not None:heartbeat['max_gap_ms']=max(heartbeat['max_gap_ms'],1000*(now-heartbeat['last']))
            heartbeat['last']=now;heartbeat['count']+=1
        timer.timeout.connect(beat);timer.start();QTimer.singleShot(round(args.seconds*1000),loop.quit)
        started=time.perf_counter();cpu=time.process_time();loop.exec();elapsed=time.perf_counter()-started;cpu=time.process_time()-cpu;timer.stop()
        after=dict(process=process_sample(),qt=qt_sample(APP),topology=process_topology())
        report=dict(mode=args.mode,platform=APP.platformName(),files=1000,seconds=elapsed,
            process_cpu_seconds=cpu,cpu_percent_one_core=100*cpu/elapsed,before=before,after=after,heartbeat=heartbeat,
            scope='Offscreen, disposable profile, no networking or GPU; one identical 100ms diagnostic heartbeat, no sampling traversal inside CPU interval.')
        args.output.write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps({key:report[key] for key in ('mode','seconds','cpu_percent_one_core')}))
    finally:fixture.tearDown()


if __name__=='__main__':main()
