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
 def test_cancelled_open_always_releases_physical_button_on_motion_error(self):
  control=Control('打开',parent=self.root)
  self.runner.overview=Mock();self.runner.click=Mock();self.runner.input=Mock();self.runner.guard=Mock()
  self.runner.resolve=Mock(return_value=control);self.mouse.move.side_effect=RuntimeError('synthetic OS input failure')
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
 def explorer_fixture(self,*,wrong_active_address=False,wrong_selected=False):
  expected=(self.base/'files').resolve();folder=Control('folder',parent=self.root)
  old=Control('Old unrelated folder','Window','CabinetWClass');old.handle=210
  new=Control('File Explorer','Window','CabinetWClass');new.handle=321
  Control('Address: '+str(expected if not wrong_active_address else self.base/'other'),'ToolBar','ToolbarWindow32',new)
  active=[False];desktop=types.SimpleNamespace(windows=lambda **_:[old,*([new] if active[0] else [])])
  self.runner.input=Mock(side_effect=lambda *_:active.__setitem__(0,True))
  selected_items=types.SimpleNamespace(Count=1,Item=lambda index:types.SimpleNamespace(Path=str(expected/('other.bin' if wrong_selected else 'tiny.bin'))))
  document=types.SimpleNamespace(Folder=types.SimpleNamespace(Self=types.SimpleNamespace(Path=str(expected))),SelectedItems=lambda:selected_items)
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
 def test_history_reveal_wrong_active_tab_or_selected_file_fails_without_closing_any_window(self):
  for kwargs in ({'wrong_active_address':True},{'wrong_selected':True}):
   with self.subTest(kwargs=kwargs):
    folder,old,new,keys,modules=self.explorer_fixture(**kwargs)
    with patch.dict(sys.modules,modules),self.assertRaisesRegex(AssertionError,'unique Explorer'):self.runner.reveal(folder)
    keys.assert_not_called();new.set_focus.assert_not_called()
 def test_unknown_recipe_has_no_input_and_plan_has_ordering(self):
  with self.assertRaisesRegex(ValueError,'Unknown'):self.runner.run('anything')
  self.mouse.click.assert_not_called()
  actions=json.loads(Path(__file__).with_name('ui-batch2-actions.json').read_text())
  self.assertEqual(len(actions),7);self.assertEqual(actions[0]['recipe'],'history-folder')
  self.assertEqual(actions[-1]['recipe'],'empty-zones-last')
  self.assertTrue(all(a['kind']=='batch2' and a['ordering'] for a in actions));self.assertTrue(all('passed' not in a for a in actions))


if __name__=='__main__':unittest.main(verbosity=2)
