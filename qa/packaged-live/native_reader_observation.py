"""Hosted-only passive metadata / exact frozen-helper failure observation.

Never changes the clipboard, supplies an app hook, or changes a failed normal
flow into a pass. Native APIs are constructed only after strict runner/scope
guards. Payload hashes are emitted only for the still-owned synthetic producer.
"""
from __future__ import annotations
import hashlib,json,os,re,sys,tempfile
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
EXPECTED_REPOSITORY='los0711/-flickdock-build'
OBSERVER_PREFIX='flickdock-native-observer-'
NORMAL_PREFIX='flickdock-frozen-normal-ci-'
HELPER_DEADLINE_MS=3000
OBSERVATION_WAIT_SECONDS=8
CLOSE_WAIT_SECONDS=2
EXE_HASH_CHUNK_BYTES=1024*1024
MARKER_MAX_BYTES=512
_NONCE=re.compile('[0-9a-f]{32}')
_SHA=re.compile('[0-9a-f]{64}')

def _no_links(path):
    for item in (path,*path.parents):
        if item.is_symlink() or item.is_junction():raise RuntimeError('OBSERVER_REPARSE_REJECTED')

def _hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or os.environ.get('GITHUB_REPOSITORY')!=EXPECTED_REPOSITORY or not os.environ.get('RUNNER_TEMP')):
        raise RuntimeError('OBSERVER_HOSTED_RUNNER_REQUIRED')

def _normal_profile(profile):
    root=Path(profile).absolute();anchor=Path(tempfile.gettempdir()).absolute()
    _no_links(root);_no_links(anchor)
    if (root.name!='FlickDock' or not root.parent.name.startswith(NORMAL_PREFIX)
            or len(root.parent.name)<=len(NORMAL_PREFIX) or root.parent.parent.resolve(strict=True)!=anchor.resolve(strict=True)
            or not root.is_dir()):raise RuntimeError('OBSERVER_NORMAL_PROFILE_REQUIRED')
    return root

def _diagnostic(phase,error):
    kinds={'OSError','PermissionError','FileNotFoundError','TimeoutError','BrokenPipeError','ValueError','RuntimeError','TimeoutExpired'}
    result={'phase':phase,'exception':type(error).__name__ if type(error).__name__ in kinds else 'Exception'}
    for name in ('errno','winerror'):
        value=getattr(error,name,None)
        if type(value)is int and -(2**31)<=value<2**32:result[name]=value
    return result

def _digest(path):
    sha=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(EXE_HASH_CHUNK_BYTES),b''):sha.update(chunk)
    return sha.hexdigest()

class _MetadataAPI:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes=ctypes;self.w=w;self.user=ctypes.WinDLL('user32',use_last_error=True)
        signatures=(('GetClipboardSequenceNumber',[],w.DWORD),('OpenClipboard',[w.HWND],w.BOOL),
            ('CountClipboardFormats',[],ctypes.c_int),('CloseClipboard',[],w.BOOL),
            ('GetClipboardOwner',[],w.HWND),('IsWindow',[w.HWND],w.BOOL),
            ('GetWindowThreadProcessId',[w.HWND,ctypes.POINTER(w.DWORD)],w.DWORD))
        for name,args,result in signatures:
            fn=getattr(self.user,name);fn.argtypes=args;fn.restype=result
    def error(self):
        error=OSError('Observer native metadata unavailable');error.winerror=self.ctypes.get_last_error();return error
    def sequence(self):return int(self.user.GetClipboardSequenceNumber())
    def open(self):return bool(self.user.OpenClipboard(None))
    def count(self):
        self.ctypes.set_last_error(0);value=int(self.user.CountClipboardFormats())
        if value<0 or value==0 and self.ctypes.get_last_error():raise self.error()
        return value
    def close(self):
        if not self.user.CloseClipboard():raise self.error()
    def owner(self):
        hwnd=self.user.GetClipboardOwner()
        if not hwnd or not self.user.IsWindow(hwnd):return None
        pid=self.w.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd,self.ctypes.byref(pid)) or not pid.value:return None
        return int(hwnd),int(pid.value)

def _operation(command):
    from win_clipboard_io import _ReadOperation
    from win_owned_process import spawn_owned
    return _ReadOperation('hosted-native-observation',None,command,HELPER_DEADLINE_MS,spawn_owned)

def _result_summary(result):
    statuses={'ready','unknown','changed','overlimit'}
    codes={'','BUSY','READ_UNAVAILABLE','SEQUENCE_UNAVAILABLE','UNSUPPORTED','SIZE_POLICY','RESOURCE_UNAVAILABLE',
        'METADATA_UNAVAILABLE','CLOSE_UNAVAILABLE','INVALID_FRAME','START_FAILED','PROCESS_FAILED','REAP_FAILED',
        'CACHE_UNAVAILABLE','TERMINATE_FAILED','OUTPUT_LIMIT','DEADLINE','CANCELLED','SUPERSEDED','CLOSED'}
    status=result.get('status');code=result.get('code')
    row={'status':status if isinstance(status,str) and status in statuses else 'invalid',
         'valid':result.get('valid') is True,'known_empty':result.get('known_empty') is True,
         'code':code if isinstance(code,str) and code in codes else 'UNRECOGNIZED_CODE'}
    for name in ('sequence','sequence_before','sequence_after'):
        value=result.get(name);row[name]=value if type(value)is int and 0<=value<=0xffffffff else None
    diagnostic=result.get('diagnostic')
    if isinstance(diagnostic,dict):
        phases={'init','metadata','copy','transform','sequence','close','spawn','job','protect','resume','wait','pipe','decode','cleanup','cache'}
        classes={'OSError','PermissionError','FileNotFoundError','TimeoutError','BrokenPipeError','ValueError','UnicodeDecodeError','RuntimeError','TooLarge','Unsupported','TimeoutExpired','MemoryError','InterruptedError','Exception'}
        if isinstance(diagnostic.get('phase'),str) and isinstance(diagnostic.get('exception'),str) and diagnostic['phase'] in phases and diagnostic['exception'] in classes:
            row['diagnostic']={name:diagnostic[name] for name in ('phase','exception')}
            for name in ('errno','winerror'):
                value=diagnostic.get(name)
                if type(value)is int and -(2**31)<=value<2**32:row['diagnostic'][name]=value
    return row

def _owned_owner(owner,pid):
    return isinstance(owner,tuple) and len(owner)==2 and all(type(value)is int and value>0 for value in owner) and owner[1]==pid

class HostedNativeReaderObservation:
    def __init__(self,profile,nonce,*,producer_profile,create=True,_api_factory=None,_operation_factory=None):
        _hosted()
        if not isinstance(nonce,str) or not _NONCE.fullmatch(nonce):raise RuntimeError('OBSERVER_NONCE_REQUIRED')
        anchor=Path(os.environ['RUNNER_TEMP']).absolute();root=Path(profile).absolute()
        _no_links(anchor);_no_links(root)
        if root.name!=OBSERVER_PREFIX+nonce or root.parent.resolve(strict=True)!=anchor.resolve(strict=True):raise RuntimeError('OBSERVER_OWNED_ROOT_REQUIRED')
        self.producer_profile=_normal_profile(producer_profile)
        marker=root/'observer-owner.json'
        expected={'nonce':nonce,'fixture':'native-reader-observer-v1'}
        if create:
            root.mkdir(exist_ok=False);marker.write_text(json.dumps(expected),encoding='ascii')
        else:
            _no_links(marker)
            if not marker.is_file() or marker.stat().st_size>MARKER_MAX_BYTES or json.loads(marker.read_text(encoding='ascii'))!=expected:raise RuntimeError('OBSERVER_WRONG_OWNER')
        self.root=root;self.nonce=nonce;self._api_factory=_api_factory or _MetadataAPI;self._operation_factory=_operation_factory or _operation
        self._api=None;self._initial=False;self._failure=False;self._closed=False;self._owned_operation=None
    def _guard(self):
        _hosted();_no_links(self.root);marker=self.root/'observer-owner.json';_no_links(marker)
        if not marker.is_file() or marker.stat().st_size>MARKER_MAX_BYTES or json.loads(marker.read_text(encoding='ascii'))!={'nonce':self.nonce,'fixture':'native-reader-observer-v1'}:raise RuntimeError('OBSERVER_WRONG_OWNER')
        if self._closed:raise RuntimeError('OBSERVER_ALREADY_CLOSED')
    def _native(self):
        if self._api is None:self._api=self._api_factory()
        return self._api
    def before_app_start(self):
        self._guard()
        if self._initial:raise RuntimeError('OBSERVER_INITIAL_ALREADY_RECORDED')
        self._initial=True;api=self._native();opened=False
        row={'observation_only':True,'normal_acceptance':False,'clipboard_modified':False,'sequence_before':None,'sequence_after':None,'format_count':None,'opened':False,'closed':None}
        try:
            row['sequence_before']=api.sequence();opened=api.open();row['opened']=opened
            if opened:row['format_count']=api.count()
            row['sequence_after']=api.sequence()
        except OSError as error:row['diagnostic']=_diagnostic('initial_metadata',error)
        finally:
            if opened:
                try:api.close();row['closed']=True
                except OSError as error:
                    row['closed']=False;row['diagnostic']=_diagnostic('initial_close',error)
                    # Never continue an observation while its own clipboard
                    # lock may still be held. No retry/clear/payload read.
                    failure=RuntimeError('OBSERVER_INITIAL_CLOSE_FAILED');failure.diagnostic=row['diagnostic'];raise failure from None
        row['equal_sequence_fact']=type(row['sequence_before'])is int and type(row['sequence_after'])is int and row['sequence_before']==row['sequence_after']
        row['positive_sequence_proof']=type(row['sequence_before'])is int and row['sequence_before']>0 and row['equal_sequence_fact']
        return row
    def _producer(self,producer):
        import subprocess
        if not isinstance(producer,subprocess.Popen) or type(producer.pid)is not int or producer.pid<=0 or producer.poll() is not None:raise RuntimeError('OBSERVER_LIVE_OWNED_PRODUCER_REQUIRED')
        args=producer.args
        if not isinstance(args,(list,tuple)) or len(args)!=3:raise RuntimeError('OBSERVER_PRODUCER_ARGS_REQUIRED')
        script=REPO/'qa/packaged-live/native_editor.py'
        if Path(args[0]).resolve(strict=True)!=Path(sys.executable).resolve(strict=True) or Path(args[1]).resolve(strict=True)!=script.resolve(strict=True) or Path(args[2]).resolve(strict=True)!=self.producer_profile.parent.resolve(strict=True):raise RuntimeError('OBSERVER_PRODUCER_ARGS_REQUIRED')
    def after_native_text_failure(self,exe,exe_sha256,producer,*,failure_label='02-native-text-copy'):
        self._guard()
        if not self._initial or self._failure or failure_label!='02-native-text-copy':raise RuntimeError('OBSERVER_FAILURE_SCOPE_REQUIRED')
        self._failure=True;self._producer(producer)
        path=Path(exe).absolute();_no_links(path)
        if path.resolve(strict=True)!=(REPO/'dist/FlickDock/FlickDock.exe').resolve(strict=True) or not isinstance(exe_sha256,str) or not _SHA.fullmatch(exe_sha256):raise RuntimeError('OBSERVER_IMMUTABLE_EXE_REQUIRED')
        if _digest(path)!=exe_sha256:raise RuntimeError('OBSERVER_EXE_HASH_MISMATCH')
        api=self._native();owner=api.owner();sequence=api.sequence()
        row={'observation_only':True,'normal_acceptance':False,'original_failure_preserved':True,'owner_verified':_owned_owner(owner,producer.pid),'sequence_before_launch':sequence,'helper_started':False}
        if not row['owner_verified']:row['unavailable']='CLIPBOARD_OWNER_NOT_THIS_PRODUCER';return row
        # Recheck exact live Popen ownership immediately before launch.
        self._producer(producer)
        operation=self._operation_factory([str(path),'--clipboard-reader']);self._owned_operation=operation
        operation.start();row['helper_started']=True
        if not operation.done.wait(OBSERVATION_WAIT_SECONDS):
            operation.cancel('CANCELLED');operation.done.wait(CLOSE_WAIT_SECONDS)
        row.update(helper_done=operation.done.is_set(),helper_reaped=operation.reaped)
        process=operation.process
        row['helper_exit_code']=process.returncode if process is not None and type(process.returncode)is int else None
        if not row['helper_done']:row['unavailable']='HELPER_UNREAPED';return row
        result=operation.result if isinstance(operation.result,dict) else {}
        row['result']=_result_summary(result)
        if _digest(path)!=exe_sha256:row['unavailable']='EXE_CHANGED_DURING_OBSERVATION';return row
        after=api.owner();after_sequence=api.sequence()
        same_owner=_owned_owner(after,producer.pid) and after==owner and producer.poll() is None
        same_sequence=type(sequence)is int and sequence>0 and result.get('sequence_before')==result.get('sequence_after')==sequence==after_sequence
        row.update(owner_still_verified=same_owner,sequence_still_verified=same_sequence)
        if same_owner and same_sequence and result.get('valid') is True and operation.reaped:
            value=result.get('value')
            if isinstance(value,tuple) and len(value)==2 and value[0]=='text' and isinstance(value[1],dict):
                text=value[1].get('text');html=value[1].get('html','')
                if isinstance(text,str) and isinstance(html,str):
                    text=text.encode('utf8');html=html.encode('utf8')
                    row['content']={'kind':'text','text_utf8_size':len(text),'text_utf8_sha256':hashlib.sha256(text).hexdigest(),'html_utf8_size':len(html),'html_utf8_sha256':hashlib.sha256(html).hexdigest()}
        return row
    def close(self):
        if not self._closed:self._guard();self._closed=True
        operation=self._owned_operation
        if operation is None:return {'observation_only':True,'normal_acceptance':False,'helper_started':False}
        if not operation.done.is_set():operation.cancel('CANCELLED');operation.done.wait(CLOSE_WAIT_SECONDS)
        return {'observation_only':True,'normal_acceptance':False,'helper_done':operation.done.is_set(),'helper_reaped':operation.reaped,'retained_ownership':not operation.reaped}
