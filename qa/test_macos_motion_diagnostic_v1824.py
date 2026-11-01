"""Portable launcher/guard fixtures; no Qt imports, app build or OS input."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

SPEC=importlib.util.spec_from_file_location('intel_motion_diagnostic',Path(__file__).with_name('macos_motion_diagnostic_v1824.py'))
qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)


class DiagnosticTests(unittest.TestCase):
    def test_baseline_command_preserves_exact_nine_module_order_single_process_and_gc_policy(self):
        expected=['test_ui_followup_v1823','test_cached_surface_v1823','test_stack_spread_v1823',
                  'test_input_motion_v1823','test_content_entrance_v1823','test_transfer_drop_feedback_v1823',
                  'test_transfer_item_motion_v1823','test_popup_motion_v1823','test_drop_target_motion_v1823']
        values=qa.command('baseline',Path('unused'))
        self.assertEqual(values,[qa.sys.executable,'-X','faulthandler','-u','-m','unittest',*expected,'-v'])
        self.assertEqual(qa.EXPECTED_TESTS,117)
        with self.assertRaisesRegex(ValueError,'original GC'):qa.command('baseline',Path('unused'),True)

    def test_gc_treatment_is_explicit_and_never_a_baseline_command(self):
        values=qa.command('gc-trace',Path('diagnostics'),True)
        self.assertIn('--worker',values);self.assertIn('--collect-after-test',values)
        self.assertNotIn('--collect-after-test',qa.command('gc-trace',Path('diagnostics')))

    def test_host_guard_refuses_other_platform_architecture_or_non_ephemeral_runner(self):
        markers={'GITHUB_ACTIONS':'true','RUNNER_OS':'macOS','RUNNER_ENVIRONMENT':'github-hosted','RUNNER_ARCH':'X64'}
        with patch.object(qa.sys,'platform','darwin'),patch.object(qa.platform,'machine',return_value='x86_64'),patch.dict(qa.os.environ,markers):
            qa.require_hosted_intel()
            for key in markers:
                with self.subTest(key=key),patch.dict(qa.os.environ,{key:'no'}):
                    with self.assertRaises(RuntimeError):qa.require_hosted_intel()
            with patch.object(qa.platform,'machine',return_value='arm64'):
                with self.assertRaises(RuntimeError):qa.require_hosted_intel()
        with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,markers):
            with self.assertRaises(RuntimeError):qa.require_hosted_intel()

    def test_worker_rejects_changed_test_count_without_running_or_collecting(self):
        suite=Mock();suite.countTestCases.return_value=116
        with tempfile.TemporaryDirectory() as temp,patch.object(qa,'require_hosted_intel'),\
             patch.object(qa.unittest.defaultTestLoader,'loadTestsFromNames',return_value=suite) as loader,\
             patch.object(qa.gc,'collect') as collect:
            with self.assertRaisesRegex(AssertionError,'exactly 117'):qa.worker(Path(temp))
            loader.assert_called_once_with(qa.MODULES);collect.assert_not_called()

    def run_fixture(self,code,*,log='Ran 117 tests in 1.0s\n\nOK\n',mode='baseline'):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);output=Path(temp.name)/'result'
        captured={};child=types.SimpleNamespace(pid=27218,wait=Mock(return_value=code),poll=Mock(return_value=code),kill=Mock())
        def launch(args,**kwargs):
            captured.update(args=args,**kwargs);kwargs['stdout'].write(log.encode());kwargs['stdout'].flush();return child
        with patch.object(qa,'require_hosted_intel'),patch.object(qa,'inventory',return_value={'expected_test_count':117}),\
             patch.object(qa.subprocess,'Popen',side_effect=launch),patch.object(qa,'crash_reports',return_value=[]),\
             patch.object(qa.time,'sleep'),patch.object(qa.sys,'stdout',io.StringIO()):
            result=qa.run(output,mode=mode)
        return result,json.loads((output/'diagnostic-result.json').read_text()),captured,child

    def test_successful_baseline_retains_raw_log_real_exit_and_isolated_profile_cleanup(self):
        result,report,captured,_=self.run_fixture(0)
        self.assertEqual(result,0);self.assertTrue(report['ok']);self.assertTrue(report['completed_117'])
        self.assertFalse(report['passing_variant_replaces_failed_baseline'])
        self.assertEqual(captured['cwd'],qa.ROOT);self.assertEqual(captured['env']['QT_QPA_PLATFORM'],'offscreen')
        self.assertEqual(captured['env']['PYTHONFAULTHANDLER'],'1');self.assertTrue(report['isolated_profile_removed'])
        self.assertFalse(Path(captured['env']['APPDATA']).exists())

    def test_sigsegv_remains_failure_with_original_raw_signal_not_a_passed_variant(self):
        result,report,_,_=self.run_fixture(-11,log='test_one (module.Case.test_one) ... Fatal Python error: Segmentation fault\n')
        self.assertEqual(result,1);self.assertEqual(report['returncode'],-11)
        self.assertTrue(report['segmentation_signal']);self.assertFalse(report['ok']);self.assertFalse(report['completed_117'])

    def test_exit_zero_without_complete117_cannot_mark_the_diagnostic_passed(self):
        result,report,_,_=self.run_fixture(0,log='Ran 116 tests in 1.0s\n\nOK\n')
        self.assertEqual(result,1);self.assertFalse(report['ok'])

    def test_gc_mode_retains_diagnostic_treatment_label_even_if_it_passes(self):
        result,report,captured,_=self.run_fixture(0,mode='gc-trace')
        self.assertEqual(result,0);self.assertEqual(report['mode'],'gc-trace')
        self.assertTrue(report['diagnostic_only']);self.assertFalse(report['passing_variant_replaces_failed_baseline'])
        self.assertIn('--worker',captured['args'])

    def test_native_crash_export_is_new_python_exact_pid_only(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);source=home/'Library'/'Logs'/'DiagnosticReports';source.mkdir(parents=True)
            output=home/'output';output.mkdir();started=qa.time.time()-1
            files={'Python-actual.ips':'{"pid": 27218}',
                   'Python-other.ips':'{"pid": 88}',
                   'Other-actual.ips':'{"pid": 27218}',
                   'Python-old.ips':'{"pid": 27218}'}
            for name,text in files.items():(source/name).write_text(text)
            qa.os.utime(source/'Python-old.ips',(started-100,started-100))
            with patch.object(qa.Path,'home',return_value=home):copied=qa.crash_reports(output,27218,started)
            self.assertEqual(copied,['Python-actual.ips'])
            self.assertEqual([p.name for p in (output/'native-crash-reports').iterdir()],copied)


if __name__=='__main__':unittest.main()
