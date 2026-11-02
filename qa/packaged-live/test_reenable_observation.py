"""Portable scheduling boundaries of the real native-observation QA closure."""
import ast,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock

class ReenableObservationTests(unittest.TestCase):
    def execute(self,times,*,end_wrong=False):
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/'original.bin';original.write_bytes(b'original')
            other=Path(folder)/'other.bin';other.write_bytes(b'other')
            controller=Path(__file__).with_name('ci_packaged_windows_live.py')
            node=next(n for n in ast.walk(ast.parse(controller.read_text(encoding='utf8')))
                      if isinstance(n,ast.FunctionDef) and n.name=='reenable_keeps_original')
            ticks=iter(times);samples=[dict(files=[str(original)]),dict(files=[str(other if end_wrong else original)])]
            driver=types.SimpleNamespace(setting=Mock(),inspect=Mock(side_effect=samples))
            cloud=types.SimpleNamespace(stats=Mock(side_effect=[{'rows':[]},{'rows':[dict(method='GET',path='/v1/clipboard',status=200,finished=1)]}]))
            namespace=dict(driver=driver,cloud=cloud,wait=lambda action,**_:action(),
                           time=types.SimpleNamespace(monotonic=lambda:next(ticks),sleep=Mock()),Path=Path,refs=[str(original)])
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(controller),'exec'),namespace)
            result=namespace['reenable_keeps_original']()
            return result,driver.inspect.call_count

    def test_preempted_entire_observation_window_cannot_pass_on_end_sample_alone(self):
        with self.assertRaisesRegex(AssertionError,'HARNESS_REENABLE_OBSERVATION'):
            self.execute((0.,2.))

    def test_late_native_clobber_in_final_sample_cannot_pass(self):
        with self.assertRaisesRegex(AssertionError,'older cloud head|native original|native payload'):
            self.execute((0.,.1,2.),end_wrong=True)

    def test_success_requires_both_window_and_end_native_samples(self):
        result,count=self.execute((0.,.1,2.))
        self.assertEqual(count,2);self.assertEqual(result['observed_samples'],2)
        self.assertTrue(result['native_original_preserved'])

if __name__=='__main__':unittest.main()
