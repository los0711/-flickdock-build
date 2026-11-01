"""Intel-hosted diagnostic of the unchanged, ordered 117-test motion process.

Baseline launches the exact unittest module order with only -v, -u and Python
faulthandler added. GC tracing is a separate diagnostic treatment, never a
replacement passing gate for a failed baseline. No application build or native
desktop interaction is performed here.
"""
from __future__ import annotations
import argparse
import ast
import faulthandler
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
MODULES=('test_ui_followup_v1823','test_cached_surface_v1823','test_stack_spread_v1823',
         'test_input_motion_v1823','test_content_entrance_v1823','test_transfer_drop_feedback_v1823',
         'test_transfer_item_motion_v1823','test_popup_motion_v1823','test_drop_target_motion_v1823')
EXPECTED_TESTS=117
PACKAGES=('PySide6','shiboken6','PySide6_Essentials','PySide6_Addons','PySide6_WebEngine',
          'PySide6_Pdf','pyobjc-core','pyobjc-framework-Cocoa','pyobjc-framework-Quartz',
          'keyring','cryptography','cffi','PyInstaller')


def require_hosted_intel():
    if (sys.platform!='darwin' or platform.machine()!='x86_64'
            or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='macOS'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or os.environ.get('RUNNER_ARCH')!='X64'):
        raise RuntimeError('Motion crash diagnostic requires an ephemeral hosted Intel macOS runner')


def command(mode,output,collect_after_test=False):
    if mode=='baseline':
        if collect_after_test:raise ValueError('Baseline must retain original GC policy')
        return [sys.executable,'-X','faulthandler','-u','-m','unittest',*MODULES,'-v']
    if mode!='gc-trace':raise ValueError('Unknown diagnostic mode')
    values=[sys.executable,'-X','faulthandler','-u',str(Path(__file__).resolve()),
            '--worker','--output',str(output)]
    if collect_after_test:values.append('--collect-after-test')
    return values


def inventory():
    packages={}
    for name in PACKAGES:
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    source={}
    for module in MODULES:
        source[module+'.py']=hashlib.sha256((ROOT/(module+'.py')).read_bytes()).hexdigest()
    manifest=ast.parse((ROOT/'release_manifest.py').read_text(encoding='utf8'))
    names=next(ast.literal_eval(node.value) for node in manifest.body if isinstance(node,ast.Assign)
               and any(isinstance(target,ast.Name) and target.id=='RUNTIME_SOURCES' for target in node.targets))
    runtime={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    try:commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    except subprocess.SubprocessError:commit=None
    return dict(python=sys.version,executable=sys.executable,arch=platform.machine(),
        platform=platform.platform(),packages=packages,commit=commit,test_sources=source,
        modules=list(MODULES),expected_test_count=EXPECTED_TESTS,
        qpa=os.environ.get('QT_QPA_PLATFORM'),runtime_sources=runtime,runtime_code_edited_by_diagnostic=False,
        application_bundle_executed=False,physical_native_input=False)


def worker(output,collect_after_test=False):
    """Same one-process module order; optional GC observations are explicit."""
    require_hosted_intel();sys.path.insert(0,str(ROOT));output.mkdir(parents=True,exist_ok=True)
    # Import all nine modules in exactly the original unittest argument order.
    suite=unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    if suite.countTestCases()!=EXPECTED_TESTS:raise AssertionError('Ordered motion suite no longer contains exactly 117 tests')
    stream=(output/'gc-progress.jsonl').open('a',encoding='utf8',buffering=1)
    current=[None]
    def record(event,**values):
        stream.write(json.dumps(dict(event=event,test=current[0],mono=time.monotonic(),
            gc_counts=gc.get_count(),threads=threading.active_count(),**values))+'\n');stream.flush()
    def gc_event(phase,info):record('gc:'+phase,info=info)
    class Result(unittest.TextTestResult):
        def startTest(self,test):
            current[0]=test.id();record('startTest');super().startTest(test)
        def stopTest(self,test):
            try:
                super().stopTest(test);record('stopTest')
                if collect_after_test:
                    record('before:explicit-gc');collected=gc.collect();record('after:explicit-gc',collected=collected)
            finally:current[0]=None
    gc.callbacks.append(gc_event);faulthandler.dump_traceback_later(45,repeat=True)
    try:
        record('suite-start',collect_after_test=collect_after_test)
        result=unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
        record('suite-end',tests_run=result.testsRun,ok=result.wasSuccessful())
        return 0 if result.wasSuccessful() else 1
    finally:
        faulthandler.cancel_dump_traceback_later();gc.callbacks.remove(gc_event);stream.close()


def crash_reports(output,pid,started):
    """Only newly generated Python crash reports for this exact child PID."""
    source=Path.home()/'Library'/'Logs'/'DiagnosticReports';target=output/'native-crash-reports'
    copied=[]
    for path in sorted(source.glob('*.ips')) if source.is_dir() else ():
        if not re.search('python',path.name,re.I) or path.stat().st_mtime<started-2 or path.stat().st_size>10_000_000:continue
        raw=path.read_text(encoding='utf8',errors='replace')
        if not re.search(r'"pid"\s*:\s*'+str(pid)+r'\b',raw):continue
        target.mkdir(exist_ok=True);shutil.copy2(path,target/path.name);copied.append(path.name)
        if len(copied)>=3:break
    return copied


def run(output,mode='baseline',collect_after_test=False,timeout=240):
    require_hosted_intel();output=output.resolve();output.mkdir(parents=True,exist_ok=True)
    if not 30<=timeout<=300:raise ValueError('Diagnostic process timeout must be 30..300 seconds')
    report=inventory();report.update(mode=mode,collect_after_test=collect_after_test,
        diagnostic_only=True,passing_variant_replaces_failed_baseline=False)
    args=command(mode,output,collect_after_test);report['command']=args
    (output/'environment.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    started=time.time();mono=time.monotonic();profile=None;child=None
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-intel-motion-') as folder:
            profile=folder;environment=dict(os.environ,APPDATA=folder,LOCALAPPDATA=str(Path(folder)/'local'),
                XDG_CONFIG_HOME=folder,QT_QPA_PLATFORM='offscreen',PYTHONFAULTHANDLER='1',PYTHONUNBUFFERED='1')
            with (output/'ordered-117.log').open('wb') as log:
                child=subprocess.Popen(args,cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT)
                report['child_pid']=child.pid
                try:code=child.wait(timeout=timeout);report['timed_out']=False
                except subprocess.TimeoutExpired:
                    report['timed_out']=True;child.terminate()
                    try:code=child.wait(3)
                    except subprocess.TimeoutExpired:child.kill();code=child.wait(3)
            report['returncode']=code;report['elapsed_seconds']=time.monotonic()-mono
            raw=(output/'ordered-117.log').read_text(encoding='utf8',errors='replace')
            report['completed_117']=bool(re.search(r'Ran 117 tests? in ',raw))
            report['segmentation_signal']=code in (-11,139)
            report['ok']=code==0 and report['completed_117'] and not report['timed_out']
            # Apple may create its exact-PID .ips shortly after child exit.
            if code!=0:time.sleep(2)
            report['native_crash_reports']=crash_reports(output,child.pid,started)
            print(raw[-24000:],flush=True)
    finally:
        if child is not None and child.poll() is None:child.kill();child.wait(3)
        report['isolated_profile_removed']=profile is not None and not Path(profile).exists()
        (output/'diagnostic-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return 0 if report['ok'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mode',choices=('baseline','gc-trace'),default='baseline')
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--collect-after-test',action='store_true')
    options=parser.parse_args()
    if options.worker:return worker(options.output,options.collect_after_test)
    return run(options.output,options.mode,options.collect_after_test)


if __name__=='__main__':raise SystemExit(main())
