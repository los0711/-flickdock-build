"""Pure protocol, guard and owned-process lifetime; no Qt or native imports."""
from __future__ import annotations
import json,os,queue,re,stat,subprocess,sys,threading,time
from pathlib import Path

JOB='owned-producer-lookup-probe'
REPOSITORY='los0711/-flickdock-build'
PREFIX='fd-owned-uia-'
STARTUP_SECONDS=12
REQUEST_SECONDS=12
CLEANUP_SECONDS=3
LINE_LIMIT=4096
HEX=re.compile(r'[0-9a-f]{64}\Z')
STAGES=frozenset({'worker_boot','authorization_ready','backend_import_begin','backend_import_complete',
    'backend_ready','request_begin','desktop_query_begin','desktop_query_complete','visibility_begin','visibility_complete',
    'enum_owned_begin','enum_owned_complete','bind_owned_begin','bind_owned_complete','request_complete'})

class BoundaryError(RuntimeError):pass

def hosted(*,env=None,platform=None):
    env=os.environ if env is None else env;platform=sys.platform if platform is None else platform
    required={'GITHUB_ACTIONS':'true','CI':'true','RUNNER_ENVIRONMENT':'github-hosted','RUNNER_OS':'Windows',
        'GITHUB_REPOSITORY':REPOSITORY,'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_JOB':JOB,
        'FD_QA_NATIVE':'1','FD_OWNED_PRODUCER_LOOKUP':'1'}
    if platform!='win32' or any(env.get(k)!=v for k,v in required.items()):raise BoundaryError('HOSTED_ONLY')
    if any(not env.get(k) for k in ('RUNNER_TEMP','GITHUB_WORKSPACE','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT')):raise BoundaryError('HOST_CONTEXT_MISSING')
    return Path(env['RUNNER_TEMP']).resolve(strict=True),Path(env['GITHUB_WORKSPACE']).resolve(strict=True)

def no_links(path,base):
    if not path.is_relative_to(base):raise BoundaryError('OWNED_PATH_REQUIRED')
    for current in (path,*path.parents):
        if current.is_symlink() or current.is_junction():raise BoundaryError('OWNED_REPARSE_REJECTED')
        if current==base:break

def read_object(path,base):
    no_links(path,base);info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>LINE_LIMIT:raise BoundaryError('OWNED_JSON_SIZE')
    value=json.loads(path.read_text(encoding='utf8'))
    if not isinstance(value,dict):raise BoundaryError('OWNED_JSON_OBJECT')
    return value

def write_object(path,value):
    temporary=path.with_suffix('.partial')
    with temporary.open('x',encoding='utf8') as stream:json.dump(value,stream)
    temporary.replace(path)

def context(root,nonce,role,*,env=None,platform=None,pid=None,parent_pid=None):
    env=os.environ if env is None else env;temp,workspace=hosted(env=env,platform=platform)
    if not isinstance(nonce,str) or not HEX.fullmatch(nonce) or role not in ('controller','producer','worker'):raise BoundaryError('ROLE_OR_NONCE')
    root=Path(root).absolute()
    if root.parent!=temp or root.name!=PREFIX+nonce[:32]:raise BoundaryError('OWNED_ROOT_REQUIRED')
    no_links(root,temp);record=read_object(root/'owner.json',temp)
    if (set(record)!={'schema','nonce','run','attempt','controller_pid'} or type(record['schema'])is not int or record['schema']!=1
        or record['nonce']!=nonce or record['run']!=env['GITHUB_RUN_ID'] or record['attempt']!=env['GITHUB_RUN_ATTEMPT']
        or type(record['controller_pid'])is not int or record['controller_pid']<=0):raise BoundaryError('OWNER_CONTEXT_MISMATCH')
    actual=os.getpid() if role=='controller' else os.getppid()
    if role=='controller' and pid is not None:actual=pid
    elif role!='controller' and parent_pid is not None:actual=parent_pid
    if record['controller_pid']!=actual:raise BoundaryError('ACTUAL_PARENT_MISMATCH')
    return root,workspace,record

def safe_type(error):
    name=type(error).__name__
    return name if name in ('BoundaryError','OSError','PermissionError','TimeoutError','RuntimeError','ValueError','COMError') else 'Exception'

def retirement_ok(value):return bool(value and value['reaped'] and value['closed'] and not value['errors'])

def decode_packet(line,nonce):
    if not isinstance(line,bytes) or len(line)>LINE_LIMIT or not line.endswith(b'\n'):raise BoundaryError('PIPE_LINE_INVALID')
    value=json.loads(line.decode('utf8'))
    if (not isinstance(value,dict) or type(value.get('protocol'))is not int or value['protocol']!=1
        or value.get('nonce')!=nonce):raise BoundaryError('PIPE_AUTH_INVALID')
    kind=value.get('event')
    if kind=='stage':
        if set(value)!={'protocol','nonce','event','stage'} or type(value.get('stage'))is not str or value['stage'] not in STAGES:raise BoundaryError('PIPE_STAGE_INVALID')
        return dict(event='stage',stage=value['stage'])
    if kind=='ready':
        if set(value)!={'protocol','nonce','event','pid'} or type(value.get('pid'))is not int or value['pid']<=0:raise BoundaryError('PIPE_READY_INVALID')
        return dict(event='ready',pid=value['pid'])
    if kind=='result':
        allowed={'protocol','nonce','event','status','visible_count','owned_count','error_type'}
        if set(value)-allowed or value.get('status') not in ('ok','error'):raise BoundaryError('PIPE_RESULT_INVALID')
        if value['status']=='ok':
            if set(value)-{'protocol','nonce','event','status','visible_count','owned_count'}:raise BoundaryError('PIPE_RESULT_INVALID')
            if type(value.get('visible_count'))is not int or not 0<=value['visible_count']<=4096:raise BoundaryError('PIPE_COUNT_INVALID')
            if 'owned_count' in value and (type(value['owned_count'])is not int or not 0<=value['owned_count']<=4096):raise BoundaryError('PIPE_COUNT_INVALID')
        elif (set(value)!={'protocol','nonce','event','status','error_type'}
                or value.get('error_type') not in ('BoundaryError','OSError','PermissionError','TimeoutError','RuntimeError','ValueError','COMError','Exception')):raise BoundaryError('PIPE_ERROR_INVALID')
        return {k:v for k,v in value.items() if k not in ('protocol','nonce')}
    raise BoundaryError('PIPE_EVENT_INVALID')

def retire(process,kernel,reader=None):
    """Only an object returned by our protected factory is accepted here."""
    errors=[];reaped=False;closed=False;code=None
    try:
        try:code=process.wait(0);reaped=type(code)is int
        except subprocess.TimeoutExpired:pass
        except Exception as error:errors.append('initial_wait:'+safe_type(error))
        if not reaped:
            try:process.kill()
            except Exception as error:errors.append('job_kill:'+safe_type(error))
            try:code=process.wait(CLEANUP_SECONDS);reaped=type(code)is int
            except Exception as error:errors.append('job_wait:'+safe_type(error))
        if not reaped:
            # Never OpenProcess by PID: kernel validates this exact retained
            # process handle before the one owned fallback termination.
            try:kernel.terminate_owned(process)
            except Exception as error:errors.append('handle_terminate:'+safe_type(error))
            try:code=process.wait(CLEANUP_SECONDS);reaped=type(code)is int
            except Exception as error:errors.append('handle_wait:'+safe_type(error))
        if reaped:
            if reader is not None:
                reader.join(1)
                if reader.is_alive():errors.append('reader_not_terminal');return dict(reaped=True,closed=False,exit_code=code,errors=errors)
            stream=getattr(process,'stdout',None)
            if stream is not None:
                try:stream.close()
                except Exception as error:errors.append('pipe_close:'+safe_type(error))
            try:process.close();closed=True
            except Exception as error:errors.append('owner_close:'+safe_type(error))
    finally:
        if not reaped:errors.append('owner_retained_unreaped')
    return dict(reaped=reaped,closed=closed,exit_code=code,errors=errors)

class Spawn:
    """Transfer ownership once; timeout never waits forever for CreateProcess."""
    def __init__(self,factory,command,kernel):
        self.lock=threading.Lock();self.done=threading.Event();self.cancelled=False;self.process=None;self.error=None;self.cleanup=None;self.retained=None
        def launch():
            process=None
            try:process=factory(command)
            except Exception as error:self.error=safe_type(error);process=getattr(error,'_helper_owner',None)
            with self.lock:
                late=self.cancelled
                if not late:self.process=process;self.done.set()
            if late:
                self.retained=process
                try:
                    if process is not None:self.cleanup=retire(process,kernel)
                finally:self.done.set()
        self.thread=threading.Thread(target=launch,daemon=True);self.thread.start()
    def take(self,seconds=STARTUP_SECONDS):
        if not self.done.wait(seconds):
            with self.lock:
                if self.done.is_set():return self.process,self.error
                self.cancelled=True
            # A late protected owner is retired by its creator. Keep this
            # Spawn object alive if creation has not returned; never fake reap.
            self.done.wait(CLEANUP_SECONDS)
            raise BoundaryError('SPAWN_NOT_READY')
        return self.process,self.error

class Lines:
    def __init__(self,process,nonce):
        self.queue=queue.Queue();self.failure=None
        def read():
            try:
                while True:
                    line=process.stdout.readline(LINE_LIMIT+1)
                    if not line:break
                    self.queue.put(decode_packet(line,nonce))
            except Exception as error:self.failure=safe_type(error)
            finally:self.queue.put(dict(event='eof'))
        self.thread=threading.Thread(target=read,daemon=True);self.thread.start()
    def wait(self,event,process,seconds,stages):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:value=self.queue.get(timeout=min(.05,max(.001,deadline-time.monotonic())))
            except queue.Empty:continue
            if value['event']=='stage':stages.append(dict(stage=value['stage'],received_at=time.monotonic()));continue
            if value['event']=='eof':raise BoundaryError('WORKER_PIPE_ENDED')
            if value['event']!=event:raise BoundaryError('WORKER_EVENT_ORDER')
            if event=='ready' and value['pid']!=process.pid:raise BoundaryError('WORKER_PID_MISMATCH')
            return value
        raise BoundaryError('STARTUP_TIMEOUT' if event=='ready' else 'REQUEST_TIMEOUT')
