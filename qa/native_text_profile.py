"""Hosted-only phase evidence for the unchanged complete-text deadline test."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types

ROOT=Path(__file__).resolve().parents[1]
PROFILE_FILES=('win_clipboard_native.py','win_clipboard_cache.py','win_clipboard_io.py',
    'test_clipboard_local_original_v1825.py')


def hosted_guard():
    if (sys.platform!='darwin' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='macOS' or os.environ.get('GITHUB_JOB')!='text-profile'
            or os.environ.get('FD_QA_TEXT_PROFILE')!='1'):
        raise RuntimeError('Text phase profiling requires its isolated hosted macOS job')


def instrument(owner,name,log):
    import functools
    import time
    original=getattr(owner,name)
    @functools.wraps(original)
    def measured(*args,**kwargs):
        started=time.perf_counter()
        emit(log,dict(stage=name,event='start',pid=os.getpid(),t=started))
        try:return original(*args,**kwargs)
        finally:emit(log,dict(stage=name,event='end',pid=os.getpid(),t=time.perf_counter(),seconds=time.perf_counter()-started))
    setattr(owner,name,measured)


def emit(log,row):
    descriptor=os.open(log,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
    try:
        raw=(json.dumps(row,separators=(',',':'))+'\n').encode('ascii')
        if os.write(descriptor,raw)!=len(raw):raise OSError('Incomplete diagnostic stage write')
    finally:os.close(descriptor)


CHILD_INSTRUMENT=r'''
import functools,json,os,time
import win_clipboard_native as native
def emit(row):
 fd=os.open(os.environ['FD_TEXT_STAGE_LOG'],os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
 try:
  raw=(json.dumps(row,separators=(',',':'))+'\n').encode('ascii')
  if os.write(fd,raw)!=len(raw):raise OSError('Incomplete diagnostic stage write')
 finally:os.close(fd)
def instrument(owner,name):
 original=getattr(owner,name)
 @functools.wraps(original)
 def measured(*args,**kwargs):
  started=time.perf_counter();emit(dict(stage=name,event='start',pid=os.getpid(),t=started))
  try:return original(*args,**kwargs)
  finally:emit(dict(stage=name,event='end',pid=os.getpid(),t=time.perf_counter(),seconds=time.perf_counter()-started))
 setattr(owner,name,measured)
instrument(native,'_text_parts');instrument(cache.CacheSink,'write')
emit(dict(stage='child_imported',event='point',pid=os.getpid(),t=time.perf_counter()))
'''


def main():
    hosted_guard()
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.absolute();allowed=(ROOT/'build').resolve()
    if not output.resolve().is_relative_to(allowed):raise ValueError('Diagnostic output must stay inside owned build directory')
    output.mkdir(parents=True,exist_ok=False)
    def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in PROFILE_FILES}
    before=hashes()
    with tempfile.TemporaryDirectory(prefix='fd-text-profile-') as profile:
        os.environ.update(QT_QPA_PLATFORM='offscreen',TEMP=profile,TMP=profile,TMPDIR=profile,
            APPDATA=profile,LOCALAPPDATA=profile,XDG_CONFIG_HOME=profile,
            FD_TEXT_STAGE_LOG=str(output/'stages.jsonl'))
        os.environ.pop('FD_QA_NATIVE',None);tempfile.tempdir=profile
        sys.path.insert(0,str(ROOT))
        import unittest
        import win_clipboard_native as native
        import win_clipboard_cache as cache
        import win_clipboard_io as reader
        import test_clipboard_local_original_v1825 as checks
        from qt_unittest import QtTestRunner
        log=output/'stages.jsonl'
        instrument(native,'_text_parts',log);instrument(cache.CacheCapability,'adopt',log)
        instrument(reader._ReadOperation,'_run',log)
        method=checks.LocalOriginalTests.test_real_100m_plus_one_logical_text_keeps_full_utf16_under_default_deadline
        constants=list(method.__code__.co_consts)
        slots=[i for i,value in enumerate(constants) if isinstance(value,str) and value.startswith('import sys,argparse,mmap')]
        if len(slots)!=1:raise RuntimeError('Exact original child program was not found')
        index=slots[0];needle='from win_clipboard_native import capture,encode_frame\n'
        if constants[index].count(needle)!=1:raise RuntimeError('Exact child instrumentation boundary changed')
        constants[index]=constants[index].replace(needle,needle+CHILD_INSTRUMENT)
        compile(constants[index],'owned-synthetic-text-child','exec')
        setattr(checks.LocalOriginalTests,method.__name__,types.FunctionType(
            method.__code__.replace(co_consts=tuple(constants)),method.__globals__,method.__name__,method.__defaults__,method.__closure__))
        suite=unittest.TestSuite([checks.LocalOriginalTests(method.__name__)])
        with (output/'check.log').open('x',encoding='utf8') as stream:
            result=QtTestRunner(stream=stream,verbosity=2).run(suite)
        after=hashes()
    receipt=dict(before=before,after=after,stable=before==after,successful=result.wasSuccessful(),
        tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        scope='Instrumented original complete synthetic subprocess test on hosted Mac; no OS clipboard/public service/installer/physical cross-device acceptance',
        default_deadline_ms=3000,deadline_or_assertions_changed=False,publication=False)
    (output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))
    return 0 if result.wasSuccessful() and before==after else 1


if __name__=='__main__':raise SystemExit(main())
