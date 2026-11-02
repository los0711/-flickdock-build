"""Portable QA-only observations: no processes, desktop or credentials accessed."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SPEC=importlib.util.spec_from_file_location('cleanup_qa',Path(__file__).with_name('ci_packaged_windows_live.py'))
qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)


class CleanupDiagnosticsTests(unittest.TestCase):
    def process(self,pid,code=None):
        return SimpleNamespace(pid=pid,returncode=code,poll=Mock(return_value=code),
            terminate=Mock(),kill=Mock(),wait=Mock(),args='private argv token must never appear')

    def test_five_existing_roles_only_poll_without_new_cleanup_actions(self):
        procs=[self.process(n,code) for n,code in enumerate((None,0,1,9,-1),101)]
        report={'ok':False,'error':'existing primary failure','profile_cleanup_error':'PermissionError'}
        qa.retain_cleanup_state(report,'after',application=procs[0],producer=procs[1],
            driver=SimpleNamespace(_worker=procs[2]),cloud=SimpleNamespace(gateway=procs[3],server=procs[4]))
        rows=report['cleanup_diagnostics']['after']
        self.assertEqual([row['role'] for row in rows],['application','producer','UIA','gateway','qa-server'])
        self.assertEqual([row['pid'] for row in rows],list(range(101,106)))
        self.assertEqual([row['poll'] for row in rows],[None,0,1,9,-1])
        self.assertTrue(all(row['present'] and row['poll_observed'] and row['descendants']=='not_observed' for row in rows))
        for process in procs:
            process.poll.assert_called_once_with();process.terminate.assert_not_called()
            process.kill.assert_not_called();process.wait.assert_not_called()
        self.assertFalse(report['ok']);self.assertEqual(report['error'],'existing primary failure')
        self.assertEqual(report['profile_cleanup_error'],'PermissionError')
        self.assertNotIn('private argv',json.dumps(report))

    def test_missing_roles_are_not_claimed_to_have_exited(self):
        report={};qa.retain_cleanup_state(report,'before')
        for row in report['cleanup_diagnostics']['before']:
            self.assertFalse(row['present']);self.assertIsNone(row['poll']);self.assertIsNone(row['returncode'])
            self.assertFalse(row['poll_observed']);self.assertEqual(row['descendants'],'not_observed')

    def test_only_fixed_path_categories_and_numeric_error_fields_leave_process(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            cases={
                'FlickDock/clipboard-cache/secret-original-name':'clipboard-cache',
                'FlickDock/cloud-shelves/private-account-id/state.json':'cloud-shelves',
                'files/private-user-file.txt':'synthetic-originals',
                'native-paste-secret/totally-private.bin':'synthetic-paste',
                'local-key.pem':'local-service','application.stderr':'runner-control',
                'FlickDock/settings.json':'runner-control','unrecognized-secret.txt':'other'}
            for relative,category in cases.items():
                error=PermissionError(13,'PRIVATE CONTENT AND CREDENTIAL',str(base/relative));error.winerror=32
                value=qa.cleanup_failure_details(error,base)
                self.assertEqual(value,dict(category=category,errno=13,winerror=32))
                encoded=json.dumps(value)
                self.assertNotIn(str(base),encoded);self.assertNotIn(relative,encoded)
                self.assertNotIn('PRIVATE',encoded);self.assertNotIn('secret',encoded)
            error=PermissionError(13,'private',str(base.parent/'private-outside-file'))
            self.assertEqual(qa.cleanup_failure_details(error,base)['category'],'outside')

    def test_no_filename_relative_and_normalized_escape_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            self.assertEqual(qa.cleanup_failure_details(PermissionError('secret'),base),{'category':'unknown'})
            error=PermissionError(13,'secret','relative/private-file')
            self.assertEqual(qa.cleanup_failure_details(error,base)['category'],'unknown')
            error=PermissionError(13,'secret',str(base/'files'/'..'/'..'/'private'))
            self.assertEqual(qa.cleanup_failure_details(error,base)['category'],'outside')

    def test_hostile_scalar_values_are_rejected_without_repr_or_str(self):
        class Private:
            def __str__(self):raise AssertionError('must not stringify')
            def __repr__(self):raise AssertionError('must not repr')
        process=SimpleNamespace(pid=Private(),returncode=Private(),poll=lambda:Private())
        error=SimpleNamespace(errno='secret token',winerror=Private(),filename=None)
        report={'ok':False,'error':'primary'}
        qa.retain_cleanup_state(report,'after',application=process,error=error)
        row=report['cleanup_diagnostics']['after'][0]
        self.assertIsNone(row['pid']);self.assertIsNone(row['poll']);self.assertIsNone(row['returncode'])
        self.assertFalse(row['poll_observed']);self.assertEqual(report['cleanup_diagnostics']['profile_failure'],{'category':'unknown'})
        self.assertFalse(report['ok']);self.assertEqual(report['error'],'primary')

    def test_observation_failure_is_fixed_unavailable_and_preserves_primary(self):
        class Broken:
            pid=1;returncode=None
            def poll(self):raise RuntimeError('secret path and token')
        report={'ok':False,'error':'primary'}
        qa.retain_cleanup_state(report,'after',application=Broken())
        self.assertEqual(report['cleanup_diagnostics']['after'][0]['status'],'unavailable')
        self.assertFalse(report['ok']);self.assertEqual(report['error'],'primary')
        self.assertNotIn('secret',json.dumps(report))
        class BrokenReport(dict):
            def setdefault(self,*args):raise RuntimeError('private diagnostic sink')
        report=BrokenReport(ok=False,error='primary')
        qa.retain_cleanup_state(report,'after',application=Broken())
        self.assertEqual(dict(report),dict(ok=False,error='primary'))


if __name__=='__main__':unittest.main()
