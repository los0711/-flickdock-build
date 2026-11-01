"""Portable fixture only. No real UIA, mouse, keyboard, hooks or desktop."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

HERE=Path(__file__).parent
sys.path.insert(0,str(HERE))
try:
    SPEC=importlib.util.spec_from_file_location('shake_qa',HERE/'native_shake.py')
    qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)
finally:sys.path.remove(str(HERE))
HOST_GUARD=qa.require_hosted


class Control:
    def __init__(self,name,role,cls,rect=(0,0,32,32),parent=None,handle=99):
        self.element_info=types.SimpleNamespace(name=name,control_type=role,class_name=cls)
        self.box=rect;self.children=[];self.owner=parent;self.handle=handle;self.visible=True
        self.set_focus=Mock()
        if parent:parent.children.append(self)
    def descendants(self):return [r for c in self.children for r in (c,*c.descendants())]
    def is_visible(self):return self.visible
    def is_enabled(self):return True
    def rectangle(self):
        a,b,c,d=self.box;return types.SimpleNamespace(left=a,top=b,right=c,bottom=d)
    def top_level_parent(self):return self.owner.top_level_parent() if self.owner else self


class NativeShakeTests(unittest.TestCase):
    def setUp(self):
        import native_batch2,native_final_motion
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        (self.base/'files').mkdir()
        for name in native_batch2.ORIGINALS:(self.base/'files'/name).write_bytes(('unaltered '+name).encode())
        self.mouse=types.SimpleNamespace(move=Mock(),press=Mock(),release=Mock(),click=Mock())
        self.keyboard=types.SimpleNamespace(send_keys=Mock())
        self.actual_cursor=(0,0)
        self.mouse.move.side_effect=lambda **kw:setattr(self,'actual_cursor',kw['coords'])
        def read_cursor(pointer):
            pointer._obj.x,pointer._obj.y=self.actual_cursor
            return True
        self.api=types.SimpleNamespace(WindowFromPoint=lambda p:99 if p.x>700 else 77,
            GetForegroundWindow=Mock(return_value=77),GetWindowLongPtrW=Mock(return_value=8),
            GetAsyncKeyState=Mock(return_value=0x8000),GetCursorPos=Mock(side_effect=read_cursor))
        self.producer=Control('producer','Window','QWidget',(90,80,590,350),handle=77)
        self.source=Control('Drag original files','Button','QPushButton',(100,300,580,336),self.producer,77)
        self.backend=types.SimpleNamespace(pid=111,producer_pid=222,base=self.base,
            find=Mock(return_value=self.source),click=Mock(),_input_api=Mock(return_value=self.api),
            _window_pid=Mock(side_effect=lambda h:111 if h==99 else 222),_producer_topmost=Mock(),
            _assert_foreground=Mock(),_assert_pointer_owner=Mock(),hotkey=Mock(),windows=Mock(return_value=[]))
        for p in [patch.object(qa,'require_hosted'),patch.object(native_final_motion,'require_hosted'),
                  patch.object(native_batch2,'require_hosted'),patch.dict(sys.modules,{
                      'pywinauto':types.SimpleNamespace(mouse=self.mouse),'pywinauto.keyboard':self.keyboard})]:
            p.start();self.addCleanup(p.stop)
        self.runner=qa.Shake(self.backend)
        self.root=self.panel()

    def panel(self):
        root=Control('FlickDock','Window','ShelfWindow',(650,0,1010,360))
        Control('搁架文件预览','Group','MiniStack',(720,78,960,248),root)
        Control('云端网络畅通','Group','NetworkStatus',(912,21,944,53),root)
        Control('云同步：已开启，打开设置','Button','ToolbarButton',(950,21,982,53),root)
        Control('选择局域网或远程传输','Button','LanButton',(683,290,795,322),root)
        Control('6 项','Text','QLabel',(916,290,984,322),root)
        return root

    def immediate(self,action,label,seconds=2):
        return action() or (_ for _ in ()).throw(AssertionError('controlled bounded wait '+label))

    def route(self,shown=True):
        original=dict(mode='overview',pinned=True)
        self.runner.state=Mock(return_value=original);self.runner.set_pin=Mock();self.runner.hide_explicitly=Mock()
        self.runner.restore=Mock();self.runner.wait=self.immediate
        self.runner.scope=Mock(side_effect=[self.root,self.root,None] if shown else [None])
        return original

    def test_guard_requires_all_five_hosted_and_opt_in_conditions(self):
        markers=dict(GITHUB_ACTIONS='true',RUNNER_OS='Windows',RUNNER_ENVIRONMENT='github-hosted',FD_QA_NATIVE='1')
        for key in markers:
            with self.subTest(key=key),patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,{**markers,key:'false'}):
                with self.assertRaisesRegex(RuntimeError,'refuses'):HOST_GUARD()
        with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,markers):HOST_GUARD()
        with patch.object(qa.sys,'platform','linux'),patch.dict(qa.os.environ,markers):
            with self.assertRaises(RuntimeError):HOST_GUARD()

    def test_gesture_uses_only_real_source_rectangle_and_bounded_reversals(self):
        points=qa.gesture_points([100,300,580,336])
        self.assertEqual(len(points),12);self.assertEqual(len(set(points)),2)
        self.assertEqual(len([1 for a,b in zip(points,points[1:]) if a[0]!=b[0]]),11)
        for x,y in points:self.assertTrue(100<x<580 and 300<y<336)
        for rect in ([0,0,100,32],[0,0,500,5]):
            with self.assertRaises(AssertionError):qa.gesture_points(rect)
        with self.assertRaises(AssertionError):qa.gesture_points([0,0,500,32],100)

    def test_actual_panel_read_requires_topmost_simplified_header_and_footer(self):
        self.runner.assert_panel(self.root)
        row=self.runner.events[-1]
        self.assertTrue(row['native']['topmost']);self.assertEqual(row['count_text'],'6 项')
        self.assertEqual(len(row['visible_toolbar_names']),1)
        self.mouse.click.assert_not_called();self.backend.hotkey.assert_not_called()

    def test_regular_preview_is_not_accepted_as_a_shake_panel(self):
        Control('展开详情','Button','ToolbarButton',(850,21,882,53),self.root)
        with self.assertRaisesRegex(AssertionError,'regular pin/expand'):self.runner.assert_panel(self.root)

    def test_missing_count_misaligned_glyph_or_overlapping_footer_fails(self):
        for defect in ('count','alignment','footer'):
            with self.subTest(defect=defect):
                root=self.panel()
                if defect=='count':root.children[-1].visible=False
                elif defect=='alignment':root.children[2].box=(950,30,982,62)
                else:root.children[-1].box=(916,200,984,232)
                with self.assertRaises(AssertionError):self.runner.assert_panel(root)

    def test_native_non_topmost_or_focus_steal_is_a_real_failure(self):
        self.api.GetWindowLongPtrW.return_value=0
        with self.assertRaisesRegex(AssertionError,'not topmost'):self.runner.assert_panel(self.root)
        self.api.GetWindowLongPtrW.return_value=8;self.api.GetForegroundWindow.return_value=99
        with self.assertRaisesRegex(AssertionError,'stole foreground'):self.runner.assert_panel(self.root)

    def test_released_native_button_is_not_accepted_as_held_external_drag(self):
        self.api.GetAsyncKeyState.return_value=0
        with self.assertRaisesRegex(AssertionError,'left button is not held'):self.runner.assert_panel(self.root)

    def test_full_route_uses_os_mouse_reversals_and_cancel_then_restores(self):
        original=self.route()
        with patch.object(qa.time,'sleep'):self.runner.perform()
        self.mouse.press.assert_called_once_with(button='left',coords=(340,318))
        self.assertEqual(len([r for r in self.runner.events if r['step']=='physical-shake-turn']),12)
        self.keyboard.send_keys.assert_called_once_with('{ESC}',pause=.02)
        self.mouse.release.assert_called_once_with(button='left',coords=(840,163))
        self.assertFalse(self.runner.held);self.runner.restore.assert_called_once_with(original)
        self.backend.click.assert_called_once_with('Synthetic external editor',pid=222,roles=('Edit',))
        self.producer.set_focus.assert_not_called();self.backend._assert_foreground.assert_called_once_with(222)
        self.mouse.click.assert_not_called();self.backend.hotkey.assert_not_called()
        self.assertEqual(len([r for r in self.runner.events if r['step']=='actual-shake-panel']),2)
        self.assertEqual(self.runner.events[-1]['step'],'original-file-hashes-unchanged')

    def test_no_real_shake_window_is_strict_failure_not_shortcut_fallback(self):
        original=self.route(shown=False)
        with patch.object(qa.time,'sleep'),self.assertRaisesRegex(AssertionError,'shelf opened by actual file shake'):
            self.runner.perform()
        self.mouse.release.assert_called_once();self.runner.restore.assert_called_once_with(original)
        self.backend.hotkey.assert_not_called()

    def test_failure_mid_gesture_cancels_and_releases_before_restore(self):
        original=self.route();self.runner.owned_point=Mock(side_effect=RuntimeError('foreign point'))
        with self.assertRaisesRegex(RuntimeError,'foreign point'):self.runner.perform()
        self.keyboard.send_keys.assert_called_once();self.mouse.release.assert_called_once()
        self.runner.restore.assert_called_once_with(original);self.assertFalse(self.runner.held)

    def test_changed_producer_owner_blocks_focus_and_press(self):
        self.route();self.backend._window_pid.side_effect=lambda h:999
        with self.assertRaisesRegex(RuntimeError,'owner changed before focus/input'):self.runner.perform()
        self.producer.set_focus.assert_not_called();self.mouse.press.assert_not_called()
        self.backend.click.assert_not_called()
        self.mouse.move.assert_not_called();self.keyboard.send_keys.assert_not_called()

    def test_failed_physical_producer_activation_sends_no_drag_press(self):
        self.route();self.backend._assert_foreground.side_effect=RuntimeError('actual producer activation failed')
        with self.assertRaisesRegex(RuntimeError,'actual producer activation failed'):self.runner.perform()
        self.backend.click.assert_called_once_with('Synthetic external editor',pid=222,roles=('Edit',))
        self.producer.set_focus.assert_not_called();self.mouse.press.assert_not_called()
        self.mouse.move.assert_not_called();self.keyboard.send_keys.assert_not_called()

    def test_partial_press_error_still_pairs_release(self):
        self.route();self.mouse.press.side_effect=RuntimeError('controlled input failure')
        with self.assertRaisesRegex(RuntimeError,'controlled input failure'):self.runner.perform()
        self.mouse.release.assert_called_once();self.assertFalse(self.runner.held)

    def test_cancel_error_or_foreign_foreground_still_releases_without_foreign_key(self):
        self.runner.held=True;self.runner.last_point=(340,318)
        self.api.GetForegroundWindow.return_value=1;self.backend._window_pid.side_effect=lambda h:999
        with self.assertRaisesRegex(RuntimeError,'outside the owned'):self.runner.cancel_drag()
        self.keyboard.send_keys.assert_not_called();self.mouse.release.assert_called_once();self.assertFalse(self.runner.held)
        self.runner.cancel_drag();self.mouse.release.assert_called_once()

    def test_foreign_physical_point_or_expired_budget_sends_no_motion(self):
        self.backend._window_pid.side_effect=lambda h:999
        with self.assertRaisesRegex(RuntimeError,'outside the two'):self.runner.move((1,1))
        self.mouse.move.assert_not_called()
        self.runner.deadline=0
        with self.assertRaisesRegex(AssertionError,'deadline'):self.runner.move((1,1))
        self.mouse.move.assert_not_called()

    def test_unreached_actual_cursor_is_not_reported_as_a_shake_turn(self):
        self.mouse.move.side_effect=None
        with self.assertRaisesRegex(AssertionError,'did not reach'):self.runner.move((340,318))
        self.assertFalse(any(e['step']=='physical-shake-turn' for e in self.runner.events))

    def test_unknown_recipe_never_constructs_or_sends_input(self):
        with patch.object(qa,'Shake') as constructor:
            with self.assertRaises(ValueError):qa.run(self.backend,'shortcut-fake-shake')
            constructor.assert_not_called()


if __name__=='__main__':unittest.main()
