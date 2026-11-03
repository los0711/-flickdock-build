"""Owned TEMP/offscreen cleanup capability and lifecycle regressions."""
import copy,hashlib,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import clipboard_cleanup as lane
import clipboard_cleanup_worker as worker
import clipboard_sync as sync
import test_clipboard_native_read_v1825 as fixture
from test_html_native_workspace import APP,flush,until


def synthetic_process(command,**options):
    from qa.clipboard_cleanup_synthetic import synthetic_spawn
    return synthetic_spawn(command,**options)


class WorkerCleanupTests(unittest.TestCase):
    def test_windows_directory_pin_requests_list_access_and_denies_delete_sharing(self):
        from qa.clipboard_cleanup_synthetic import production_held_directory
        calls=[]
        class Function:
            def __init__(self,name,result):self.name=name;self.result=result
            def __call__(self,*args):calls.append((self.name,args));return self.result
        kernel=SimpleNamespace(CreateFileW=Function('open',123),CloseHandle=Function('close',True))
        with patch.object(worker,'os',SimpleNamespace(name='nt')),patch.object(worker,'stamp',return_value=[1,2]),patch.object(worker,'windows_directory_api',return_value=kernel):
            with production_held_directory(Path('owned-fixture')):self.assertEqual(len(calls),1)
        self.assertEqual(calls[-1],('close',(123,)))
        opened=calls[0][1]
        self.assertEqual(opened[1]&0x0001,0x0001,'Attribute-only handles do not pin a directory name')
        self.assertEqual(opened[2]&0x0004,0,'Cleanup must not share deletion of an inspected directory')
        self.assertEqual(opened[5]&0x00200000,0x00200000)
    def setUp(self):
        from test_win_clipboard_io_v1825 import ensure_owned_process_temp
        from qa.clipboard_cleanup_synthetic import synthetic_scope
        self.scope=synthetic_scope();self.scope.__enter__()
        ensure_owned_process_temp();self.temp=tempfile.TemporaryDirectory(prefix='fd-cleanup-worker-')
        self.cache=Path(self.temp.name).resolve(strict=True)/'clipboard-cache';self.cache.mkdir()
        self.target=self.cache/('clip-'+uuid4().hex);self.target.mkdir();self.created=time.time()-90000
        (self.target/'.lease.json').write_text(json.dumps(dict(flickdock_clipboard=1,created_at=self.created)))
        self.body=self.target/'original.bin';self.body.write_bytes(b'owned cache original')
    def tearDown(self):self.temp.cleanup();self.scope.__exit__(None,None,None)
    def request(self,mode='plan',**kw):
        return dict(version=1,token='a'*32,mode=mode,cache=str(self.cache),targets=[],references=[],protected=[],inventory=True,cursor='',cache_stamp=None,**kw)
    def delete_request(self):
        plan=worker.execute(self.request())
        return dict(self.request(),mode='delete',targets=plan['candidates'],inventory=False,cache_stamp=plan['cache_stamp'])
    def test_partial_unlink_failure_retains_marker_sidecar_and_real_restart_owner(self):
        request=self.delete_request();real=worker.os.unlink;parent=self.target.stat()
        def denied(path,*a,**kw):
            candidate=Path(path);match=candidate.is_absolute() and candidate==self.body
            if not candidate.is_absolute() and candidate.name==self.body.name and kw.get('dir_fd')is not None:
                opened=worker.os.fstat(kw['dir_fd']);match=(opened.st_dev,opened.st_ino)==(parent.st_dev,parent.st_ino)
            if match:raise PermissionError(13,'Synthetic deletion denied')
            return real(path,*a,**kw)
        with patch.object(worker.os,'unlink',denied):result=worker.execute(request)
        self.assertEqual(result['errors'][0]['code'],'ACCESS_DENIED');self.assertEqual(result['deleted'],[])
        self.assertTrue((self.target/'.lease.json').exists());self.assertTrue(worker.sidecar(self.cache,self.target.name).exists())
        restart=worker.execute(self.request());self.assertEqual(restart['candidates'][0]['name'],self.target.name)
        result=worker.execute(dict(request,targets=restart['candidates']))
        self.assertEqual(result['deleted'],[self.target.name]);self.assertFalse(self.target.exists());self.assertFalse(worker.sidecar(self.cache,self.target.name).exists())
    def test_missing_inner_marker_after_partial_delete_recovers_only_from_exact_sidecar(self):
        request=self.delete_request();worker.save_owner(self.cache,request['targets'][0]);(self.target/'.lease.json').unlink()
        plan=worker.execute(self.request());self.assertEqual(plan['candidates'][0]['name'],self.target.name)
        result=worker.execute(dict(request,targets=plan['candidates']));self.assertEqual(result['deleted'],[self.target.name])
    def test_current_references_and_held_paths_are_not_deleted(self):
        for field,value in (('references',[str(self.body)]),('protected',[str(self.target)])):
            request=self.request();request[field]=value;result=worker.execute(request)
            self.assertEqual(result['candidates'],[]);self.assertEqual(self.body.read_bytes(),b'owned cache original')
    def test_missing_or_corrupt_owner_and_reparse_never_authorize_delete(self):
        (self.target/'.lease.json').unlink();result=worker.execute(self.request());self.assertEqual(result['candidates'],[])
        self.assertTrue(self.body.exists());(self.target/'.lease.json').write_text('{broken')
        result=worker.execute(self.request());self.assertTrue(result['errors']);self.assertTrue(self.body.exists())
    def test_junction_or_symlink_preserves_external_original(self):
        source=Path(self.temp.name)/'external-original';source.mkdir();original=source/'keep.bin';original.write_bytes(b'never delete this original')
        link=self.target/'external-link'
        if os.name=='nt':
            subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command','New-Item -ItemType Junction -Path $env:FD_CLEAN_LINK -Target $env:FD_CLEAN_SOURCE | Out-Null'],env=dict(os.environ,FD_CLEAN_LINK=str(link),FD_CLEAN_SOURCE=str(source)),check=True,capture_output=True,timeout=15)
        else:link.symlink_to(source,target_is_directory=True)
        request=self.delete_request();result=worker.execute(request)
        self.assertEqual(result['errors'][0]['code'],'UNSAFE');self.assertEqual(original.read_bytes(),b'never delete this original')
    def test_changed_directory_generation_is_not_deleted(self):
        request=self.delete_request();(self.target/'.lease.json').write_text(json.dumps(dict(flickdock_clipboard=1,created_at=self.created+1)))
        result=worker.execute(request);self.assertTrue(result['errors']);self.assertTrue(self.body.exists())
    def test_worker_cancel_boundary_stops_before_first_unlink(self):
        request=self.delete_request();calls=[]
        def cancel():calls.append(1);raise worker.CleanupStale('Synthetic revoked authority')
        with self.assertRaises(worker.CleanupStale):worker.execute(request,check=cancel)
        self.assertTrue(self.body.exists());self.assertTrue(calls)
    def test_bad_cli_duplicate_path_and_nonce_reject_before_target_io(self):
        for arguments in ([],['--request','x']*4,['--request',str(self.body),'--token','a'*32,'--parent-pid',str(os.getpid()),'--deadline-ms','5000']):
            with patch.object(worker,'execute',side_effect=AssertionError('Invalid cap reached target')):
                self.assertEqual(worker.main(arguments),2)
    def test_unknown_fixed_partial_is_not_overwritten_or_multiplied(self):
        request=self.delete_request();partial=self.cache/(self.target.name+worker.PARTIAL_SUFFIX);raw=b'unknown truncated original';partial.write_bytes(raw)
        for unused in range(3):
            result=worker.execute(request);self.assertEqual(result['errors'][0]['code'],'UNSAFE')
        self.assertEqual(partial.read_bytes(),raw);self.assertEqual(len(list(self.cache.glob('*'+worker.PARTIAL_SUFFIX))),1);self.assertTrue(self.body.exists())
    def test_valid_fixed_partial_from_crash_is_recovered(self):
        request=self.delete_request();record=request['targets'][0]
        (self.cache/(self.target.name+worker.PARTIAL_SUFFIX)).write_text(json.dumps({key:record[key] for key in ('version','name','created','stamp','lease_sha256')}))
        result=worker.execute(request);self.assertEqual(result['deleted'],[self.target.name]);self.assertFalse(self.target.exists());self.assertFalse((self.cache/(self.target.name+worker.PARTIAL_SUFFIX)).exists())
    def test_unavailable_directory_identity_is_unsafe_not_verified(self):
        with self.assertRaises(worker.CleanupBoundary):worker.file_identity(SimpleNamespace(st_dev=0,st_ino=0))
        real=Path.stat
        def unavailable(path,*args,**kw):
            info=real(path,*args,**kw)
            return SimpleNamespace(st_dev=info.st_dev,st_ino=0,st_mode=info.st_mode) if path==self.target and not kw else info
        with patch.object(Path,'stat',unavailable):result=worker.execute(self.request())
        self.assertEqual(result['candidates'],[]);self.assertEqual(result['errors'][0]['code'],'UNSAFE');self.assertTrue(self.body.exists())
    def test_argv_grammar_does_not_touch_filesystem_before_alarm_is_installed(self):
        token='a'*32;path=Path(self.temp.name)/'FlickDock-cleanup-v1'/token/'request.json'
        with patch.object(worker.tempfile,'gettempdir',side_effect=AssertionError('Grammar touched FS')):
            parsed=worker.parse_arguments(['--request',str(path),'--token',token,'--parent-pid',str(os.getpid()),'--deadline-ms','200'])
        self.assertEqual(parsed[1:],(token,os.getpid(),200))


class CleanupClientTests(unittest.TestCase):
    def setUp(self):
        from test_win_clipboard_io_v1825 import ensure_owned_process_temp
        from qa.clipboard_cleanup_synthetic import synthetic_scope
        self.scope=synthetic_scope();self.scope.__enter__()
        ensure_owned_process_temp();self.spawn=patch.object(lane,'spawn',side_effect=synthetic_process);self.spawn_mock=self.spawn.start()
        self.f=fixture.NativeReadTests();self.f.setUp();until(lambda:self.f.client._cleanup_runner.active is None)
        self.client=self.f.client;self.source=Path(self.f.temp.name)/'original.bin';self.source.write_bytes(b'untouched original')
    def tearDown(self):
        self.client.close();until(lambda:self.client._cleanup_runner.active is None);self.f.tearDown();self.spawn.stop();self.scope.__exit__(None,None,None)
    def expired(self):
        root=self.client._directory();created=time.time()-90000;(root/'.lease.json').write_text(json.dumps(dict(flickdock_clipboard=1,created_at=created)))
        self.client._directories=[(path,created if path==root else at) for path,at in self.client._directories]
        body=root/'cached.bin';body.write_bytes(b'cache bytes');return root,body
    def native_files(self,path):
        info=path.stat();self.f.clip.value=('files',[str(path)]);self.f.clip.sequence+=1
        self.f.clip.file_info=dict(signature=('files',((os.path.normcase(os.path.abspath(path)),info.st_size,info.st_mtime_ns),)),source_stat=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns])
        self.client._ensure_native(lambda:None,'test');self.f.reader.deliver();flush()
    def test_original_resolve_never_runs_on_gui_and_real_delete_finishes(self):
        self.native_files(self.source);root,body=self.expired();gui=threading.get_ident();real=Path.resolve
        def resolve(path,*a,**kw):
            if path==self.source and threading.get_ident()==gui:raise AssertionError('GUI resolved native path')
            return real(path,*a,**kw)
        self.client._cleanup_retry_at=0
        with patch.object(Path,'resolve',resolve):
            started=time.perf_counter();self.client._cleanup();elapsed=time.perf_counter()-started
            until(lambda:self.client._cleanup_runner.active is None)
        self.assertLess(elapsed,.1);self.assertFalse(root.exists());self.assertNotIn(root,dict(self.client._directories));self.assertEqual(self.source.read_bytes(),b'untouched original')
    def test_current_and_file_job_held_directory_are_kept(self):
        root,body=self.expired();self.client._current_directory=root;self.client._cleanup_retry_at=0;self.client._cleanup()
        until(lambda:self.client._cleanup_runner.active is None);self.assertTrue(body.exists())
        self.client._current_directory=None
        with patch.object(self.client.file_jobs,'protected_directories',return_value={root}):
            self.client._cleanup_retry_at=0;self.client._cleanup();until(lambda:self.client._cleanup_runner.active is None)
        self.assertTrue(body.exists())
    def test_successful_idle_plan_rechecks_when_current_reference_is_released(self):
        self.native_files(self.source);root,body=self.expired()
        self.client._current_directory=root;self.client._cleanup_inventory=True;self.client._cleanup_retry_at=0
        self.client._cleanup();until(lambda:self.client._cleanup_runner.active is None)
        self.assertTrue(body.exists());self.assertGreater(self.client._cleanup_retry_at,time.monotonic())
        self.client._current_directory=None;self.client._cleanup()
        until(lambda:not root.exists() and self.client._cleanup_runner.active is None)
        self.assertEqual(self.source.read_bytes(),b'untouched original')
    def test_format_noise_keeps_the_successful_idle_budget(self):
        self.native_files(self.source);root,body=self.expired()
        self.client._current_directory=root;self.client._cleanup_inventory=True;self.client._cleanup_retry_at=0
        self.client._cleanup();until(lambda:self.client._cleanup_runner.active is None)
        before=self.spawn_mock.call_count
        for unused in range(12):
            self.f.clip.sequence+=1;self.f.clip.dataChanged.emit();self.client.change_debounce.stop();self.client._cleanup()
        self.assertEqual(self.spawn_mock.call_count,before);self.assertTrue(body.exists())
    def test_actual_new_client_recovers_sidecar_without_inner_marker(self):
        root,body=self.expired();record=worker.inspect(self.client.cache,root.name);worker.save_owner(self.client.cache,record);(root/'.lease.json').unlink()
        self.client.close();until(lambda:self.client._cleanup_runner.active is None)
        from test_clipboard_sync_v1819 import Clipboard,Events
        with patch.object(sync,'_windows_clipboard_reader',return_value=None):
            other=sync.ClipboardSync(self.f.peer,Events(),self.f.temp.name,clipboard=Clipboard())
            try:
                until(lambda:not root.exists() and other._cleanup_runner.active is None)
                self.assertNotIn(root,dict(other._directories))
            finally:other.close();until(lambda:other._cleanup_runner.active is None)
    def test_late_plan_after_new_notification_cannot_dispatch_delete(self):
        root,body=self.expired();record=worker.inspect(self.client.cache,root.name);actual=self.client._cleanup_runner
        class Held:
            def __init__(self):self.active=None;self.calls=[]
            def submit(self,request,callback):self.active=object();self.calls.append((request,callback));return 'held'
            def cancel(self):pass
        held=Held();self.client._cleanup_runner=held
        try:
            self.client._cleanup_retry_at=0;self.client._cleanup();self.assertEqual(len(held.calls),1)
            self.f.clip.sequence+=1;self.f.clip.dataChanged.emit();self.client.change_debounce.stop()
            request,callback=held.calls[0];held.active=None
            callback(dict(status='ready',mode='plan',records=[{key:record[key] for key in ('name','created','stamp','lease_sha256')}],candidates=[record],errors=[],deleted=[],more=False,cursor='',cache_stamp=worker.stamp(self.client.cache)))
            self.assertEqual(len(held.calls),1);self.assertTrue(body.exists());self.assertIn(root,dict(self.client._directories))
        finally:self.client._cleanup_runner=actual
    def test_failed_capability_does_not_retry_from_finished_or_repeated_sweeps(self):
        root,body=self.expired();actual=self.client._cleanup_runner
        class Failed:
            active=None
            def __init__(self):self.calls=[]
            def submit(self,request,callback):self.calls.append(request);callback(dict(status='failed',code='IO'));return 'failed'
            def cancel(self):pass
        failed=Failed();self.client._cleanup_runner=failed
        try:
            self.client._cleanup_retry_at=0;self.client._cleanup()
            for unused in range(20):
                self.client._generation+=1
                self.client._current_directory=root if unused%2 else None
                self.client._cleanup()
            self.assertEqual(len(failed.calls),1);self.assertTrue(body.exists());self.assertIn(root,dict(self.client._directories))
        finally:self.client._cleanup_runner=actual
    def test_fatal_generation_is_paused_without_blocking_good_target_then_owner_change_recovers(self):
        bad,body=self.expired();(bad/'.lease.json').write_text('{invalid')
        self.client._cleanup_retry_at=0;self.client._cleanup();until(lambda:self.client._cleanup_runner.active is None)
        self.assertIn(bad.name,self.client._cleanup_paused);before=self.spawn_mock.call_count
        for unused in range(4):self.client._cleanup_retry_at=0;self.client._cleanup()
        self.assertEqual(self.spawn_mock.call_count,before);self.assertTrue(body.exists())
        good,good_body=self.expired();self.client._cleanup_retry_at=0;self.client._cleanup();until(lambda:self.client._cleanup_runner.active is None)
        self.assertFalse(good.exists());self.assertTrue(body.exists())
        changed=time.time()-90000;(bad/'.lease.json').write_text(json.dumps(dict(flickdock_clipboard=1,created_at=changed)))
        self.client._cleanup_fatal_recheck=0;self.client._cleanup_retry_at=0;self.client._cleanup();until(lambda:self.client._cleanup_runner.active is None)
        self.assertFalse(bad.exists());self.assertNotIn(bad.name,self.client._cleanup_paused)
    def test_coalesced_calls_do_not_spawn_parallel_cleanup_and_close_reaps(self):
        root,body=self.expired();self.client._cleanup_retry_at=0;self.client._cleanup();active=self.client._cleanup_runner.active
        for unused in range(20):self.client._cleanup()
        self.assertIs(self.client._cleanup_runner.active,active);self.assertTrue(self.client._cleanup_requested)
        self.client.close();until(lambda:self.client._cleanup_runner.active is None)
        self.assertTrue(active.reaped);self.assertIsNone(self.client._cleanup_runner.quarantined)
    def test_notification_before_debounce_immediately_cancels_exact_capability(self):
        root,body=self.expired();self.client._cleanup_retry_at=0;self.client._cleanup();operation=self.client._cleanup_runner.active
        self.f.clip.sequence+=1;self.f.clip.dataChanged.emit()
        self.assertTrue(operation.cancelled.is_set());self.assertTrue(self.client.change_debounce.isActive())
        until(lambda:self.client._cleanup_runner.active is None);self.assertTrue(body.exists())
    def test_account_and_disabled_transitions_cancel_the_owned_operation(self):
        for change in ('account','off'):
            with self.subTest(change=change):
                if not self.client.enabled:self.client.set_enabled(True)
                self.expired();self.client._cleanup_retry_at=0;self.client._cleanup();operation=self.client._cleanup_runner.active
                if change=='account':self.f.peer.account={'id':str(uuid4())};self.f.peer.account_changed.emit(self.f.peer.account)
                else:self.client.set_enabled(False)
                self.assertTrue(operation.cancelled.is_set());until(lambda:self.client._cleanup_runner.active is None)


class ProcessCleanupTests(unittest.TestCase):
    def setUp(self):
        from test_win_clipboard_io_v1825 import ensure_owned_process_temp
        from qa.clipboard_cleanup_synthetic import synthetic_scope
        self.scope=synthetic_scope();self.scope.__enter__()
        ensure_owned_process_temp();self.temp=tempfile.TemporaryDirectory(prefix='fd-cleanup-process-');self.cache=Path(self.temp.name).resolve(strict=True)/'clipboard-cache';self.cache.mkdir();self.controllers=[]
    def tearDown(self):
        for controller in self.controllers:controller.close();until(lambda:controller.active is None)
        self.temp.cleanup();self.scope.__exit__(None,None,None)
    def request(self,**kw):
        value=dict(mode='plan',cache=str(self.cache),targets=[],references=[],protected=[],inventory=True,cursor='',cache_stamp=None);value.update(kw);return value
    def actor(self,program=None,deadline_ms=5000):
        command=None if program is None else [sys.executable,'-c',program,str(Path(worker.__file__).parent)]
        actor=lane.ClipboardCleanup(command=command,process_factory=synthetic_process,deadline_ms=deadline_ms);self.controllers.append(actor);return actor
    def test_actual_fd_stdout_writer_with_windowed_stdout_none(self):
        program="import sys;sys.path.insert(0,sys.argv.pop(1));import clipboard_cleanup_worker as w;w.write_response=w.write_posix;sys.stdout=None;raise SystemExit(w.main(sys.argv[1:]))"
        actor=self.actor(program);results=[];actor.submit(self.request(),results.append);operation=actor.active
        until(lambda:bool(results));self.assertEqual(results[0]['status'],'ready');self.assertTrue(operation.reaped);self.assertEqual(operation.process.returncode,0)
    def test_slow_external_planning_is_really_killed_without_gui_event_pumping(self):
        source=Path(self.temp.name)/'owned-reference.bin';source.write_bytes(b'keep original')
        program="""import sys,time
sys.path.insert(0,sys.argv.pop(1))
import clipboard_cleanup_worker as w
real=w.Path.resolve
def slow(path,*a,**kw):
 if path.name=='owned-reference.bin':time.sleep(2)
 return real(path,*a,**kw)
w.Path.resolve=slow
raise SystemExit(w.main(sys.argv[1:]))
"""
        actor=self.actor(program,deadline_ms=200);results=[];actor.submit(self.request(references=[str(source)]),results.append);operation=actor.active
        self.assertTrue(operation.done.wait(1.5),'Deadline must not depend on GUI processEvents')
        self.assertTrue(operation.reaped);self.assertIsNotNone(operation.process.returncode)
        until(lambda:bool(results));self.assertEqual(results[0]['status'],'deferred',dict(
            result=results[0],reason=operation.reason,reaped=operation.reaped,
            returncode=operation.process.returncode))
        self.assertEqual(results[0]['code'],'DEADLINE');self.assertEqual(source.read_bytes(),b'keep original')


if __name__=='__main__':unittest.main()
