"""Worker outcomes and actual consumer recovery, isolated Qt and owned files."""
import base64,errno,hashlib,os,struct,tempfile,threading,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt,QPoint,QRect
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,until,flush
from test_clipboard_sync_v1819 import Clipboard,Account,Events,Hub
from test_shared_clipboard_files_v1821 import Peer
import test_shared_clipboard_files_v1821 as shared_tests
import test_clipboard_preview_v1821 as preview_tests
import test_clipboard_multipart_v1825 as multipart_tests
from cloud_api import CloudError
import clipboard_sync as subject


class WorkerOutcomeTests(unittest.TestCase):
    def outcome(self,action,**options):
        job=subject._Job(action,**options);rows=[];job.signals.finished.connect(lambda job,outcome:rows.append(outcome))
        job.run();self.assertEqual(len(rows),1);return rows[0]
    def test_success_absence_policy_cancel_io_integrity_and_bug_are_distinct(self):
        self.assertEqual(self.outcome(lambda cancel:False).value,False)
        self.assertEqual(self.outcome(lambda cancel:None).status,'ready')
        self.assertEqual(self.outcome(lambda cancel:None,policy={'policy_skip':'oversize'}).status,'skipped')
        self.assertEqual(self.outcome(lambda cancel:cancel.set()).status,'cancelled')
        for exc,kind,operation in ((OSError(errno.EAGAIN,'PRIVATE_PATH'),'io','generic'),
                (InterruptedError('PRIVATE_PATH'),'io','generic'),
                (FileNotFoundError('PRIVATE_PATH'),'missing','generic'),
                (PermissionError(errno.EACCES,'PRIVATE_PATH'),'access','generic'),
                (PermissionError('PRIVATE_PATH'),'io','generic'),
                (OSError(errno.ENOSPC,'PRIVATE_PATH'),'storage','generic'),
                (zipfile.BadZipFile('PRIVATE_CONTENT'),'integrity','unpack'),
                (ValueError('PRIVATE_CONTENT'),'integrity','unpack'),
                (ValueError('PRIVATE_CONTENT'),'bug','generic'),
                (TypeError('PRIVATE_CONTENT'),'bug','generic')):
            with self.subTest(kind=kind,operation=operation):
                def action(cancel):raise exc
                outcome=self.outcome(action,operation=operation)
                self.assertEqual(outcome.status,'failed');self.assertEqual(outcome.error.kind,kind)
                self.assertNotIn('PRIVATE',str(outcome.error));self.assertIsNone(outcome.error.__traceback__)
        sharing=PermissionError(errno.EACCES,'PRIVATE_PATH');sharing.winerror=32
        self.assertEqual(subject.WorkerFailure.from_exception(sharing,'generic').kind,'io')
    def test_transient_io_backoff_remains_recoverable_but_unknown_bug_pauses(self):
        state={};error=subject.WorkerFailure('io');delays=[]
        for _ in range(20):
            self.assertTrue(subject.worker_retry_allowed(state,error));delays.append(subject.worker_retry_delay(state))
        self.assertEqual(delays[:4],[5000,10000,20000,30000]);self.assertEqual(max(delays),30000)
        state={};error=subject.WorkerFailure('bug')
        self.assertEqual([subject.worker_retry_allowed(state,error) for _ in range(3)],[True,True,False])


class WorkerConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.peer=Peer();self.clip=Clipboard()
        self.client=subject.ClipboardSync(self.peer,Events(),self.base,clipboard=self.clip)
        self.clients=[self.client]
        self.events=[];self.client.copy_result.connect(lambda ok,context:self.events.append(ok));flush()
    def tearDown(self):
        # Replaced parentless clients remain owned until their disk pool and
        # cleanup process retire; collect their QObjects on the GUI thread.
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs and client.pool.activeThreadCount()==0
            and client._cleanup_runner.active is None for client in self.clients))
        for client in self.clients:
            self.assertIsNone(client._cleanup_runner.quarantined)
            client.deleteLater()
        flush();self.temp.cleanup()
    def copy(self,label):
        self.clip.text(label+'x'*33000);self.client.change_debounce.stop();self.client._changed()
    def posts(self):return sum(m=='POST' and p=='/v1/clipboard' for m,p,d in self.peer.calls)
    def finish_text_upload(self):
        until(lambda:bool(self.peer.held))
        identity=next(iter(self.peer.held));_,options,respond=self.peer.held.pop(identity)
        raw=Path(options['source']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),options['headers']['X-Content-SHA256'])
        head=self.peer.staged[identity];head.update(ready=True,kind='text',size=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        self.peer.head=self.peer.heads[identity]=head;respond(dict(clipboard=head,shared_files=False))
    def test_unexpected_worker_exception_completes_and_does_not_starve_next_job(self):
        errors=[];values=[]
        def broken(cancel):raise TypeError('PRIVATE_FIXTURE')
        self.client._work(broken,values.append,failure=errors.append)
        self.client._work(lambda cancel:'still works',values.append)
        until(lambda:not self.client._jobs)
        self.assertEqual(values,['still works']);self.assertEqual(len(errors),1);self.assertEqual(errors[0].kind,'bug')
    def test_completion_callback_error_stays_visible_and_always_runs_cleanup(self):
        def raises(*args):raise TypeError('synthetic callback error')
        for outcome,callbacks in ((subject.WorkerOutcome('ready',value=1),(raises,None,None)),
                (subject.WorkerOutcome('failed',error=subject.WorkerFailure('io')),(lambda value:None,raises,None)),
                (subject.WorkerOutcome('cancelled'),(lambda value:None,None,raises))):
            with self.subTest(status=outcome.status),patch.object(self.client,'_cleanup') as cleanup:
                job=subject._Job(lambda cancel:None);self.client._jobs[job]=callbacks
                with self.assertRaisesRegex(TypeError,'synthetic callback error'):self.client._finished(job,outcome)
                cleanup.assert_called_once();self.assertNotIn(job,self.client._jobs)
    def test_temporary_package_io_retries_same_copy_then_uploads_once(self):
        original=subject.package;attempts=[];activity=[]
        self.client.activity_changed.connect(activity.append)
        def temporary(*args,**kwargs):
            attempts.append(True)
            if len(attempts)<3:raise OSError(errno.EAGAIN,'PRIVATE_FIXTURE')
            return original(*args,**kwargs)
        with patch.object(subject,'package',side_effect=temporary):
            self.copy('temporary');mime=self.clip.mimeData()
            until(lambda:self.client.retry.isActive() and not self.client._jobs)
            task=self.client._local_retry;self.assertIsNone(self.client._pending);self.assertEqual(self.client.retry.interval(),5000)
            self.client.retry.stop();self.client._retry()
            until(lambda:self.client.retry.isActive() and not self.client._jobs)
            self.assertIs(self.client._local_retry,task);self.assertIsNone(self.client._pending);self.assertEqual(self.client.retry.interval(),10000)
            self.client.retry.stop();self.client._retry()
            self.finish_text_upload()
            until(lambda:self.posts()==1 and self.client.view['phase']=='synced')
        self.assertEqual(len(attempts),3);self.assertEqual(self.events,[False,True]);self.assertIs(self.clip.mimeData(),mime)
        self.assertEqual(activity.count(True),3,'Each resumed clipboard task regains its existing highest priority')
    def test_storage_full_stops_without_requests_and_new_copy_recovers(self):
        with patch.object(subject,'package',side_effect=OSError(errno.ENOSPC,'PRIVATE_FIXTURE')):
            self.copy('full');until(lambda:not self.client._jobs and self.client.view['phase']=='error')
        self.assertIsNone(self.client._pending);self.assertIsNone(self.client._local_retry)
        self.assertFalse(self.client.retry.isActive());self.assertEqual(self.posts(),0)
        self.assertEqual(self.client.view['error_code'],'CLIPBOARD_STORAGE_FULL')
        self.assertNotIn('PRIVATE',self.client.view['error_message'])
        self.client._retry();flush();self.assertEqual(self.posts(),0)
        self.copy('genuinely changed Copy');self.finish_text_upload()
        until(lambda:self.posts()==1 and self.client.view['phase']=='synced')
    def test_real_transport_failure_keeps_network_retry_and_does_not_repeat_stage(self):
        self.copy('transient network');until(lambda:bool(self.peer.held))
        task=self.client._pending;identity=task['id'];posts=self.posts()
        _,_,respond=self.peer.held.pop(identity)
        respond(error=CloudError('synthetic connection reset',503,'NETWORK_UNREACHABLE'))
        until(lambda:self.client.retry.isActive())
        self.assertIs(self.client._pending,task);self.assertNotIn('error_code',self.client.view)
        self.client.retry.stop();self.client._retry();self.finish_text_upload()
        until(lambda:self.client.view['phase']=='synced')
        self.assertEqual(self.posts(),posts);self.assertEqual(len(self.peer.puts),2)
    def test_unknown_program_error_has_three_attempts_then_explicit_pause(self):
        attempts=[]
        def broken(*args,**kwargs):attempts.append(True);raise TypeError('PRIVATE_FIXTURE')
        with patch.object(subject,'package',side_effect=broken):
            self.copy('unknown')
            for index in range(3):
                until(lambda:not self.client._jobs and self.client.view['phase']=='error')
                if index<2:self.client.retry.stop();self.client._retry()
        self.assertEqual(len(attempts),3);self.assertFalse(self.client.retry.isActive())
        self.assertIsNone(self.client._pending);self.assertEqual(self.posts(),0)
        self.assertEqual(self.client.view['error_code'],'CLIPBOARD_WORKER_PAUSED')
    def test_pre_worker_permanent_io_uses_the_same_stop_policy(self):
        for operation in ('_directory','_next_sequence'):
            for error,code in ((OSError(errno.ENOSPC,'PRIVATE_FIXTURE'),'CLIPBOARD_STORAGE_FULL'),
                    (PermissionError(errno.EACCES,'PRIVATE_FIXTURE'),'CLIPBOARD_ACCESS_DENIED')):
                with self.subTest(operation=operation,code=code):
                    with patch.object(self.client,operation,side_effect=error) as failed:
                        self.copy(operation+code);mime=self.clip.mimeData()
                        self.assertEqual(failed.call_count,1);self.assertFalse(self.client.retry.isActive())
                        self.assertIsNone(self.client._pending);self.assertIsNone(self.client._local_retry)
                        self.assertEqual(self.client.view['error_code'],code);self.assertEqual(self.posts(),0)
                        self.client._retry();flush();self.assertEqual(failed.call_count,1)
                    self.assertIs(self.clip.mimeData(),mime)
    def test_pre_worker_temporary_io_preserves_intent_then_recovers(self):
        for operation in ('_directory','_next_sequence'):
            with self.subTest(operation=operation):
                original=getattr(self.client,operation);calls=[];before=self.posts()
                def temporary():
                    calls.append(True)
                    if len(calls)<3:raise OSError(errno.EAGAIN,'PRIVATE_FIXTURE')
                    return original()
                with patch.object(self.client,operation,side_effect=temporary):
                    self.copy('transient'+operation);task=self.client._local_retry;mime=self.clip.mimeData()
                    self.assertIsNone(self.client._pending);self.assertEqual(self.client.retry.interval(),5000)
                    self.client.retry.stop();self.client._retry()
                    self.assertIs(self.client._local_retry,task);self.assertIsNone(self.client._pending);self.assertEqual(self.client.retry.interval(),10000)
                    self.client.retry.stop();self.client._retry();self.finish_text_upload()
                    until(lambda:self.client.view['phase']=='synced')
                self.assertEqual(len(calls),3);self.assertEqual(self.posts()-before,1)
                self.assertIs(self.clip.mimeData(),mime)
    def test_cancel_during_work_removes_callback_and_never_reports_failure(self):
        started=threading.Event();release=threading.Event();success=[];failed=[];cancelled=[]
        def work(cancel):started.set();release.wait(2);raise OSError(errno.EIO,'PRIVATE_FIXTURE')
        job=self.client._work(work,success.append,failure=failed.append,cancelled=lambda:cancelled.append(True))
        until(started.is_set);job.cancelled.set();release.set();until(lambda:not self.client._jobs)
        self.assertEqual(success,[]);self.assertEqual(failed,[]);self.assertEqual(cancelled,[True]);self.assertEqual(self.events,[])
    def test_account_change_cancels_late_worker_error_without_old_status_or_notice(self):
        started=threading.Event();release=threading.Event();failures=[]
        def work(cancel):started.set();release.wait(2);raise OSError(errno.ENOSPC,'PRIVATE_FIXTURE')
        self.client._work(work,lambda value:self.fail('old worker success'),failure=failures.append)
        until(started.is_set);self.peer.account={'id':str(uuid4())};self.peer.token='new fixture session'
        self.peer.account_changed.emit(self.peer.account);release.set();until(lambda:not self.client._jobs)
        self.assertEqual(failures,[]);self.assertEqual(self.events,[]);self.assertNotIn('error_message',self.client.view)
    def test_invalid_remote_archive_stops_same_head_and_accepts_next_valid_head(self):
        self.client.close();hub=Hub();clip=Clipboard();clip.text('original native content')
        account=Account(hub.api('receiver'),'receiver')
        raw=b'not a valid archive';identity=str(uuid4())
        hub.body=raw;hub.head=dict(id=identity,version=1,ready=True,kind='files',size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),copied_at=round(time.time()*1000)+1000)
        self.client=subject.ClipboardSync(account,Events(),self.base/'receiver',clipboard=clip)
        self.clients.append(self.client)
        until(lambda:self.client.view.get('error_code')=='CLIPBOARD_WORKER_INTEGRITY')
        self.assertFalse(self.client.retry.isActive());self.assertEqual(clip.mimeData().text(),'original native content')
        count=sum(p.endswith('/content') for m,p in hub.calls)
        for _ in range(4):self.client.refresh();until(lambda:not self.client._fetching)
        self.assertEqual(sum(p.endswith('/content') for m,p in hub.calls),count)
        item=subject.inline_text(('text',dict(text='new valid peer copy',html='')))
        hub.head=dict(id=str(uuid4()),version=2,ready=True,kind='text',size=item['size'],sha256=item['sha256'],
            inline=item['inline']['body'],copied_at=round(time.time()*1000)+2000)
        self.client.refresh();until(lambda:clip.mimeData().text()=='new valid peer copy')
    def test_invalid_inline_text_stops_without_a_retry_loop(self):
        self.clip.text('unchanged native text');self.client.change_debounce.stop()
        raw=subject.TEXT_MAGIC+struct.pack('!I',999)+b'x';identity=str(uuid4())
        self.peer.head=dict(id=identity,version=1,ready=True,kind='text',size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),inline=base64.b64encode(raw).decode('ascii'),copied_at=round(time.time()*1000)+1000)
        self.client.refresh();until(lambda:self.client.view.get('error_code')=='CLIPBOARD_WORKER_INTEGRITY')
        self.assertFalse(self.client.retry.isActive());self.assertEqual(self.clip.mimeData().text(),'unchanged native text')
        self.assertFalse(any(path.endswith('/content') for method,path,data in self.peer.calls))
    def test_receive_directory_full_is_storage_error_not_network_retry(self):
        self.peer.head=dict(id=str(uuid4()),version=1,ready=True,kind='files',size=32,sha256='0'*64,
            copied_at=round(time.time()*1000)+1000)
        with patch.object(self.client,'_directory',side_effect=OSError(errno.ENOSPC,'PRIVATE_FIXTURE')):
            self.client.refresh();until(lambda:self.client.view.get('error_code')=='CLIPBOARD_STORAGE_FULL')
        self.assertFalse(self.client.retry.isActive())
        self.assertFalse(any(path.endswith('/content') for method,path,data in self.peer.calls))
    def test_inline_materialization_access_error_stops_using_existing_owner(self):
        self.client.close();hub=Hub();clip=Clipboard()
        self.client=subject.ClipboardSync(Account(hub.api('legacy'),'legacy'),Events(),self.base/'legacy',clipboard=clip)
        self.clients.append(self.client)
        flush()
        with patch.object(self.client,'_directory',side_effect=PermissionError(errno.EACCES,'PRIVATE_FIXTURE')):
            clip.text('legacy inline fallback fixture');self.client.change_debounce.stop();self.client._changed()
            until(lambda:self.client.view.get('error_code')=='CLIPBOARD_ACCESS_DENIED')
        self.assertFalse(self.client.retry.isActive());self.assertIsNone(self.client._pending)
        self.assertEqual(sum(method=='PUT' for method,path in hub.calls),0)
        self.assertEqual(clip.mimeData().text(),'legacy inline fallback fixture')


class SharedRetryOwnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture=shared_tests.SharedFilesTests('test_attach_at_sixty_percent_has_one_payload_and_one_progress_source')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
    def waiting(self):
        f=self.fixture;path=f.source();identity=f.copy(path);until(lambda:identity in f.peer.held)
        entry=f.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        _,_,respond=f.peer.held.pop(identity);respond(error=CloudError('synthetic transport interruption',503,'SERVICE_UNAVAILABLE'))
        until(lambda:f.client.retry.isActive() and f.client.file_jobs.retry.isActive())
        task=f.client._pending;task['_file_retry_at']=0
        return f,path,task
    def retained_with_later_retry(self):
        f,path,task=self.waiting();f.client.retry.stop();f.client._pending=None;f.client._local_retry=None
        f.client.file_jobs.retry.stop()
        later=dict(task);later.update(id=str(uuid4()),holders={str(uuid4()):{}},_file_phase='retrying',
            _file_retry_at=time.monotonic()+30)
        later.pop('_file_attempt',None);f.client.file_jobs.tasks[later['id']]=later
        return f,path,task
    def test_retained_stage_wait_owns_one_request_despite_later_retry_timer(self):
        f,path,task=self.retained_with_later_retry();original=f.peer.request;delayed=[]
        def request(method,path,callback,**options):
            if method=='POST' and path=='/v1/clipboard':
                def hold(result,error):delayed.append((callback,result,error))
                return original(method,path,hold,**options)
            return original(method,path,callback,**options)
        with patch.object(f.peer,'request',side_effect=request):
            before=len(f.peer.calls);f.client.file_jobs._retry();until(lambda:len(delayed)==1)
            QTest.qWait(1200);flush()
            self.assertEqual(sum(m=='POST' and p=='/v1/clipboard' for m,p,d in f.peer.calls[before:]),1)
            old_callback,old_result,_=delayed[0]
            old_callback(None,CloudError('synthetic interruption',503,'SERVICE_UNAVAILABLE'))
            task['_file_retry_at']=0;f.client.file_jobs._retry();until(lambda:len(delayed)==2)
            token=task['_file_attempt'];old_callback(old_result,None)
            self.assertIs(task['_file_attempt'],token);self.assertNotIn(task['id'],f.peer.held)
            callback,result,error=delayed[1];callback(result,error)
            until(lambda:task['id'] in f.peer.held);f.peer.finish(task['id'])
            until(lambda:task['_file_phase']=='ready')
            self.assertEqual(f.peer.objects[task['id']]['bytes'],path.read_bytes())
    def test_retained_prepare_wait_owns_one_worker_and_cancelled_attempt_can_recover(self):
        f,path,task=self.retained_with_later_retry();task.update(prepared=False,package=None)
        original=subject.package;started=[];release=threading.Event()
        def held(*args,**options):
            started.append(True);release.wait(3);return original(*args,**options)
        try:
            with patch.object(subject,'package',side_effect=held):
                f.client.file_jobs._retry();until(lambda:bool(started));QTest.qWait(1200);flush()
                self.assertEqual(len(started),1);self.assertEqual(len(f.client._jobs),1)
                task['_job'].cancelled.set();release.set()
                until(lambda:task['id'] in f.peer.held)
            self.assertEqual(len(started),2)
            f.peer.finish(task['id']);until(lambda:task['_file_phase']=='ready')
            self.assertEqual(f.peer.objects[task['id']]['bytes'],path.read_bytes())
        finally:release.set()
    def test_primary_prepare_retains_its_attempt_after_new_copy_owns_clipboard(self):
        f,path,task=self.retained_with_later_retry();task.update(prepared=False,package=None,value=('files',[str(path)]))
        f.client._local_retry=task;original=subject.package;started=[];release=threading.Event()
        def held(*args,**options):
            if args[0][0]=='files':started.append(True);release.wait(3)
            return original(*args,**options)
        try:
            with patch.object(subject,'package',side_effect=held):
                f.client._retry();until(lambda:bool(started))
                f.clip.text('newer independent clipboard');f.client.change_debounce.stop();f.client._changed()
                until(lambda:f.client.view['phase']=='synced' and f.client.view['id']!=task['id'])
                latest=f.client.view['id'];f.client.file_jobs._retry();QTest.qWait(1200);flush()
                self.assertEqual(len(started),1);self.assertIsNone(f.client._pending)
                release.set();until(lambda:task['id'] in f.peer.held)
            f.peer.finish(task['id']);until(lambda:task['_file_phase']=='ready')
            self.assertEqual(f.peer.head['id'],latest);self.assertEqual(f.client.view['id'],latest)
            self.assertEqual(f.clip.mimeData().text(),'newer independent clipboard')
            self.assertEqual(f.peer.objects[task['id']]['bytes'],path.read_bytes())
        finally:release.set()
    def test_retained_then_primary_timer_issues_no_duplicate_metadata_request(self):
        f,path,task=self.waiting();before=len(f.peer.calls);puts=len(f.peer.puts)
        f.client.file_jobs._retry();f.client._retry();until(lambda:len(f.peer.puts)>puts);flush()
        self.assertEqual(sum(m=='POST' and p=='/v1/clipboard' for m,p,d in f.peer.calls[before:]),0)
        self.assertEqual(len(f.peer.puts)-puts,1)
    def test_primary_then_retained_and_same_tick_both_keep_one_owner(self):
        f,path,task=self.waiting();before=len(f.peer.calls);puts=len(f.peer.puts)
        QTimer.singleShot(0,f.client._retry);QTimer.singleShot(0,f.client.file_jobs._retry)
        until(lambda:len(f.peer.puts)>puts);flush()
        self.assertEqual(sum(m=='POST' and p=='/v1/clipboard' for m,p,d in f.peer.calls[before:]),0)
        self.assertEqual(len(f.peer.puts)-puts,1)
    def test_retained_holder_takes_over_when_primary_owner_is_released(self):
        f,path,task=self.waiting();f.client.retry.stop();f.client._pending=None;f.client._local_retry=None
        before=len(f.peer.calls);puts=len(f.peer.puts);f.client.file_jobs._retry()
        until(lambda:len(f.peer.puts)>puts)
        self.assertEqual(sum(m=='POST' and p=='/v1/clipboard' for m,p,d in f.peer.calls[before:]),1)
        self.assertEqual(len(f.peer.puts)-puts,1);self.assertTrue(task['holders'])
        self.assertTrue(path.exists())
    def test_cancelled_matching_probe_releases_pins_without_cancelling_upload(self):
        f=self.fixture;source=f.source();identity=f.copy(source);until(lambda:identity in f.peer.held)
        other=f.base/'same-content-other-path.bin';other.write_bytes(source.read_bytes())
        original=f.client._work;started=threading.Event();release=threading.Event();jobs=[];matched=[]
        def held_work(action,callback,**options):
            def held(cancel):started.set();release.wait(2);return action(cancel)
            job=original(held,callback,**options);jobs.append(job);return job
        with patch.object(f.client,'_work',side_effect=held_work):
            self.assertTrue(f.client.file_jobs.claim(str(other),str(uuid4()),matched.append))
        until(started.is_set);task=f.client.file_jobs.tasks[identity]
        self.assertEqual(task['_probes'],1);jobs[0].cancelled.set();release.set()
        until(lambda:not f.client._jobs)
        self.assertEqual(task['_probes'],0);self.assertEqual(matched,[None])
        self.assertIn(identity,f.peer.held);self.assertTrue(f.client._pending is task)
    def test_old_held_prepare_retry_cannot_reclaim_newer_clipboard_owner(self):
        f=self.fixture;source=f.source();original=subject.package;started=threading.Event();release=threading.Event();attempts=[]
        def first_attempt(*args,**kwargs):
            value=args[0]
            if value[0]=='files' and os.path.normcase(os.path.abspath(value[1][0]))==os.path.normcase(os.path.abspath(source)):
                attempts.append(True)
                if len(attempts)==1:started.set();release.wait(3);raise OSError(errno.EAGAIN,'PRIVATE_FIXTURE')
            return original(*args,**kwargs)
        try:
            with patch.object(subject,'package',side_effect=first_attempt):
                identity=f.copy(source);until(started.is_set);entry=f.add(source)
                until(lambda:entry.get('shared_clipboard')==identity)
                task=f.client.file_jobs.tasks[identity]
                f.clip.text('newer current clipboard');f.client.change_debounce.stop();f.client._changed()
                until(lambda:f.client.view['phase']=='synced' and f.client.view['id']!=identity)
                new_id=f.client.view['id'];release.set();until(lambda:not f.client._jobs)
                self.assertIsNone(f.client._local_retry);self.assertIsNone(f.client._pending)
                self.assertFalse(f.client.retry.isActive());self.assertTrue(f.client.file_jobs.retry.isActive())
                task['_file_retry_at']=0;f.client.file_jobs._retry();until(lambda:identity in f.peer.held)
                f.peer.finish(identity);until(lambda:not entry['dirty'])
                self.assertEqual(f.client.view['id'],new_id);self.assertEqual(f.peer.head['id'],new_id)
                self.assertEqual(f.clip.mimeData().text(),'newer current clipboard');self.assertEqual(source.read_bytes(),f.peer.objects[identity]['bytes'])
        finally:release.set()


class ExistingErrorViewTests(unittest.TestCase):
    def test_missing_or_changed_multipart_source_keeps_original_and_shows_fixed_reason(self):
        for change in ('missing','changed'):
            with self.subTest(change=change):
                f=multipart_tests.MultipartTests('test_missing_or_resized_frozen_snapshot_is_terminal_not_reinitialized_forever');f.setUp()
                try:
                    request=f.peer.request;mutated=[False]
                    def mutate(method,path,callback,**options):
                        if path.endswith('/upload') and not mutated[0]:
                            def initialized(result,error):
                                mutated[0]=True;cache=Path(f.client._pending['package']['path'])
                                if change=='missing':cache.unlink()
                                else:
                                    with cache.open('ab') as stream:stream.write(b'changed')
                                callback(result,error)
                            return request(method,path,initialized,**options)
                        return request(method,path,callback,**options)
                    f.peer.request=mutate;source,mime=f.copy_file()
                    until(lambda:f.results==[False] and f.client._pending is None)
                    self.assertEqual(f.client.view.get('error_code'),'CLIPBOARD_SOURCE_CHANGED')
                    self.assertEqual(f.client.view.get('error_message'),'剪贴板源文件已改变，请重新复制。')
                    self.assertFalse(f.client.retry.isActive());self.assertEqual(f.peer.body_bytes,0)
                    self.assertEqual(f.peer.commit_count,0);self.assertIs(f.clip.mimeData(),mime)
                    self.assertEqual(source.read_bytes(),b'\0'*(2*1024*1024+17))
                    before=len(f.peer.calls)
                    for _ in range(3):f.clip.reannounce();f.client.change_debounce.stop();f.client._changed();f.client._retry()
                    flush();self.assertFalse(any(m in ('POST','PUT') for m,p,o in f.peer.calls[before:]))
                finally:f.tearDown()
    def test_short_safe_failure_uses_existing_label_and_preserves_click_to_close(self):
        f=preview_tests.PreviewTests('test_each_failed_retry_has_error_state_but_notice_remains_deduplicated');f.setUp()
        try:
            f.clip.text('synthetic native text');until(lambda:f.model.state.get('phase')=='synced')
            identity=f.model.state['id'];before=[row for row in f.hub.calls if row[0]!='GET']
            f.client._set_view(f.client.view['value'],'error',identity)
            error=subject.WorkerFailure('storage');f.client._worker_error_view(identity,error)
            f.manager.show_clipboard_preview();dialog=f.manager.clipboard_preview_dialog
            motion=dialog._popup_motion;motion.animation.setCurrentTime(motion.animation.duration());flush()
            self.assertEqual(dialog.status.text(),str(error));self.assertEqual(dialog.badge.status,'error')
            viewport=dialog.scroll.viewport()
            self.assertTrue(viewport.rect().contains(QRect(dialog.status.mapTo(viewport,QPoint()),dialog.status.size())))
            self.assertLess(motion.target_rect.height(),330)
            QTest.mouseClick(dialog.status,Qt.LeftButton)
            motion.animation.setCurrentTime(motion.animation.duration());flush();self.assertFalse(dialog.isVisible())
            self.assertEqual([row for row in f.hub.calls if row[0]!='GET'],before)
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
