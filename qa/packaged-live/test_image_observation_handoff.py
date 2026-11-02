"""Pure diagnostic handoff checks; no clipboard, window or EXE execution."""
from pathlib import Path
from types import SimpleNamespace
import ast
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import ci_packaged_windows_live as qa


class ImageObservationHandoffTests(unittest.TestCase):
    def invoke(self,observer,report):
        return qa.wait_original_png('ready-function',observer,'exe','sha','producer','original','expected',report)

    def test_success_preserves_original_wait_and_skips_observation(self):
        observer=Mock();report={'ok':False};probe=Mock()
        with patch.object(qa,'wait',return_value='original-result') as waiting,patch.dict(sys.modules,{'native_image_observation':SimpleNamespace(after_native_image_failure=probe)}):
            self.assertEqual(self.invoke(observer,report),'original-result')
        waiting.assert_called_once_with('ready-function',label='actual native original PNG committed by frozen client')
        probe.assert_not_called();self.assertEqual(report,{'ok':False})

    def test_original_assertion_is_preserved_with_diagnostic(self):
        original=AssertionError('original image sha mismatch');observer=Mock()
        probe=Mock(return_value={'observation_only':True,'normal_acceptance':False})
        report={'ok':False,'checks':['text'],'error':'original'}
        with patch.object(qa,'wait',side_effect=original),patch.dict(sys.modules,{'native_image_observation':SimpleNamespace(after_native_image_failure=probe)}):
            with self.assertRaises(AssertionError) as caught:self.invoke(observer,report)
        self.assertIs(caught.exception,original)
        probe.assert_called_once_with(observer,'exe','sha','producer','original','expected')
        self.assertFalse(report['ok']);self.assertEqual(report['checks'],['text']);self.assertEqual(report['error'],'original')
        self.assertFalse(report['native_image_failure_observation']['normal_acceptance'])

    def test_observer_error_cannot_replace_original_or_expose_message(self):
        original=AssertionError('original');probe=Mock(side_effect=RuntimeError('private diagnostic detail'))
        report={'ok':False}
        with patch.object(qa,'wait',side_effect=original),patch.dict(sys.modules,{'native_image_observation':SimpleNamespace(after_native_image_failure=probe)}):
            with self.assertRaises(AssertionError) as caught:self.invoke(Mock(),report)
        self.assertIs(caught.exception,original);self.assertNotIn('private diagnostic detail',repr(report))
        self.assertEqual(report['native_image_observation_error']['type'],'RuntimeError')

    def test_absent_observer_keeps_failure_and_does_not_invent_result(self):
        original=AssertionError('original');report={'ok':False}
        with patch.object(qa,'wait',side_effect=original):
            with self.assertRaises(AssertionError) as caught:self.invoke(None,report)
        self.assertIs(caught.exception,original);self.assertEqual(report,{'ok':False})

    def test_unrelated_error_does_not_start_image_read(self):
        original=ValueError('unrelated');probe=Mock();report={'ok':False}
        with patch.object(qa,'wait',side_effect=original),patch.dict(sys.modules,{'native_image_observation':SimpleNamespace(after_native_image_failure=probe)}):
            with self.assertRaises(ValueError) as caught:self.invoke(Mock(),report)
        self.assertIs(caught.exception,original);probe.assert_not_called()

    def test_actual_main_still_compares_original_sha_before_handoff(self):
        tree=ast.parse(Path(qa.__file__).read_text(encoding='utf-8'))
        main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        predicate=next(node for node in ast.walk(main) if isinstance(node,ast.FunctionDef) and node.name=='image_ready')
        comparisons=[node for node in ast.walk(predicate) if isinstance(node,ast.Compare)]
        self.assertTrue(any(isinstance(node.comparators[-1],ast.Name) and node.comparators[-1].id=='image_expected' for node in comparisons))
        calls=[node for node in ast.walk(main) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='wait_original_png']
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].args[0].id,'image_ready')


if __name__=='__main__':unittest.main()
