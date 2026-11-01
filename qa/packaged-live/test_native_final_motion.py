"""Portable safety/negative fixtures. Every UIA/OS object is a fake."""
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
    SPEC=importlib.util.spec_from_file_location('final_motion_qa',DIRECTORY/'native_final_motion.py')
    qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)
finally:sys.path.remove(str(DIRECTORY))
HOST_GUARD=qa.require_hosted


class Control:
    def __init__(self,name,role,cls,parent=None,rectangle=(10,20,42,52)):
        self.element_info=types.SimpleNamespace(name=name,control_type=role,class_name=cls)
        self.children=[];self.owner=parent;self.box=rectangle;self.handle=90;self.visible=True;self.toggle=1
        if parent:parent.children.append(self)
    def descendants(self):
        return [item for child in self.children for item in (child,*child.descendants())]
    def rectangle(self):
        a,b,c,d=self.box;return types.SimpleNamespace(left=a,top=b,right=c,bottom=d)
    def top_level_parent(self):return self.owner.top_level_parent() if self.owner else self
    def is_visible(self):return self.visible
    def is_enabled(self):return True
    def get_toggle_state(self):return self.toggle


class FinalMotionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        (self.base/'files').mkdir()
        import native_batch2
        for name in native_batch2.ORIGINALS:(self.base/'files'/name).write_bytes(('synthetic '+name).encode())
        self.root=Control('FlickDock','Window','ShelfWindow',rectangle=(0,0,1000,700))
        self.mouse=types.SimpleNamespace(move=Mock(),click=Mock(),press=Mock(),release=Mock())
        self.api=types.SimpleNamespace(GetForegroundWindow=Mock(return_value=90),
            GetAncestor=Mock(side_effect=lambda h,flag:h),WindowFromPoint=Mock(return_value=90),
            GetSystemMetrics=Mock(side_effect=lambda index:{0:1920,1:1080,76:0,77:0,78:1920,79:1080}[index]))
        self.backend=types.SimpleNamespace(pid=123,producer_pid=456,base=self.base,
            windows=Mock(side_effect=lambda pid=None:[self.root]),hotkey=Mock(),
            _producer_topmost=Mock(),_assert_pointer_owner=Mock(),
            _window_pid=Mock(side_effect=lambda handle:123 if handle==90 else 456),
            _input_api=Mock(return_value=self.api))
        patches=[patch.object(qa,'require_hosted'),patch.object(native_batch2,'require_hosted'),
                 patch.dict(sys.modules,{'pywinauto':types.SimpleNamespace(mouse=self.mouse)})]
        for p in patches:p.start();self.addCleanup(p.stop)
        self.runner=qa.FinalMotion(self.backend);self.runner.recipe='portable'

    def header_controls(self):
        names={'network':('云端网络畅通','Group','NetworkStatus'),
               'cloud':('云同步：已开启，打开设置','Button','ToolbarButton'),
               'pin':('取消钉住','CheckBox','ToolbarButton'),
               'expand':('展开详情','Button','ToolbarButton'),
               'more':('更多操作','Button','ToolbarButton'),
               'close':('收起到屏幕边缘','Button','ToolbarButton')}
        return {key:Control(*names[key],self.root,rectangle=(100+38*i,20,132+38*i,52))
                for i,key in enumerate(qa.HEADER)}

    def immediate_wait(self,action,label,seconds=2):
        return action() or (_ for _ in ()).throw(AssertionError(label))

    def test_host_guard_requires_all_four_ephemeral_markers(self):
        markers={'GITHUB_ACTIONS':'true','RUNNER_OS':'Windows','RUNNER_ENVIRONMENT':'github-hosted'}
        for key in markers:
            with self.subTest(key=key),patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,{**markers,key:'false'}):
                with self.assertRaisesRegex(RuntimeError,'refuses'):HOST_GUARD()
        with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,markers):HOST_GUARD()
        with patch.object(qa.sys,'platform','linux'),patch.dict(qa.os.environ,markers):
            with self.assertRaises(RuntimeError):HOST_GUARD()

    def test_selectors_are_exact_class_role_and_owned_scope(self):
        controls=self.header_controls()
        Control('取消钉住','Button','ToolbarButton',self.root)
        Control('云同步：已开启，打开设置','Button','CloudSelector',self.root)
        self.assertIs(self.runner.named('pin'),controls['pin'])
        self.assertIs(self.runner.named('cloud'),controls['cloud'])
        Control('取消钉住','CheckBox','ToolbarButton',self.root)
        with self.assertRaisesRegex(AssertionError,'Ambiguous'):self.runner.target('pin')
        self.mouse.click.assert_not_called()

    def test_raw_header_rows_preserve_absolute_rect_and_measurement_timestamp(self):
        controls=self.header_controls();rows=self.runner.header('before')
        self.assertEqual(tuple(rows),qa.HEADER)
        for key,row in rows.items():
            self.assertEqual(row['bounds'],list(controls[key].box));self.assertGreater(row['monotonic'],0)
        stored=[json.loads(line) for line in (self.base/'final-motion-events.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertEqual([row['step'] for row in stored],['before:'+key for key in qa.HEADER])

    def test_mini_uses_actual_observed_expand_accessible_name_not_only_its_tooltip(self):
        control=self.header_controls()['expand']
        self.assertEqual(control.element_info.name,'展开详情')
        self.assertIs(self.runner.named('expand'),control);self.assertEqual(self.runner.mode(),'mini')
        control.element_info.name='展开内容';self.assertEqual(self.runner.mode(),'mini')
        control.element_info.name='收起详情';self.assertEqual(self.runner.mode(),'overview')

    def prepare_header_recipe(self,shift=False):
        controls=self.header_controls();original=dict(mode='overview',pinned=False)
        self.runner.state=Mock(return_value=original);self.runner.set_pin=Mock();self.runner.show_mode=Mock()
        self.runner.mode=Mock(return_value='overview');self.runner.settle=Mock();self.runner.restore=Mock()
        def click(control,**_):
            controls['expand'].element_info.name='收起详情'
            if shift:
                a,b,c,d=controls['network'].box;controls['network'].box=(a+1,b,c+1,d)
        self.runner.input=Mock(side_effect=click)
        return original

    def test_header_strict_endpoint_comparison_and_original_state_restore(self):
        original=self.prepare_header_recipe();self.runner.header_fixed_expand()
        self.runner.restore.assert_called_once_with(original)
        comparison=next(r for r in self.runner.events if r['step']=='absolute-header-rect-comparison')
        self.assertEqual(comparison['changed'],{});self.assertEqual(len(comparison['before']),6)

    def test_one_pixel_header_shift_fails_without_relative_offset_normalization(self):
        original=self.prepare_header_recipe(shift=True)
        with self.assertRaisesRegex(AssertionError,'changed absolute endpoint bounds: network'):
            self.runner.header_fixed_expand()
        self.runner.restore.assert_called_once_with(original)

    def test_restore_failure_does_not_hide_original_rect_failure(self):
        self.prepare_header_recipe(shift=True);self.runner.restore.side_effect=RuntimeError('synthetic restoration failure')
        with self.assertRaisesRegex(AssertionError,'changed absolute endpoint'):
            self.runner.header_fixed_expand()
        self.assertEqual(self.runner.events[-1]['step'],'restore-failed')

    def test_pin_is_read_only_uia_and_changed_by_real_click(self):
        controls=self.header_controls();pin=controls['pin'];pin.Toggle=Mock()
        def click(**_):pin.toggle=0;pin.element_info.name='钉住搁架'
        self.mouse.click.side_effect=click;self.runner.set_pin(False)
        self.mouse.click.assert_called_once_with(button='left',coords=(192,36));pin.Toggle.assert_not_called()

    def test_physical_click_does_not_reread_destroyed_provider(self):
        c=self.header_controls()['expand']
        self.mouse.click.side_effect=lambda **_:setattr(c,'rectangle',Mock(side_effect=RuntimeError('deleted wrapper')))
        self.runner.input(c)
        self.assertEqual(self.runner.events[-1]['input_bounds'],[214,20,246,52])

    def popup_fixture(self):
        popup=Control('FlickDock · 剪贴板预览','Window','ClipboardPreview',rectangle=(900,150,1300,450));popup.handle=91
        current=[self.root,popup]
        self.backend.windows.side_effect=lambda pid=None:[w for w in current if w.visible]
        self.runner.leave_point=Mock(return_value=(1400,50));self.runner.wait=self.immediate_wait
        self.runner.cursor=Mock(return_value=[1400,50])
        return popup,current

    def test_popup_requires_actual_title_role_class_and_unique_owned_window(self):
        popup,current=self.popup_fixture();self.assertIs(self.runner.popup(),popup)
        wrong=Control('FlickDock · 剪贴板预览','Window','QDialog');current.append(wrong)
        self.assertIs(self.runner.popup(),popup)
        current.append(Control('FlickDock · 剪贴板预览','Window','ClipboardPreview'))
        with self.assertRaisesRegex(AssertionError,'Duplicate'):self.runner.popup()

    def test_popup_can_be_an_actual_visible_owned_descendant_not_a_desktop_root(self):
        popup=Control('FlickDock · 剪贴板预览','Window','ClipboardPreview',self.root);popup.handle=91
        self.assertIs(self.runner.popup(),popup)
        popup.visible=False;self.assertIsNone(self.runner.popup())
        popup.visible=True;popup.element_info.control_type='Group';self.assertIsNone(self.runner.popup())

    def test_same_actual_popup_in_root_and_descendants_is_not_two_windows(self):
        popup=Control('FlickDock · 剪贴板预览','Window','ClipboardPreview',self.root);popup.handle=91
        self.backend.windows.side_effect=lambda pid=None:[self.root,popup]
        self.assertIs(self.runner.popup(),popup)
        other=Control('FlickDock · 剪贴板预览','Window','ClipboardPreview',self.root);other.handle=92
        with self.assertRaisesRegex(AssertionError,'Duplicate'):self.runner.popup()

    def test_popup_without_hwnd_uses_observed_runtime_identity_and_stays_visible_strict(self):
        popup=Control('FlickDock · 剪贴板预览','Window','ClipboardPreview',self.root);popup.handle=0
        popup.element_info.runtime_id=[42,100,9]
        self.assertIs(self.runner.popup(),popup)
        popup.visible=False;self.assertIsNone(self.runner.popup())

    def test_popup_without_observable_identity_cannot_be_claimed_visible(self):
        popup=Control('FlickDock · 剪贴板预览','Window','ClipboardPreview',self.root);popup.handle=0
        with self.assertRaisesRegex(AssertionError,'no observable window identity'):self.runner.popup()

    def test_real_leave_phase_only_moves_mouse_and_requires_both_real_hosts_hidden(self):
        popup,_=self.popup_fixture()
        def move(**_):self.root.visible=False;popup.visible=False
        self.mouse.move.side_effect=move;self.runner.leave_and_assert('overview','manual-position')
        self.mouse.move.assert_called_once_with(coords=(1400,50));self.mouse.click.assert_not_called()
        self.backend.hotkey.assert_not_called();self.backend._producer_topmost.assert_not_called()
        last=self.runner.events[-1]['observations'][-1]
        self.assertFalse(last['host_visible']);self.assertFalse(last['popup_visible'])

    def test_either_host_left_visible_fails_not_just_close_button_missing(self):
        for survivor in ('host','popup'):
            with self.subTest(survivor=survivor):
                self.root.visible=True;popup,_=self.popup_fixture()
                def move(**_):
                    self.root.visible=survivor=='host';popup.visible=survivor=='popup'
                self.mouse.move.side_effect=move
                with self.assertRaisesRegex(AssertionError,'both hidden'):
                    self.runner.leave_and_assert('mini','default-edge')

    def test_mouse_only_leave_must_not_activate_flickdock_from_external_foreground(self):
        popup,_=self.popup_fixture();foreground=[456]
        self.runner.foreground=lambda:dict(hwnd=foreground[0],pid=foreground[0])
        def move(**_):self.root.visible=False;popup.visible=False;foreground[0]=123
        self.mouse.move.side_effect=move
        with self.assertRaisesRegex(AssertionError,'activated FlickDock'):
            self.runner.leave_and_assert('mini','default-edge')

    def test_real_leave_requires_observed_cursor_reaching_the_owned_target(self):
        self.popup_fixture();self.runner.cursor.return_value=[100,100]
        with self.assertRaisesRegex(AssertionError,'did not reach'):
            self.runner.leave_and_assert('mini','default-edge')
        self.mouse.click.assert_not_called()

    def test_outside_excludes_actual_host_union_and_32_pixel_bridge_margin(self):
        rectangles=[[0,0,100,100],[150,20,250,120]]
        self.assertFalse(qa.FinalMotion.outside((125,50),rectangles))
        self.assertTrue(qa.FinalMotion.outside((300,50),rectangles))

    def test_leave_point_is_exposed_owned_producer_and_never_raises_or_focuses_it(self):
        producer=Control('Synthetic external editor','Window','QMainWindow',rectangle=(1300,0,1800,600))
        self.backend.windows.side_effect=lambda pid=None:[producer] if pid==456 else [self.root]
        point=self.runner.leave_point();self.assertTrue(qa.FinalMotion.outside(point,[list(self.root.box)]))
        self.backend._assert_pointer_owner.assert_called_with(point,456)
        self.backend._producer_topmost.assert_not_called();self.mouse.move.assert_not_called()

    def test_fully_covered_producer_can_move_without_activation_and_restores_only_owned_geometry(self):
        producer=Control('Synthetic external editor','Window','QWidget',rectangle=(20,20,500,280));producer.handle=98
        original=list(producer.box)
        self.backend.windows.side_effect=lambda pid=None:[producer] if pid==456 else [self.root]
        self.api.GetSystemMetrics=Mock(side_effect=lambda index:{0:1280,1:720,76:0,77:0,78:1280,79:720}[index]);self.api.SetWindowPos=Mock(return_value=1)
        self.backend._assert_pointer_owner.side_effect=lambda point,pid:True if point[0]>1032 else (_ for _ in ()).throw(RuntimeError('covered'))
        point=self.runner.leave_point()
        self.assertTrue(qa.FinalMotion.outside(point,[list(self.root.box)]))
        first=self.api.SetWindowPos.call_args_list[0]
        self.assertEqual(first.args[0],98);self.assertEqual(first.args[4:6],(0,0));self.assertEqual(first.args[-1],0x0015)
        self.assertEqual(self.runner._producer_geometry,(98,original))
        self.runner.restore_producer()
        self.assertEqual(self.api.SetWindowPos.call_args.args,(98,0,20,20,480,260,0x0014))
        self.assertIsNone(self.runner._producer_geometry)
        self.mouse.click.assert_not_called();self.backend._producer_topmost.assert_not_called()

    def test_no_real_free_point_fails_without_moving_any_window(self):
        producer=Control('Synthetic external editor','Window','QWidget',rectangle=(20,20,500,280));producer.handle=98
        self.root.box=(0,0,1280,720)
        self.backend.windows.side_effect=lambda pid=None:[producer] if pid==456 else [self.root]
        self.api.GetSystemMetrics=Mock(side_effect=lambda index:{0:1280,1:720,76:0,77:0,78:1280,79:720}[index]);self.api.SetWindowPos=Mock()
        self.backend._assert_pointer_owner.side_effect=RuntimeError('covered')
        with self.assertRaisesRegex(AssertionError,'No exposed owned producer point'):self.runner.leave_point()
        self.api.SetWindowPos.assert_not_called();self.mouse.move.assert_not_called()

    def test_leave_point_intersects_partly_offscreen_producer_before_sampling(self):
        # Actual ed57 native run: the disposable producer extended below the
        # 1024x768 desktop; its .9 sample y773 cannot physically be reached.
        producer=Control('Synthetic external editor','Window','QWidget',rectangle=(8,539,488,799))
        self.root.box=(-6,7,1040,658)
        self.backend.windows.side_effect=lambda pid=None:[producer] if pid==456 else [self.root]
        self.api.GetSystemMetrics=Mock(side_effect=lambda index:{0:1024,1:768,76:0,77:0,78:1024,79:768}[index])
        self.api.SetWindowPos=Mock(return_value=1)
        point=self.runner.leave_point()
        self.assertGreater(point[1],690);self.assertLess(point[1],768)
        self.assertNotEqual(point,(56,773));self.api.SetWindowPos.assert_not_called()
        queried=[call.args[0] for call in self.backend._assert_pointer_owner.call_args_list]
        self.assertTrue(all(0<=p[0]<1024 and 0<=p[1]<768 for p in queried))

    def test_wholly_offscreen_producer_never_passes_unreachable_owned_point(self):
        producer=Control('Synthetic external editor','Window','QWidget',rectangle=(1300,1100,1780,1360));producer.handle=98
        self.backend.windows.side_effect=lambda pid=None:[producer] if pid==456 else [self.root]
        self.api.SetWindowPos=Mock(return_value=1)
        self.api.GetSystemMetrics=Mock(side_effect=lambda index:{0:1280,1:720,76:-1280,77:-200,78:2560,79:920}[index])
        point=self.runner.leave_point()
        self.assertTrue(-1280<=point[0]<1280 and -200<=point[1]<720)
        self.api.SetWindowPos.assert_called()

    def test_each_placement_covers_overview_and_mini_and_restores_original_pin_mode(self):
        for placement in qa.PLACEMENTS:
            with self.subTest(placement=placement),patch.object(qa.time,'sleep'):
                original=dict(mode='overview',pinned=True)
                for name in ('set_pin','edge_open','show_mode','manual_move','input','leave_and_assert','restore'):
                    setattr(self.runner,name,Mock())
                self.runner.state=Mock(return_value=original);self.runner.target=Mock(return_value=object())
                self.runner.wait=Mock(return_value=object())
                self.runner.host_real_leave_close(placement)
                self.assertEqual(self.runner.leave_and_assert.call_args_list,
                    [unittest.mock.call('overview',placement),unittest.mock.call('mini',placement)])
                self.runner.restore.assert_called_once_with(original)
                if placement=='default-edge':self.assertEqual(self.runner.edge_open.call_count,2);self.runner.manual_move.assert_not_called()
                else:self.assertEqual(self.runner.manual_move.call_count,2);self.runner.edge_open.assert_not_called()

    def test_producer_restore_error_still_attempts_original_shelf_restore(self):
        self.runner.state=Mock(return_value=dict(mode='overview',pinned=True));self.runner.set_pin=Mock()
        self.runner.edge_open=Mock(side_effect=AssertionError('original real host failure'))
        self.runner.restore_producer=Mock(side_effect=RuntimeError('producer restore failed'));self.runner.restore=Mock()
        with self.assertRaisesRegex(AssertionError,'original real host failure'):
            self.runner.host_real_leave_close('default-edge')
        self.runner.restore.assert_called_once_with(dict(mode='overview',pinned=True))

    def test_soft_deadline_blocks_new_physical_input_and_wait_does_not_ignore_it(self):
        controls=self.header_controls();self.runner.deadline=qa.time.monotonic()-1
        with self.assertRaisesRegex(AssertionError,'20 s deadline'):self.runner.input(controls['expand'])
        self.mouse.click.assert_not_called()
        with self.assertRaisesRegex(AssertionError,'bounded wait'):self.runner.wait(lambda:False,'expired')

    def test_two_mode_leave_recipe_expands_only_its_total_overall_budget(self):
        self.assertEqual(self.runner.budget_seconds,20)
        self.runner.host_real_leave_close=Mock()
        with patch.object(qa,'FinalMotion',return_value=self.runner):
            qa.run(self.backend,'host-real-leave-close',placement='default-edge')
        self.runner.host_real_leave_close.assert_called_once_with('default-edge')
        self.assertEqual(self.runner.budget_seconds,45)
        self.assertEqual(self.runner.deadline,self.runner.started+45)
        self.assertEqual(self.runner.events[0]['soft_deadline_seconds'],45)


if __name__=='__main__':unittest.main()
