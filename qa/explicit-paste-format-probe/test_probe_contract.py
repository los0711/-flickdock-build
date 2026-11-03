"""Portable fake guard/parser/ownership checks. No Qt, DLL or clipboard access."""
from __future__ import annotations
import ast,ctypes,io,json,os,struct,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import probe_contract as c
import native_observer as observer
import qt_format_probe as controller
import qt_legacy_observer as legacy
import native_blocked_provider as blocked

def block(paths,wide=True):
    text='\0'.join(paths)+'\0\0'
    data=text.encode('utf-16-le' if wide else 'cp1252')
    return struct.pack('<IiiII',20,0,0,0,int(wide))+data

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fd-format-guard-pure-');self.base=Path(self.temp.name).resolve()
        self.root=self.base/('fd-explicit-format-'+'a'*32);self.root.mkdir();self.nonce='b'*64
        self.env=dict(GITHUB_ACTIONS='true',CI='true',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Windows',
                      GITHUB_REPOSITORY='los0711/-flickdock-build',GITHUB_EVENT_NAME='workflow_dispatch',
                      GITHUB_JOB=c.JOB,FD_QA_NATIVE='1',FD_EXPLICIT_PASTE_FORMAT_PROBE='1',RUNNER_TEMP=str(self.base),
                      GITHUB_WORKSPACE=str(self.base),GITHUB_RUN_ID='100',GITHUB_RUN_ATTEMPT='1')
        self.marker=dict(schema=1,nonce=self.nonce,run_id='100',attempt='1',parent_pid=123)
        (self.root/'scope.json').write_text(json.dumps(self.marker),encoding='utf8')
    def tearDown(self):self.temp.cleanup()
    def test_positive_pure_scope(self):
        with patch.object(c.os,'getpid',return_value=123):
            root,workspace,data=c.scope_record(self.root,self.nonce,env=self.env,platform='win32');self.assertEqual(root,self.root)
    def test_controller_and_child_parent_binding(self):
        with patch.object(c.os,'getpid',return_value=123),patch.object(c.os,'getppid',return_value=123):
            c.scope_record(self.root,self.nonce,role='controller',env=self.env,platform='win32')
            c.scope_record(self.root,self.nonce,role='child',env=self.env,platform='win32')
        for role,key in (('controller','getpid'),('child','getppid')):
            with patch.object(c.os,key,return_value=999),self.assertRaises(c.GuardError):
                c.scope_record(self.root,self.nonce,role=role,env=self.env,platform='win32')
    def test_positive_but_unrelated_marker_parent_is_rejected(self):
        data=dict(self.marker,parent_pid=os.getpid()+100003)
        (self.root/'scope.json').write_text(json.dumps(data),encoding='utf8')
        with self.assertRaises(c.GuardError):c.scope_record(self.root,self.nonce,env=self.env,platform='win32')
    def test_observer_owner_is_controller_or_explicit_delegated_fixture(self):
        self.assertEqual(c.checked_observer_owner(self.root,self.nonce,self.marker,123),123)
        with self.assertRaises(c.GuardError):c.checked_observer_owner(self.root,self.nonce,self.marker,456)
        ready=dict(nonce=self.nonce,controller_pid=123,pid=456)
        capability=dict(schema=1,nonce=self.nonce,controller_pid=123,fixture_pid=456,purpose='blocked_fixture')
        (self.root/'blocked-provider-ready.json').write_text(json.dumps(ready),encoding='utf8')
        (self.root/'observer-owner.json').write_text(json.dumps(capability),encoding='utf8')
        self.assertEqual(c.checked_observer_owner(self.root,self.nonce,self.marker,456),456)
        for mutation in ({'nonce':'c'*64},{'fixture_pid':789},{'controller_pid':999},{'purpose':'arbitrary'}):
            (self.root/'observer-owner.json').write_text(json.dumps(dict(capability,**mutation)),encoding='utf8')
            with self.assertRaises(c.GuardError):c.checked_observer_owner(self.root,self.nonce,self.marker,456)
    def test_each_required_host_gate(self):
        for key in self.env:
            env=dict(self.env);env.pop(key)
            with self.subTest(key=key),self.assertRaises(c.GuardError):c.require_hosted(env=env,platform='win32')
    def test_wrong_platform_or_role(self):
        with self.assertRaises(c.GuardError):c.require_hosted(env=self.env,platform='darwin')
        env=dict(self.env,GITHUB_JOB='normal-frozen')
        with self.assertRaises(c.GuardError):c.require_hosted(env=env,platform='win32')
        for key in ('GITHUB_REPOSITORY','GITHUB_EVENT_NAME'):
            with self.subTest(key=key),self.assertRaises(c.GuardError):c.require_hosted(env=dict(self.env,**{key:'other'}),platform='win32')
    def test_nonce_run_or_schema_mismatch(self):
        for mutation in ({'nonce':'c'*64},{'run_id':'101'},{'schema':2},{'schema':True},{'parent_pid':0}):
            (self.root/'scope.json').write_text(json.dumps(dict(self.marker,**mutation)),encoding='utf8')
            with self.subTest(mutation=mutation),self.assertRaises(c.GuardError):c.scope_record(self.root,self.nonce,env=self.env,platform='win32')
    def test_outside_or_unowned_scope(self):
        for path in (self.base,self.base/'unowned'):
            with self.assertRaises(c.GuardError):c.scope_record(path,self.nonce,env=self.env,platform='win32')
    def test_all_native_entrypoints_guard_before_api_or_qt(self):
        native_args=['--scope',str(self.root),'--nonce',self.nonce]
        with patch.dict(os.environ,{},clear=True),patch.object(ctypes,'WinDLL',side_effect=AssertionError('NO_DLL'),create=True):
            for call in (
                lambda:observer.main(native_args+['--phase','metadata','--expected-owner','123']),
                lambda:legacy.main(native_args+['--source-root',str(self.base),'--phase','legacy-inspect']),
                lambda:blocked.main(native_args),
                lambda:controller.run(SimpleNamespace(source_root=str(self.base),output=str(self.base/'out')))):
                with self.assertRaises(c.GuardError):call()
        self.assertFalse(any(name.startswith('PySide6') for name in sys.modules))
    def test_native_api_constructor_itself_is_guarded(self):
        with patch.dict(os.environ,{},clear=True),patch.object(ctypes,'WinDLL',side_effect=AssertionError('NO_DLL'),create=True):
            with self.assertRaises(c.GuardError):observer.WinReadAPI()
    def test_safe_diagnostics_never_export_messages(self):
        try:
            try:raise OSError(13,'private text C:/secret/private-file.txt')
            except OSError:raise c.BoundaryError('CLOSE_FAILED')
        except c.BoundaryError as error:record=c.safe_error(error)
        raw=json.dumps(record)
        self.assertNotIn('private-file',raw);self.assertNotIn('private text',raw)
        self.assertEqual(record[0]['code'],'CLOSE_FAILED');self.assertEqual(record[1]['errno'],13)

class ParserTests(unittest.TestCase):
    def test_wide_multi_directory_unicode_and_order(self):
        paths=['C:\\owned\\文件 A.txt','C:\\owned\\Directory','C:\\owned\\file B.bin']
        self.assertEqual(c.parse_hdrop(block(paths)),paths)
    def test_ansi_paths(self):
        paths=['C:\\owned\\café.txt','C:\\owned\\folder'];self.assertEqual(c.parse_hdrop(block(paths,False)),paths)
    def test_more_than_old_header_budget_without_cloud_limit(self):
        paths=['C:\\owned\\'+str(i)+'x'*80 for i in range(300)]
        raw=block(paths);self.assertGreater(len(raw),16384);self.assertEqual(c.parse_hdrop(raw),paths)
    def test_empty_list(self):self.assertEqual(c.parse_hdrop(block([])),[])
    def test_bad_offset_alignment_termination_or_encoding(self):
        valid=block(['C:\\owned\\a.txt'])
        invalid=[b'bad',struct.pack('<IiiII',9999,0,0,0,1)+b'xx',valid[:-4],
                 struct.pack('<IiiII',21,0,0,0,1)+b'\0'+valid[20:],
                 struct.pack('<IiiII',20,0,0,0,2)+valid[20:],
                 struct.pack('<IiiII',20,0,0,0,1)+b'\x00\xd8\0\0\0\0']
        for raw in invalid:
            with self.subTest(size=len(raw)),self.assertRaises((ValueError,UnicodeError)):c.parse_hdrop(raw)
    def test_frame_purpose_and_lengths(self):
        value=c.result(paths=['C:/owned/a']);encoded=c.encode_frame(value);self.assertEqual(c.decode_frame(encoded),value)
        for bad in (encoded+b'x',encoded[:-1],b'FDCR1\0'+encoded, c.encode_frame(dict(value,purpose='cloud'))):
            with self.assertRaises(ValueError):c.decode_frame(bad)
    def test_path_fingerprint_is_ordered_no_stat(self):
        with patch.object(Path,'stat',side_effect=AssertionError('NO_STAT')):
            self.assertNotEqual(c.path_fingerprint(['C:/a','C:/b']),c.path_fingerprint(['C:/b','C:/a']))

class CoverageTests(unittest.TestCase):
    def test_advertisement_without_actual_paths_is_unresolved(self):
        meta=dict(hdrop_available=True,owner_pid=123,owner_pid_after=123)
        for native in (dict(hdrop_available=False,paths=None,owner_pid=123,owner_pid_after=123),
                       dict(hdrop_available=True,paths=None,owner_pid=123,owner_pid_after=123),
                       dict(hdrop_available=True,paths=[],owner_pid=123,owner_pid_after=123)):
            self.assertEqual(c.classify_fastpath(True,['C:/owned/a'],meta,native,True),'unresolved')
    def test_verified_paths_owner_and_order_required(self):
        paths=['C:/owned/a','C:/owned/directory'];meta=dict(hdrop_available=True,owner_pid=123,owner_pid_after=123)
        native=dict(hdrop_available=True,paths=paths,owner_pid=123,owner_pid_after=123)
        self.assertEqual(c.classify_fastpath(True,paths,meta,native,True),'CF_HDROP')
        for mutation in ({'paths':list(reversed(paths))},{'owner_pid_after':999},{'hdrop_available':False}):
            self.assertEqual(c.classify_fastpath(True,paths,meta,dict(native,**mutation),True),'unresolved')
        self.assertEqual(c.classify_fastpath(False,[],meta,native,False),'not_file_route')

class FakeAPI:
    def __init__(self,*,available=True,busy=False,foreign=False,error=False):self.calls=[];self.available=available;self.busy=busy;self.foreign=foreign;self.error=error;self.closed=False
    def sequence(self):self.calls.append('sequence');return 22
    def owner(self):self.calls.append('owner');return 124 if self.foreign else 123
    def hdrop_available(self):self.calls.append('available');return self.available
    def open(self):self.calls.append('open');return not self.busy
    def close(self):self.calls.append('close');self.closed=True
    def formats(self):self.calls.append('formats');return [{'id':15,'name':'CF_HDROP'}]
    def copy_hdrop(self):
        self.calls.append('GetData')
        if self.error:raise OSError('SYNTHETIC')
        return block(['C:\\owned\\a','C:\\owned\\dir'])
    def ansi_codec(self):self.calls.append('codepage');assert self.closed;return 'cp1252'
class ObserverTests(unittest.TestCase):
    def test_metadata_never_requests_payload(self):
        api=FakeAPI();receipt=observer.observe(api,'metadata',123)
        self.assertEqual(receipt['status'],'ok');self.assertNotIn('GetData',api.calls);self.assertTrue(api.closed)
    def test_paths_close_before_parse(self):
        api=FakeAPI();parse=c.parse_hdrop
        def checked(raw,**kwargs):self.assertTrue(api.closed);return parse(raw,**kwargs)
        with patch.object(observer,'parse_hdrop',side_effect=checked):receipt=observer.observe(api,'paths',123)
        self.assertEqual(len(receipt['paths']),2);self.assertEqual(api.calls.count('GetData'),1)
    def test_no_hdrop_is_unresolved_not_empty_proof(self):
        api=FakeAPI(available=False);receipt=observer.observe(api,'paths',123)
        self.assertIsNone(receipt['paths']);self.assertNotIn('known_empty',receipt);self.assertNotIn('GetData',api.calls)
    def test_foreign_and_busy_no_payload_no_false_close(self):
        for api in (FakeAPI(foreign=True),FakeAPI(busy=True)):
            receipt=observer.observe(api,'paths',123);self.assertEqual(receipt['status'],'unavailable');self.assertNotIn('GetData',api.calls);self.assertFalse(api.closed)
    def test_getdata_failure_still_closes(self):
        api=FakeAPI(error=True)
        with self.assertRaises(OSError):observer.observe(api,'paths',123)
        self.assertTrue(api.closed)
    def test_owner_change_after_open_closes_without_getdata(self):
        api=FakeAPI();owners=iter((123,124));api.owner=lambda:next(owners)
        receipt=observer.observe(api,'paths',123)
        self.assertEqual(receipt['status'],'unavailable');self.assertEqual(receipt['code'],'OWNER_CHANGED')
        self.assertTrue(api.closed);self.assertNotIn('GetData',api.calls)

class FakeProcess:
    def __init__(self,raw=None,blocked=False,close_error=False):
        self.pid=123;self.returncode=None;self.event=threading.Event();self.kills=0;self.closed=False;self.close_error=close_error
        if blocked:self.stdout=self
        else:self.stdout=io.BytesIO(raw if raw is not None else c.encode_frame(c.result()));self.event.set()
    def read(self,count):self.event.wait();return b''
    def kill(self):self.kills+=1;self.returncode=-9;self.event.set()
    def wait(self,timeout=None):
        if not self.event.wait(timeout):raise subprocess.TimeoutExpired('synthetic',timeout)
        if self.returncode is None:self.returncode=0
        return self.returncode
    def close(self):
        self.closed=True
        if self.close_error:raise OSError('SYNTHETIC_CLOSE')
class OperationTests(unittest.TestCase):
    def finish(self,operation):
        self.assertTrue(operation.done.wait(2.));operation.thread.join(1.);self.assertFalse(operation.thread.is_alive());return operation.receipt
    def test_normal_frame_and_real_terminal(self):
        p=FakeProcess();r=self.finish(c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start())
        self.assertEqual(r['value']['status'],'ok');self.assertTrue(r['reaped'] and r['closed']);self.assertEqual(p.kills,0)
    def test_deadline_kill_and_reap_without_gui_or_native_calls(self):
        p=FakeProcess(blocked=True);r=self.finish(c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p,deadline=.03).start())
        self.assertEqual(r['error'],'DEADLINE');self.assertTrue(r['reaped'] and r['closed']);self.assertGreaterEqual(p.kills,1)
    def test_getdata_event_survives_and_is_not_a_ready_frame(self):
        p=FakeProcess(c.GETDATA_EVENT+c.encode_frame(c.result(paths=['C:/owned/a'])))
        r=self.finish(c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start())
        self.assertEqual(r['events'],['GETDATA_ENTER']);self.assertEqual(r['value']['status'],'ok')
    def test_cancel_before_start_never_spawns(self):
        def forbidden(*args,**kwargs):raise AssertionError('MUST_NOT_SPAWN')
        operation=c.ChildOperation(['synthetic'],process_factory=forbidden);operation.cancel();r=self.finish(operation.start())
        self.assertEqual(r['error'],'CANCELLED');self.assertTrue(r['reaped']);self.assertIsNone(r['value'])
    def test_malformed_frame_not_success(self):
        p=FakeProcess(b'invalid');r=self.finish(c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start())
        self.assertIsNone(r['value']);self.assertEqual(r['error'],'ValueError');self.assertTrue(r['reaped'])
    def test_spawn_failure_keeps_partial_owner_for_reap(self):
        p=FakeProcess(blocked=True)
        def failed(*args,**kwargs):
            error=OSError('SYNTHETIC');error._helper_owner=p;raise error
        r=self.finish(c.ChildOperation(['synthetic'],process_factory=failed).start())
        self.assertEqual(r['error'],'OSError');self.assertTrue(r['reaped'] and r['closed']);self.assertGreaterEqual(p.kills,1)
    def test_cleanup_error_is_visible(self):
        p=FakeProcess(close_error=True);r=self.finish(c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start())
        self.assertFalse(r['closed']);self.assertEqual(r['cleanup_errors'],['OSError'])
    def test_nonzero_exit_keeps_structured_diagnostic_without_success(self):
        p=FakeProcess(c.encode_frame(c.result('error',diagnostic=[{'type':'RuntimeError','code':'FIXTURE_FAILED'}])));p.returncode=1
        r=self.finish(c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start())
        self.assertEqual(r['error'],'CHILD_EXIT');self.assertEqual(r['value']['status'],'error');self.assertTrue(r['reaped'])
    def test_failed_kill_has_controller_budget_without_faking_terminal(self):
        class KillFails(FakeProcess):
            def kill(self):self.kills+=1;raise OSError('SYNTHETIC_KILL')
        p=KillFails(blocked=True);operation=c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p,deadline=.03).start()
        try:
            started=time.monotonic();receipt=c.wait_operation(operation,lambda:None,cleanup_budget=.05)
            self.assertLess(time.monotonic()-started,.5)
            self.assertTrue(receipt['quarantined']);self.assertFalse(operation.done.is_set())
            self.assertIsNone(receipt['reaped']);self.assertIsNone(receipt['closed']);self.assertTrue(receipt['retained_owner'])
            self.assertEqual(receipt['cleanup_errors'],['OSError']);self.assertEqual(p.kills,1)
            operation.cancel('SECOND_REASON');self.assertEqual(p.kills,1)
        finally:
            # Only this fake fixture is released; its later true terminal is retained.
            p.event.set();final=self.finish(operation)
        self.assertTrue(final['reaped']);self.assertTrue(final['closed']);self.assertIn('OSError',final['cleanup_errors'])
    def test_wait_normal_receipt_is_actual_terminal(self):
        p=FakeProcess();operation=c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start()
        receipt=c.wait_operation(operation,lambda:None)
        self.assertTrue(receipt['operation_done']);self.assertTrue(receipt['terminal_observed']);self.assertFalse(receipt['quarantined'])
        self.assertEqual(receipt['event_evidence'],'postmortem_prefix_before_API_only')
    def test_wait_cleanup_error_quarantines_without_overwriting_true_reap(self):
        p=FakeProcess(close_error=True);operation=c.ChildOperation(['synthetic'],process_factory=lambda *a,**k:p).start()
        receipt=c.wait_operation(operation,lambda:None)
        self.assertTrue(receipt['operation_done']);self.assertTrue(receipt['reaped']);self.assertFalse(receipt['closed'])
        self.assertTrue(receipt['quarantined']);self.assertFalse(receipt['resource_cleanup_verified'])
    def test_controller_has_no_unbounded_done_loop_and_self_exit_is_only_guarded_controller(self):
        tree=ast.parse(Path(controller.__file__).read_text(encoding='utf8'))
        loops=[node for node in ast.walk(tree) if isinstance(node,ast.While)]
        self.assertEqual(len(loops),1)
        self.assertTrue(any(isinstance(n,ast.Compare) and any(isinstance(op,ast.Lt) for op in n.ops) for n in ast.walk(loops[0].test)))
        with patch.object(controller,'require_hosted',return_value=(Path('.'),Path('.'))),patch.object(controller.os,'_exit') as exit_call:
            with self.assertRaises(c.GuardError):controller._exit_quarantined_controller({'parent_pid':os.getpid()+999})
            exit_call.assert_not_called()
            controller._exit_quarantined_controller({'parent_pid':os.getpid()});exit_call.assert_called_once_with(2)
    def test_existing_protected_job_is_scope_owned_and_not_in_inherited_handle_list(self):
        candidates=(Path(os.environ.get('GITHUB_WORKSPACE','.')),Path(__file__).resolve().parents[2],
                    Path(__file__).resolve().parents[3]/'work/flickdock-smart-hybrid-1825')
        path=next(base/'win_owned_process.py' for base in candidates if (base/'win_owned_process.py').is_file())
        tree=ast.parse(path.read_text(encoding='utf8'))
        constants={node.targets[0].id:ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name)
                   and node.targets[0].id in ('KILL_ON_JOB_CLOSE','HANDLE_LIST','JOB_LIST')}
        self.assertEqual(constants,dict(KILL_ON_JOB_CLOSE=0x2000,HANDLE_LIST=0x20002,JOB_LIST=0x2000d))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='_Win32')
        spawn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='spawn')
        assignments={node.targets[0].id:node.value for node in ast.walk(spawn) if isinstance(node,ast.Assign)
                     and len(node.targets)==1 and isinstance(node.targets[0],ast.Name)}
        self.assertEqual([arg.id for arg in assignments['handles'].args],['ih','write','eh'])
        self.assertEqual([arg.id for arg in assignments['jobs'].args],['job'])
        pairs=[node for node in ast.walk(spawn) if isinstance(node,ast.Tuple) and len(node.elts)==2
               and all(isinstance(part,ast.Name) for part in node.elts)]
        observed={tuple(part.id for part in node.elts) for node in pairs}
        self.assertIn(('HANDLE_LIST','handles'),observed);self.assertIn(('JOB_LIST','jobs'),observed)
        protected=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ProtectedProcess')
        self.assertTrue(any(isinstance(n,ast.Assign) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute)
                            and n.value.func.attr=='job' for n in ast.walk(protected)))

if __name__=='__main__':unittest.main(verbosity=2)
