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
    def test_final_motion_uses_existing_isolated_worker_with_hard_deadline(self):
        driver=qa.Driver.__new__(qa.Driver)
        from unittest.mock import Mock
        driver._rpc=Mock(return_value={'events':[]})
        self.assertEqual(driver.final_motion('host-real-leave-close','manual-position'),{'events':[]})
        driver._rpc.assert_called_once_with('final_motion',seconds=50,recipe='host-real-leave-close',placement='manual-position')

    def test_motion_diagnostics_keep_raw_geometry_and_times_without_text(self):
        raw=dict(recipe='header-fixed-expand',name='private clipboard text',error='private response',before={'cloud':dict(bounds=[-80,10,20,42],monotonic=123.25)},observations=[dict(host_visible=False,popup_visible=True,foreground=dict(pid=17,hwnd=88))])
        safe=qa.safe_motion_diagnostic(raw)
        self.assertNotIn('name',safe);self.assertNotIn('error',safe)
        self.assertEqual(safe['before'],raw['before']);self.assertEqual(safe['observations'],raw['observations'])

    def test_prior_ready_head_or_different_payload_cannot_prove_new_copy_commit(self):
        value='synthetic fixture';inline=qa.inline_clipboard(value)
        head=dict(id='new-copy',kind='text',ready=True,inline=inline['body'],size=inline['size'],sha256=inline['sha256'])
        self.assertTrue(qa.exact_ready_text_head(head,value,'previous'))
        self.assertFalse(qa.exact_ready_text_head(head,value,'new-copy'))
        self.assertFalse(qa.exact_ready_text_head(head,'different','previous'))
        for change in (dict(ready=False),dict(id=''),dict(size=1),dict(sha256='0'*64),dict(inline='corrupt')):
            self.assertFalse(qa.exact_ready_text_head(dict(head,**change),value,'previous'))

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
        control=SimpleNamespace()
        backend.find=lambda *args,**kwargs:control
        def prepare(target,pid):events.append(('pointer_verified',pid));return (20,30)
        backend._prepare_control=prepare
        backend._assert_foreground=lambda pid:events.append(('foreground_verified',pid))
        physical=lambda **values:events.append(('physical_click',values))
        with patch.dict('sys.modules',{'pywinauto':SimpleNamespace(mouse=SimpleNamespace(click=physical))}):
            self.assertEqual(backend.click('editor',pid=202),dict(point=[20,30],input_sent=True))
            self.assertEqual(events,[('pointer_verified',202),('physical_click',dict(button='left',coords=(20,30))),('foreground_verified',202)])
            backend._assert_foreground=lambda pid:(_ for _ in ()).throw(RuntimeError('actual keyboard foreground unavailable'))
            with self.assertRaisesRegex(RuntimeError,'actual keyboard foreground unavailable'):backend.click('editor',pid=202)

    def test_click_deleted_control_is_never_read_after_one_physical_input(self):
        backend=qa._UIABackend.__new__(qa._UIABackend);backend.pid=101;backend.producer_pid=202
        state=dict(deleted=False,reads=0,inputs=[])
        class TransientControl:
            def rectangle(self):
                if state['deleted']:raise RuntimeError('provider target has been deleted')
                state['reads']+=1;return SimpleNamespace(left=10,top=20,right=30,bottom=40)
            def click_input(self):raise AssertionError('must use the already verified physical point')
        control=TransientControl();backend.find=lambda *args,**kwargs:control
        def prepare(target,pid):
            rect=target.rectangle();self.assertEqual(pid,101);return ((rect.left+rect.right)//2,(rect.top+rect.bottom)//2)
        backend._prepare_control=prepare
        def physical(**values):state['inputs'].append(values);state['deleted']=True
        with patch.dict('sys.modules',{'pywinauto':SimpleNamespace(mouse=SimpleNamespace(click=physical))}):
            receipt=backend.click('delete row')
        self.assertEqual(state['reads'],1);self.assertEqual(state['inputs'],[dict(button='left',coords=(20,30))])
        self.assertEqual(json.loads(json.dumps(receipt)),dict(point=[20,30],input_sent=True))

    def test_click_worker_receipt_does_not_serialize_a_deleted_provider(self):
        class Backend:
            def __init__(self,*args):pass
            def click(self,**values):return dict(point=[20,30],input_sent=True)
        stdout=io.StringIO()
        command=dict(id=1,operation='click',values=dict(name='delete row'))
        with patch.object(qa,'require_hosted'),patch.object(qa,'_UIABackend',Backend),patch.object(qa.sys,'argv',['qa','--uia-worker','101','202','owned']),patch.object(qa.sys,'stdin',io.StringIO(json.dumps(command)+'\n')),patch.object(qa.sys,'stdout',stdout):
            self.assertEqual(qa.uia_worker(),0)
        self.assertEqual(json.loads(stdout.getvalue()),dict(id=1,result=dict(point=[20,30],input_sent=True)))

    def test_remote_click_returns_plain_receipt_without_control_wrapper(self):
        driver=qa.Driver.__new__(qa.Driver);receipt=dict(point=[20,30],input_sent=True)
        driver._rpc=lambda *args,**kwargs:receipt
        self.assertIs(driver.click('delete row'),receipt)

    def focus_fixture(self,*,labels=True,covered=False,other_root=False):
        events=[];backend=qa._UIABackend.__new__(qa._UIABackend);backend.pid=101;backend.producer_pid=202
        def control(role,cls,area):
            return SimpleNamespace(element_info=SimpleNamespace(control_type=role,class_name=cls),
                is_visible=lambda:True,is_enabled=lambda:True,
                rectangle=lambda:SimpleNamespace(left=area[0],top=area[1],right=area[2],bottom=area[3]))
        status=control('Text','_StatusLabel',[282,44,814,64]);back=control('Button','_PressButton',[176,40,270,68])
        header=control('Group','_DialogTitleBar',[44,30,978,78])
        header.descendants=lambda:([status] if labels else [])+[back]+([control('Edit','QLineEdit',[282,44,814,64])] if covered else [])
        window=control('Window','LanDialog',[21,7,1001,711]);window.handle=404
        window.element_info.name='FlickDock · 文件互传';window.descendants=lambda:[header]
        window.set_focus=lambda:(_ for _ in ()).throw(AssertionError('must use a real nonfunctional header click'))
        backend.windows=lambda:[window];backend._producer_topmost=lambda value:events.append(('producer_topmost',value))
        backend._assert_pointer_owner=lambda point,pid:events.append(('pointer_owner',point,pid))
        backend._input_api=lambda:SimpleNamespace(WindowFromPoint=lambda _:303,GetAncestor=lambda *_:405 if other_root else 404)
        backend._assert_foreground=lambda pid:events.append(('foreground',pid))
        physical=lambda **values:events.append(('physical',values))
        return backend,events,physical

    def test_focus_for_paste_uses_real_verified_header_text_then_strict_foreground(self):
        backend,events,physical=self.focus_fixture()
        with patch.dict('sys.modules',{'pywinauto':SimpleNamespace(mouse=SimpleNamespace(click=physical))}):
            self.assertTrue(backend.focus_window('FlickDock · 文件互传'))
        self.assertEqual(events,[('producer_topmost',False),('pointer_owner',(548,54),101),
            ('physical',dict(button='left',coords=(548,54))),('foreground',101)])

    def test_focus_for_paste_refuses_missing_or_interactive_covered_header_without_input(self):
        for values in (dict(labels=False),dict(covered=True),dict(other_root=True)):
            backend,events,physical=self.focus_fixture(**values)
            with patch.dict('sys.modules',{'pywinauto':SimpleNamespace(mouse=SimpleNamespace(click=physical))}):
                with self.assertRaisesRegex(RuntimeError,'HARNESS_'):backend.focus_window('FlickDock · 文件互传')
            self.assertFalse(any(event[0]=='physical' for event in events))

    def test_focus_for_paste_does_not_hide_real_activation_failure_or_click_twice(self):
        backend,events,physical=self.focus_fixture()
        backend._assert_foreground=lambda pid:(_ for _ in ()).throw(RuntimeError('actual foreground is still producer'))
        with patch.dict('sys.modules',{'pywinauto':SimpleNamespace(mouse=SimpleNamespace(click=physical))}):
            with self.assertRaisesRegex(RuntimeError,'still producer'):backend.focus_window('FlickDock · 文件互传')
        self.assertEqual(sum(event[0]=='physical' for event in events),1)

    def test_occluded_pointer_target_is_rejected_even_for_nonactivating_app(self):
        backend=qa._UIABackend.__new__(qa._UIABackend);backend.pid=101;backend.producer_pid=202
        backend._producer_topmost=lambda _:None
        backend._assert_pointer_owner=lambda point,pid:(_ for _ in ()).throw(RuntimeError('occluded pointer'))
        control=SimpleNamespace(rectangle=lambda:SimpleNamespace(left=10,top=20,right=30,bottom=40))
        with self.assertRaisesRegex(RuntimeError,'occluded pointer'):backend._prepare_control(control,101)

if __name__=='__main__':unittest.main()
