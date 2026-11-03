"""Portable QA contracts. Importing this file never loads Qt or a native DLL."""
from __future__ import annotations
import hashlib,json,math,os,re,struct,subprocess,sys,threading,time,traceback
from pathlib import Path

MAGIC=b'FDXPQA1\0'
GETDATA_EVENT=b'FDXEVENT\0GETDATA_ENTER\n'
MAX_FRAME=8*1024*1024  # QA reference-list/diagnostic bound; never an app/cloud limit.
SCOPE_NAME=re.compile(r'fd-explicit-format-[0-9a-f]{32}\Z')
JOB='explicit-paste-format-probe'
PHASES={'metadata','paths','legacy-inspect','legacy-control','legacy-filtered','blocked-provider'}
LEGACY_STAGES=frozenset({'imports_begin','imports_complete','application_begin','application_ready',
    'clipboard_read_begin','clipboard_read_complete','dialog_begin','dialog_ready','paste_begin','paste_complete',
    'undo_begin','undo_complete','dialog_retire_begin','dialog_retire_complete','frame_begin','frame_complete'})
LEGACY_TRACE_MAX_BYTES=16384

def read_legacy_trace(root,pid):
    if type(pid)is not int or pid<=0:raise ValueError('OWNED_TRACE_PID_REQUIRED')
    path=root/('legacy-stage-'+str(pid)+'.jsonl')
    if not path.exists():return []
    if path.is_symlink() or path.is_junction() or path.stat().st_size>LEGACY_TRACE_MAX_BYTES:raise ValueError('OWNED_TRACE_INVALID')
    rows=[]
    for line in path.read_text(encoding='utf8').splitlines():
        row=json.loads(line)
        if (not isinstance(row,dict) or set(row)!={'stage','seconds'} or type(row['stage'])is not str or row['stage'] not in LEGACY_STAGES
            or type(row['seconds'])not in (int,float) or not math.isfinite(row['seconds']) or row['seconds']<0):raise ValueError('OWNED_TRACE_SCHEMA')
        rows.append(row)
    return rows

class GuardError(RuntimeError):pass
class QuarantineError(RuntimeError):
    def __init__(self,receipt):self.receipt=receipt;super().__init__('OWNED_OPERATION_QUARANTINED')
class BoundaryError(RuntimeError):
    def __init__(self,code):
        if not isinstance(code,str) or not re.fullmatch('[A-Z0-9_]{1,80}',code):raise ValueError('INVALID_FIXED_ERROR_CODE')
        self.code=code;super().__init__(code)

def safe_error(cause):
    chain=[];seen=set()
    while cause is not None and id(cause) not in seen and len(chain)<4:
        seen.add(id(cause));entry=dict(type=type(cause).__name__,frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno)
                for f in traceback.extract_tb(cause.__traceback__)])
        if isinstance(cause,BoundaryError):entry['code']=cause.code
        for key in ('errno','winerror'):
            if type(getattr(cause,key,None))is int:entry[key]=getattr(cause,key)
        chain.append(entry);cause=cause.__cause__ or cause.__context__
    return chain

def inside(path,base):
    try:path.relative_to(base);return True
    except ValueError:return False

def require_hosted(*,env=None,platform=None):
    env=os.environ if env is None else env;platform=sys.platform if platform is None else platform
    required={'GITHUB_ACTIONS':'true','CI':'true','RUNNER_ENVIRONMENT':'github-hosted',
              'GITHUB_REPOSITORY':'los0711/-flickdock-build','GITHUB_EVENT_NAME':'workflow_dispatch',
              'RUNNER_OS':'Windows','GITHUB_JOB':JOB,'FD_QA_NATIVE':'1','FD_EXPLICIT_PASTE_FORMAT_PROBE':'1'}
    if platform!='win32' or any(env.get(key)!=value for key,value in required.items()):
        raise GuardError('HOSTED_ONLY_GUARD')
    for key in ('RUNNER_TEMP','GITHUB_WORKSPACE','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'):
        if not isinstance(env.get(key),str) or not env[key]:raise GuardError('HOSTED_CONTEXT_MISSING')
    return Path(env['RUNNER_TEMP']).resolve(),Path(env['GITHUB_WORKSPACE']).resolve()

def scope_record(root,nonce,*,role='controller',env=None,platform=None):
    env=os.environ if env is None else env;temp,workspace=require_hosted(env=env,platform=platform)
    lexical=Path(os.path.abspath(root));actual=lexical.resolve()
    if not SCOPE_NAME.fullmatch(lexical.name) or lexical!=actual or not inside(actual,temp) or actual==temp:
        raise GuardError('OWNED_SCOPE_INVALID')
    try:
        if actual.is_symlink() or actual.stat().st_file_attributes&0x400:raise GuardError('OWNED_SCOPE_LINK')
    except AttributeError:pass  # POSIX pure guard tests have no Windows attribute field.
    marker=actual/'scope.json'
    if marker.is_symlink():raise GuardError('OWNED_MARKER_LINK')
    try:record=json.loads(marker.read_text(encoding='utf8'))
    except (OSError,ValueError):raise GuardError('OWNED_MARKER_UNAVAILABLE') from None
    if (not isinstance(record,dict) or set(record)!={'schema','nonce','run_id','attempt','parent_pid'} or
        type(record['schema'])is not int or record['schema']!=1 or not isinstance(nonce,str) or not re.fullmatch('[0-9a-f]{64}',nonce) or record['nonce']!=nonce or
        record['run_id']!=env['GITHUB_RUN_ID'] or record['attempt']!=env['GITHUB_RUN_ATTEMPT'] or
        type(record['parent_pid'])is not int or record['parent_pid']<=0):raise GuardError('OWNED_MARKER_MISMATCH')
    if role not in ('controller','child'):raise GuardError('OWNED_ROLE_INVALID')
    identity=os.getpid() if role=='controller' else os.getppid()
    if record['parent_pid']!=identity:raise GuardError('OWNED_PARENT_MISMATCH')
    return actual,workspace,record

def checked_observer_owner(scope,nonce,record,expected_owner):
    """Normal provider is controller; one fixture delegation requires two receipts."""
    if type(expected_owner)is not int or expected_owner<=0:raise GuardError('INVALID_EXPECTED_OWNER')
    if expected_owner==record['parent_pid']:return expected_owner
    values=[]
    for name in ('observer-owner.json','blocked-provider-ready.json'):
        path=scope/name
        if path.is_symlink():raise GuardError('DELEGATION_LINK')
        try:
            if path.stat().st_size>4096:raise GuardError('DELEGATION_LIMIT')
            values.append(json.loads(path.read_text(encoding='utf8')))
        except (OSError,ValueError):raise GuardError('DELEGATION_UNAVAILABLE') from None
    capability,ready=values
    if (not isinstance(capability,dict) or set(capability)!={'schema','nonce','controller_pid','fixture_pid','purpose'} or
        capability!=dict(schema=1,nonce=nonce,controller_pid=record['parent_pid'],fixture_pid=expected_owner,purpose='blocked_fixture') or
        ready!=dict(nonce=nonce,controller_pid=record['parent_pid'],pid=expected_owner)):
        raise GuardError('DELEGATION_MISMATCH')
    return expected_owner

def checked_source(value,workspace):
    source=Path(value).resolve()
    if not inside(source,workspace) or not (source/'lan_panel.py').is_file():raise GuardError('SOURCE_OUTSIDE_CHECKOUT')
    return source

def normal_path(value):
    if not isinstance(value,str) or not value or '\0' in value or len(value)>32767:raise ValueError('INVALID_PATH_REFERENCE')
    return os.path.normcase(os.path.normpath(value))  # Lexical only; no filesystem resolution.

def path_fingerprint(paths):
    if not isinstance(paths,(list,tuple)):raise ValueError('INVALID_PATH_LIST')
    normalized=[normal_path(path) for path in paths]
    return hashlib.sha256(json.dumps(normalized,ensure_ascii=False,separators=(',',':')).encode('utf8')).hexdigest()

def text_fingerprint(text):return hashlib.sha256(text.encode('utf8')).hexdigest()

def classify_fastpath(legacy_routable,legacy_paths,metadata,native,declared_candidate):
    if (legacy_routable and metadata.get('hdrop_available') is True and native.get('hdrop_available') is True and
        isinstance(native.get('paths'),list) and native['paths'] and
        path_fingerprint(native['paths'])==path_fingerprint(legacy_paths) and
        type(metadata.get('owner_pid'))is int and metadata['owner_pid']>0 and
        metadata['owner_pid']==metadata.get('owner_pid_after')==native.get('owner_pid')==native.get('owner_pid_after')):
        return 'CF_HDROP'
    return 'unresolved' if declared_candidate or legacy_routable else 'not_file_route'

def parse_hdrop(raw,*,ansi_codec='cp1252'):
    """Parse copied DROPFILES outside the clipboard lock, preserving every ref."""
    if not isinstance(raw,bytes) or not 20<=len(raw)<=MAX_FRAME:raise ValueError('INVALID_DROPFILES_SIZE')
    offset,x,y,nonclient,wide=struct.unpack_from('<IiiII',raw)
    if not 20<=offset<len(raw) or wide not in (0,1):raise ValueError('INVALID_DROPFILES_HEADER')
    data=raw[offset:]
    if wide:
        if offset%2 or len(data)%2:raise ValueError('INVALID_WIDE_ALIGNMENT')
        end=next((i for i in range(0,len(data)-3,2) if data[i:i+4]==b'\0\0\0\0'),None)
        if end is None:raise ValueError('MISSING_LIST_TERMINATOR')
        text=data[:end].decode('utf-16-le','strict')
    else:
        end=data.find(b'\0\0')
        if end<0:raise ValueError('MISSING_LIST_TERMINATOR')
        text=data[:end].decode(ansi_codec,'strict')
    paths=text.split('\0') if text else []
    for path in paths:normal_path(path)
    return paths

def encode_frame(value):
    if not isinstance(value,dict):raise ValueError('INVALID_RESULT')
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf8')
    if len(raw)>MAX_FRAME:raise ValueError('QA_OUTPUT_LIMIT')
    return MAGIC+struct.pack('<I',len(raw))+raw

def decode_frame(raw):
    if not isinstance(raw,bytes) or not raw.startswith(MAGIC) or len(raw)<len(MAGIC)+4:raise ValueError('BAD_FRAME_MAGIC')
    size=struct.unpack_from('<I',raw,len(MAGIC))[0];offset=len(MAGIC)+4
    if size>MAX_FRAME or len(raw)!=offset+size:raise ValueError('BAD_FRAME_LENGTH')
    result=json.loads(raw[offset:].decode('utf8','strict'))
    if not isinstance(result,dict) or result.get('schema')!=1 or result.get('purpose')!='explicit-paste-format-probe':raise ValueError('BAD_FRAME_PURPOSE')
    if result.get('status') not in ('ok','unavailable','error','ready'):raise ValueError('BAD_FRAME_STATUS')
    return result

def result(status='ok',**fields):return dict(schema=1,purpose='explicit-paste-format-probe',status=status,**fields)

class ChildOperation:
    """Worker owns pipe/wait/close. Deadline may only kill this owned child."""
    def __init__(self,command,*,process_factory,deadline=3.):
        self.command=list(command);self.factory=process_factory;self.deadline=deadline
        self.lock=threading.Lock();self.done=threading.Event();self.process=None;self.cancel_reason=None;self.cancel_error=None
        self.receipt=None;self.events=[];self.started_monotonic=None;self._kill_attempted=False
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.timer=threading.Timer(deadline,self.cancel,args=('DEADLINE',));self.timer.daemon=True
    def start(self):self.started_monotonic=time.monotonic();self.timer.start();self.thread.start();return self
    def _kill_once(self,process):
        with self.lock:
            if self._kill_attempted:return
            self._kill_attempted=True
        try:process.kill()
        except Exception as cause:self.cancel_error=type(cause).__name__
    def cancel(self,reason='CANCELLED'):
        with self.lock:
            if self.done.is_set():return
            self.cancel_reason=self.cancel_reason or reason;process=self.process
        if process is not None:self._kill_once(process)
    def _run(self):
        process=None;raw=bytearray();error=None;reaped=False;code=None;value=None;started=time.perf_counter();closed=False
        try:
            with self.lock:cancelled=self.cancel_reason is not None
            if cancelled:return
            process=self.factory(self.command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            with self.lock:self.process=process;cancelled=self.cancel_reason is not None
            if cancelled:self._kill_once(process)
            while True:
                block=process.stdout.read(65536)
                if not block:break
                raw.extend(block)
                if len(raw)>MAX_FRAME+len(MAGIC)+4:self.cancel('QA_OUTPUT_LIMIT');raw.clear()
            code=process.wait(timeout=3.);reaped=True
            with self.lock:reason=self.cancel_reason
            if not reason:
                if code!=0:error='CHILD_EXIT'
                if raw:value=decode_frame(bytes(raw[len(GETDATA_EVENT):] if raw.startswith(GETDATA_EVENT) else raw))
                elif code==0:error='EMPTY_FRAME'
        except Exception as cause:
            error=type(cause).__name__
            if process is None:process=getattr(cause,'_helper_owner',None)
        finally:
            cleanup=[]
            if process is not None:
                if not reaped:
                    try:self._kill_once(process);code=process.wait(timeout=3.);reaped=True
                    except Exception as cause:cleanup.append(type(cause).__name__)
                if process.stdout is not None:
                    try:process.stdout.close()
                    except Exception as cause:cleanup.append(type(cause).__name__)
                closer=getattr(process,'close',None)
                if closer is not None:
                    try:closer();closed=True
                    except Exception as cause:cleanup.append(type(cause).__name__)
                else:closed=True
            else:closed=True;reaped=True
            with self.lock:reason=self.cancel_reason
            self.events=['GETDATA_ENTER'] if raw.startswith(GETDATA_EVENT) else []
            if self.cancel_error:cleanup.append(self.cancel_error)
            self.receipt=dict(value=value,error=reason or error,reaped=reaped,closed=closed,cleanup_errors=cleanup,events=self.events,
                              child_exit=code,elapsed_seconds=time.perf_counter()-started,
                              event_evidence='postmortem_prefix_before_API_only',deadline_scope='operation_start_including_spawn_import')
            self.timer.cancel();self.done.set()

def wait_operation(operation,pump,*,cleanup_budget=7.,clock=time.monotonic,idle=None):
    """Controller budget never fabricates a worker terminal or a reap result."""
    if operation.started_monotonic is None:raise RuntimeError('OPERATION_NOT_STARTED')
    expires=operation.started_monotonic+operation.deadline+cleanup_budget
    while not operation.done.is_set():
        if clock()>=expires:
            if operation.done.is_set():break
            with operation.lock:reason=operation.cancel_reason;kill_error=operation.cancel_error;pid=getattr(operation.process,'pid',None)
            return dict(value=None,error='CONTROLLER_TERMINAL_BUDGET',operation_done=False,terminal_observed=False,
                        reaped=None,closed=None,quarantined=True,retained_owner=True,
                        cleanup_errors=[kill_error] if kill_error else [],events=[],child_exit=None,
                        cancel_reason=reason,owned_pid=pid,elapsed_seconds=clock()-operation.started_monotonic,
                        event_evidence='not_observed_while_pipe_blocked')
        pump()
        if idle is None:operation.done.wait(.002)
        else:idle()
    receipt=dict(operation.receipt);verified=bool(receipt['reaped'] and receipt['closed'] and not receipt['cleanup_errors'])
    receipt.update(operation_done=True,terminal_observed=True,resource_cleanup_verified=verified,
                   quarantined=not verified,retained_owner=not verified)
    return receipt
