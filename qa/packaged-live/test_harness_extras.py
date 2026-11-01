"""Portable recipes: all OS/UIA/clipboard interfaces are replaced by fakes."""
import ctypes,importlib.util,json,sys,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
spec=importlib.util.spec_from_file_location('candidate_qa',Path(__file__).with_name('ci_packaged_windows_live.py'))
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)
def rect(x,y,w,h):return types.SimpleNamespace(left=x,top=y,right=x+w,bottom=y+h)
def immediate(action,**kwargs):
 value=action()
 if not value:raise AssertionError('Portable required state absent: '+kwargs.get('label',''))
 return value

class Extras(unittest.TestCase):
 def setUp(self):
  self.mouse=types.SimpleNamespace(move=Mock(),press=Mock(),release=Mock(),click=Mock())
  self.patches=[patch.object(qa,'require_hosted'),patch.object(qa.time,'sleep'),patch.object(qa,'wait',side_effect=immediate),
                patch.dict(sys.modules,{'pywinauto':types.SimpleNamespace(mouse=self.mouse)})]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
 def backend(self):
  value=qa._UIABackend.__new__(qa._UIABackend);value.pid=11;value.producer_pid=22
  value._producer_topmost=Mock();value._assert_pointer_owner=Mock();value._assert_foreground=Mock();value._prepare_control=Mock()
  value.hotkey=Mock();return value
 def test_chooser_is_real_class_not_fabricated_painted_label_name(self):
  value=self.backend();label=types.SimpleNamespace(element_info=types.SimpleNamespace(class_name='Text'),is_visible=lambda:True,rectangle=lambda:rect(1,2,30,30))
  surface=types.SimpleNamespace(element_info=types.SimpleNamespace(class_name='TransferDropSurface'),is_visible=lambda:True,rectangle=lambda:rect(10,20,174,138))
  value.windows=lambda:[types.SimpleNamespace(descendants=lambda:[label,surface])]
  self.assertIs(value.find_class('TransferDropSurface'),surface);self.assertIsNone(value.find_class('局域网传输'))
 def drag(self,mode,missing=False):
  value=self.backend();window=types.SimpleNamespace(handle=1234,x=10,y=10,set_focus=Mock());window.rectangle=lambda:rect(window.x,window.y,480,290)
  source=types.SimpleNamespace(top_level_parent=lambda:window,rectangle=lambda:rect(window.x+30,window.y+160,420,34))
  target=types.SimpleNamespace(rectangle=lambda:rect(600,620,112,32));surface=types.SimpleNamespace(rectangle=lambda:rect(600,482,174,138))
  value.find=lambda name,**kwargs:source if name=='Drag original files' else target;value.find_class=Mock(return_value=None if missing else surface)
  api=types.SimpleNamespace(GetSystemMetrics=lambda index:1280 if index==0 else 900)
  api.GetWindowThreadProcessId=lambda handle,pointer:setattr(pointer._obj,'value',22)
  def place(handle,after,x,y,w,h,flags):window.x=x;window.y=y;return 1
  api.SetWindowPos=Mock(side_effect=place)
  with patch.object(ctypes,'windll',types.SimpleNamespace(user32=api)):
   if missing:
    with self.assertRaisesRegex(AssertionError,'TransferDropSurface'):value.drag_files('选择局域网或远程传输',choice_mode=mode)
   else:
    result=value.drag_files('选择局域网或远程传输',choice_mode=mode)
    expected=(687,round(482+138*(.25 if mode=='lan' else .75)))
    self.assertEqual(result['choice']['physical_drop'],list(expected));self.assertEqual(result['choice']['mode'],mode)
    self.assertEqual(self.mouse.release.call_args.kwargs['coords'],expected)
    value._assert_pointer_owner.assert_called_once_with(expected,11)
  self.assertEqual(self.mouse.press.call_count,1);self.assertEqual(self.mouse.release.call_count,1)
 def test_lan_choice_uses_visible_surface_top_quarter(self):self.drag('lan')
 def test_wan_choice_uses_visible_surface_bottom_quarter(self):self.drag('wan')
 def test_unexposed_surface_aborts_and_releases_without_simulating_drop_success(self):self.drag('lan',True)
 def test_focus_filter_uses_only_real_mouse_controls(self):
  value=self.backend();value.click=Mock();result=value.ui_extra('overview-focus-filter')
  self.assertEqual([v.args[0] for v in value.click.call_args_list],['搜索搁架内容','图片','全部']);self.assertEqual(len(result['events']),3)
  self.assertEqual([v.kwargs['roles'] for v in value.click.call_args_list],[('Edit',),('CheckBox',),('CheckBox',)])
 def test_queue_remove_requires_three_actual_survivors(self):
  value=self.backend();removed=[False];value.drag_files=Mock(return_value={'os_drag':True})
  def click(name,**kwargs):
   if name=='移除待发送文件 original-text.txt':removed[0]=True
  value.click=Mock(side_effect=click);value.find=lambda name,**kwargs:None if removed[0] and name.endswith('original-text.txt') else object()
  result=value.ui_extra('queue-multiple-remove');self.assertIn('three-real-survivors',[r['step'] for r in result['events']])
  self.assertEqual(value.click.call_args_list[1].kwargs['roles'],('CheckBox',))
  value.find=lambda name,**kwargs:None
  with self.assertRaises(AssertionError):value.ui_extra('queue-multiple-remove')
 def test_mini_popup_inside_x_and_owned_outside_click_close_are_distinct(self):
  value=self.backend();opened=[False];window=types.SimpleNamespace(rectangle=lambda:rect(5,6,480,290),set_focus=Mock())
  editor=types.SimpleNamespace(top_level_parent=lambda:window)
  def find(name,**kwargs):
   if name=='Synthetic external editor':return editor
   if name=='关闭剪贴板预览':return object() if opened[0] else None
   return None
  def click(name,**kwargs):opened[0]=name=='放大查看剪贴板'
  self.mouse.click.side_effect=lambda **kwargs:opened.__setitem__(0,False)
  value.find=find;value.click=Mock(side_effect=click);result=value.ui_extra('mini-clipboard-close-routes')
  self.assertEqual(sum(c.args[0]=='放大查看剪贴板' for c in value.click.call_args_list),3)
  self.assertEqual(result['events'][-1]['step'],'outside-owned-blank-close-finished');self.assertFalse(opened[0])
  value._assert_pointer_owner.assert_called_once_with((85,21),22)
  self.assertEqual(window.set_focus.call_count,0,'The real caption click must activate the external producer')
 def test_extra_rpc_has_a_finite_deadline_and_uses_no_invoke_or_runtime_callback(self):
  driver=qa.Driver.__new__(qa.Driver);driver._rpc=Mock(return_value={'events':[]})
  self.assertEqual(driver.ui_extra('choice-lan'),{'events':[]})
  driver._rpc.assert_called_once_with('ui_extra',seconds=40,recipe='choice-lan')
  value=self.backend()
  with self.assertRaisesRegex(ValueError,'Unsupported bounded extra recipe'):value.ui_extra('not-a-recipe')
 def test_plan_is_seven_bounded_recipes_no_success_claim(self):
  actions=json.loads(Path(__file__).with_name('ui-extra-actions.json').read_text());self.assertEqual(len(actions),7)
  self.assertEqual(len({a['recipe'] for a in actions}),7);self.assertTrue(all(a['kind']=='ui_extra' for a in actions))
  self.assertTrue(all('passed' not in a for a in actions))
 def test_default_close_uses_real_edge_hover_and_waits_for_hidden_preview(self):
  value=self.backend();visible=[True];value.click=Mock(side_effect=lambda name,**kwargs:visible.__setitem__(0,False))
  value.find=lambda name,**kwargs:object() if visible[0] else None
  value.hover=Mock(side_effect=lambda name:visible.__setitem__(0,True))
  result=value.ui_extra('default-edge-close');self.assertFalse(visible[0]);value.hover.assert_called_once_with('FlickDock edge')
  self.assertEqual(result['events'][-1]['step'],'default-close-finished')
 def test_manual_close_requires_a_measured_header_move_before_clicking_close(self):
  value=self.backend();position=[0,0];visible=[True];pressed=[False]
  close=types.SimpleNamespace(rectangle=lambda:rect(1000+position[0],80+position[1],32,32))
  network=types.SimpleNamespace(rectangle=lambda:rect(900,80,32,32))
  def find(name,**kwargs):
   if name.startswith('云端网络'):return network
   if name=='收起到屏幕边缘':return close
   if name=='搁架文件预览':return object() if visible[0] else None
   return None
  value.find=find;value.click=Mock(side_effect=lambda name,**kwargs:visible.__setitem__(0,False))
  def pressed_at(**kwargs):pressed[0]=True
  def moved(**kwargs):
   if pressed[0]:position[:]=[kwargs['coords'][0]-888,kwargs['coords'][1]-96]
  self.mouse.press.side_effect=pressed_at;self.mouse.move.side_effect=moved;self.mouse.release.side_effect=lambda **kwargs:pressed.__setitem__(0,False)
  result=value.ui_extra('manual-position-close');self.assertEqual(position,[-100,50]);self.assertFalse(visible[0])
  self.assertEqual(result['events'][-1]['step'],'manual-close-finished');self.assertEqual(self.mouse.release.call_count,1)

if __name__=='__main__':unittest.main(verbosity=2)
