"""Pure/fake only; no real process, WinDLL, Qt, clipboard or desktop calls."""
import ast,ctypes,io,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import lookup_contract as c
import lookup_probe as p

N='b'*64
def packet(event,**fields):return (json.dumps(dict(protocol=1,nonce=N,event=event,**fields))+'\n').encode()

class Guards(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='owned-lookup-pure-');self.base=Path(self.temp.name).resolve()
        self.root=self.base/(c.PREFIX+N[:32]);self.root.mkdir()
        self.env=dict(GITHUB_ACTIONS='true',CI='true',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Windows',
            GITHUB_REPOSITORY=c.REPOSITORY,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_JOB=c.JOB,
            FD_QA_NATIVE='1',FD_OWNED_PRODUCER_LOOKUP='1',RUNNER_TEMP=str(self.base),GITHUB_WORKSPACE=str(self.base),
            GITHUB_RUN_ID='1',GITHUB_RUN_ATTEMPT='2')
        self.record=dict(schema=1,nonce=N,run='1',attempt='2',controller_pid=123)
        c.write_object(self.root/'owner.json',self.record)
    def tearDown(self):self.temp.cleanup()
    def test_parent_and_controller_bind_actual_roles(self):
        self.assertEqual(c.context(self.root,N,'controller',env=self.env,platform='win32',pid=123)[0],self.root)
        self.assertEqual(c.context(self.root,N,'worker',env=self.env,platform='win32',parent_pid=123)[0],self.root)
        for role,extra in (('controller',dict(pid=124)),('worker',dict(parent_pid=124)),('producer',dict(parent_pid=124))):
            with self.subTest(role=role),self.assertRaisesRegex(c.BoundaryError,'ACTUAL_PARENT'):
                c.context(self.root,N,role,env=self.env,platform='win32',**extra)
    def test_each_host_flag_missing_is_rejected(self):
        for key in self.env:
            env=dict(self.env);env.pop(key)
            with self.subTest(key=key),self.assertRaises(c.BoundaryError):c.hosted(env=env,platform='win32')
    def test_other_job_repository_platform_or_manual_gate_rejected(self):
        for key in ('GITHUB_JOB','GITHUB_REPOSITORY','GITHUB_EVENT_NAME','RUNNER_ENVIRONMENT'):
            with self.subTest(key=key),self.assertRaises(c.BoundaryError):c.hosted(env=dict(self.env,**{key:'other'}),platform='win32')
        with self.assertRaises(c.BoundaryError):c.hosted(env=self.env,platform='darwin')
    def test_nonce_context_size_schema_rejected(self):
        for change in (dict(nonce='c'*64),dict(schema=True),dict(attempt='other'),dict(controller_pid=0)):
            (self.root/'owner.json').write_text(json.dumps(dict(self.record,**change)))
            with self.subTest(change=change),self.assertRaises(c.BoundaryError):c.context(self.root,N,'controller',env=self.env,platform='win32',pid=123)
        (self.root/'owner.json').write_text('x'*(c.LINE_LIMIT+1))
        with self.assertRaisesRegex(c.BoundaryError,'SIZE'):c.context(self.root,N,'controller',env=self.env,platform='win32',pid=123)
    def test_all_real_entry_roles_and_kernel_guard_before_dll_or_qt(self):
        with patch.dict(os.environ,{},clear=True),patch.object(ctypes,'WinDLL',side_effect=AssertionError('NO_DLL'),create=True),patch.object(p.runpy,'run_path',side_effect=AssertionError('NO_QT')):
            for argv in (['--output','unused'],['--role','worker','--scope','unused','--nonce',N],['--role','producer','--scope','unused','--nonce',N]):
                with self.assertRaises(c.BoundaryError):p.main(argv)
            with self.assertRaises(c.BoundaryError):p.Kernel('unused',N,'worker')
        self.assertFalse(any(name.startswith(('PySide6','pywinauto')) for name in sys.modules))
    def test_private_symlink_scope_never_accepted(self):
        if os.name=='nt':
            # Reparse API policy is checked in production; do not create a
            # native junction in this strictly pure/fake candidate suite.
            with patch.object(Path,'is_junction',return_value=True):
                with self.assertRaisesRegex(c.BoundaryError,'REPARSE'):c.no_links(self.root,self.base)
        else:
            alias=self.base/'alias';alias.symlink_to(self.root,target_is_directory=True)
            with self.assertRaises(c.BoundaryError):c.no_links(alias,self.base)

class Protocol(unittest.TestCase):
    def test_success_cannot_export_arbitrary_error_message(self):
        raw=packet('result',status='ok',visible_count=1,error_type='synthetic_private_diagnostic')
        with self.assertRaisesRegex(c.BoundaryError,'PIPE_RESULT_INVALID'):c.decode_packet(raw,N)
        self.assertEqual(c.decode_packet(packet('result',status='ok',visible_count=1,owned_count=2),N),
                         dict(event='result',status='ok',visible_count=1,owned_count=2))
    def test_fixed_packets_drop_nonce_without_exporting_private_fields(self):
        self.assertEqual(c.decode_packet(packet('stage',stage='visibility_begin'),N),dict(event='stage',stage='visibility_begin'))
        for change in (dict(event='stage',stage=[]),dict(event='stage',stage='visibility_begin',name='private name'),dict(event='ready',pid=True)):
            with self.assertRaises(c.BoundaryError):c.decode_packet(packet(**change),N)
        for value in (None,[],True):
            with self.assertRaises(c.BoundaryError):c.decode_packet((json.dumps(value)+'\n').encode(),N)
    def test_wrong_nonce_protocol_size_and_result_schema_rejected(self):
        for raw in (packet('ready',pid=12),b'x'*(c.LINE_LIMIT+1),b'{}'):
            with self.assertRaises(c.BoundaryError):c.decode_packet(raw,'c'*64)
        value=dict(protocol=True,nonce=N,event='ready',pid=12)
        with self.assertRaises(c.BoundaryError):c.decode_packet((json.dumps(value)+'\n').encode(),N)
        with self.assertRaises(c.BoundaryError):c.decode_packet(packet('result',status='ok',visible_count=True),N)
        with self.assertRaises(c.BoundaryError):c.decode_packet(packet('result',status='error',error_type='OSError',visible_count='private path'),N)
    def test_stage_lines_arrive_before_stream_eof(self):
        class Reader:
            def __init__(self):self.n=0;self.release=threading.Event()
            def readline(self,n):
                self.n+=1
                if self.n==1:return packet('stage',stage='backend_ready')
                if self.n==2:return packet('ready',pid=123)
                self.release.wait();return b''
        stream=Reader();process=SimpleNamespace(stdout=stream,pid=123);lines=c.Lines(process,N);stages=[]
        self.assertEqual(lines.wait('ready',process,.1,stages)['pid'],123);self.assertTrue(lines.thread.is_alive())
        self.assertEqual(stages[0]['stage'],'backend_ready');stream.release.set();lines.thread.join(1)
        self.assertFalse(lines.thread.is_alive())

class FakeProcess:
    def __init__(self,kill_error=False,raw=None):
        self.pid=123;self._process=1010;self.kills=0;self.closes=0;self.kill_error=kill_error;self.event=threading.Event();self.returncode=None
        self.stdout=io.BytesIO(raw or b'')
    def wait(self,timeout=0):
        if self.returncode is None:raise subprocess.TimeoutExpired('owned-fake',timeout)
        return self.returncode
    def kill(self):
        self.kills+=1
        if self.kill_error:raise OSError('private kill error')
        self.returncode=1;self.event.set()
    def close(self):self.closes+=1
class FakeKernel:
    def __init__(self,terminate_error=False):self.terminated=[];self.terminate_error=terminate_error
    def terminate_owned(self,process):
        self.terminated.append(process)
        if self.terminate_error:raise OSError('private terminate error')
        process.returncode=1;process.event.set()

class Ownership(unittest.TestCase):
    def test_normal_job_kill_is_reaped_and_closed(self):
        process=FakeProcess();kernel=FakeKernel();receipt=c.retire(process,kernel)
        self.assertTrue(c.retirement_ok(receipt));self.assertEqual(process.kills,1);self.assertEqual(kernel.terminated,[])
    def test_job_kill_failure_fallback_retires_exact_owner_but_preserves_error(self):
        process=FakeProcess(kill_error=True);kernel=FakeKernel();receipt=c.retire(process,kernel)
        self.assertTrue(receipt['reaped'] and receipt['closed']);self.assertFalse(c.retirement_ok(receipt))
        self.assertIs(kernel.terminated[0],process);self.assertIn('job_kill:OSError',receipt['errors'])
    def test_failed_termination_returns_unreaped_without_closing_blocked_pipe(self):
        process=FakeProcess(kill_error=True);process.stdout=SimpleNamespace(close=lambda:(_ for _ in ()).throw(AssertionError('MUST_NOT_CLOSE_BLOCKED_PIPE')))
        receipt=c.retire(process,FakeKernel(terminate_error=True))
        self.assertFalse(receipt['reaped'] or receipt['closed']);self.assertEqual(process.closes,0)
        self.assertIn('owner_retained_unreaped',receipt['errors']);self.assertFalse(c.retirement_ok(receipt))
    def test_partial_spawn_owner_is_transferred_and_can_be_retired(self):
        process=FakeProcess()
        def fail(command):
            error=OSError('private startup');error._helper_owner=process;raise error
        spawn=c.Spawn(fail,['fixed'],FakeKernel());owner,error=spawn.take(.1)
        self.assertIs(owner,process);self.assertEqual(error,'OSError');self.assertTrue(c.retirement_ok(c.retire(owner,FakeKernel())))
    def test_late_spawn_after_deadline_is_not_falsely_reaped_and_creator_retires(self):
        release=threading.Event();process=FakeProcess()
        def delayed(command):release.wait();return process
        spawn=c.Spawn(delayed,['fixed'],FakeKernel())
        with patch.object(c,'CLEANUP_SECONDS',.01),self.assertRaisesRegex(c.BoundaryError,'SPAWN_NOT_READY'):spawn.take(.01)
        self.assertFalse(spawn.done.is_set());self.assertIsNone(spawn.process)
        release.set();self.assertTrue(spawn.done.wait(1));spawn.thread.join(1)
        self.assertTrue(c.retirement_ok(spawn.cleanup));self.assertEqual(process.closes,1)
    def test_no_false_retirement_for_reader_still_active(self):
        process=FakeProcess();process.returncode=0
        reader=SimpleNamespace(join=lambda timeout:None,is_alive=lambda:True)
        receipt=c.retire(process,FakeKernel(),reader)
        self.assertFalse(receipt['closed']);self.assertEqual(process.closes,0);self.assertIn('reader_not_terminal',receipt['errors'])

class KernelIdentity(unittest.TestCase):
    def kernel(self):
        calls=[];ids={1010:10,2020:20,3030:10}
        kernel=p.Kernel.__new__(p.Kernel);kernel.authorized=None
        def duplicate(current,source,target,remote,access,inherited,options):
            calls.append(('duplicate',current,source,target,access,inherited,options));remote.value=3030;return True
        kernel.k=SimpleNamespace(GetCurrentProcess=lambda:-1,GetProcessId=lambda handle:ids.get(handle,0),
            WaitForSingleObject=lambda handle,timeout:p.WAIT_TIMEOUT,DuplicateHandle=duplicate,
            CloseHandle=lambda handle:calls.append(('close',handle)) or True,
            TerminateProcess=lambda handle,code:calls.append(('terminate',handle,code)) or True)
        kernel.w=SimpleNamespace(HANDLE=lambda:SimpleNamespace(value=None));kernel.c=SimpleNamespace(byref=lambda value:value)
        return kernel,calls
    def test_duplicate_uses_retained_source_and_target_objects_with_minimal_rights(self):
        kernel,calls=self.kernel()
        self.assertEqual(kernel.duplicate_producer(SimpleNamespace(_process=1010,pid=10),SimpleNamespace(_process=2020,pid=20)),3030)
        self.assertEqual(calls,[('duplicate',-1,1010,2020,0x00101000,False,0)])
    def test_wrong_kernel_pid_never_duplicates_or_terminates(self):
        kernel,calls=self.kernel();wrong=SimpleNamespace(_process=1010,pid=99)
        with self.assertRaisesRegex(c.BoundaryError,'IDENTITY'):kernel.duplicate_producer(wrong,SimpleNamespace(_process=2020,pid=20))
        with self.assertRaisesRegex(c.BoundaryError,'IDENTITY'):kernel.terminate_owned(wrong)
        self.assertEqual(calls,[])
    def test_remote_authorization_rejects_wrong_pid_and_closes_verified_handle_once(self):
        kernel,calls=self.kernel()
        with self.assertRaises(c.BoundaryError):kernel.accept_producer(3030,99)
        self.assertIsNone(kernel.authorized);self.assertEqual(calls,[])
        kernel.accept_producer(3030,10);kernel.producer_alive(10);kernel.close_authorized();kernel.close_authorized()
        self.assertEqual(calls,[('close',3030)])
    def test_enum_collects_only_owned_pid_numeric_handles(self):
        kernel,calls=self.kernel();kernel.producer_alive=lambda pid:None;kernel.callback=lambda function:function
        owners={10:999,20:123,30:777};lookups=[]
        kernel.window_pid=lambda handle:lookups.append(handle) or owners[handle]
        def enumerate_windows(callback,value):
            for handle in (10,20,30):
                if not callback(handle,0):return False
            return True
        kernel._user=lambda:SimpleNamespace(EnumWindows=enumerate_windows)
        self.assertEqual(kernel.owned_handles(123),[20]);self.assertEqual(lookups,[10,20,30]);self.assertEqual(calls,[])

class Queries(unittest.TestCase):
    def setup(self):
        events=[]
        class Kernel:
            def producer_alive(self,pid):events.append(('alive',pid))
            def owned_handles(self,pid):events.append(('enum',pid));return [10,20]
            def window_pid(self,hwnd):events.append(('pid',hwnd));return 123
        class Window:
            def __init__(self,visible):self.visible=visible
            def is_visible(self):events.append(('visible',self.visible));return self.visible
            @property
            def element_info(self):raise AssertionError('NO_TITLE_OR_PATH_QUERY')
        windows={10:Window(True),20:Window(False)}
        class Desktop:
            def windows(self,**args):events.append(('desktop',args));return list(windows.values())
            def window(self,**args):events.append(('bind',args));return SimpleNamespace(wrapper_object=lambda:windows[args['handle']])
        return events,Kernel(),Desktop(),windows
    def test_original_process_filtered_order_and_visibility(self):
        events,kernel,desktop,windows=self.setup();stages=[];value=p.query(desktop,kernel,123,'desktop',stages.append)
        self.assertEqual(value,dict(visible_count=1));self.assertIn(('desktop',{'process':123}),events)
        self.assertFalse(any(e[0] in ('bind','enum') for e in events))
        self.assertEqual(stages,['desktop_query_begin','desktop_query_complete','visibility_begin','visibility_complete','visibility_begin','visibility_complete'])
    def test_fallback_binds_only_owned_handles_and_uses_same_visibility(self):
        events,kernel,desktop,windows=self.setup();stages=[];value=p.query(desktop,kernel,123,'owned_handles',stages.append)
        self.assertEqual(value,dict(visible_count=1,owned_count=2));self.assertNotIn('desktop',[e[0] for e in events])
        self.assertEqual([e for e in events if e[0]=='bind'],[('bind',{'handle':10}),('bind',{'handle':20})])
    def test_foreign_handle_is_rejected_before_binding(self):
        events,kernel,desktop,windows=self.setup();kernel.window_pid=lambda hwnd:999
        with self.assertRaisesRegex(c.BoundaryError,'HWND_CHANGED'):p.query(desktop,kernel,123,'owned_handles',lambda x:None)
        self.assertFalse(any(e[0]=='bind' for e in events))
    def test_handle_owner_change_after_binding_is_rejected_before_visibility(self):
        events,kernel,desktop,windows=self.setup();calls=iter([123,999]);kernel.window_pid=lambda hwnd:next(calls)
        with self.assertRaisesRegex(c.BoundaryError,'HWND_CHANGED'):p.query(desktop,kernel,123,'owned_handles',lambda x:None)
        self.assertFalse(any(e[0]=='visible' for e in events))
    def test_original_error_not_replaced_by_fallback_success(self):
        events,kernel,desktop,windows=self.setup();error=TimeoutError('private source title')
        desktop.windows=lambda **kwargs:(_ for _ in ()).throw(error)
        with self.assertRaises(TimeoutError) as seen:p.query(desktop,kernel,123,'desktop',lambda x:None)
        self.assertIs(seen.exception,error)
        self.assertEqual(p.query(desktop,kernel,123,'owned_handles',lambda x:None)['visible_count'],1)
        self.assertIs(seen.exception,error)
    def test_visibility_exception_is_original_and_not_reported_as_invisible(self):
        events,kernel,desktop,windows=self.setup();error=ValueError('private file name')
        windows[10].is_visible=lambda:(_ for _ in ()).throw(error)
        with self.assertRaises(ValueError) as seen:p.query(desktop,kernel,123,'owned_handles',lambda x:None)
        self.assertIs(seen.exception,error)
    def test_same_single_producer_pid_used_in_both_queries(self):
        events,kernel,desktop,windows=self.setup()
        p.query(desktop,kernel,123,'desktop',lambda x:None);p.query(desktop,kernel,123,'owned_handles',lambda x:None)
        self.assertTrue(all(e[1]==123 for e in events if e[0] in ('alive','enum')))

class Attempt(unittest.TestCase):
    def test_ready_operation_real_result_and_clean_terminal(self):
        raw=packet('stage',stage='backend_ready')+packet('ready',pid=123)+packet('stage',stage='request_begin')+packet('result',status='ok',visible_count=1)
        process=FakeProcess(raw=raw);process.returncode=0;kernel=FakeKernel();kernel.owned=lambda x:1010;kernel.duplicate_producer=lambda a,b:2020
        with tempfile.TemporaryDirectory() as directory,patch.object(p.secrets,'token_hex',return_value=N):
            row=p.run_attempt('desktop',Path(directory),N,SimpleNamespace(pid=456),kernel,lambda command:process,[])
        self.assertTrue(row['pass_']);self.assertTrue(c.retirement_ok(row['retirement']))
        self.assertEqual(row['request_budget_seconds'],12);self.assertNotIn(N,json.dumps(row))
    def test_ready_but_request_hangs_is_deadline_and_kill_reap(self):
        class Stream:
            def __init__(self,process):self.process=process;self.n=0
            def readline(self,n):
                self.n+=1
                if self.n==1:return packet('ready',pid=123)
                self.process.event.wait();return b''
            def close(self):pass
        process=FakeProcess();process.stdout=Stream(process);kernel=FakeKernel();kernel.owned=lambda x:1010;kernel.duplicate_producer=lambda a,b:2020
        with tempfile.TemporaryDirectory() as directory,patch.object(p.secrets,'token_hex',return_value=N),patch.object(p,'REQUEST_SECONDS',.03):
            row=p.run_attempt('desktop',Path(directory),N,SimpleNamespace(pid=456),kernel,lambda command:process,[])
        self.assertFalse(row['pass_']);self.assertEqual(row['code'],'REQUEST_TIMEOUT');self.assertTrue(c.retirement_ok(row['retirement']))
    def test_default_request_and_startup_budgets_and_stage_vocabulary_fixed(self):
        self.assertEqual(c.REQUEST_SECONDS,12);self.assertEqual(c.STARTUP_SECONDS,12)
        for value in ('desktop_query_begin','desktop_query_complete','enum_owned_begin','bind_owned_complete','visibility_complete'):self.assertIn(value,c.STAGES)
    def test_fallback_success_cannot_change_original_failure_or_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace=Path(directory).resolve();temp=workspace/'temp';temp.mkdir();(workspace/'qa/packaged-live').mkdir(parents=True)
            for path in (workspace/'qa/packaged-live/native_editor.py',workspace/'win_owned_process.py',workspace/'win_clipboard_native.py'):path.write_text('# controlled hash-only fixture')
            process=FakeProcess();kernel=FakeKernel();kernel.owned=lambda owner:1010
            clean=dict(reaped=True,closed=True,errors=[],exit_code=0)
            original=dict(strategy='desktop',pass_=False,status='failed',code='REQUEST_TIMEOUT',retirement=clean)
            fallback=dict(strategy='owned_handles',pass_=True,status='returned',retirement=clean)
            env=dict(GITHUB_RUN_ID='1',GITHUB_RUN_ATTEMPT='1')
            with patch.object(p,'hosted',return_value=(temp,workspace)),patch.object(p,'context'),patch.object(p,'Kernel',return_value=kernel),\
                 patch.object(p.secrets,'token_hex',return_value=N),patch.object(p,'wait_object',return_value={'id':N}),\
                 patch.object(p,'run_attempt',side_effect=[original,fallback]),patch.dict(sys.modules,{'win_owned_process':SimpleNamespace(spawn_owned=lambda command:process)}),patch.dict(os.environ,env),patch.object(p.sys,'path',list(sys.path)),patch.object(tempfile,'tempdir',tempfile.gettempdir()):
                self.assertEqual(p.run(SimpleNamespace(output=str(workspace/'build/report'))),1)
            report=json.loads((workspace/'build/report/report.json').read_text())
            self.assertFalse(report['ok'] or report['original_pass'] or report['normal_acceptance'])
            self.assertTrue(report['fallback_pass']);self.assertEqual(report['rows'][0],original)
    def test_noop_ready_ack_uses_exact_fixture_command_without_clipboard_or_input(self):
        mapped=Path(__file__).resolve().parents[2]/'qa/packaged-live/native_editor.py'
        path=mapped if mapped.is_file() else Path(__file__).resolve().parents[3]/'work/flickdock-smart-hybrid-1825/qa/packaged-live/native_editor.py'
        node=next(n for n in ast.parse(path.read_bytes()).body if isinstance(n,ast.FunctionDef) and n.name=='command')
        class Poison:
            def __getattr__(self,name):raise AssertionError('NO_CLIPBOARD_OR_INPUT_ACCESS')
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);c.write_object(folder/'editor-command.json',dict(id=N,action='owned_lookup_ready_noop'))
            scope=dict(folder=folder,json=json,seen=[None],text=Poison(),app=Poison())
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope);scope['command']()
            self.assertEqual(json.loads((folder/'editor-ack.json').read_text()),{'id':N})
            self.assertEqual(sorted(p.name for p in folder.iterdir()),['editor-ack.json','editor-command.json'])

if __name__=='__main__':unittest.main(verbosity=2)
