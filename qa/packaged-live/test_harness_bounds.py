"""Portable QA-controller tests. No desktop, UIA, clipboard or credential calls."""
import importlib.util,io,queue,time,unittest,tempfile
from pathlib import Path
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('frozen_live_qa',Path(__file__).with_name('ci_packaged_windows_live.py'))
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

class FakeWorker:
    def __init__(self):self.stdin=io.StringIO();self.stdout=io.StringIO();self.terminated=0;self.killed=0;self.exited=False
    def poll(self):return 0 if self.exited else None
    def terminate(self):self.terminated+=1;self.exited=True
    def kill(self):self.killed+=1;self.exited=True
    def wait(self,timeout):return 0

class BoundTests(unittest.TestCase):
    def controller(self):
        driver=qa.Driver.__new__(qa.Driver);driver._provider_failed=False;driver._rpc_id=0
        driver._responses=queue.Queue();driver._worker=FakeWorker()
        return driver
    def test_unresponsive_provider_is_bounded_and_only_own_worker_is_terminated(self):
        driver=self.controller();started=time.monotonic()
        with self.assertRaisesRegex(TimeoutError,'HARNESS_AUTOMATION_REQUEST_TIMEOUT'):
            driver._rpc('find',seconds=.03,name='synthetic control')
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(driver._worker.terminated,1);self.assertEqual(driver._worker.killed,0)
        self.assertTrue(driver._provider_failed)
        # Failure diagnostics cannot perform a second unbounded UIA query.
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'refusing repeated diagnostic COM calls'):driver.tree()
        self.assertLess(time.monotonic()-started,.5)
    def test_success_and_stale_responses_are_correlated_to_current_request(self):
        driver=self.controller();driver._responses.put(dict(id=0,result='obsolete'))
        driver._responses.put(dict(id=1,result=[10,20,30,40]))
        self.assertEqual(driver._rpc('find',seconds=.05,name='synthetic'),[10,20,30,40])
        self.assertEqual(driver._worker.terminated,0)
    def test_provider_error_is_reported_as_automation_failure_not_application_freeze(self):
        driver=self.controller();driver._responses.put(dict(id=1,error='LookupError: synthetic control missing'))
        with self.assertRaisesRegex(RuntimeError,'UIA click: LookupError'):driver._rpc('click',seconds=.05,name='synthetic')
        self.assertFalse(driver._provider_failed);self.assertEqual(driver._worker.terminated,0)

class PointerOwnershipTests(unittest.TestCase):
    def test_nonactivating_app_mouse_target_works_but_producer_requires_foreground(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=qa._UIABackend.__new__(qa._UIABackend);backend.base=Path(directory)
            backend.pid=101;backend.producer_pid=202;events=[]
            backend._producer_topmost=lambda value:events.append(('producer_topmost',value))
            backend._assert_pointer_owner=lambda point,pid:events.append(('pointer_verified',pid))
            backend._input_api=lambda:SimpleNamespace(GetForegroundWindow=lambda:77)
            backend._window_pid=lambda _:202
            def foreground(pid):
                if pid!=202:raise RuntimeError('wrong keyboard foreground')
            backend._assert_foreground=foreground
            control=SimpleNamespace(rectangle=lambda:SimpleNamespace(left=10,top=20,right=30,bottom=40),
                top_level_parent=lambda:SimpleNamespace(set_focus=lambda:events.append(('source_focus',True))))
            self.assertEqual(backend._prepare_control(control,101),(20,30))
            self.assertNotIn(('source_focus',True),events)
            self.assertEqual(backend._prepare_control(control,202),(20,30))
            self.assertIn(('source_focus',True),events)
            backend._assert_foreground=lambda _:(_ for _ in ()).throw(RuntimeError('wrong source keyboard owner'))
            with self.assertRaisesRegex(RuntimeError,'wrong source keyboard owner'):backend._prepare_control(control,202)

    def test_occluded_pointer_target_is_rejected_even_for_nonactivating_app(self):
        backend=qa._UIABackend.__new__(qa._UIABackend);backend.pid=101;backend.producer_pid=202
        backend._producer_topmost=lambda _:None
        backend._assert_pointer_owner=lambda point,pid:(_ for _ in ()).throw(RuntimeError('occluded pointer'))
        control=SimpleNamespace(rectangle=lambda:SimpleNamespace(left=10,top=20,right=30,bottom=40))
        with self.assertRaisesRegex(RuntimeError,'occluded pointer'):backend._prepare_control(control,101)

if __name__=='__main__':unittest.main()
