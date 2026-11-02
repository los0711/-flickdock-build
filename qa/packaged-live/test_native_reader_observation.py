"""Fake-only observer guard/lifecycle/privacy tests; no native clipboard/Job."""
import hashlib,importlib.util,json,os,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('native_reader_observation',Path(__file__).with_name('native_reader_observation.py'))
subject=importlib.util.module_from_spec(spec);spec.loader.exec_module(subject)

class FakeAPI:
    def __init__(self,*,sequence=77,count=0,owner=(99,321),busy=False):self.seq=sequence;self.formats=count;self.owned=owner;self.busy=busy;self.events=[]
    def sequence(self):self.events.append('sequence');return self.seq
    def open(self):self.events.append('open');return not self.busy
    def count(self):self.events.append('count');return self.formats
    def close(self):self.events.append('close')
    def owner(self):self.events.append('owner');return self.owned

class FakeProducer:
    def __init__(self,args,pid=321):self.args=args;self.pid=pid;self.exit=None
    def poll(self):return self.exit

class FakeOperation:
    def __init__(self,*,ready=True,reaped=True,result=None):
        self.done=threading.Event();self.ready=ready;self.reaped=reaped;self.process=SimpleNamespace(returncode=0 if ready else None);self.cancelled=[];self.started=False
        self.result=result or dict(status='ready',valid=True,known_empty=False,code='',sequence=77,sequence_before=77,sequence_after=77,value=('text',dict(text='synthetic private body',html='<b>synthetic</b>')),marker=b'private marker')
    def start(self):self.started=True;self.done.set() if self.ready else None
    def cancel(self,reason):self.cancelled.append(reason)

class ScopeTests(unittest.TestCase):
    def test_wrong_runner_refuses_before_any_fixture_io_or_native_factory(self):
        for changes in ({'GITHUB_ACTIONS':'false'},{'RUNNER_ENVIRONMENT':'self-hosted'},{'GITHUB_REPOSITORY':'other/repo'},{'RUNNER_OS':'Linux'}):
            environment=dict(GITHUB_ACTIONS='true',RUNNER_OS='Windows',RUNNER_ENVIRONMENT='github-hosted',GITHUB_REPOSITORY=subject.EXPECTED_REPOSITORY,RUNNER_TEMP='unused');environment.update(changes)
            with self.subTest(changes=changes),patch.dict(os.environ,environment,clear=True),patch.object(subject.sys,'platform','win32'),patch.object(Path,'mkdir',side_effect=AssertionError('guard bypass')):
                with self.assertRaisesRegex(RuntimeError,'HOSTED_RUNNER_REQUIRED'):subject.HostedNativeReaderObservation('unused','a'*32,producer_profile='unused',_api_factory=lambda:(_ for _ in ()).throw(AssertionError('native touched')))

class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-observer-test-');self.base=Path(self.temp.name)
        self.repo=self.base/'repo';(self.repo/'qa/packaged-live').mkdir(parents=True);(self.repo/'qa/packaged-live/native_editor.py').write_text('# owned fixture')
        self.exe=self.repo/'dist/FlickDock/FlickDock.exe';self.exe.parent.mkdir(parents=True);self.exe.write_bytes(b'synthetic-unexecuted-exe');self.sha=hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.normal=self.base/(subject.NORMAL_PREFIX+'synthetic')/'FlickDock';self.normal.mkdir(parents=True)
        self.nonce='a'*32;self.root=self.base/(subject.OBSERVER_PREFIX+self.nonce)
        self.patches=[patch.dict(os.environ,dict(GITHUB_ACTIONS='true',RUNNER_OS='Windows',RUNNER_ENVIRONMENT='github-hosted',GITHUB_REPOSITORY=subject.EXPECTED_REPOSITORY,RUNNER_TEMP=str(self.base)),clear=True),patch.object(subject.sys,'platform','win32'),patch.object(subject,'REPO',self.repo),patch.object(subject.tempfile,'gettempdir',return_value=str(self.base)),patch('subprocess.Popen',FakeProducer)]
        for item in self.patches:item.start()
        self.api=FakeAPI();self.operation=FakeOperation();self.commands=[]
        self.producer=FakeProducer([sys.executable,str(self.repo/'qa/packaged-live/native_editor.py'),str(self.normal.parent)])
    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def observer(self):
        def operation(command):self.commands.append(command);return self.operation
        return subject.HostedNativeReaderObservation(self.root,self.nonce,producer_profile=self.normal,_api_factory=lambda:self.api,_operation_factory=operation)
    def test_zero_sequence_and_independent_zero_count_are_facts_not_empty_proof(self):
        self.api.seq=0;observer=self.observer();row=observer.before_app_start()
        self.assertEqual(row['sequence_before'],0);self.assertEqual(row['format_count'],0);self.assertTrue(row['closed']);self.assertFalse(row['positive_sequence_proof']);self.assertNotIn('known_empty',row)
        self.assertEqual(self.api.events,['sequence','open','count','sequence','close']);self.assertFalse(self.commands)
    def test_busy_is_immediate_and_never_counts_or_closes_unopened_clipboard(self):
        self.api.busy=True;row=self.observer().before_app_start();self.assertFalse(row['opened']);self.assertIsNone(row['format_count']);self.assertNotIn('count',self.api.events);self.assertNotIn('close',self.api.events)
    def test_initial_count_failure_still_closes_without_private_diagnostics(self):
        self.api.count=lambda:(_ for _ in ()).throw(OSError(5,'private profile path'))
        row=self.observer().before_app_start();self.assertTrue(row['closed']);self.assertEqual(row['diagnostic']['errno'],5);self.assertNotIn('private',json.dumps(row))
    def test_missing_metadata_never_claims_equal_zero_sequences(self):
        self.api.sequence=lambda:(_ for _ in ()).throw(OSError(5,'private native failure'))
        row=self.observer().before_app_start();self.assertFalse(row['equal_sequence_fact']);self.assertFalse(row['positive_sequence_proof']);self.assertIsNone(row['format_count'])
    def test_initial_close_failure_is_explicit_not_success_or_unhandled_native_text(self):
        self.api.close=lambda:(_ for _ in ()).throw(OSError(5,'private path'))
        with self.assertRaisesRegex(RuntimeError,'OBSERVER_INITIAL_CLOSE_FAILED'):self.observer().before_app_start()
    def test_exact_owner_and_exe_produce_hashes_only_and_preserve_normal_failure(self):
        observer=self.observer();observer.before_app_start();row=observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertEqual(self.commands,[[str(self.exe),'--clipboard-reader']]);self.assertTrue(row['helper_reaped']);self.assertFalse(row['normal_acceptance']);self.assertTrue(row['original_failure_preserved'])
        self.assertEqual(row['content']['text_utf8_sha256'],hashlib.sha256(b'synthetic private body').hexdigest());encoded=json.dumps(row)
        for private in ('synthetic private body','private marker',str(self.exe),str(self.normal)):self.assertNotIn(private,encoded)
        self.assertTrue(observer.close()['helper_reaped'])
    def test_other_or_missing_owner_never_starts_helper_or_hashes_content(self):
        for owner in (None,(99,999)):
            with self.subTest(owner=owner):
                root=self.root.with_name(subject.OBSERVER_PREFIX+('b' if owner else 'c')*32);nonce=('b' if owner else 'c')*32
                observer=subject.HostedNativeReaderObservation(root,nonce,producer_profile=self.normal,_api_factory=lambda:self.api,_operation_factory=lambda command:(_ for _ in ()).throw(AssertionError('wrong owner launch')))
                observer.before_app_start();self.api.owned=owner;row=observer.after_native_text_failure(self.exe,self.sha,self.producer);self.assertFalse(row['helper_started']);self.assertNotIn('content',row)
    def test_foreign_producer_args_dead_object_or_wrong_hash_refuse_before_owner(self):
        observer=self.observer();observer.before_app_start();self.producer.args[-1]=str(self.base)
        with self.assertRaisesRegex(RuntimeError,'PRODUCER_ARGS_REQUIRED'):observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertNotIn('owner',self.api.events);self.assertFalse(self.commands)
    def test_dead_owned_producer_refuses_before_native_owner_read(self):
        observer=self.observer();observer.before_app_start();self.producer.exit=0
        with self.assertRaisesRegex(RuntimeError,'LIVE_OWNED_PRODUCER_REQUIRED'):observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertNotIn('owner',self.api.events);self.assertFalse(self.commands)
    def test_wrong_immutable_hash_refuses_before_native_owner_read(self):
        observer=self.observer();observer.before_app_start()
        with self.assertRaisesRegex(RuntimeError,'EXE_HASH_MISMATCH'):observer.after_native_text_failure(self.exe,'0'*64,self.producer)
        self.assertNotIn('owner',self.api.events);self.assertFalse(self.commands)
    def test_sequence_or_owner_change_suppresses_all_payload_hashes(self):
        observer=self.observer();observer.before_app_start()
        initial=self.api.owner;calls=[]
        def changed():calls.append(True);return initial() if len(calls)==1 else (100,321)
        self.api.owner=changed;row=observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertFalse(row['owner_still_verified']);self.assertNotIn('content',row)
    def test_changed_sequence_and_unknown_diagnostics_do_not_log_payload(self):
        observer=self.observer();observer.before_app_start();self.operation.result.update(status='unknown',valid=False,code='private path',diagnostic=dict(phase='private path',exception='private secret',errno=5))
        self.operation.result['sequence_after']=88;row=observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertFalse(row['sequence_still_verified']);self.assertNotIn('content',row);self.assertNotIn('diagnostic',row['result']);self.assertEqual(row['result']['code'],'UNRECOGNIZED_CODE');self.assertNotIn('private',json.dumps(row))
    def test_scope_is_once_only_and_marker_tamper_refuses_native(self):
        observer=self.observer();observer.before_app_start()
        with self.assertRaisesRegex(RuntimeError,'ALREADY_RECORDED'):observer.before_app_start()
        (self.root/'observer-owner.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'WRONG_OWNER'):observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertFalse(self.commands)
    def test_unreaped_owned_operation_remains_retained_without_acceptance(self):
        observer=self.observer();observer.before_app_start();self.operation.reaped=False
        row=observer.after_native_text_failure(self.exe,self.sha,self.producer);self.assertFalse(row['helper_reaped']);self.assertNotIn('content',row)
        self.assertTrue(observer.close()['retained_ownership']);self.assertTrue(observer.close()['retained_ownership'])
    def test_timeout_budget_is_bounded_and_retains_only_exact_owned_operation(self):
        waits=[]
        class NeverDone:
            def wait(self,seconds):waits.append(seconds);return False
            def is_set(self):return False
        self.operation=FakeOperation(ready=False,reaped=False);self.operation.done=NeverDone()
        observer=self.observer();observer.before_app_start();row=observer.after_native_text_failure(self.exe,self.sha,self.producer)
        self.assertEqual(waits,[subject.OBSERVATION_WAIT_SECONDS,subject.CLOSE_WAIT_SECONDS]);self.assertEqual(row['unavailable'],'HELPER_UNREAPED');self.assertIs(observer._owned_operation,self.operation)
        self.assertTrue(observer.close()['retained_ownership']);self.assertEqual(waits[-1],subject.CLOSE_WAIT_SECONDS)
    def test_malformed_error_fields_are_sanitized_without_serializing_payload(self):
        row=subject._result_summary(dict(status=['private body'],code=['private path'],diagnostic=dict(phase=['private path'],exception=['private data']),value='private data'))
        self.assertEqual(row['status'],'invalid');self.assertEqual(row['code'],'UNRECOGNIZED_CODE');self.assertNotIn('private',json.dumps(row))

if __name__=='__main__':unittest.main(verbosity=2)
