"""Portable batch3 safety probes: no real UIA, input, process, or clipboard."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

DIRECTORY=Path(__file__).parent
sys.path.insert(0,str(DIRECTORY))
try:
    import native_batch2
    SPEC=importlib.util.spec_from_file_location('batch3_qa',DIRECTORY/'native_batch3.py')
    qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)
finally:sys.path.remove(str(DIRECTORY))
HOST_GUARD=qa.require_hosted


class Control:
    def __init__(self,name,role='Button',cls='IconTextButton',parent=None,rectangle=(10,20,150,76),pid=123,handle=90):
        self.element_info=types.SimpleNamespace(name=name,control_type=role,class_name=cls,process_id=pid)
        self.children=[];self.owner=parent;self.box=rectangle;self.visible=True;self.enabled=True;self.handle=handle
        if parent:parent.children.append(self)
    def descendants(self):return [c for child in self.children for c in (child,*child.descendants())]
    def parent(self):return self.owner
    def rectangle(self):
        a,b,c,d=self.box;return types.SimpleNamespace(left=a,top=b,right=c,bottom=d)
    def is_visible(self):return self.visible
    def is_enabled(self):return self.enabled
    def top_level_parent(self):return self.owner.top_level_parent() if self.owner else self


class Batch3Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        (self.base/'files').mkdir()
        for name in native_batch2.ORIGINALS:(self.base/'files'/name).write_bytes(('original '+name).encode())
        self.root=Control('FlickDock','Window','ShelfWindow',rectangle=(0,0,1000,700))
        self.mouse=types.SimpleNamespace(move=Mock(),click=Mock(),double_click=Mock())
        self.api=types.SimpleNamespace(GetForegroundWindow=Mock(return_value=90),
            GetAncestor=Mock(side_effect=lambda hwnd,kind:hwnd),WindowFromPoint=Mock(return_value=90))
        self.backend=types.SimpleNamespace(pid=123,producer_pid=456,base=self.base,
            windows=Mock(return_value=[self.root]),hotkey=Mock(),_producer_topmost=Mock(),
            _assert_pointer_owner=Mock(),_assert_foreground=Mock(),
            _window_pid=Mock(return_value=123),_input_api=Mock(return_value=self.api))
        patches=[patch.object(qa,'require_hosted'),patch.object(native_batch2,'require_hosted'),
                 patch.dict(sys.modules,{'pywinauto':types.SimpleNamespace(mouse=self.mouse)})]
        for p in patches:p.start();self.addCleanup(p.stop)
        self.runner=qa.Batch3(self.backend);self.runner.recipe='portable'
        self.runner.process_image=Mock(return_value=r'C:\Windows\explorer.exe')
        self.runner.desktop=Mock()
        self.windir=patch.dict(qa.os.environ,{'WINDIR':r'C:\Windows'});self.windir.start();self.addCleanup(self.windir.stop)

    def immediate(self,action,label,seconds=3):
        return action() or (_ for _ in ()).throw(AssertionError(label))

    def test_requires_real_hosted_ephemeral_windows_all_markers(self):
        markers={'GITHUB_ACTIONS':'true','RUNNER_OS':'Windows','RUNNER_ENVIRONMENT':'github-hosted'}
        for key in markers:
            with self.subTest(key=key),patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,{**markers,key:'false'}):
                with self.assertRaisesRegex(RuntimeError,'refuses'):HOST_GUARD()
        with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,markers):HOST_GUARD()

    def test_exact_command_path_rejects_basename_different_folder_and_suffix(self):
        expected=r'C:\synthetic files\original-text.txt'
        self.assertTrue(qa.command_has_exact_path(r'notepad.exe "C:\synthetic files\original-text.txt"',expected))
        self.assertTrue(qa.command_has_exact_path('notepad.exe C:/files/a.txt',r'C:\files\a.txt'))
        for command in ('notepad.exe original-text.txt',r'notepad.exe "D:\synthetic files\original-text.txt"',
                        r'notepad.exe "C:\synthetic files\original-text.txt.bak"',r'notepad.exe "XC:\synthetic files\original-text.txt"'):
            with self.subTest(command=command):self.assertFalse(qa.command_has_exact_path(command,expected))

    def tray_fixture(self):
        root=Control('','Window','Shell_TrayWnd',rectangle=(0,700,1000,768),pid=999,handle=99)
        notify=Control('','ToolBar','ToolbarWindow32',root,rectangle=(700,700,1000,768),pid=999,handle=99)
        icon=Control('FlickDock 1.8.24 · 文件搁架','Button','Button',notify,rectangle=(900,710,930,740),pid=999,handle=99)
        self.runner.desktop.return_value.windows.return_value=[root]
        self.backend._window_pid.return_value=999;self.api.WindowFromPoint.return_value=99
        return root,notify,icon

    def test_only_exact_tooltip_in_actual_tray_roots_is_discovered(self):
        root,notify,icon=self.tray_fixture()
        Control('FlickDock','Button','Button',notify)
        unrelated=Control('','Window','Unrelated');Control(icon.element_info.name,'Button','Button',unrelated)
        self.runner.desktop.return_value.windows.return_value.append(unrelated)
        self.assertEqual(self.runner.system_tray_targets(),[icon]);icon.visible=False
        self.assertEqual(self.runner.system_tray_targets(),[])

    def test_passive_tray_inventory_redacts_private_names_and_does_not_authorize_unknown_root(self):
        root,notify,icon=self.tray_fixture()
        hidden=Control('Hidden icon menu','Button','Button',notify)
        hidden.element_info.automation_id='SystemTrayChevron'
        private=Control('private.account@example.invalid - secret document.txt','Button','Button',notify)
        private.element_info.automation_id='private account label'
        unknown=Control('private window title','Window','NewExplorerShellRoot')
        Control(icon.element_info.name,'Button','Button',unknown)
        self.runner.desktop.return_value.windows.return_value.append(unknown)
        self.runner.observe_system_tray()
        record=self.runner.events[-1];raw=json.dumps(record,ensure_ascii=False)
        self.assertEqual(record['step'],'passive-system-tray-inventory')
        self.assertFalse(record['actual_native_input']);self.assertEqual(len(record['roots']),2)
        self.assertIn('Hidden icon menu',raw);self.assertIn('SystemTrayChevron',raw)
        self.assertIn('NewExplorerShellRoot',raw)
        self.assertNotIn('private.account',raw);self.assertNotIn('secret document',raw)
        self.assertNotIn('private window title',raw);self.assertNotIn('private account label',raw)
        # The actual artifact exporter applies this second allowlist. Technical
        # IDs and fixed English captions must survive without changing it.
        spec=importlib.util.spec_from_file_location('normal_qa_export',DIRECTORY/'ci_packaged_windows_live.py')
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
        exported=json.dumps(exporter.safe_motion_diagnostic(record))
        self.assertIn('SystemTrayChevron',exported);self.assertIn('Hidden icon menu',exported)
        self.assertNotIn('private.account',exported)
        self.assertEqual(self.runner.system_tray_targets(),[icon])
        with self.assertRaisesRegex(AssertionError,'ancestry'):self.runner.tray_input(unknown.children[0])
        self.mouse.click.assert_not_called();self.mouse.move.assert_not_called()

    def test_passive_inventory_bounds_roots_and_children(self):
        roots=[]
        for index in range(30):
            root=Control('Taskbar','Window','Shell_TrayWnd');roots.append(root)
            for child in range(120):Control('private-'+str(child),parent=root)
        self.runner.desktop.return_value.windows.return_value=roots
        self.runner.observe_system_tray();record=self.runner.events[-1]
        self.assertEqual(len(record['roots']),24);self.assertTrue(record['truncated'])
        self.assertEqual(sum(len(row['children']) for row in record['roots']),288)
        self.assertTrue(all(len(row['children'])<=96 for row in record['roots']))
        self.mouse.click.assert_not_called()

    def test_passive_inventory_failure_is_recorded_and_never_sends_input(self):
        self.runner.desktop.return_value.windows.side_effect=RuntimeError('private provider message')
        self.runner.observe_system_tray();row=self.runner.events[-1]
        self.assertEqual(row['query_error'],'RuntimeError')
        self.assertNotIn('private provider message',json.dumps(row))
        self.mouse.click.assert_not_called()

    def test_tray_click_one_real_input_then_no_deleted_provider_access(self):
        root,notify,icon=self.tray_fixture()
        self.mouse.click.side_effect=lambda **_:setattr(icon,'rectangle',Mock(side_effect=RuntimeError('deleted provider')))
        self.runner.tray_input(icon)
        self.mouse.click.assert_called_once_with(button='left',coords=(915,725))
        self.assertEqual(self.runner.events[-1]['input_bounds'],[900,710,930,740])
        self.backend._producer_topmost.assert_called_once_with(False)
        self.backend._assert_foreground.assert_not_called()

    def test_observed_title_case_overflow_caption_requires_exact_name_and_guarded_owner(self):
        import hashlib
        root,notify,icon=self.tray_fixture();icon.visible=False
        name='Show Hidden Icons'
        self.assertEqual(len(name),17)
        self.assertEqual(hashlib.sha256(name.encode()).hexdigest()[:20],'ba250111842d4c6cbfbd')
        overflow=Control(name,'Button','SystemTray.NormalButton',notify,
            rectangle=(797,720,829,768),pid=999,handle=99)
        overflow.element_info.automation_id='SystemTrayIcon'
        for other in ('Notifications','Show Hidden Icons extra','Prefix Show Hidden Icons'):
            candidate=Control(other,'Button','SystemTray.NormalButton',notify,pid=999,handle=99)
            candidate.element_info.automation_id='SystemTrayIcon'
        self.assertEqual(self.runner.system_tray_targets(overflow=True),[overflow])
        self.assertEqual(self.runner.system_tray_targets(),[])
        self.runner.tray_input(overflow)
        self.mouse.click.assert_called_once_with(button='left',coords=(813,744))
        self.runner.process_image.assert_called_once_with(999)
        self.assertIn('Shell_TrayWnd',self.runner.events[-2]['ancestry'])

    def test_title_case_caption_outside_tray_or_with_wrong_native_image_cannot_be_clicked(self):
        root=Control('Other','Window','Unrelated');overflow=Control('Show Hidden Icons','Button','SystemTray.NormalButton',root)
        with self.assertRaisesRegex(AssertionError,'ancestry'):self.runner.tray_input(overflow)
        root,notify,icon=self.tray_fixture()
        overflow=Control('Show Hidden Icons','Button','SystemTray.NormalButton',notify,pid=999,handle=99)
        self.runner.process_image.return_value=r'C:\private\explorer.exe'
        with self.assertRaisesRegex(AssertionError,'system Explorer'):self.runner.tray_input(overflow)
        self.mouse.click.assert_not_called()

    def test_wrong_pointer_owner_or_non_system_explorer_never_receives_input(self):
        root,notify,icon=self.tray_fixture();self.backend._window_pid.return_value=888
        with self.assertRaisesRegex(AssertionError,'different native owner'):self.runner.tray_input(icon)
        self.backend._window_pid.return_value=999;self.runner.process_image.return_value=r'C:\temp\explorer.exe'
        with self.assertRaisesRegex(AssertionError,'system Explorer'):self.runner.tray_input(icon)
        self.mouse.click.assert_not_called()

    def test_exact_tooltip_outside_actual_notification_ancestry_is_not_input_target(self):
        root=Control('other','Window','QWidget');icon=Control('FlickDock 1.8.24 · 文件搁架',parent=root)
        with self.assertRaisesRegex(AssertionError,'ancestry'):self.runner.tray_input(icon)
        self.mouse.click.assert_not_called()

    def test_tray_menu_requires_all_five_unique_owned_actual_actions(self):
        menu=Control('','Menu','AnimatedMenu')
        for name in qa.TRAY_ACTIONS:Control(name,'MenuItem','',menu)
        self.backend.windows.return_value=[menu]
        self.assertEqual(len(self.runner.menu()),5)
        menu.children[-1].visible=False;self.assertIsNone(self.runner.menu())
        menu.children[-1].visible=True;Control(qa.TRAY_ACTIONS[0],'MenuItem','',menu)
        with self.assertRaisesRegex(AssertionError,'Duplicate'):self.runner.menu()

    def recent_fixture(self):
        popup=Control('','Window','QFrame',rectangle=(100,100,500,350))
        view=Control('','List','QListView',popup,rectangle=(100,100,500,350))
        row=Control(qa.RECENT_NAME,'ListItem','',view,rectangle=(100,110,500,166))
        survivor=Control('Native QA peer','ListItem','',view,rectangle=(100,168,500,224))
        self.backend.windows.return_value=[self.root,popup]
        return popup,view,row,survivor

    def test_recent_drawn_remove_point_is_measured_and_clipped_by_real_ancestors(self):
        popup,view,row,survivor=self.recent_fixture()
        self.assertEqual(self.runner.row_remove_point(row),(470,138))
        view.box=(100,100,455,350)
        with self.assertRaisesRegex(AssertionError,'clipped'):self.runner.row_remove_point(row)
        self.mouse.click.assert_not_called()

    def test_recent_rows_require_visible_owned_actual_list_view(self):
        popup,view,row,survivor=self.recent_fixture()
        Control('Other','ListItem','',self.root)
        self.assertEqual(self.runner.popup_rows(),[row,survivor]);view.element_info.class_name='OtherList'
        self.assertEqual(self.runner.popup_rows(),[])

    def test_recent_clipping_diagnostic_preserves_refusal_and_has_no_names_or_input(self):
        popup,view,row,_=self.recent_fixture();popup.element_info.class_name='QComboBoxPrivateContainer'
        combo=Control('private user text','ComboBox','_DevicesCombo',rectangle=(100,50,500,90))
        view.owner=combo
        with self.assertRaisesRegex(AssertionError,'clipped'):self.runner.row_remove_point(row)
        ancestry=next(e for e in self.runner.events if e['step']=='recent-clipping-ancestry')
        self.assertEqual([r['class_name'] for r in ancestry['ancestors']],['','QListView','_DevicesCombo'])
        self.assertEqual([r['contains'] for r in ancestry['ancestors']],[True,True,False])
        hosts=next(e for e in self.runner.events if e['step']=='recent-clipping-native-popup-hosts')
        self.assertEqual(hosts['hosts'][0]['bounds'],[100,100,500,350])
        self.assertNotIn('private user text',str(self.runner.events))
        self.assertFalse(any('name' in r for r in ancestry['ancestors']))
        self.mouse.move.assert_not_called();self.mouse.click.assert_not_called()

    def test_recent_clipping_observer_failure_never_hides_original_refusal(self):
        _,view,row,_=self.recent_fixture();view.box=(100,100,455,350)
        self.api.WindowFromPoint.side_effect=RuntimeError('private payload')
        with self.assertRaisesRegex(AssertionError,'clipped'):self.runner.row_remove_point(row)
        self.assertEqual(self.runner.events[-1]['error_type'],'RuntimeError')
        self.assertNotIn('private payload',str(self.runner.events));self.mouse.click.assert_not_called()

    def test_recent_clipping_native_point_uses_actual_ctypes_structure_not_tuple(self):
        import ctypes
        from ctypes import wintypes
        _,view,row,_=self.recent_fixture();view.box=(100,100,455,350)
        received=[]
        # A real ctypes argument converter, not a Mock that accepts any tuple.
        prototype=ctypes.CFUNCTYPE(ctypes.c_int,wintypes.POINT)
        callback=prototype(lambda point:received.append((point.x,point.y)) or 90)
        self.api.WindowFromPoint=callback
        with self.assertRaises(ctypes.ArgumentError):callback((470,138))
        with self.assertRaises(TypeError):wintypes.POINT.from_param((470,138))
        with self.assertRaisesRegex(AssertionError,'clipped'):self.runner.row_remove_point(row)
        self.assertEqual(received,[(470,138)])
        hit=next(e for e in self.runner.events if e['step']=='recent-clipping-native-hit')
        self.assertEqual((hit['hit'],hit['root'],hit['hit_pid'],hit['root_pid']),(90,90,123,123))
        self.assertFalse(any(e['step']=='recent-clipping-observation-failed' for e in self.runner.events))
        self.mouse.move.assert_not_called();self.mouse.click.assert_not_called()

    def drive_recent(self,close_whole_popup=False):
        popup,view,row,survivor=self.recent_fixture()
        combo=Control('Native QA peer','ComboBox','_DevicesCombo',self.root)
        self.runner.scope=Mock(return_value=self.root);self.runner.transfer=Mock();self.runner.click=Mock()
        self.runner.wait=self.immediate;self.runner.guard=Mock()
        count=[0]
        def physical(**values):
            if values['coords']==(470,138):
                row.visible=False
                if close_whole_popup:survivor.visible=False
            else:
                count[0]+=1
                if count[0]==2:survivor.visible=False
                elif count[0]==3:survivor.visible=True
                elif count[0]==4:survivor.visible=False
        self.mouse.click.side_effect=physical
        return row,survivor,combo

    def test_recent_deletes_one_row_by_physical_hover_then_click_and_reopens(self):
        row,survivor,combo=self.drive_recent()
        with patch.object(qa.time,'sleep'),patch.object(native_batch2.time,'sleep'):self.runner.recent()
        self.mouse.move.assert_called_once_with(coords=(470,138))
        actual=[call for call in self.mouse.click.call_args_list if call.kwargs['coords']==(470,138)]
        self.assertEqual(len(actual),1)
        self.assertTrue(any(e['step']=='actual-recent-removal-persists-on-reopen' for e in self.runner.events))
        self.runner.click.assert_called_once_with('back','transfer')

    def test_closing_popup_instead_of_deleting_row_does_not_pass(self):
        self.drive_recent(close_whole_popup=True)
        with patch.object(qa.time,'sleep'),patch.object(native_batch2.time,'sleep'):
            with self.assertRaisesRegex(AssertionError,'closed popup'):self.runner.recent()

    def editor_fixture(self,path_match=True):
        editor=Control('original-text.txt - Notepad','Window','Notepad',rectangle=(20,20,800,600),pid=789,handle=98)
        self.api.GetForegroundWindow.return_value=98;self.backend._window_pid.return_value=789
        self.runner.desktop.return_value.windows.return_value=[editor]
        expected=(self.base/'files'/'original-text.txt').resolve()
        self.runner.process_command=Mock(return_value=dict(ProcessId=789,ExecutablePath=r'C:\Windows\notepad.exe',
            CommandLine='notepad.exe "'+str(expected if path_match else self.base/'different'/'original-text.txt')+'"'))
        return editor,expected

    def test_external_editor_requires_new_foreground_and_full_original_path(self):
        editor,expected=self.editor_fixture();observed=self.runner.observe_editor({90},expected)
        self.assertEqual(observed['handle'],98);self.assertTrue(observed['full_expected_path'])
        self.assertIsNone(self.runner.observe_editor({98},expected))
        self.mouse.click.assert_not_called();self.backend.hotkey.assert_not_called()

    def test_basename_only_or_unknown_editor_is_not_success_and_never_receives_input(self):
        editor,expected=self.editor_fixture(False)
        with self.assertRaisesRegex(AssertionError,'full original'):self.runner.observe_editor({90},expected)
        self.runner.process_command.return_value['ExecutablePath']=r'C:\other\unknown.exe'
        with self.assertRaisesRegex(AssertionError,'allowed passive'):self.runner.observe_editor({90},expected)
        self.mouse.click.assert_not_called();self.mouse.move.assert_not_called()

    def test_own_or_producer_foreground_is_not_an_external_open_result(self):
        editor,expected=self.editor_fixture()
        for pid in (0,123,456):
            self.backend._window_pid.return_value=pid
            self.assertIsNone(self.runner.observe_editor(set(),expected))
        self.runner.process_command.assert_not_called()

    def test_events_keep_actual_bounds_without_exporting_command_line(self):
        editor,expected=self.editor_fixture();self.runner.observe_editor(set(),expected)
        stored=(self.base/'batch3-events.jsonl').read_text(encoding='utf8')
        self.assertNotIn('CommandLine',stored);self.assertNotIn(str(expected),stored)
        row=json.loads(stored.splitlines()[-1]);self.assertEqual(row['bounds'],[20,20,800,600]);self.assertTrue(row['full_expected_path'])

    def test_actions_are_exactly_three_qa_only_routes_before_empty_last(self):
        actions=json.loads((DIRECTORY/'ui-batch3-actions.json').read_text(encoding='utf8'))
        self.assertEqual(tuple(a['recipe'] for a in actions),qa.RECIPES)
        self.assertTrue(all(a['kind']=='batch3' and 'empty-zones-last' in a['ordering'] for a in actions))
        with self.assertRaises(ValueError):self.runner.run('unknown')
        self.mouse.click.assert_not_called()


if __name__=='__main__':unittest.main()
