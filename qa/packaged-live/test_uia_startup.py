"""Pure/synthetic-child controller boundaries; never loads native UIA/COM."""
import importlib.util,io,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('uia_startup_qa',Path(__file__).with_name('ci_packaged_windows_live.py'))
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

SYNTHETIC=r'''
import json,os,sys,time
mode=sys.argv[1]
print(json.dumps(dict(event='uia_stage',stage='backend_import_begin')),flush=True)
if mode in ('list_packet','null_packet'):
 print(json.dumps([] if mode=='list_packet' else None),flush=True)
 time.sleep(5)
if mode=='exit':raise SystemExit(7)
time.sleep(.12 if mode=='cold' else .01)
if mode=='never_ready':time.sleep(5)
print(json.dumps(dict(event='uia_ready',protocol=True if mode=='bad_protocol' else 1,pid=os.getpid()+1 if mode=='wrong_pid' else os.getpid())),flush=True)
for line in sys.stdin:
 command=json.loads(line)
 print(json.dumps(dict(event='uia_stage',stage='request_begin',request_id=command['id'],operation=command['operation'])),flush=True)
 if mode=='request_hang':time.sleep(5)
 print(json.dumps(dict(id=command['id'],result=[])),flush=True)
'''

class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fd-owned-uia-startup-');self.base=Path(self.temp.name)
        self.child=self.base/'synthetic.py';self.child.write_text(SYNTHETIC,encoding='utf8');self.children=[]
        self.popen=subprocess.Popen;self.drivers=[]
    def tearDown(self):
        for driver in self.drivers:driver.close()
        for child in self.children:
            if child.poll() is None:child.kill()
            child.wait(2)
            for stream in (child.stdin,child.stdout):
                if stream is not None and not stream.closed:stream.close()
        self.temp.cleanup()
    def spawn(self,mode):
        def start(args,**kwargs):
            child=self.popen([sys.executable,str(self.child),mode],**kwargs);self.children.append(child);return child
        return start
    def create(self,mode,timeout=.5):
        with patch.object(qa,'require_hosted'),patch.object(qa.subprocess,'Popen',side_effect=self.spawn(mode)),patch.object(qa,'UIA_WORKER_READY_SECONDS',timeout,create=True):
            driver=qa.Driver(101,202,self.base);self.drivers.append(driver);return driver
    def test_cold_start_does_not_spend_the_first_request_deadline(self):
        driver=self.create('cold')
        try:self.assertEqual(driver._rpc('windows',seconds=.08,pid=202),[])
        finally:driver.close()
    def test_ready_then_hung_request_keeps_its_original_deadline(self):
        driver=self.create('request_hang');started=time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError,'HARNESS_AUTOMATION_REQUEST_TIMEOUT'):
                driver._rpc('windows',seconds=.04,pid=202)
            self.assertLess(time.monotonic()-started,.8)
            self.assertTrue(driver._provider_failed)
        finally:driver.close()
    def test_never_ready_is_startup_failure_and_reaps_owned_child(self):
        with self.assertRaisesRegex(TimeoutError,'HARNESS_AUTOMATION_STARTUP_TIMEOUT'):
            self.create('never_ready',timeout=.06)
        self.assertIsNotNone(self.children[0].poll())
    def test_startup_exit_is_not_reported_as_unresponsive_application(self):
        with self.assertRaisesRegex(RuntimeError,'HARNESS_AUTOMATION_STARTUP_FAILED'):
            self.create('exit')
        self.assertIsNotNone(self.children[0].poll())
    def test_host_guard_precedes_all_process_or_file_access(self):
        with patch.object(qa,'require_hosted',side_effect=RuntimeError('guard')),patch.object(qa.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError,'guard'):qa.Driver(101,202,self.base)
        spawn.assert_not_called();self.assertFalse((self.base/'uia-worker-private.stderr').exists())
    def test_default_rpc_budget_remains_twelve_seconds(self):
        import inspect
        self.assertEqual(inspect.signature(qa.Driver._rpc).parameters['seconds'].default,12)
    def test_wrong_ready_owner_is_rejected_and_child_reaped(self):
        for mode in ('wrong_pid','bad_protocol'):
            with self.subTest(mode=mode),self.assertRaisesRegex(RuntimeError,'HARNESS_AUTOMATION_PROTOCOL_ERROR'):
                self.create(mode)
            self.assertIsNotNone(self.children[-1].poll())
    def test_json_list_before_ready_is_explicit_protocol_failure(self):
        with self.assertRaisesRegex(RuntimeError,'HARNESS_AUTOMATION_PROTOCOL_ERROR'):
            self.create('list_packet')
        self.assertIsNotNone(self.children[-1].poll())
    def test_json_null_before_ready_is_explicit_protocol_failure(self):
        with self.assertRaisesRegex(RuntimeError,'HARNESS_AUTOMATION_PROTOCOL_ERROR'):
            self.create('null_packet')
        self.assertIsNotNone(self.children[-1].poll())
    def test_stage_trace_has_no_payload_and_keeps_progress_phase_clock_separate(self):
        driver=qa.Driver.__new__(qa.Driver);driver.base=self.base;driver._worker_started=time.monotonic()
        progress=qa.Progress.__new__(qa.Progress);progress.lock=threading.Lock();progress.path=self.base/'progress.jsonl'
        progress.phase='owned-phase';progress.started=time.monotonic()
        with patch.object(qa,'_PROGRESS',progress),patch.object(qa.sys,'stdout',io.StringIO()):
            self.assertTrue(driver._observe_stage(dict(event='uia_stage',stage='request_begin',request_id=1,operation='windows',
                                                      name='private title',path='private path',values='private content')))
        raw=(self.base/'uia-worker-stages.jsonl').read_text();self.assertNotIn('private',raw)
        row=json.loads((self.base/'progress.jsonl').read_text())
        self.assertEqual(row['phase'],'owned-phase');self.assertIn('elapsed_s',row);self.assertIn('worker_elapsed_s',row)
        self.assertEqual(row['request_id'],1)
        with self.assertRaisesRegex(RuntimeError,'PROTOCOL_ERROR'):
            driver._observe_stage(dict(event='uia_stage',stage='private injected stage'))
    def test_non_string_stage_fields_are_explicit_protocol_failures(self):
        driver=qa.Driver.__new__(qa.Driver);driver.base=self.base
        for packet in (dict(event='uia_stage',stage=[]),dict(event='uia_stage',stage={}),
                       dict(event='uia_stage',stage='request_begin',operation=[]),
                       dict(event='uia_stage',stage='request_begin',operation={})):
            with self.subTest(field_types={k:type(v).__name__ for k,v in packet.items()}),self.assertRaisesRegex(RuntimeError,'HARNESS_AUTOMATION_PROTOCOL_ERROR'):
                driver._observe_stage(packet)
            self.assertTrue(driver._provider_failed)
        self.assertFalse((self.base/'uia-worker-stages.jsonl').exists())

if __name__=='__main__':unittest.main()
