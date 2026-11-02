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
import contextvars,functools,itertools,json,os,time
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
_detail_sample_every=64
_detail_current=contextvars.ContextVar('qa_text_cache_write',default=None)
_detail_ids=itertools.count(1)
_detail_original_open=cache.Path.open
_detail_original_replace=cache.Path.replace
_detail_original_sha256=cache.hashlib.sha256
_detail_original_prepare=cache.CacheSink._prepare
_detail_original_write=cache.CacheSink.write
_detail_original_text_parts=native._text_parts

def _detail_snapshot(state):
 return {key:state[key] for key in (
  'write_id','part_type','prepare_calls','prepare_seconds','partial_open_calls','partial_open_seconds',
  'target_write_calls','target_write_completed','target_write_requested_bytes','target_write_returned_bytes','target_write_seconds',
  'sha_constructor_calls','sha_constructor_seconds','sha_update_calls','sha_update_completed','sha_update_bytes','sha_update_seconds',
  'partial_close_calls','partial_close_seconds','replace_calls','replace_seconds','emit_calls','emit_seconds')}

def _detail_emit(state,stage,event,**fields):
 started=time.perf_counter()
 row=dict(stage=stage,event=event,pid=os.getpid(),t=started,**_detail_snapshot(state))
 row.update(fields);emit(row)
 state['emit_calls']+=1;state['emit_seconds']+=time.perf_counter()-started

class _DetailTarget:
 def __init__(self,target,state):self.target=target;self.state=state
 def __getattr__(self,name):return getattr(self.target,name)
 def __enter__(self):self.target.__enter__();return self
 def __exit__(self,*args):
  state=self.state;state['partial_close_calls']+=1
  _detail_emit(state,'partial_close','start');started=time.perf_counter()
  try:return self.target.__exit__(*args)
  finally:
   state['partial_close_seconds']+=time.perf_counter()-started
   _detail_emit(state,'partial_close','end')
 def write(self,chunk):
  state=self.state;first=state['target_write_calls']==0
  if first:_detail_emit(state,'target_write','first_start')
  state['target_write_calls']+=1;state['target_write_requested_bytes']+=len(chunk);started=time.perf_counter()
  try:
   returned=self.target.write(chunk);state['target_write_completed']+=1
   if type(returned)is int:state['target_write_returned_bytes']+=returned
   return returned
  finally:
   state['target_write_seconds']+=time.perf_counter()-started
   if first:_detail_emit(state,'target_write','first_end')

class _DetailHash:
 def __init__(self,target,state):self.target=target;self.state=state
 def __getattr__(self,name):return getattr(self.target,name)
 def update(self,chunk):
  state=self.state;first=state['sha_update_calls']==0
  if first:_detail_emit(state,'sha_update','first_start')
  state['sha_update_calls']+=1;started=time.perf_counter();completed=False
  try:
   returned=self.target.update(chunk);state['sha_update_completed']+=1;state['sha_update_bytes']+=len(chunk);completed=True
   return returned
  finally:
   state['sha_update_seconds']+=time.perf_counter()-started
   if first:_detail_emit(state,'sha_update','first_end')
   if completed and state['sha_update_completed']%_detail_sample_every==0:
    _detail_emit(state,'cache_write_progress','point')

def _detail_open(path,*args,**kwargs):
 state=_detail_current.get();mode=args[0] if args else kwargs.get('mode','r')
 if state is None or path!=state['partial'] or mode!='xb':return _detail_original_open(path,*args,**kwargs)
 state['partial_open_calls']+=1;_detail_emit(state,'partial_open','start');started=time.perf_counter()
 try:return _DetailTarget(_detail_original_open(path,*args,**kwargs),state)
 finally:
  state['partial_open_seconds']+=time.perf_counter()-started
  _detail_emit(state,'partial_open','end')

def _detail_replace(path,*args,**kwargs):
 state=_detail_current.get()
 if state is None or path!=state['partial']:return _detail_original_replace(path,*args,**kwargs)
 state['replace_calls']+=1;_detail_emit(state,'partial_replace','start');started=time.perf_counter()
 try:return _detail_original_replace(path,*args,**kwargs)
 finally:
  state['replace_seconds']+=time.perf_counter()-started
  _detail_emit(state,'partial_replace','end')

def _detail_sha256(*args,**kwargs):
 state=_detail_current.get()
 if state is None:return _detail_original_sha256(*args,**kwargs)
 state['sha_constructor_calls']+=1;_detail_emit(state,'sha_constructor','start');started=time.perf_counter()
 try:return _DetailHash(_detail_original_sha256(*args,**kwargs),state)
 finally:
  state['sha_constructor_seconds']+=time.perf_counter()-started
  _detail_emit(state,'sha_constructor','end')

@functools.wraps(_detail_original_prepare)
def _detail_prepare(self,*args,**kwargs):
 state=_detail_current.get()
 if state is None or state['owner'] is not self:return _detail_original_prepare(self,*args,**kwargs)
 state['prepare_calls']+=1;_detail_emit(state,'cache_prepare','start');started=time.perf_counter()
 try:return _detail_original_prepare(self,*args,**kwargs)
 finally:
  state['prepare_seconds']+=time.perf_counter()-started
  _detail_emit(state,'cache_prepare','end')

@functools.wraps(_detail_original_write)
def _detail_write(self,name,*args,**kwargs):
 state=dict.fromkeys((
  'prepare_calls','prepare_seconds','partial_open_calls','partial_open_seconds',
  'target_write_calls','target_write_completed','target_write_requested_bytes','target_write_returned_bytes','target_write_seconds',
  'sha_constructor_calls','sha_constructor_seconds','sha_update_calls','sha_update_completed','sha_update_bytes','sha_update_seconds',
  'partial_close_calls','partial_close_seconds','replace_calls','replace_seconds','emit_calls','emit_seconds'),0)
 state.update(owner=self,partial=self.root/(name+'.partial') if isinstance(name,str) and name in ('text','html','image','native_image') else None,write_id=next(_detail_ids),
              part_type=name if name in ('text','html','image','native_image') else 'other')
 token=_detail_current.set(state);started=time.perf_counter();returned=False
 try:
  _detail_emit(state,'write','start',sample_every_chunks=_detail_sample_every)
  value=_detail_original_write(self,name,*args,**kwargs);returned=True;return value
 finally:
  try:_detail_emit(state,'write','end',seconds=time.perf_counter()-started,outcome='returned' if returned else 'raised')
  finally:_detail_current.reset(token)

def _restore_detail_hooks():
 cache.Path.open=_detail_original_open;cache.Path.replace=_detail_original_replace
 cache.hashlib.sha256=_detail_original_sha256
 cache.CacheSink._prepare=_detail_original_prepare;cache.CacheSink.write=_detail_original_write
 native._text_parts=_detail_original_text_parts

cache.Path.open=_detail_open;cache.Path.replace=_detail_replace;cache.hashlib.sha256=_detail_sha256
cache.CacheSink._prepare=_detail_prepare;cache.CacheSink.write=_detail_write
instrument(native,'_text_parts')
emit(dict(stage='child_imported',event='point',pid=os.getpid(),t=time.perf_counter()))
'''


def main():
    hosted_guard()
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.absolute();allowed=(ROOT/'build').resolve()
    if not output.resolve().is_relative_to(allowed):raise ValueError('Diagnostic output must stay inside owned build directory')
    output.mkdir(parents=True,exist_ok=False)
    def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in PROFILE_FILES}
    before=hashes();qa_before=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
        qa_profile_before=qa_before,qa_profile_after=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        scope='Instrumented original complete synthetic subprocess test on hosted Mac; no OS clipboard/public service/installer/physical cross-device acceptance',
        default_deadline_ms=3000,deadline_or_assertions_changed=False,publication=False,
        instrumentation=dict(sample_every_chunks=64,expected_original_chunk_bytes=262144,
            per_chunk_logs=False,additional_body_reads=False,additional_hash_passes=False,
            per_chunk_timing_clock_calls=4,overhead='emit_seconds is cumulative completed diagnostic append time; timing/proxy/clock overhead remains additional and is not subtracted from the original 3s deadline'))
    (output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))
    return 0 if result.wasSuccessful() and before==after else 1


if __name__=='__main__':raise SystemExit(main())
