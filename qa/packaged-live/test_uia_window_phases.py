"""Pure phase/order/error checks, no Desktop/COM/Qt constructor calls."""
import importlib.util,inspect,io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('fine_uia_qa',Path(__file__).with_name('ci_packaged_windows_live.py'))
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

class FineWindowTests(unittest.TestCase):
    def backend(self,visibility):
        events=[]
        class Window:
            def __init__(self,index,visible):self.index=index;self.visible=visible
            def is_visible(self):events.append(('visible',self.index));return self.visible
            @property
            def element_info(self):raise AssertionError('NO_PROVIDER_NAMES_OR_PATHS')
        windows=[Window(i+1,v) for i,v in enumerate(visibility)]
        class Desktop:
            def windows(self,**options):events.append(('enumerate',options));return windows
        value=qa._UIABackend.__new__(qa._UIABackend);value.pid=101;value._request_id=7;value.desktop=Desktop()
        return value,events,windows
    def test_each_visibility_is_after_desktop_return_with_original_order_and_filter(self):
        value,events,windows=self.backend([True,False,True]);original=qa._uia_worker_stage;stdout=io.StringIO()
        def record(stage,**fields):events.append(('stage',stage));original(stage,**fields)
        with patch.object(qa,'_uia_worker_stage',side_effect=record),patch.object(qa.sys,'stdout',stdout):result=value.windows(202)
        self.assertEqual(result,[windows[0],windows[2]])
        self.assertEqual(events,[('stage','windows_findall_begin'),('enumerate',{'process':202}),('stage','windows_findall_complete'),
            ('stage','windows_visibility_begin'),('visible',1),('stage','windows_visibility_complete'),
            ('stage','windows_visibility_begin'),('visible',2),('stage','windows_visibility_complete'),
            ('stage','windows_visibility_begin'),('visible',3),('stage','windows_visibility_complete')])
        packets=[json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(all(set(p)=={'event','stage','request_id'} and p['request_id']==7 for p in packets))
    def test_empty_owned_window_list_does_not_query_visibility(self):
        value,events,_=self.backend([])
        with patch.object(qa.sys,'stdout',io.StringIO()):self.assertEqual(value.windows(),[])
        self.assertEqual(events,[('enumerate',{'process':101})])
    def test_desktop_error_is_original_and_not_mislabeled_as_returned(self):
        value,events,_=self.backend([]);error=OSError('private title and path')
        def fail(**options):raise error
        value.desktop.windows=fail;stdout=io.StringIO()
        with patch.object(qa.sys,'stdout',stdout),self.assertRaises(OSError) as seen:value.windows()
        self.assertIs(seen.exception,error)
        self.assertEqual([json.loads(line)['stage'] for line in stdout.getvalue().splitlines()],['windows_findall_begin'])
        self.assertNotIn('private',stdout.getvalue())
    def test_visibility_error_keeps_original_and_ends_at_its_begin(self):
        value,events,windows=self.backend([True]);error=ValueError('private document title')
        def fail():raise error
        windows[0].is_visible=fail;stdout=io.StringIO()
        with patch.object(qa.sys,'stdout',stdout),self.assertRaises(ValueError) as seen:value.windows()
        self.assertIs(seen.exception,error)
        self.assertEqual([json.loads(line)['stage'] for line in stdout.getvalue().splitlines()],
                         ['windows_findall_begin','windows_findall_complete','windows_visibility_begin'])
        self.assertNotIn('private',stdout.getvalue())
    def test_controller_accepts_only_safe_fixed_new_stages(self):
        with tempfile.TemporaryDirectory(prefix='owned-fine-trace-') as directory:
            driver=qa.Driver.__new__(qa.Driver);driver.base=Path(directory);driver._worker_started=time.monotonic()
            for stage in ('windows_visibility_begin','windows_visibility_complete'):
                self.assertIn(stage,qa.UIA_WORKER_STAGES)
                self.assertTrue(driver._observe_stage(dict(event='uia_stage',stage=stage,request_id=7,name='private title',path='private path')))
            output=(driver.base/'uia-worker-stages.jsonl').read_text()
            self.assertNotIn('private',output)
            for line in output.splitlines():self.assertEqual(set(json.loads(line)),{'stage','elapsed_s','request_id'})
    def test_deadlines_and_lookup_strategy_are_unchanged(self):
        self.assertEqual(inspect.signature(qa.Driver._rpc).parameters['seconds'].default,12)
        self.assertEqual(qa.UIA_WORKER_READY_SECONDS,12)
        source=inspect.getsource(qa._UIABackend.windows)
        self.assertEqual(source.count('self.desktop.windows('),1);self.assertNotIn('EnumWindows',source)

if __name__=='__main__':unittest.main(verbosity=2)
