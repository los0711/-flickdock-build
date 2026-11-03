"""Standalone hosted-only comparison; never starts FlickDock or sends input."""
from __future__ import annotations
import argparse,hashlib,json,os,runpy,secrets,sys,tempfile,time
from pathlib import Path
from lookup_contract import (BoundaryError,CLEANUP_SECONDS,Lines,PREFIX,REQUEST_SECONDS,STARTUP_SECONDS,
    Spawn,STAGES,context,hosted,no_links,read_object,retire,retirement_ok,safe_type,write_object)

HERE=Path(__file__).resolve().parent
WAIT_OBJECT_0=0;WAIT_TIMEOUT=258

class Kernel:
    def __init__(self,root,nonce,role):
        self.root,self.workspace,self.record=context(root,nonce,role)
        import ctypes
        from ctypes import wintypes as w
        self.c=ctypes;self.w=w;self.k=ctypes.WinDLL('kernel32',use_last_error=True);self.user=None;self.authorized=None
        signatures={'GetCurrentProcess':([],w.HANDLE),'GetProcessId':([w.HANDLE],w.DWORD),
            'WaitForSingleObject':([w.HANDLE,w.DWORD],w.DWORD),
            'DuplicateHandle':([w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD],w.BOOL),
            'CloseHandle':([w.HANDLE],w.BOOL),'TerminateProcess':([w.HANDLE,w.UINT],w.BOOL)}
        for name,(args,result) in signatures.items():fn=getattr(self.k,name);fn.argtypes=args;fn.restype=result
    def process_pid(self,handle):
        value=int(self.k.GetProcessId(handle))
        if not value:raise BoundaryError('KERNEL_PROCESS_ID_FAILED')
        return value
    def alive(self,handle):
        result=self.k.WaitForSingleObject(handle,0)
        if result not in (WAIT_OBJECT_0,WAIT_TIMEOUT):raise BoundaryError('KERNEL_WAIT_FAILED')
        return result==WAIT_TIMEOUT
    def owned(self,process,*,live=True):
        handle=getattr(process,'_process',None)
        if not handle or self.process_pid(handle)!=process.pid:raise BoundaryError('OWNED_KERNEL_IDENTITY_FAILED')
        if live and not self.alive(handle):raise BoundaryError('OWNED_PROCESS_EXITED')
        return handle
    def duplicate_producer(self,producer,worker):
        source=self.owned(producer);target=self.owned(worker);remote=self.w.HANDLE()
        # Only query/synchronize access crosses into the owned worker. This is
        # the exact kernel object, never OpenProcess on a reusable PID.
        if not self.k.DuplicateHandle(self.k.GetCurrentProcess(),source,target,self.c.byref(remote),0x00101000,False,0):
            raise BoundaryError('OWNED_DUPLICATE_FAILED')
        return int(remote.value)
    def accept_producer(self,handle,pid):
        if type(handle)is not int or handle<=0 or type(pid)is not int or pid<=0:raise BoundaryError('PRODUCER_HANDLE_SCHEMA')
        if self.process_pid(handle)!=pid or not self.alive(handle):raise BoundaryError('PRODUCER_KERNEL_MISMATCH')
        self.authorized=handle
    def producer_alive(self,pid):
        if self.authorized is None or self.process_pid(self.authorized)!=pid or not self.alive(self.authorized):raise BoundaryError('PRODUCER_NOT_ALIVE')
    def close_authorized(self):
        handle,self.authorized=self.authorized,None
        if handle is not None and not self.k.CloseHandle(handle):raise BoundaryError('PRODUCER_HANDLE_CLOSE_FAILED')
    def terminate_owned(self,process):
        handle=self.owned(process,live=False)
        if self.alive(handle) and not self.k.TerminateProcess(handle,1):raise BoundaryError('OWNED_TERMINATE_FAILED')
    def _user(self):
        if self.user is None:
            self.user=self.c.WinDLL('user32',use_last_error=True)
            self.callback=self.c.WINFUNCTYPE(self.w.BOOL,self.w.HWND,self.w.LPARAM)
            self.user.EnumWindows.argtypes=[self.callback,self.w.LPARAM];self.user.EnumWindows.restype=self.w.BOOL
            self.user.GetWindowThreadProcessId.argtypes=[self.w.HWND,self.c.POINTER(self.w.DWORD)];self.user.GetWindowThreadProcessId.restype=self.w.DWORD
        return self.user
    def window_pid(self,hwnd):
        pid=self.w.DWORD();api=self._user()
        if not api.GetWindowThreadProcessId(hwnd,self.c.byref(pid)) or not pid.value:raise BoundaryError('WINDOW_PID_UNAVAILABLE')
        return int(pid.value)
    def owned_handles(self,pid):
        self.producer_alive(pid);handles=[];failure=[];api=self._user()
        @self.callback
        def collect(hwnd,_):
            try:
                if self.window_pid(hwnd)==pid:handles.append(int(hwnd))
                return True
            except Exception as error:failure.append(error);return False
        ok=api.EnumWindows(collect,0)
        if failure:raise failure[0]
        if not ok:raise BoundaryError('ENUM_WINDOWS_FAILED')
        self.producer_alive(pid);return handles

def query(desktop,kernel,pid,strategy,stage):
    kernel.producer_alive(pid)
    if strategy=='desktop':
        stage('desktop_query_begin');candidates=desktop.windows(process=pid);stage('desktop_query_complete')
        visible=[]
        for window in candidates:
            stage('visibility_begin');found=window.is_visible();stage('visibility_complete')
            if found:visible.append(window)
        kernel.producer_alive(pid)
        return dict(visible_count=len(visible))
    if strategy!='owned_handles':raise BoundaryError('STRATEGY_NOT_ALLOWED')
    stage('enum_owned_begin');handles=kernel.owned_handles(pid);stage('enum_owned_complete');count=0
    for handle in handles:
        kernel.producer_alive(pid)
        if kernel.window_pid(handle)!=pid:raise BoundaryError('OWNED_HWND_CHANGED')
        stage('bind_owned_begin');window=desktop.window(handle=handle).wrapper_object();stage('bind_owned_complete')
        if kernel.window_pid(handle)!=pid:raise BoundaryError('OWNED_HWND_CHANGED')
        stage('visibility_begin');found=window.is_visible();stage('visibility_complete')
        if kernel.window_pid(handle)!=pid:raise BoundaryError('OWNED_HWND_CHANGED')
        if found:count+=1
    kernel.producer_alive(pid);return dict(visible_count=count,owned_count=len(handles))

def wait_object(path,root,seconds):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if path.exists():return read_object(path,root)
        time.sleep(.005)
    raise BoundaryError('OWNED_HANDOFF_TIMEOUT')

def emit(nonce,event,**fields):print(json.dumps(dict(protocol=1,nonce=nonce,event=event,**fields)),flush=True)

def producer(args):
    root,workspace,record=context(args.scope,args.nonce,'producer')
    source=workspace/'qa/packaged-live/native_editor.py';no_links(source,workspace)
    if not source.is_file() or source.stat().st_size>65536:raise BoundaryError('EDITOR_SOURCE_INVALID')
    # No source patch or callbacks. The owned empty command folder contains
    # no Copy/inspect/drag/text/quit command; this executes the exact fixture.
    sys.argv=[str(source),str(root/'editor')]
    runpy.run_path(str(source),run_name='__main__')

def worker(args):
    root,workspace,record=context(args.scope,args.nonce,'worker')
    if args.strategy not in ('desktop','owned_handles'):raise BoundaryError('STRATEGY_NOT_ALLOWED')
    if not args.worker_nonce or len(args.worker_nonce)!=64 or any(c not in '0123456789abcdef' for c in args.worker_nonce):raise BoundaryError('WORKER_NONCE_INVALID')
    stage=lambda name:emit(args.worker_nonce,'stage',stage=name)
    stage('worker_boot');kernel=Kernel(root,args.nonce,'worker');value=None;error=None
    try:
        authorization=wait_object(root/('authorization-'+args.worker_nonce+'.json'),root,STARTUP_SECONDS)
        if (set(authorization)!={'schema','nonce','worker_pid','producer_pid','producer_handle','strategy'}
            or type(authorization['schema'])is not int or authorization['schema']!=1
            or authorization['nonce']!=args.worker_nonce or authorization['worker_pid']!=os.getpid()
            or authorization['strategy']!=args.strategy):raise BoundaryError('WORKER_AUTHORIZATION_MISMATCH')
        pid=authorization['producer_pid'];kernel.accept_producer(authorization['producer_handle'],pid);stage('authorization_ready')
        stage('backend_import_begin');from pywinauto import Desktop
        stage('backend_import_complete');desktop=Desktop(backend='uia');stage('backend_ready')
        emit(args.worker_nonce,'ready',pid=os.getpid())
        request=wait_object(root/('request-'+args.worker_nonce+'.json'),root,STARTUP_SECONDS)
        if type(request.get('schema'))is not int or request!={'schema':1,'nonce':args.worker_nonce,'strategy':args.strategy}:raise BoundaryError('REQUEST_CONTEXT_MISMATCH')
        context(root,args.nonce,'worker');stage('request_begin')
        value=query(desktop,kernel,pid,args.strategy,stage);stage('request_complete')
    except Exception as cause:error=safe_type(cause)
    finally:
        try:kernel.close_authorized()
        except Exception as cause:error=safe_type(cause)
    emit(args.worker_nonce,'result',status='error' if error else 'ok',**(dict(error_type=error) if error else value))
    return 1 if error else 0

def run_attempt(strategy,root,nonce,producer_process,kernel,factory,spawners):
    worker_nonce=secrets.token_hex(32);stages=[];process=None;lines=None;retirement=None
    startup_started=time.monotonic();request_started=None
    row=dict(strategy=strategy,request_budget_seconds=REQUEST_SECONDS,startup_budget_seconds=STARTUP_SECONDS,
             pass_=False,status='not_started',stages=stages)
    command=[sys.executable,'-X','utf8',str(HERE/'lookup_probe.py'),'--role','worker','--scope',str(root),'--nonce',nonce,
             '--worker-nonce',worker_nonce,'--strategy',strategy]
    try:
        creation=Spawn(factory,command,kernel);spawners.append(creation)
        process,error=creation.take(max(0.,startup_started+STARTUP_SECONDS-time.monotonic()))
        if error:raise BoundaryError('WORKER_PROTECTED_START_FAILED')
        if process is None:raise BoundaryError('WORKER_NOT_OWNED')
        kernel.owned(process);row['worker_pid']=process.pid
        remote=kernel.duplicate_producer(producer_process,process)
        write_object(root/('authorization-'+worker_nonce+'.json'),dict(schema=1,nonce=worker_nonce,worker_pid=process.pid,
            producer_pid=producer_process.pid,producer_handle=remote,strategy=strategy))
        lines=Lines(process,worker_nonce)
        lines.wait('ready',process,max(0.,startup_started+STARTUP_SECONDS-time.monotonic()),stages)
        row['ready_seconds']=time.monotonic()-startup_started
        request_started=time.monotonic()
        write_object(root/('request-'+worker_nonce+'.json'),dict(schema=1,nonce=worker_nonce,strategy=strategy))
        result=lines.wait('result',process,REQUEST_SECONDS,stages);row['request_seconds']=time.monotonic()-request_started
        row['result']=result
        code=process.wait(CLEANUP_SECONDS)
        row['status']='returned';row['pass_']=bool(code==0 and result['status']=='ok' and result['visible_count']>0)
    except Exception as cause:
        row['status']='failed';row['error_type']=safe_type(cause)
        if isinstance(cause,BoundaryError):row['code']=str(cause)
        if request_started is not None:row['request_seconds']=time.monotonic()-request_started
        else:row['startup_elapsed_seconds']=time.monotonic()-startup_started
    finally:
        retired_at=time.monotonic()
        if process is not None:retirement=retire(process,kernel,lines.thread if lines is not None else None)
        row['retire_seconds']=time.monotonic()-retired_at
        if lines is not None and lines.failure:row['pipe_error_type']=lines.failure
        row['retirement']=retirement
        if not retirement_ok(retirement):row['pass_']=False
    return row

def run(args):
    temp,workspace=hosted();nonce=secrets.token_hex(32);root=temp/(PREFIX+nonce[:32]);root.mkdir()
    write_object(root/'owner.json',dict(schema=1,nonce=nonce,run=os.environ['GITHUB_RUN_ID'],attempt=os.environ['GITHUB_RUN_ATTEMPT'],controller_pid=os.getpid()))
    context(root,nonce,'controller');output=Path(args.output).absolute()
    if '..' in output.parts or not output.is_relative_to(workspace/'build') or output==workspace/'build':raise BoundaryError('OUTPUT_OUTSIDE_BUILD')
    no_links(output,workspace);output.mkdir(parents=True,exist_ok=False)
    editor=root/'editor';editor.mkdir()
    for name in ('APPDATA','LOCALAPPDATA','XDG_CONFIG_HOME'):
        folder=root/name.lower();folder.mkdir();os.environ[name]=str(folder)
    temporary=root/'temp';temporary.mkdir()
    for name in ('TEMP','TMP','TMPDIR'):os.environ[name]=str(temporary)
    tempfile.tempdir=str(temporary)
    os.environ['QT_QPA_PLATFORM']='windows'
    sys.path.insert(0,str(workspace));from win_owned_process import spawn_owned
    kernel=Kernel(root,nonce,'controller');process=None;spawners=[];rows=[];cleanup=None
    report=dict(schema=1,purpose='owned producer UIA lookup diagnostic only',original_pass=False,fallback_pass=False,
        normal_acceptance=False,product_app_started=False,clipboard_commands_sent=False,os_input_sent=False,
        rows=rows,controller_pid=os.getpid())
    sources=[workspace/'qa/packaged-live/native_editor.py',workspace/'win_owned_process.py',workspace/'win_clipboard_native.py']
    before={str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    try:
        startup_started=time.monotonic()
        creation=Spawn(spawn_owned,[sys.executable,'-X','utf8',str(HERE/'lookup_probe.py'),'--role','producer','--scope',str(root),'--nonce',nonce],kernel)
        spawners.append(creation);process,error=creation.take(max(0.,startup_started+STARTUP_SECONDS-time.monotonic()))
        if error or process is None:raise BoundaryError('PRODUCER_PROTECTED_START_FAILED')
        kernel.owned(process);report['producer_pid']=process.pid
        # The exact existing fixture acknowledges unknown actions without
        # changing text, reading clipboard or sending input. This proves its
        # own QTimer/event loop is running before either UIA lookup starts.
        write_object(editor/'editor-command.json',dict(id=nonce,action='owned_lookup_ready_noop'))
        ack=wait_object(editor/'editor-ack.json',root,max(0.,startup_started+STARTUP_SECONDS-time.monotonic()))
        if ack!={'id':nonce}:raise BoundaryError('PRODUCER_READY_MISMATCH')
        kernel.owned(process);report['producer_ready_ack']=True
        original=run_attempt('desktop',root,nonce,process,kernel,spawn_owned,spawners);rows.append(original)
        report['original_pass']=original['pass_']
        terminal=original['retirement']
        if not retirement_ok(terminal):
            raise BoundaryError('ORIGINAL_WORKER_NOT_RETIRED_NO_FALLBACK')
        kernel.owned(process)
        fallback=run_attempt('owned_handles',root,nonce,process,kernel,spawn_owned,spawners);rows.append(fallback)
        report['fallback_pass']=fallback['pass_']
    except Exception as cause:
        report['error_type']=safe_type(cause)
        if isinstance(cause,BoundaryError):report['code']=str(cause)
    finally:
        if process is not None:cleanup=retire(process,kernel)
        report['producer_retirement']=cleanup
        report['spawns']=[dict(creation_terminal=s.done.is_set(),handoff=s.process is not None,
            late_retirement=s.cleanup,creator_still_running=s.thread.is_alive()) for s in spawners]
        after={str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        report['source_before']=before;report['source_after']=after;report['source_stable']=before==after
        report['diagnostic_completed']=len(rows)==2 and all(retirement_ok(r.get('retirement')) for r in rows)
        report['ok']=bool(report['original_pass'] and report['fallback_pass'] and before==after and cleanup
            and cleanup['reaped'] and cleanup['closed'] and not cleanup['errors']
            and all(s.done.is_set() and not s.thread.is_alive() for s in spawners))
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return 0 if report['ok'] else 1

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--role',choices=('controller','producer','worker'),default='controller')
    parser.add_argument('--scope');parser.add_argument('--nonce');parser.add_argument('--worker-nonce');parser.add_argument('--strategy');parser.add_argument('--output')
    args=parser.parse_args(argv)
    if args.role=='controller':return run(args)
    if args.role=='producer':producer(args);return 0
    return worker(args)

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print(json.dumps(dict(status='failed',error_type=safe_type(error))),file=sys.stderr);raise SystemExit(2)
