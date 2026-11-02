"""Portable batch2 safety fixtures; every native/COM interface is a fake."""
import importlib.util,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
SPEC=importlib.util.spec_from_file_location('batch2_qa',Path(__file__).with_name('native_batch2.py'))
qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)
HOST_GUARD=qa.require_hosted


class Control:
 def __init__(self,name,role='Button',cls='IconTextButton',parent=None,enabled=True,visible=True,bounds=(10,20,110,60)):
  self.element_info=types.SimpleNamespace(name=name,control_type=role,class_name=cls)
  self.children=[];self.owner=parent;self.enabled=enabled;self.visible=visible;self.handle=90;self.box=bounds
  if parent:parent.children.append(self)
 def descendants(self,control_type=None):
  result=[]
  for child in self.children:result.extend([child,*child.descendants()])
  return [c for c in result if control_type is None or c.element_info.control_type==control_type]
 def is_visible(self):return self.visible
 def is_enabled(self):return self.enabled
 def rectangle(self):
  x,y,r,b=self.box;return types.SimpleNamespace(left=x,top=y,right=r,bottom=b)
 def parent(self):return self.owner
 def top_level_parent(self):return self.owner.top_level_parent() if self.owner else self
 def get_toggle_state(self):return self.toggle


class Batch2Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
  (self.base/'files').mkdir()
  for name in qa.ORIGINALS:(self.base/'files'/name).write_bytes(('original '+name).encode())
  self.mouse=types.SimpleNamespace(move=Mock(),click=Mock(),double_click=Mock(),press=Mock(),release=Mock())
  self.root=Control('FlickDock','Window','ShelfWindow');self.root.handle=90
  self.api=types.SimpleNamespace(GetAncestor=Mock(side_effect=lambda h,flag:h),WindowFromPoint=Mock(return_value=90),
                                 GetForegroundWindow=Mock(return_value=90))
  self.backend=types.SimpleNamespace(pid=123,producer_pid=456,base=self.base,windows=Mock(side_effect=lambda:[self.root]),
    _producer_topmost=Mock(),_assert_pointer_owner=Mock(),_assert_foreground=Mock(),_window_pid=Mock(return_value=123),
    _input_api=Mock(return_value=self.api),hotkey=Mock(),tree=Mock(return_value=[]))
  self.patches=[patch.object(qa,'require_hosted'),patch.object(qa.time,'sleep'),
    patch.dict(sys.modules,{'pywinauto':types.SimpleNamespace(mouse=self.mouse)})]
  for p in self.patches:p.start();self.addCleanup(p.stop)
  self.runner=qa.Batch2(self.backend)
 def test_host_guard_refuses_local_desktop(self):
  with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,{'GITHUB_ACTIONS':'false','RUNNER_OS':'Windows'}):
   with self.assertRaisesRegex(RuntimeError,'refuses'):HOST_GUARD()
 def test_exact_scope_role_class_and_disabled_read_are_preserved(self):
  wrong=Control('打包所选','Text','QLabel',self.root)
  correct=Control('打包所选',parent=self.root,enabled=False)
  self.assertEqual(self.runner.matches('package',self.root),[])
  self.assertEqual(self.runner.matches('package',self.root,enabled=False),[correct])
  self.runner.package_enabled(False)
  self.assertEqual(self.runner.events[-1]['value'],False)
 def test_duplicates_fail_instead_of_picking_first_global_match(self):
  Control('图标视图','CheckBox','IconTextButton',self.root);Control('图标视图','CheckBox','IconTextButton',self.root)
  with self.assertRaisesRegex(AssertionError,'Ambiguous'):self.runner.resolve('grid')
  self.mouse.click.assert_not_called()
 def test_modes_match_actual_normal_windows_uia_class_not_qt_base_class(self):
  # Recorded exact name/role/class from run41268257366 failure-accessibility;
  # all checks retain an actual transfer-window scope and uniqueness.
  transfer=Control('FlickDock · 文件互传','Window','LanDialog')
  self.backend.windows.side_effect=lambda:[self.root,transfer]
  for key,name in (('lan','局域网传输'),('wan','远程传输')):
   mode=Control(name,'CheckBox','_PressButton',transfer)
   Control(name,'CheckBox','QPushButton',self.root)
   self.assertIs(self.runner.resolve(key,'transfer'),mode)
   self.assertEqual(qa.SELECTORS[key],(name,'CheckBox','_PressButton'))
  duplicate=Control('局域网传输','CheckBox','_PressButton',transfer)
  with self.assertRaisesRegex(AssertionError,'Ambiguous'):self.runner.resolve('lan','transfer')
 def test_physical_click_guard_and_deleted_wrapper_are_safe(self):
  control=Control('Synthetic delete',parent=self.root)
  self.mouse.click.side_effect=lambda **_:setattr(control,'rectangle',Mock(side_effect=RuntimeError('native object removed')))
  self.runner.input(control)
  self.mouse.click.assert_called_once_with(button='left',coords=(60,40))
  self.assertEqual(self.runner.events[-1]['input_bounds'],[10,20,110,60])
  self.backend._assert_pointer_owner.assert_called_once_with((60,40),123)
 def test_wrong_native_root_prevents_any_input(self):
  control=Control('Open',parent=self.root);self.api.WindowFromPoint.return_value=999
  with self.assertRaisesRegex(AssertionError,'Native root'):self.runner.input(control)
  self.mouse.click.assert_not_called()
 def test_double_click_uses_pywinauto_069_supported_signature(self):
  # The pinned mouse.double_click API has no interval keyword. A permissive
  # Mock hid this controller error until the actual frozen GUI run.
  sent=[]
  def double_click(button='left',coords=(0,0)):
   sent.append((button,coords))
  self.mouse.double_click=double_click
  control=Control('Synthetic file body',parent=self.root)
  self.runner.input(control,kind='double')
  self.assertEqual(sent,[('left',(60,40))])
  self.backend._assert_pointer_owner.assert_called_once_with((60,40),123)
  self.assertEqual(self.runner.events[-1]['input_bounds'],[10,20,110,60])
 def test_partly_visible_card_body_intersects_actual_ancestor_viewport_above_footer(self):
  self.root.box=(21,7,1001,711)
  viewport=Control('','Custom','QWidget',self.root,bounds=(247,131,659,582))
  content=Control('','Custom','QWidget',viewport,bounds=(247,131,659,850))
  card=Control('original-text.txt','Group','IconItem',content,bounds=(247,498,381,666))
  point=self.runner.body_point(card)
  self.assertEqual(point,(314,557));self.assertGreater(point[1],498+32)
  self.assertLess(point[1],582);self.assertNotEqual(point,qa.centre(card,(.5,.55)))
  self.assertEqual(self.runner.events[-1]['clipped_body'],[276,535,352,582])
  self.assertEqual(self.runner.events[-1]['ancestors'][2]['bounds'],[247,131,659,582])
 def test_fully_clipped_card_or_header_only_sliver_never_clicks_footer_or_checkbox(self):
  self.root.box=(21,7,1001,711)
  viewport=Control('','Custom','QWidget',self.root,bounds=(247,131,659,582))
  for top in (590,570):
   with self.subTest(top=top):
    card=Control('original-text.txt','Group','IconItem',viewport,bounds=(247,top,381,top+168))
    with self.assertRaisesRegex(AssertionError,'fully clipped'):self.runner.body_point(card)
  self.mouse.click.assert_not_called();self.mouse.double_click.assert_not_called()
 def test_partly_visible_list_row_uses_body_away_from_checkbox_and_delete(self):
  self.root.box=(21,7,1001,711)
  viewport=Control('','Custom','QWidget',self.root,bounds=(247,131,659,582))
  row=Control('','Group','FileRow',viewport,bounds=(247,560,659,618))
  point=self.runner.body_point(row)
  self.assertGreater(point[0],247+40);self.assertLess(point[0],659-32);self.assertLess(point[1],582)
 def test_cancelled_open_always_releases_physical_button_on_motion_error(self):
  control=Control('打开',parent=self.root)
  card=Control('original-text.txt','Group','IconItem',self.root)
  self.runner.overview=Mock();self.runner.click=Mock();self.runner.input=Mock();self.runner.guard=Mock()
  self.runner.resolve=Mock(side_effect=lambda key:card if key=='grid_text' else control);self.mouse.move.side_effect=RuntimeError('synthetic OS input failure')
  with self.assertRaisesRegex(RuntimeError,'synthetic OS'):self.runner.detail()
  self.mouse.press.assert_called_once();self.mouse.release.assert_called_once_with(button='left',coords=(2,40))
 def test_checkbox_state_is_only_read(self):
  control=Control('Theme','CheckBox');control.toggle=1
  control.toggle_method=Mock();self.assertEqual(qa.selected(control),1);control.toggle_method.assert_not_called()
 def test_selection_resets_via_real_controls_and_double_clicks_body(self):
  self.runner.overview=Mock();self.runner.click=Mock();self.runner.package_enabled=Mock()
  card=Control('original-text.txt','Group','IconItem',self.root);row=Control('','Group','FileRow',self.root)
  check=Control('选择 original-text.txt','CheckBox','SelectionCheck',row);check.toggle=0
  self.runner.resolve=Mock(side_effect=lambda key,**_:card if key=='grid_text' else check)
  self.runner.matches=Mock(return_value=[])
  def physical(c,kind='click',point=None):
   if c is row:check.toggle=0 if kind=='double' else 1-check.toggle
  self.runner.input=Mock(side_effect=physical);self.runner.selection()
  self.assertEqual([c.args[0] for c in self.runner.click.call_args_list[:3]],['grid','select_all','deselect_all'])
  doubles=[c for c in self.runner.input.call_args_list if c.kwargs.get('kind')=='double']
  self.assertEqual([c.args[0] for c in doubles],[card,row]);self.assertEqual(check.toggle,0)
 def test_context_mini_all_checks_entire_observed_shelf_including_peer(self):
  names=[*qa.ORIGINALS,'peer-shared.txt'];cards=[Control(name,'Group','IconItem',self.root) for name in names]
  Control('5 项','Text','QLabel',self.root)
  mini=Control('搁架文件预览','Client','MiniStack',self.root)
  self.runner.overview=Mock();self.runner.click=Mock();self.runner.input=Mock();self.runner.menu_action=Mock()
  self.runner.resolve=Mock(return_value=cards[1]);self.runner.matches=Mock(return_value=[mini]);self.runner.queue=Mock()
  self.runner.context()
  mini_calls=[c for c in self.runner.queue.call_args_list if c.kwargs.get('exact')]
  self.assertEqual(len(mini_calls),2);self.assertEqual(mini_calls[0].args[0],sorted(names))
  self.assertEqual([c.args[0] for c in self.runner.menu_action.call_args_list],['lan','wan','lan','wan'])
 def test_theme_repeats_and_restores_visible_initial_selection(self):
  initial={'light':0,'dark':1};controls={key:Control(key,'CheckBox') for key in initial}
  for key,c in controls.items():c.toggle=initial[key]
  self.runner.overview=Mock();self.runner.scope=Mock(return_value=self.root)
  self.runner.resolve=Mock(side_effect=lambda key,*_:controls[key])
  def click(key,*_):
   if key in controls:
    for name,c in controls.items():c.toggle=int(name==key)
  self.runner.click=Mock(side_effect=click);self.runner.wait=Mock(return_value=True)
  self.runner.theme();self.assertEqual({key:c.toggle for key,c in controls.items()},initial)
  sequence=[c.args[0] for c in self.runner.click.call_args_list]
  self.assertEqual(sequence[-3:],['general','dark','settings_close']);self.assertEqual(sequence.count('light'),2)
 def test_original_hash_guard_detects_actual_changes(self):
  self.runner.originals_unchanged();(self.base/'files'/'tiny.bin').write_bytes(b'changed')
  with self.assertRaisesRegex(AssertionError,'original bytes changed'):self.runner.originals_unchanged()
 def test_safe_leave_uses_actual_control_not_transparent_window_corner(self):
  empty=Control('empty','Border','OverviewEmptyDropZone',self.root);safe=Control('设置',parent=self.root,bounds=(25,140,100,170))
  self.runner.resolve=Mock(return_value=empty);self.runner.input=Mock();self.runner.hover_leave('empty_overview','overview')
  self.mouse.move.assert_called_once_with(coords=(62,155))
 def explorer_fixture(self,*,wrong_active_address=False,wrong_selected=False,wrong_folder=False,
                      tab_name='files',tab_selected=True,extra_tab=False,no_selection=False,
                      wrong_selected_dir=False,existing=False,multiple_selection=False):
  expected=(self.base/'files').resolve();folder=Control('folder',parent=self.root)
  old=Control('Old unrelated folder','Window','CabinetWClass');old.handle=210
  new=Control('File Explorer','Window','CabinetWClass');new.handle=321
  Control('Address: '+str(expected if not wrong_active_address else self.base/'other'),'ToolBar','ToolbarWindow32',new)
  tab=Control(tab_name,'TabItem','TabItem',new);tab.iface_selection_item=types.SimpleNamespace(CurrentIsSelected=tab_selected)
  if extra_tab:
   other=Control('other','TabItem','TabItem',new);other.iface_selection_item=types.SimpleNamespace(CurrentIsSelected=False)
  active=[existing];desktop=types.SimpleNamespace(windows=lambda **_:[old,*([new] if active[0] else [])])
  self.runner.input=Mock(side_effect=lambda *_:active.__setitem__(0,True))
  selected_path=(self.base/'other'/'tiny.bin') if wrong_selected_dir else expected/('other.bin' if wrong_selected else 'tiny.bin')
  selected_items=types.SimpleNamespace(Count=0 if no_selection else 2 if multiple_selection else 1,Item=lambda index:types.SimpleNamespace(Path=str(selected_path)))
  document=types.SimpleNamespace(Folder=types.SimpleNamespace(Self=types.SimpleNamespace(Path=str(self.base/'other' if wrong_folder else expected))),SelectedItems=lambda:selected_items)
  shell=types.SimpleNamespace(Windows=lambda:[types.SimpleNamespace(HWND=321,Document=document)])
  keys=Mock(side_effect=lambda *_a,**_k:active.__setitem__(0,False))
  new.set_focus=Mock(side_effect=lambda:self.api.GetForegroundWindow.configure_mock(return_value=321))
  self.backend._window_pid.side_effect=lambda handle:123 if handle==90 else 999
  self.runner.wait=lambda action,label,**kwargs:action() or (_ for _ in ()).throw(AssertionError(label))
  modules={'pywinauto':types.SimpleNamespace(mouse=self.mouse,Desktop=lambda **_:desktop),
           'pywinauto.keyboard':types.SimpleNamespace(send_keys=keys),
           'win32com':types.SimpleNamespace(),'win32com.client':types.SimpleNamespace(Dispatch=lambda _:shell)}
  return folder,old,new,keys,modules
 def test_history_reveal_requires_exact_active_path_and_selected_full_path_then_closes_only_new_window(self):
  folder,old,new,keys,modules=self.explorer_fixture()
  with patch.dict(sys.modules,modules):self.runner.reveal(folder)
  keys.assert_called_once_with('%{F4}',pause=.02);new.set_focus.assert_called_once()
  self.assertEqual(self.runner.events[-1]['handle'],321);self.backend._assert_foreground.assert_called_once_with(999)
 def test_history_reveal_bad_tab_selection_full_path_or_old_window_fails_without_closing_any_window(self):
  for kwargs in ({'tab_name':'other'},{'tab_selected':False},{'extra_tab':True},{'no_selection':True},
                 {'wrong_selected':True},{'wrong_folder':True},{'wrong_selected_dir':True},{'existing':True},{'multiple_selection':True}):
   with self.subTest(kwargs=kwargs):
    folder,old,new,keys,modules=self.explorer_fixture(**kwargs)
    with patch.dict(sys.modules,modules),self.assertRaisesRegex(AssertionError,'unique Explorer'):self.runner.reveal(folder)
    keys.assert_not_called();new.set_focus.assert_not_called()
 def test_toolbar_name_is_not_path_proof_active_single_tab_and_com_paths_are_both_required(self):
  folder,old,new,keys,modules=self.explorer_fixture(wrong_active_address=True)
  with patch.dict(sys.modules,modules):self.runner.reveal(folder)
  keys.assert_called_once_with('%{F4}',pause=.02)
  row=json.loads((self.base/'batch2-explorer-diagnostics.jsonl').read_text(encoding='utf8').splitlines()[-1])
  current=next(w for w in row['windows'] if w['handle']==321)
  self.assertFalse(current['address'][0]['name_matches_full_expected'])
  self.assertTrue(current['tabs'][0]['selected']);self.assertEqual(row['documents'][0]['selected_count'],1)
 def test_rejected_multi_tab_still_records_real_com_path_and_active_tab_without_input(self):
  folder,old,new,keys,modules=self.explorer_fixture(wrong_active_address=True)
  secret=Control('private-other-tab','TabItem','TabItem',new);secret.iface_selection_item=types.SimpleNamespace(CurrentIsSelected=False)
  with patch.dict(sys.modules,modules),self.assertRaisesRegex(AssertionError,'unique Explorer'):self.runner.reveal(folder)
  row=json.loads((self.base/'batch2-explorer-diagnostics.jsonl').read_text(encoding='utf8').splitlines()[-1])
  self.assertEqual(row['documents'][0]['folder'],str((self.base/'files').resolve()))
  self.assertEqual(row['documents'][0]['selected_paths'],[str((self.base/'files'/'tiny.bin').resolve())])
  current=next(w for w in row['windows'] if w['handle']==321)
  self.assertEqual(current['tabs'][0]['name'],'files');self.assertTrue(current['tabs'][0]['selected'])
  self.assertTrue(current['tabs'][1]['name'].startswith('sha256:'));self.assertNotIn('private-other-tab',json.dumps(row))
  self.assertTrue(current['known_fixture_folder']);self.assertEqual(current['address'][0]['name'],'Address: '+str(self.base/'other'))
  keys.assert_not_called();self.mouse.click.assert_not_called()
 def test_explorer_observation_limits_and_redacts_unrelated_paths_without_any_input(self):
  unrelated=Control('private-folder','Window','CabinetWClass');unrelated.handle=321
  Control(r'Address: X:\private\folder','ToolBar','ToolbarWindow32',unrelated)
  desktop=types.SimpleNamespace(windows=lambda **_:[unrelated]*20)
  documents=[types.SimpleNamespace(HWND=321,Document=types.SimpleNamespace(
   Folder=types.SimpleNamespace(Self=types.SimpleNamespace(Path=r'X:\private\folder')),
   SelectedItems=lambda:types.SimpleNamespace(Count=1,Item=lambda i:types.SimpleNamespace(Path=r'X:\private\secret.txt'))))]*20
  self.runner.observe_explorer(desktop,types.SimpleNamespace(Windows=lambda:documents),(self.base/'files').resolve(),set())
  raw=(self.base/'batch2-explorer-diagnostics.jsonl').read_text(encoding='utf8');row=json.loads(raw)
  self.assertEqual(len(row['windows']),8);self.assertEqual(len(row['documents']),12)
  self.assertNotIn('private',raw);self.assertNotIn('secret.txt',raw)
  self.mouse.click.assert_not_called();self.mouse.press.assert_not_called();self.backend.hotkey.assert_not_called()
 def test_unknown_recipe_has_no_input_and_plan_has_ordering(self):
  with self.assertRaisesRegex(ValueError,'Unknown'):self.runner.run('anything')
  self.mouse.click.assert_not_called()
  actions=json.loads(Path(__file__).with_name('ui-batch2-actions.json').read_text())
  self.assertEqual(len(actions),7);self.assertEqual(actions[0]['recipe'],'history-folder')
  self.assertEqual(actions[-1]['recipe'],'empty-zones-last')
  self.assertTrue(all(a['kind']=='batch2' and a['ordering'] for a in actions));self.assertTrue(all('passed' not in a for a in actions))


if __name__=='__main__':unittest.main(verbosity=2)
