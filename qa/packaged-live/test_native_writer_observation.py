"""Portable safety/cleanup tests. No Windows ACL API or OS input is invoked."""
import importlib.util
import ast
import copy
import os
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

SPEC=importlib.util.spec_from_file_location('writer_observation',Path(__file__).with_name('native_writer_observation.py'))
qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)

ACCOUNT='11111111-1111-4111-8111-111111111111'
ITEM='22222222-2222-4222-8222-222222222222'


class WriterObservationTests(unittest.TestCase):
    def test_host_guard_requires_all_ephemeral_markers(self):
        markers={'GITHUB_ACTIONS':'true','RUNNER_OS':'Windows','RUNNER_ENVIRONMENT':'github-hosted'}
        for key in markers:
            with self.subTest(key=key),patch.object(qa.sys,'platform','win32'),patch.dict(os.environ,{**markers,key:'no'}):
                with self.assertRaisesRegex(RuntimeError,'ephemeral'):qa.require_hosted()
        with patch.object(qa.sys,'platform','linux'),patch.dict(os.environ,markers):
            with self.assertRaises(RuntimeError):qa.require_hosted()
        with patch.object(qa.sys,'platform','win32'),patch.dict(os.environ,markers):qa.require_hosted()

    def fixture(self):
        temp=tempfile.TemporaryDirectory(prefix='flickdock-frozen-normal-ci-');self.addCleanup(temp.cleanup)
        profile=Path(temp.name)/'FlickDock';profile.mkdir()
        cloud=SimpleNamespace(session={'account':{'id':ACCOUNT}},shelf_versions={ITEM:1})
        return profile,cloud

    def test_acl_path_is_exact_known_account_object_version_and_new_empty(self):
        profile,cloud=self.fixture()
        with patch.object(qa,'require_hosted'):
            parent=qa.fixture_parent(profile,cloud,ITEM)
            self.assertEqual(parent,profile/'cloud-shelves'/ACCOUNT/'files'/ITEM/'1')
            qa.write_probe(parent);self.assertEqual(list(parent.iterdir()),[])
            (parent/'original').write_bytes(b'preserve')
            with self.assertRaisesRegex(AssertionError,'empty'):qa.fixture_parent(profile,cloud,ITEM)
            self.assertEqual((parent/'original').read_bytes(),b'preserve')

    def test_unknown_object_bad_account_version_or_profile_never_gets_acl(self):
        profile,cloud=self.fixture()
        with patch.object(qa,'require_hosted'):
            for identity in ('../escape','33333333-3333-4333-8333-333333333333','A'+ITEM[1:]):
                with self.subTest(identity=identity),self.assertRaises(AssertionError):qa.fixture_parent(profile,cloud,identity)
            cloud.shelf_versions[ITEM]=True
            with self.assertRaises(AssertionError):qa.fixture_parent(profile,cloud,ITEM)
            cloud.shelf_versions[ITEM]=1;cloud.session['account']['id']='../wrong'
            with self.assertRaises(AssertionError):qa.fixture_parent(profile,cloud,ITEM)
            with self.assertRaises(AssertionError):qa.fixture_parent(profile.parent/'Other',cloud,ITEM)

    def test_link_or_junction_ancestor_is_rejected_before_creation(self):
        profile,cloud=self.fixture();original=Path.is_symlink
        def linked(path):return path==profile/'cloud-shelves' or original(path)
        with patch.object(qa,'require_hosted'),patch.object(Path,'is_symlink',linked):
            with self.assertRaisesRegex(AssertionError,'link or junction'):qa.fixture_parent(profile,cloud,ITEM)
        self.assertFalse((profile/'cloud-shelves').exists())
        with self.assertRaisesRegex(AssertionError,'TEMP anchor'):qa.reject_links(Path(tempfile.gettempdir()).parent,Path(tempfile.gettempdir()))

    def test_exact_acl_restore_runs_on_body_error_and_exports_no_private_values(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder);private=object();backend=SimpleNamespace(snapshot=Mock(return_value=private),deny=Mock(),restore=Mock())
            events=[]
            with patch.object(qa,'write_probe',side_effect=[PermissionError('private path or SID'),None]):
                with self.assertRaisesRegex(ValueError,'body'):
                    with qa.blocked_directory(parent,backend=backend,emit=lambda **row:events.append(row)):
                        raise ValueError('body')
            backend.restore.assert_called_once_with(parent,private)
            self.assertEqual(events[-1]['phase'],'acl-restored')
            self.assertNotIn('private',str(events));self.assertNotIn(str(parent),str(events))

    def test_restore_runs_even_when_deny_fails_and_error_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder);backend=SimpleNamespace(snapshot=Mock(return_value='secret'),deny=Mock(side_effect=RuntimeError('deny failed')),restore=Mock())
            with self.assertRaisesRegex(RuntimeError,'deny failed'):
                with qa.blocked_directory(parent,backend=backend):self.fail('Must not enter body')
            backend.restore.assert_called_once_with(parent,'secret')
            self.assertEqual(list(parent.iterdir()),[])

    def test_ineffective_denial_fails_and_restores_rather_than_claiming_failure_injected(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder);backend=SimpleNamespace(snapshot=Mock(return_value='private'),deny=Mock(),restore=Mock())
            with self.assertRaisesRegex(AssertionError,'not denied'):
                with qa.blocked_directory(parent,backend=backend):self.fail('Must not enter body')
            backend.restore.assert_called_once();self.assertEqual(list(parent.iterdir()),[])

    def test_failed_restore_is_visible_not_a_successful_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            backend=SimpleNamespace(snapshot=Mock(return_value='private'),deny=Mock(),restore=Mock(side_effect=RuntimeError('restore failed')))
            events=[]
            with patch.object(qa,'write_probe',side_effect=PermissionError):
                with self.assertRaisesRegex(RuntimeError,'restore failed'):
                    with qa.blocked_directory(Path(folder),backend=backend,emit=lambda **row:events.append(row)):pass
            self.assertFalse(any(row['phase']=='acl-restored' for row in events))

    def test_wait_is_bounded_and_never_returns_unobserved_success(self):
        clock=iter((0,0,.2,1.2))
        with patch.object(qa.time,'monotonic',side_effect=lambda:next(clock)),patch.object(qa.time,'sleep'):
            with self.assertRaisesRegex(AssertionError,'timeout'):qa.bounded_wait(lambda:False,'synthetic endpoint',1)

    def test_observation_restores_receive_setting_and_keeps_primary_failure_on_cleanup_error(self):
        profile,cloud=self.fixture();(profile/'settings.json').write_text(json.dumps({'sync_receive_enabled':True}))
        driver=SimpleNamespace(setting=Mock(side_effect=RuntimeError('secondary private value')),click=Mock())
        events=[]
        with patch.object(qa,'require_hosted'),patch.object(qa,'observe_fixture',side_effect=AssertionError('primary fixed label')):
            with self.assertRaisesRegex(AssertionError,'primary fixed label'):
                qa.observe(profile,cloud,driver,lambda **row:events.append(row))
        driver.setting.assert_called_once_with('sync_receive_enabled',True)
        self.assertEqual(events[-1]['phase'],'settings-cleanup-failed')
        self.assertNotIn('secondary private value',str(events))

    def test_native_errors_retain_numeric_reason_but_export_no_path_sid_or_sddl(self):
        profile,cloud=self.fixture();(profile/'settings.json').write_text(json.dumps({'sync_receive_enabled':False}))
        driver=SimpleNamespace(setting=Mock(),click=Mock());events=[]
        problem=PermissionError(13,'secret SID/SDDL/private path')
        with patch.object(qa,'require_hosted'),patch.object(qa,'observe_fixture',side_effect=problem):
            with self.assertRaisesRegex(RuntimeError,'PermissionError') as failure:
                qa.observe(profile,cloud,driver,lambda **row:events.append(row))
        self.assertNotIn('secret',str(failure.exception));self.assertNotIn('secret',str(events))
        self.assertEqual(events[0]['errno'],13);driver.setting.assert_called_once_with('sync_receive_enabled',False)
        driver.click.assert_called_once_with('关闭设置')

    def test_preflight_permission_failure_is_sanitized_before_original_setting_is_known(self):
        driver=SimpleNamespace(setting=Mock(),click=Mock());events=[]
        with patch.object(qa,'guarded_profile',side_effect=PermissionError(13,'private path SID SDDL')):
            with self.assertRaisesRegex(RuntimeError,'PermissionError') as result:
                qa.observe('unverified',None,driver,lambda **row:events.append(row))
        self.assertNotIn('private',str(result.exception));self.assertNotIn('private',str(events))
        self.assertEqual(events[0]['errno'],13);driver.setting.assert_not_called();driver.click.assert_not_called()

    def test_former_preflight_placement_negative_control_leaks_error_before_handler(self):
        tree=ast.parse(Path(qa.__file__).read_text(encoding='utf8'))
        function=copy.deepcopy(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='observe'))
        block=next(n for n in function.body if isinstance(n,ast.Try))
        # Structural negative control: move actual preflight statements before
        # the handler, reproducing the former layout without editing any source.
        preflight=block.body[:-1];block.body=block.body[-1:]
        index=function.body.index(block);function.body[index:index]=preflight
        values=dict(qa.__dict__)
        values['guarded_profile']=Mock(side_effect=PermissionError(13,'private negative control path'))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),'<former preflight control>','exec'),values)
        driver=SimpleNamespace(setting=Mock(),click=Mock());events=[]
        with self.assertRaises(PermissionError) as result:
            values['observe']('unverified',None,driver,lambda **row:events.append(row))
        self.assertIn('private negative control path',str(result.exception));self.assertEqual(events,[])
        driver.setting.assert_not_called();driver.click.assert_not_called()

    def test_invalid_original_preference_never_attempts_ui_cleanup_with_non_bool(self):
        profile,cloud=self.fixture();(profile/'settings.json').write_text(json.dumps({'sync_receive_enabled':1}))
        driver=SimpleNamespace(setting=Mock(),click=Mock())
        with patch.object(qa,'require_hosted'),self.assertRaisesRegex(AssertionError,'Invalid original'):
            qa.observe(profile,cloud,driver,lambda **row:None)
        driver.setting.assert_not_called();driver.click.assert_not_called()

    def test_settings_read_failure_is_sanitized_and_does_not_run_native_actions(self):
        profile,cloud=self.fixture();driver=SimpleNamespace(setting=Mock(),click=Mock());events=[]
        with patch.object(qa,'require_hosted'):
            with self.assertRaisesRegex(RuntimeError,'FileNotFoundError') as result:
                qa.observe(profile,cloud,driver,lambda **row:events.append(row))
        self.assertNotIn(str(profile),str(result.exception));self.assertNotIn(str(profile),str(events))
        driver.setting.assert_not_called();driver.click.assert_not_called()


if __name__=='__main__':unittest.main()
