"""Storage/child-process models only: never reads or writes macOS settings."""
import os
import subprocess
import unittest
from unittest.mock import patch
from ci_material_policy_fixture import (_guard,_pack,_typed_equal,_exercise,verify,
                                        _Preferences,KEY,DOMAIN,QA_REF,QA_REPOSITORY,SCOPES)


class Store:
    def __init__(self,any_host=True,current_host=None):
        self.data={'any-host':any_host,'current-host':current_host};self.writes=[];self.managed=False
    def snapshot(self):return dict(self.data)
    def forced(self):return self.managed
    def write(self,scope,value):self.writes.append((scope,value));self.data[scope]=value
    def sync(self,scope):return True


def fixture(store, *, policy_override=None, pixels=None, backup=None):
    phases=[];backups=[];pixel_calls=[]
    def read(stage):
        phases.append(stage)
        value=store.data['current-host']
        if value is None:value=store.data['any-host']
        actual=True if value is None else bool(value)
        if policy_override is not None:actual=policy_override(stage,actual)
        return dict(policy={'reduce_transparency':dict(bridge_matches_native=True,scalar_bool=True,native_bool=actual)},
                    preferences={scope:_pack(value) for scope,value in store.snapshot().items()},forced=store.managed)
    def run_pixels():
        pixel_calls.append(True)
        if pixels is not None:return pixels()
        return dict(ok=True,blur_verified=True)
    def save(value):
        backups.append(value)
        if backup is not None:backup(value)
    return _exercise(store,read,run_pixels,save),phases,backups,pixel_calls


class MaterialPolicyFixtureTests(unittest.TestCase):
    def test_exact_ci_guards_reject_user_machine_other_branch_repo_and_self_hosted(self):
        valid=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=QA_REPOSITORY,GITHUB_REF=QA_REF,
                   RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='macOS')
        _guard(valid,'darwin')
        for key in valid:
            changed=dict(valid);changed[key]='wrong'
            with self.assertRaises(RuntimeError):_guard(changed,'darwin')
        with self.assertRaises(RuntimeError):_guard(valid,'win32')
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}),patch('ci_material_policy_fixture._Preferences',side_effect=AssertionError('No native boundary')):
            with self.assertRaises(RuntimeError):verify('must-not-be-created.json')

    def test_presence_and_original_boolean_number_types_are_distinct(self):
        self.assertFalse(_pack(None)['present']);self.assertTrue(_pack(False)['present'])
        self.assertFalse(_typed_equal(_pack(True),_pack(1)))
        self.assertFalse(_typed_equal(_pack(False),_pack(0)))

    def test_real_false_child_condition_then_probe_and_exact_restore(self):
        store=Store();result,phases,backups,calls=fixture(store)
        self.assertTrue(result['ok']);self.assertTrue(result['blur_verified']);self.assertTrue(result['restoration_verified'])
        self.assertEqual(store.writes,[('any-host',False),('any-host',True)])
        self.assertEqual(phases,['before','temporary','restored']);self.assertEqual(len(calls),1)
        self.assertTrue(backups[0]['original']['any-host']['present'])

    def test_original_absence_is_removed_not_replaced_with_true(self):
        store=Store(None);result,_,backups,_=fixture(store)
        self.assertTrue(result['ok']);self.assertEqual(store.writes,[('any-host',False),('any-host',None)])
        self.assertIsNone(store.data['any-host']);self.assertFalse(backups[0]['original']['any-host']['present'])

    def test_original_number_type_and_current_host_scope_preserved(self):
        store=Store(False,1);result,_,_,_=fixture(store)
        self.assertTrue(result['ok']);self.assertEqual(result['target_scope'],'current-host')
        self.assertEqual(store.writes,[('current-host',False),('current-host',1)])
        self.assertIs(type(store.data['current-host']),int);self.assertIs(store.data['any-host'],False)

    def test_already_false_needs_no_mutation_but_still_checks_restoration(self):
        store=Store(False);result,_,_,calls=fixture(store)
        self.assertTrue(result['ok']);self.assertFalse(result['mutation_attempted'])
        self.assertEqual(store.writes,[]);self.assertEqual(len(calls),1)

    def test_forced_setting_is_rejected_without_setter_or_pixel_probe(self):
        store=Store();store.managed=True;result,phases,backups,calls=fixture(store)
        self.assertFalse(result['ok']);self.assertEqual(result['outcome'],'managed-policy')
        self.assertEqual(store.writes,[]);self.assertEqual(phases,[]);self.assertEqual(calls,[])
        self.assertEqual(len(backups),1)

    def test_raw_false_without_real_native_effect_does_not_run_or_pass_blur(self):
        store=Store();result,_,_,calls=fixture(store,policy_override=lambda stage,value:True)
        self.assertFalse(result['ok']);self.assertFalse(result['blur_verified']);self.assertTrue(result['restoration_verified'])
        self.assertEqual(calls,[]);self.assertIs(store.data['any-host'],True)
        self.assertIn('did not adopt',result['errors'][0])

    def test_partially_failing_setter_is_always_restored(self):
        class Partial(Store):
            def write(self,scope,value):
                super().write(scope,value)
                if len(self.writes)==1:raise RuntimeError('setter changed then raised')
        store=Partial();result,_,_,calls=fixture(store)
        self.assertFalse(result['ok']);self.assertTrue(result['restoration_verified']);self.assertEqual(calls,[])
        self.assertIs(store.data['any-host'],True);self.assertEqual(len(store.writes),2)

    def test_sync_rejection_preserves_failure_and_restores_original(self):
        class Rejected(Store):
            def sync(self,scope):return len(self.writes)!=1
        store=Rejected();result,_,_,calls=fixture(store)
        self.assertFalse(result['ok']);self.assertTrue(result['restoration_verified']);self.assertEqual(calls,[])
        self.assertIs(store.data['any-host'],True)

    def test_pixel_process_timeout_and_failed_pixels_both_restore_without_fake_success(self):
        def timeout():raise subprocess.TimeoutExpired(['isolated-pixels'],25)
        for pixels in (timeout,lambda:dict(ok=False,blur_verified=False,error='no source response')):
            with self.subTest(pixels=pixels):
                store=Store();result,_,_,calls=fixture(store,pixels=pixels)
                self.assertFalse(result['ok']);self.assertTrue(result['restoration_verified'])
                self.assertFalse(result['blur_verified']);self.assertIs(store.data['any-host'],True)
                self.assertEqual(len(calls),1)

    def test_restore_failure_overrides_success_and_is_not_hidden(self):
        class BrokenRestore(Store):
            def write(self,scope,value):
                if self.writes:raise OSError('restore blocked')
                super().write(scope,value)
        store=BrokenRestore();result,_,_,_=fixture(store)
        self.assertTrue(result['blur_verified']);self.assertFalse(result['ok']);self.assertFalse(result['restoration_verified'])
        self.assertTrue(any(error.startswith('RESTORE ') for error in result['errors']))

    def test_backup_failure_prevents_all_mutation(self):
        store=Store()
        def failed_backup(value):raise OSError('backup volume full')
        with self.assertRaises(OSError):fixture(store,backup=failed_backup)
        self.assertEqual(store.writes,[]);self.assertIs(store.data['any-host'],True)

    def test_native_boundary_hardcodes_exact_one_key_domain_and_current_user(self):
        class Native:
            kCFPreferencesCurrentUser='user';kCFPreferencesCurrentHost='current';kCFPreferencesAnyHost='any'
            calls=[]
            @classmethod
            def CFPreferencesSetValue(cls,*args):cls.calls.append(args)
        preferences=_Preferences(Native)
        preferences.write('any-host',False);preferences.write('current-host',None)
        self.assertEqual(Native.calls,[(KEY,False,DOMAIN,'user','any'),(KEY,None,DOMAIN,'user','current')])
        with self.assertRaises(ValueError):preferences.write('other-host',False)


if __name__=='__main__':unittest.main()
