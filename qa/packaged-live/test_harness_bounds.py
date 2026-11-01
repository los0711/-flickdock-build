"""Portable QA-controller tests. No desktop, UIA, clipboard or credential calls."""
import importlib.util,io,json,queue,time,unittest,tempfile
from unittest.mock import patch
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
    def test_installer_requires_exact_executable_entry_not_successful_suffix(self):
        spec=importlib.util.spec_from_file_location('exact_installer_qa',Path(__file__).with_name('ci_exact_installer.py'))
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        target=r'C:\Own Trial\FlickDock.exe'
        suffix='-- File entry --\n2026-11-01 Dest filename: '+target+'.config\nSuccessfully installed the file.\n'
        self.assertIsNone(installer.exe_install_entry(suffix,target))
        exact='-- File entry --\n2026-11-01 Dest filename: '+target.upper()+'\nSuccessfully installed the file.\n'
        self.assertIsNotNone(installer.exe_install_entry(suffix+exact,target))
        skipped=exact.replace('Successfully installed the file.','Same version, skipped.')
        self.assertIsNone(installer.exe_install_entry(skipped,target))

    def test_recording_retains_post_input_paints_when_uia_action_exceeds_minimum(self):
        self.assertEqual(qa.post_action_capture_seconds(.9,1.2),.45)
        self.assertEqual(qa.post_action_capture_seconds(3.,19.),.45)
        self.assertEqual(qa.post_action_capture_seconds(3.,1.),2.)

    def test_extra_history_precedes_original_transfer_entry_and_empty_cleanup_is_last(self):
        folder=Path(__file__).parent
        plans=[json.loads((folder/name).read_text(encoding='utf8')) for name in ('ui-actions.json','ui-extra-actions.json','ui-batch2-actions.json')]
        actions=qa.compose_actions(*plans);ids=[row['id'] for row in actions]
        self.assertEqual(ids.index('batch2-history-folder-before-clear')+1,ids.index('transfer-open'))
        self.assertLess(ids.index('batch2-history-folder-before-clear'),ids.index('history-delete-one'))
        self.assertEqual(actions[-1]['recipe'],'empty-zones-last')
        self.assertEqual(len(actions),sum(map(len,plans)))

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
    def test_explorer_full_size_reservation_is_not_completion_until_readable_original_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.bin';path.write_bytes(b'original')
            expected=qa.digest(path)
            with patch.object(qa,'digest',side_effect=[PermissionError('Explorer still writing'),expected]):
                self.assertFalse(qa.pasted_original_ready(path,8,expected))
                self.assertTrue(qa.pasted_original_ready(path,8,expected))
            self.assertFalse(qa.pasted_original_ready(path,8,'0'*64))
            self.assertFalse(qa.pasted_original_ready(path.with_name('missing.bin'),8,expected))

    def test_unique_explorer_destination_uses_native_tab_when_frame_title_is_generic(self):
        folder='native-paste-unique-synthetic'
        def window(title,tabs=(),visible=True):
            return SimpleNamespace(is_visible=lambda:visible,element_info=SimpleNamespace(name=title),
                descendants=lambda control_type:[SimpleNamespace(element_info=SimpleNamespace(name=name)) for name in tabs])
        self.assertTrue(qa.explorer_folder_matches(window(folder),folder))
        self.assertTrue(qa.explorer_folder_matches(window('File Explorer',['Home',folder]),folder))
        self.assertFalse(qa.explorer_folder_matches(window('File Explorer',['Home','other-folder']),folder))
        self.assertFalse(qa.explorer_folder_matches(window(folder,visible=False),folder))

    def test_nonactivating_app_pointer_and_external_click_activation_are_distinct(self):
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
            self.assertNotIn(('source_focus',True),events)

    def test_external_editor_click_requires_actual_foreground_before_following_keyboard(self):
        backend=qa._UIABackend.__new__(qa._UIABackend);backend.pid=101;backend.producer_pid=202;events=[]
        control=SimpleNamespace(click_input=lambda:events.append('physical_click'))
        backend.find=lambda *args,**kwargs:control
        backend._prepare_control=lambda target,pid:events.append(('pointer_verified',pid))
        backend._assert_foreground=lambda pid:events.append(('foreground_verified',pid))
        self.assertIs(backend.click('editor',pid=202),control)
        self.assertEqual(events,[('pointer_verified',202),'physical_click',('foreground_verified',202)])
        backend._assert_foreground=lambda pid:(_ for _ in ()).throw(RuntimeError('actual keyboard foreground unavailable'))
        with self.assertRaisesRegex(RuntimeError,'actual keyboard foreground unavailable'):backend.click('editor',pid=202)

    def test_occluded_pointer_target_is_rejected_even_for_nonactivating_app(self):
        backend=qa._UIABackend.__new__(qa._UIABackend);backend.pid=101;backend.producer_pid=202
        backend._producer_topmost=lambda _:None
        backend._assert_pointer_owner=lambda point,pid:(_ for _ in ()).throw(RuntimeError('occluded pointer'))
        control=SimpleNamespace(rectangle=lambda:SimpleNamespace(left=10,top=20,right=30,bottom=40))
        with self.assertRaisesRegex(RuntimeError,'occluded pointer'):backend._prepare_control(control,101)

if __name__=='__main__':unittest.main()
