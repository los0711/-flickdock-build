"""Pure controller checks; never launches an EXE or accesses native APIs."""
import ast
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
from ci_packaged_windows_live import record_failed_native_copy,cleanup_owned_children
import ci_packaged_windows_live as qa


class ReaderObservationHandoffTests(unittest.TestCase):
    def test_actual_main_cleanup_closes_observer_even_if_internal_child_cleanup_raises(self):
        tree=ast.parse(Path(qa.__file__).read_text(encoding='utf-8'))
        main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        outer=next(node for node in main.body if isinstance(node,ast.Try) and node.finalbody)
        actual=next(node for node in outer.finalbody if isinstance(node,ast.Try) and any(
            isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='cleanup_owned_children'
            for call in ast.walk(node)))
        def broken(children):raise TypeError('controlled internal cleanup failure')
        for internal in (False,True):
            with self.subTest(internal=internal):
                observer=Mock();observer.close.return_value={'retained_ownership':False,'helper_reaped':True}
                child=Mock(pid=100,returncode=0);child.poll.return_value=None
                child.terminate.side_effect=OSError('controlled external process failure')
                report={'ok':False,'error':'original native copy timeout'}
                namespace=dict(qa.__dict__,children=[child],reader_observer=observer,report=report)
                if internal:namespace['cleanup_owned_children']=broken
                code=compile(ast.Module(body=[actual],type_ignores=[]),qa.__file__,'exec')
                if internal:
                    with self.assertRaisesRegex(TypeError,'controlled internal cleanup failure'):exec(code,namespace)
                else:
                    exec(code,namespace)
                    self.assertEqual(report['owned_child_cleanup_errors'],[{'pid':100,'stage':'terminate','error':'OSError'}])
                observer.close.assert_called_once()
                self.assertFalse(report['ok']);self.assertEqual(report['error'],'original native copy timeout')

    def test_owned_child_os_failure_does_not_stop_other_children_or_hide_failure(self):
        bad=Mock(pid=100,returncode=0);bad.poll.return_value=None;bad.terminate.side_effect=OSError('owned fixture failure')
        good=Mock(pid=101,returncode=0);good.poll.return_value=None
        failures=cleanup_owned_children([good,bad])
        self.assertEqual(failures,[{'pid':100,'stage':'terminate','error':'OSError'}])
        good.terminate.assert_called_once();bad.wait.assert_called_once();good.wait.assert_called_once()

    def test_unreaped_owned_child_is_explicit_failure_not_success(self):
        import subprocess
        child=Mock(pid=102,returncode=None);child.poll.return_value=None
        child.wait.side_effect=subprocess.TimeoutExpired('owned fixture',1)
        failures=cleanup_owned_children([child])
        child.kill.assert_called_once()
        self.assertTrue(any(row['stage']=='unreaped' for row in failures))
        self.assertTrue(any(row['stage']=='reap' for row in failures))

    def test_observation_does_not_change_failed_normal_result_or_export_text(self):
        observer=Mock();observer.after_native_text_failure.return_value={'observation_only':True,'normal_acceptance':False,'helper_reaped':True}
        report={'ok':False,'error':'original copy timeout','checks':['overview']}
        record_failed_native_copy(observer,[{'label':'02-native-text-copy'}],'synthetic private text','owned-exe','digest','owned-producer',report)
        self.assertFalse(report['ok']);self.assertEqual(report['error'],'original copy timeout');self.assertEqual(report['checks'],['overview'])
        row=report['native_reader_failure_observation']
        self.assertEqual(row['expected_text_utf8_sha256'],hashlib.sha256(b'synthetic private text').hexdigest())
        self.assertNotIn('synthetic private text',repr(row));self.assertFalse(row['normal_acceptance'])

    def test_only_failed_first_copy_is_observed(self):
        observer=Mock();report={'ok':False}
        for segments in ([],[{'label':'01-normal-overview-open'}],[{'label':'02-native-text-copy'},{'label':'03-settings-open'}]):
            record_failed_native_copy(observer,segments,'synthetic','owned-exe','digest','owned-producer',report)
        observer.after_native_text_failure.assert_not_called();self.assertEqual(report,{'ok':False})

    def test_internal_observer_error_is_visible_and_cannot_invent_a_result(self):
        observer=Mock();observer.after_native_text_failure.side_effect=RuntimeError('controlled observer failure')
        report={'ok':False,'error':'original copy timeout'}
        with self.assertRaisesRegex(RuntimeError,'controlled observer failure'):
            record_failed_native_copy(observer,[{'label':'02-native-text-copy'}],'synthetic','owned-exe','digest','owned-producer',report)
        self.assertEqual(report,{'ok':False,'error':'original copy timeout'})


if __name__=='__main__':unittest.main()
