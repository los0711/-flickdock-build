"""Disposable Qt UI stress/soak harness. Never touches native user state."""
import argparse,json,time
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,WorkspaceTests,until,flush
from performance_metrics import process_sample,qt_sample


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mode',default='working_tree');parser.add_argument('--soak-seconds',type=float,default=5)
    parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
    report=dict(mode=args.mode,qt_platform=APP.platformName(),limitations=[
        'Offscreen Qt: no GPU, compositor, monitor hotplug, real native permissions or multi-device networking.',
        'Scroll figures are synthetic event batches; not physical display frame-rate measurements.'],cases=[])
    fixture=WorkspaceTests();started=time.perf_counter();fixture.setUp()
    report['fixture_start_ms']=(time.perf_counter()-started)*1000
    try:
        shelf=fixture.shelf;paths=[]
        for i in range(1000):
            path=fixture.base/f'item-{i:04}.txt';path.write_bytes(b'original file bytes '+str(i).encode());paths.append(str(path))
        previous=0
        for count in (1,20,100,500,1000):
            if count==1:
                shelf.files['permanent'].clear();shelf.refresh()
            before=time.perf_counter();shelf.add_paths('permanent',paths[previous:count]);previous=count
            shelf.show_contents();flush();popup=shelf.popup;popup.resume_content()
            until(lambda:popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle,seconds=30)
            elapsed=(time.perf_counter()-before)*1000
            bar=popup.scroll.verticalScrollBar();before=time.perf_counter()
            for i in range(50):bar.setValue(round(bar.maximum()*i/49));flush()
            case=dict(files=count,add_and_open_ms=elapsed,synthetic_scroll_batch_ms=(time.perf_counter()-before)*1000,
                      process=process_sample(),qt=qt_sample(APP))
            report['cases'].append(case)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,indent=2),encoding='utf8')
            print(json.dumps(dict(mode=args.mode,files=count,add_and_open_ms=elapsed)),flush=True)
            if args.screenshots and count==20:
                args.screenshots.mkdir(parents=True,exist_ok=True)
                popup.grab().save(str(args.screenshots/'overview.png'))
        report['before_cycles']=dict(process=process_sample(),qt=qt_sample(APP))
        for _ in range(100):
            popup.hide();flush();popup.show();flush()
        report['after_cycles']=dict(process=process_sample(),qt=qt_sample(APP))
        samples=[];heartbeat=dict(last=None,max_gap_ms=0,count=0)
        timer=QTimer();timer.setInterval(100 if args.soak_seconds>60 else 10)
        def beat():
            now=time.perf_counter()
            if heartbeat['last'] is not None:heartbeat['max_gap_ms']=max(heartbeat['max_gap_ms'],(now-heartbeat['last'])*1000)
            heartbeat['last']=now;heartbeat['count']+=1
        timer.timeout.connect(beat);timer.start()
        start=time.perf_counter();before_cpu=time.process_time();observer_cpu=[0.]
        loop=QEventLoop();sample_timer=QTimer();sample_timer.setInterval(round(1000*min(60,max(1,args.soak_seconds/120))))
        def sample():
            observer_start=time.process_time()
            elapsed=time.perf_counter()-start
            samples.append(dict(elapsed_seconds=elapsed,process=process_sample(),qt=qt_sample(APP)))
            report['soak_in_progress']=dict(actual_seconds=elapsed,samples=samples,
                completed_hours=[hour for hour in (1,4,8,24) if elapsed>=hour*3600])
            args.output.write_text(json.dumps(report,indent=2),encoding='utf8')
            observer_cpu[0]+=time.process_time()-observer_start
        sample_timer.timeout.connect(sample);sample_timer.start()
        QTimer.singleShot(max(1,round(args.soak_seconds*1000)),loop.quit);sample();loop.exec()
        sample_timer.stop();timer.stop();sample();elapsed=time.perf_counter()-start
        report['soak']=dict(actual_seconds=elapsed,
            cpu_percent_one_core=100*(time.process_time()-before_cpu)/elapsed,
            synchronous_observer_cpu_seconds=observer_cpu[0],
            cpu_percent_excluding_observer=100*max(0,time.process_time()-before_cpu-observer_cpu[0])/elapsed,samples=samples,
            heartbeat_max_gap_ms=heartbeat['max_gap_ms'],heartbeat_count=heartbeat['count'])
        report.pop('soak_in_progress',None)
    finally:fixture.tearDown()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(dict(mode=args.mode,cases=[{k:v for k,v in case.items() if k not in ('process','qt')} for case in report['cases']],
        idle_cpu=report['soak']['cpu_percent_one_core'],soak_seconds=report['soak']['actual_seconds'])))


if __name__=='__main__':main()
