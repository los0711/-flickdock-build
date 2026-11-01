"""Portable QA-controller tests. No desktop, UIA, clipboard or credential calls."""
import importlib.util,io,queue,time,unittest
from pathlib import Path

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

if __name__=='__main__':unittest.main()
