"""Hosted-only physical external QDrag/shake recipe for the normal EXE.

No Qt/app imports, internal gesture callbacks, settings edits or manufactured
application timestamps. The outer normal-EXE Recorder must retain real frames.
"""
import ctypes
import json
import os
import re
import sys
import time
from ctypes import wintypes

from native_final_motion import FinalMotion
from native_batch2 import bounds,centre

RECIPE='external-file-shake'


def require_hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or os.environ.get('FD_QA_NATIVE')!='1'):
        raise RuntimeError('Shake QA refuses non-ephemeral hosted Windows/native opt-in')


def gesture_points(rectangle,legs=12):
    """Real mouse endpoints inside the actual external drag-source control."""
    left,top,right,bottom=rectangle
    if right-left<180 or bottom-top<16 or not 8<=legs<=16:
        raise AssertionError('External drag source cannot safely contain a bounded shake')
    x=round((left+right)/2);y=round((top+bottom)/2)
    amplitude=min(180,(right-left)//2-12)
    return [(x-amplitude if i%2==0 else x+amplitude,y) for i in range(legs)]


class Shake(FinalMotion):
    def __init__(self,backend):
        require_hosted();super().__init__(backend)
        self.budget_seconds=35;self.deadline=self.started+self.budget_seconds
        self.recipe=RECIPE;self.held=False;self.last_point=None

    def mark(self,step,control=None,**values):
        now=time.monotonic();row=dict(step=step,t=round(now-self.started,6),monotonic=now,
                                    perf_counter=time.perf_counter(),**values)
        if control is not None:
            info=control.element_info
            row.update(name=info.name,role=info.control_type,class_name=info.class_name,bounds=bounds(control))
        self.events.append(row)
        # The main recorder already preserves this progress file even when a
        # recipe raises before its normal result JSON can be written.
        with (self.b.base/'batch2-events.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(dict(recipe=RECIPE,**row),ensure_ascii=False)+'\n')
        return row

    def owned_point(self,point):
        api=self.b._input_api();actual=self.b._window_pid(api.WindowFromPoint(wintypes.POINT(*point)))
        if actual not in (self.b.pid,self.b.producer_pid):
            raise RuntimeError('Shake physical point is outside the two disposable owned processes')

    def move(self,point):
        if time.monotonic()>=self.deadline:raise AssertionError('Shake bounded deadline before physical motion')
        self.owned_point(point);self.mouse.move(coords=point);self.last_point=point
        if self.cursor()!=list(point):raise AssertionError('Physical shake cursor did not reach its owned endpoint')

    def native_state(self,root):
        if self.b._window_pid(root.handle)!=self.b.pid:raise AssertionError('Shake host HWND owner mismatch')
        api=self.b._input_api()
        query=getattr(api,'GetWindowLongPtrW',None) or api.GetWindowLongW
        if isinstance(query,ctypes._CFuncPtr):query.argtypes=[wintypes.HWND,ctypes.c_int];query.restype=ctypes.c_ssize_t
        style=int(query(root.handle,-20))
        buttons=api.GetAsyncKeyState
        if isinstance(buttons,ctypes._CFuncPtr):buttons.argtypes=[ctypes.c_int];buttons.restype=ctypes.c_short
        return dict(hwnd=int(root.handle),exstyle=style,topmost=bool(style&0x8),
                    left_button_down=bool(buttons(1)&0x8000),
                    foreground_pid=self.b._window_pid(api.GetForegroundWindow()))

    def assert_panel(self,root):
        visible=[c for c in root.descendants() if c.is_visible()]
        def matches(cls,predicate=lambda name:True,roles=None):
            return [c for c in visible if c.element_info.class_name==cls and
                    (roles is None or c.element_info.control_type in roles) and predicate(c.element_info.name or '')]
        stacks=matches('MiniStack',lambda name:name=='搁架文件预览')
        network=matches('NetworkStatus',lambda name:name.startswith('云端网络') or name=='正在检查云端网络…')
        cloud=matches('ToolbarButton',lambda name:name.startswith('云同步：'),('Button',))
        toolbar=matches('ToolbarButton')
        transfer=matches('LanButton',lambda name:name=='选择局域网或远程传输',('Button',))
        counts=matches('QLabel',lambda name:bool(re.fullmatch(r'\d+\s*项',name)),('Text','StaticText'))
        if not (len(stacks)==len(network)==len(cloud)==len(transfer)==len(counts)==1):
            raise AssertionError('Actual shake panel lacks unique stack/network/cloud/transfer/count')
        if len(toolbar)!=1 or toolbar[0] is not cloud[0]:
            raise AssertionError('Shake panel exposes regular pin/expand/menu/dock toolbar controls')
        nr,cr=bounds(network[0]),bounds(cloud[0]);tr,ct=bounds(transfer[0]),bounds(counts[0])
        if abs((nr[1]+nr[3])-(cr[1]+cr[3]))>2:raise AssertionError('Shake network/cloud centers are misaligned')
        if tr[1]<bounds(stacks[0])[3] or ct[1]<bounds(stacks[0])[3]:
            raise AssertionError('Shake footer transfer/count overlaps its file stack')
        native=self.native_state(root)
        if not native['topmost']:raise AssertionError('Actual shake native host is not topmost')
        if not native['left_button_down']:raise AssertionError('Native left button is not held during shake inspection')
        if native['foreground_pid']!=self.b.producer_pid:
            raise AssertionError('Shake popup stole foreground from the external QDrag producer')
        self.mark('actual-shake-panel',root,native=native,network=nr,cloud=cr,
                  transfer=tr,count=ct,count_text=counts[0].element_info.name,
                  visible_toolbar_names=[c.element_info.name for c in toolbar])
        return stacks[0]

    def cancel_drag(self):
        if not self.held:return
        from pywinauto.keyboard import send_keys
        failure=None
        try:
            api=self.b._input_api();pid=self.b._window_pid(api.GetForegroundWindow())
            if pid not in (self.b.pid,self.b.producer_pid):
                raise RuntimeError('Cannot cancel QDrag with keyboard outside the owned processes')
            send_keys('{ESC}',pause=.02)
            self.mark('physical-escape-cancel',foreground_pid=pid)
        except BaseException as error:failure=error
        finally:
            self.mouse.release(button='left',coords=self.last_point)
            self.held=False;self.b._producer_topmost(False)
            self.mark('physical-left-release',point=list(self.last_point))
        if failure is not None:raise failure

    def perform(self):
        original=self.state();failure=None
        self.mark('recipe-start',original=original,budget_seconds=self.budget_seconds)
        try:
            self.set_pin(False);self.hide_explicitly()
            source=self.wait(lambda:self.b.find('Drag original files',pid=self.b.producer_pid,
                                               roles=('Button',)),'actual independent QDrag source',seconds=4)
            producer=source.top_level_parent()
            if self.b._window_pid(producer.handle)!=self.b.producer_pid:
                raise RuntimeError('External drag-source native owner changed before focus/input')
            self.b._producer_topmost(True)
            # Windows can ignore programmatic focus requests. Use the
            # existing measured/ownership-guarded physical producer click;
            # typing/copying or an arbitrary caption point is unnecessary.
            self.b.click('Synthetic external editor',pid=self.b.producer_pid,roles=('Edit',))
            self.b._assert_foreground(self.b.producer_pid)
            start=centre(source);self.b._assert_pointer_owner(start,self.b.producer_pid)
            points=gesture_points(bounds(source));self.mouse.move(coords=start);self.last_point=start
            self.mark('before:external-qdrag-press',source,point=list(start))
            self.held=True;self.mouse.press(button='left',coords=start)
            # Actual OS positions and actual monotonic times, not calls to
            # FlickDock's detector, MIME gate, callbacks or animation clocks.
            for point in points:
                self.move(point);self.mark('physical-shake-turn',point=list(point));time.sleep(.035)
            root=self.wait(lambda:self.scope('overview'),'shelf opened by actual file shake',seconds=3)
            stacks=[c for c in root.descendants() if c.is_visible() and c.element_info.class_name=='MiniStack'
                    and c.element_info.name=='搁架文件预览']
            if len(stacks)!=1:raise AssertionError('Real shake did not expose its compact file stack')
            target=centre(stacks[0]);self.b._assert_pointer_owner(target,self.b.pid)
            self.move(target);self.mark('held-native-drag-enter',point=list(target))
            self.assert_panel(root)
            time.sleep(.65)  # Recorder observes actual held-drag paints.
            self.assert_panel(self.wait(lambda:self.scope('overview'),'shake remains visible during held drag'))
            self.cancel_drag()
            self.wait(lambda:self.scope('overview') is None,'shake closes after actual native cancel/release',seconds=4)
            self.originals_unchanged()
        except BaseException as error:failure=error;raise
        finally:
            cleanup_error=None
            for action in (self.cancel_drag,lambda:self.b._producer_topmost(False),lambda:self.restore(original)):
                try:action()
                except Exception as error:
                    self.mark('cleanup-failed',error_type=type(error).__name__,error=str(error))
                    if cleanup_error is None:cleanup_error=error
            if failure is None and cleanup_error is not None:raise cleanup_error


def run(backend,recipe=RECIPE):
    if recipe!=RECIPE:raise ValueError('Unknown native shake recipe')
    runner=Shake(backend);runner.perform()
    return dict(recipe=recipe,events=runner.events,elapsed=time.monotonic()-runner.started,
                actual_external_os_qdrag=True,application_callbacks_invoked=False,
                manual_visual_review='required',actual_display_fps='not inferred from UIA')
