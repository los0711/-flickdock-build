"""Bounded, hosted normal-EXE motion checks using physical Windows input only.

The outer worker/Recorder owns hard timeout and screen capture.  This helper
does not import Qt or application code, invoke accessible actions, manufacture
animation time, or treat rectangle observations as displayed-frame approval.
"""
import json
import os
import sys
import time

from native_batch2 import Batch2, bounds, centre, selected


RECIPES=('header-fixed-expand','host-real-leave-close')
PLACEMENTS=('default-edge','manual-position')
SELECTORS={
    'network':(None,'Group','NetworkStatus'),
    'cloud':(None,'Button','ToolbarButton'),
    'pin':(None,'CheckBox','ToolbarButton'),
    'expand':(None,'Button','ToolbarButton'),
    'more':('更多操作','Button','ToolbarButton'),
    'close':('收起到屏幕边缘','Button','ToolbarButton'),
    'clipboard':('放大查看剪贴板','Button','ClipboardButton'),
}
HEADER=('network','cloud','pin','expand','more','close')


def require_hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'):
        raise RuntimeError('Final motion QA refuses non-ephemeral hosted Windows desktops')


class FinalMotion(Batch2):
    def __init__(self,backend):
        require_hosted()
        super().__init__(backend)
        self.budget_seconds=20;self.deadline=self.started+self.budget_seconds
        self._producer_geometry=None

    def mark(self,step,control=None,**values):
        now=time.monotonic()
        row=dict(step=step,t=round(now-self.started,6),monotonic=now,**values)
        if control is not None:
            info=control.element_info
            row.update(name=info.name,role=info.control_type,class_name=info.class_name,bounds=bounds(control))
        self.events.append(row)
        with (self.b.base/'final-motion-events.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(dict(recipe=self.recipe,**row),ensure_ascii=False)+'\n')
        return row

    def wait(self,action,label,seconds=2):
        return super().wait(action,'FINAL-MOTION '+label,seconds=seconds)

    def named(self,key,root=None):
        root=root or self.scope('overview')
        if root is None:return None
        name,role,cls=SELECTORS[key];rows=[]
        for control in [root,*root.descendants()]:
            info=control.element_info
            if not control.is_visible() or not control.is_enabled():continue
            if info.control_type!=role or info.class_name!=cls:continue
            actual=info.name
            if key=='network':matches=actual.startswith('云端网络') or actual=='正在检查云端网络…'
            elif key=='cloud':matches=actual.startswith('云同步：')
            elif key=='pin':matches=actual in ('取消钉住','钉住搁架')
            elif key=='expand':matches=actual in ('展开详情','展开内容','收起详情')
            else:matches=actual==name
            if matches:rows.append(control)
        if len(rows)>1:raise AssertionError('Ambiguous owned final-motion selector '+key)
        return rows[0] if rows else None

    def target(self,key):
        return self.wait(lambda:self.named(key),'visible scoped '+key)

    def input(self,control,*,kind='click',point=None):
        if time.monotonic()>=self.deadline:raise AssertionError(f'Final motion {self.budget_seconds} s deadline before input')
        point=point or centre(control);self.guard(control,point)
        before=self.mark('before:'+kind,control,point=list(point))
        if kind=='hover':self.mouse.move(coords=point)
        elif kind=='click':self.mouse.click(button='left',coords=point)
        else:raise ValueError('Final motion accepts physical click/hover only')
        # A click may synchronously destroy its target. Never re-read it.
        self.mark('after:'+kind,target=before['name'],input_bounds=before['bounds'],point=list(point))

    def mode(self):
        control=self.named('expand')
        return None if control is None else ('overview' if control.element_info.name=='收起详情' else 'mini')

    def show_mode(self,mode):
        if self.mode()!=mode:
            self.b.hotkey('^%+{SPACE}' if mode=='overview' else '^%{SPACE}')
        self.wait(lambda:self.mode()==mode,'actual '+mode)
        self.settle()

    def settle(self):
        # Endpoints need two matching *real* UIA samples, not a guessed sleep.
        previous=None;count=0;unchanged_since=None
        def stable():
            nonlocal previous,count,unchanged_since
            root=self.scope('overview')
            control=self.named('expand',root) if root is not None else None
            current=None if control is None else (bounds(root),bounds(control),control.element_info.name)
            if current is not None and current==previous:count+=1
            else:count=0;unchanged_since=time.monotonic()
            previous=current
            return count>=2 and time.monotonic()-unchanged_since>=.18
        self.wait(stable,'stable endpoint',seconds=2)

    def set_pin(self,value):
        control=self.target('pin');initial=selected(control)
        if initial not in (0,1):raise AssertionError('Indeterminate actual pin checkbox')
        if bool(initial)!=bool(value):
            self.input(control)
            def verified():
                current=self.named('pin')
                state=None if current is None else selected(current)
                return current if state in (0,1) and bool(state)==bool(value) else None
            control=self.wait(verified,'actual pin state')
        # Record the actual observation already verified above. Re-enumerating
        # the full UIA tree twice more adds no state check and can consume the
        # bounded recipe deadline during restoration on a busy hosted desktop.
        self.mark('actual-pin-state',control,checked=bool(value))

    def state(self):
        mode=self.mode()
        if mode is None:raise AssertionError('Recipe requires an initially visible normal shelf')
        return dict(mode=mode,pinned=bool(selected(self.target('pin'))))

    def restore(self,state):
        self.show_mode(state['mode']);self.set_pin(state['pinned'])
        self.mark('original-visible-mode-and-pin-restored',**state)

    def header(self,phase):
        rows={}
        for key in HEADER:rows[key]=self.mark(phase+':'+key,self.target(key))
        return rows

    def header_fixed_expand(self):
        original=self.state();failure=None
        try:
            self.set_pin(True);self.show_mode('mini')
            before=self.header('mini-endpoint')
            self.input(self.target('expand'))
            self.wait(lambda:self.mode()=='overview','expanded actual overview');self.settle()
            after=self.header('overview-endpoint')
            changed={key:dict(before=before[key]['bounds'],after=after[key]['bounds'])
                     for key in HEADER if before[key]['bounds']!=after[key]['bounds']}
            self.mark('absolute-header-rect-comparison',before=before,after=after,changed=changed)
            if changed:raise AssertionError('Shared header controls changed absolute endpoint bounds: '+','.join(changed))
        except BaseException as error:
            failure=error;raise
        finally:
            try:self.restore(original)
            except Exception as error:
                self.mark('restore-failed',error_type=type(error).__name__,error=str(error))
                if failure is None:raise

    def popup(self):
        # UIA can expose an owned native dialog beneath its ShelfWindow rather
        # than as a Desktop.windows root. Match the same strict visible window
        # in both locations, deduplicating its observed native identity.
        rows={}
        for root in self.b.windows():
            for w in [root,*root.descendants()]:
                info=w.element_info
                if (info.name!='FlickDock · 剪贴板预览' or info.control_type!='Window'
                        or info.class_name!='ClipboardPreview' or not w.is_visible()):continue
                handle=int(w.handle or 0)
                if handle:identity=('hwnd',handle)
                else:
                    runtime=getattr(info,'runtime_id',None)
                    if not runtime:raise AssertionError('Actual clipboard popup has no observable window identity')
                    identity=('runtime',tuple(runtime))
                rows.setdefault(identity,w)
        if len(rows)>1:raise AssertionError('Duplicate actual clipboard preview hosts')
        return next(iter(rows.values()),None)

    def hide_explicitly(self):
        control=self.named('close')
        if control is not None:self.input(control)
        self.wait(lambda:self.scope('overview') is None,'shelf hidden before edge trigger')

    def edge_open(self,mode):
        self.hide_explicitly()
        # Exact edge title is observed by the pre-existing native helper; no
        # unobserved role/class is invented for its transparent native host.
        def edge():
            matches=[]
            for root in self.b.windows():
                matches.extend(c for c in [root,*root.descendants()]
                               if c.is_visible() and c.is_enabled() and c.element_info.name=='FlickDock edge')
            if len(matches)>1:raise AssertionError('Ambiguous actual edge trigger')
            return matches[0] if matches else None
        self.input(self.wait(edge,'actual edge trigger'),kind='hover')
        self.wait(lambda:self.mode()=='mini','actual edge-triggered mini');self.settle()
        if mode=='overview':
            self.input(self.target('expand'))
            self.wait(lambda:self.mode()=='overview','edge-triggered overview');self.settle()

    def manual_move(self):
        network=self.target('network');rectangle=bounds(network);scale=(rectangle[3]-rectangle[1])/32
        point=(round(rectangle[0]-12*scale),round((rectangle[1]+rectangle[3])/2))
        self.guard(network,point)
        before=bounds(self.target('close'));destination=(point[0]-100,point[1]+50)
        self.mark('before:physical-header-drag',point=list(point),before=before,destination=list(destination))
        try:
            self.mouse.press(button='left',coords=point)
            for index in range(1,9):
                self.mouse.move(coords=tuple(round(a+(b-a)*index/8) for a,b in zip(point,destination)))
                time.sleep(.01)
        finally:self.mouse.release(button='left',coords=destination)
        self.settle();after=bounds(self.target('close'))
        self.mark('after:physical-header-drag',before=before,after=after)
        if abs(after[0]-before[0])+abs(after[1]-before[1])<40:
            raise AssertionError('Actual manual header drag did not change screen position')

    @staticmethod
    def outside(point,rectangles):
        return all(not (r[0]-32<=point[0]<=r[2]+32 and r[1]-32<=point[1]<=r[3]+32) for r in rectangles)

    def leave_point(self):
        owned=[bounds(w) for w in self.b.windows()]
        api=self.b._input_api()
        # A UIA window rectangle may extend below/off the desktop after a
        # nonactivating move. WindowFromPoint alone can still report that
        # producer for an unreachable coordinate. Sample only the intersection
        # with the real Win32 virtual desktop; keep GetCursorPos exact below.
        left,top,width,height=(int(api.GetSystemMetrics(i)) for i in (76,77,78,79))
        if width<=0 or height<=0:raise AssertionError('Cannot observe real native desktop bounds')
        desktop=[left,top,left+width,top+height]
        self.mark('native-physical-desktop-bounds',bounds=desktop)
        # Only move to an exposed point of the disposable producer. Never
        # click/activate it, move over another application, or guess a screen
        # corner that can still be inside a preallocated transparent host.
        for window in self.b.windows(self.b.producer_pid):
            original=bounds(window)
            r=[max(original[0],desktop[0]),max(original[1],desktop[1]),
               min(original[2],desktop[2]),min(original[3],desktop[3])]
            if r[2]<=r[0] or r[3]<=r[1]:continue
            for rx,ry in ((.1,.08),(.5,.08),(.9,.08),(.1,.9),(.9,.9)):
                point=(round(r[0]+(r[2]-r[0])*rx),round(r[1]+(r[3]-r[1])*ry))
                if not self.outside(point,owned):continue
                try:self.b._assert_pointer_owner(point,self.b.producer_pid)
                except RuntimeError:continue
                return point
        # A left-aligned overview can completely cover the original producer.
        # Move ONLY that owned disposable window into a real free edge
        # strip, without activation or z-order changes. Its geometry is restored
        # after the recipe. Never move FlickDock to make the leave assertion pass.
        # Probe all four actual screen corners first. Inset variants avoid a
        # taskbar occupying the outer strip; WindowFromPoint must still confirm
        # the producer owns the chosen point. Do not infer taskbar geometry.
        candidates=tuple(point for inset in (12,32,52,72) for point in
            ((left+inset,top+inset),(left+width-inset-1,top+inset),
             (left+inset,top+height-inset-1),(left+width-inset-1,top+height-inset-1)))
        candidates=tuple(p for p in candidates if left<=p[0]<left+width and top<=p[1]<top+height and self.outside(p,owned))
        producers=self.b.windows(self.b.producer_pid)
        if not candidates or len(producers)!=1:
            raise AssertionError('No exposed owned producer point outside all actual shelf/popup hosts')
        window=producers[0]
        if self.b._window_pid(window.handle)!=self.b.producer_pid:
            raise AssertionError('Producer native window ownership changed before nonactivating move')
        if self._producer_geometry is None:self._producer_geometry=(window.handle,bounds(window))
        r=bounds(window);w,h=r[2]-r[0],r[3]-r[1]
        for point in candidates:
            if time.monotonic()>=self.deadline:raise AssertionError(f'Final motion {self.budget_seconds} s deadline before producer move')
            x=max(left,min(point[0]-w//2,max(left,left+width-w)))
            y=max(top,min(point[1]-h//2,max(top,top+height-h)))
            self.mark('before:owned-producer-nonactivating-move',bounds=r,target=[x,y,x+w,y+h],foreground=self.foreground())
            # NOSIZE | NOZORDER | NOACTIVATE: never resize the producer or app.
            if not api.SetWindowPos(window.handle,0,x,y,0,0,0x0015):
                raise OSError('Cannot move owned producer without activation')
            if not self._producer_point_owned(point):continue
            self.mark('after:owned-producer-nonactivating-move',point=list(point),foreground=self.foreground())
            return point
        raise AssertionError('No actual producer-owned point in the free screen corner strips')

    def _producer_point_owned(self,point):
        try:self.b._assert_pointer_owner(point,self.b.producer_pid)
        except RuntimeError:return False
        return True

    def restore_producer(self):
        saved=self._producer_geometry
        if saved is None:return
        handle,r=saved
        if self.b._window_pid(handle)!=self.b.producer_pid:
            raise AssertionError('Producer ownership changed before geometry restoration')
        if not self.b._input_api().SetWindowPos(handle,0,r[0],r[1],r[2]-r[0],r[3]-r[1],0x0014):
            raise OSError('Cannot restore owned producer geometry without activation')
        self._producer_geometry=None;self.mark('original-producer-geometry-restored',bounds=r)

    def foreground(self):
        handle=self.b._input_api().GetForegroundWindow()
        return dict(hwnd=int(handle or 0),pid=int(self.b._window_pid(handle)))

    def cursor(self):
        import ctypes
        from ctypes import wintypes
        point=wintypes.POINT()
        if not self.b._input_api().GetCursorPos(ctypes.byref(point)):
            raise OSError('Cannot observe actual native mouse position after leave')
        return [point.x,point.y]

    def leave_and_assert(self,mode,placement):
        popup=self.wait(self.popup,'actual clipboard preview before leave')
        host=self.scope('overview')
        if host is None:raise AssertionError('Host disappeared before physical leave')
        host_before=bounds(host);popup_before=bounds(popup);point=self.leave_point()
        foreground=self.foreground()
        self.mark('before:mouse-only-real-leave',mode=mode,placement=placement,point=list(point),
                  host=host_before,popup=popup_before,foreground=foreground)
        self.mouse.move(coords=point)
        actual_cursor=self.cursor()
        self.mark('actual:observed-leave-cursor',intended=list(point),cursor=actual_cursor)
        if actual_cursor!=list(point):raise AssertionError('Physical leave did not reach its actual owned screen point')
        self.b._assert_pointer_owner(point,self.b.producer_pid)
        self.mark('actual:mouse-only-real-leave',cursor=actual_cursor)
        # This phase intentionally sends no click, key, Invoke, Toggle, focus
        # call or app command. Both independent real native windows must hide.
        observations=[]
        def closed():
            now=time.monotonic();host=self.scope('overview');popup=self.popup()
            fg=self.foreground();observations.append(dict(monotonic=now,host_visible=host is not None,
                popup_visible=popup is not None,foreground=fg))
            if foreground['pid']!=self.b.pid and fg['pid']==self.b.pid:
                raise AssertionError('Mouse-only leave activated FlickDock from another foreground process')
            return host is None and popup is None
        try:self.wait(closed,'host and clipboard both hidden after mouse-only leave',seconds=4.4)
        finally:self.mark('after:mouse-only-real-leave',mode=mode,placement=placement,point=list(point),observations=observations)

    def host_real_leave_close(self,placement):
        if placement not in PLACEMENTS:raise ValueError('Unknown real-leave placement')
        original=self.state();failure=None
        try:
            self.set_pin(False)
            for mode in ('overview','mini'):
                if placement=='default-edge':self.edge_open(mode)
                else:self.show_mode(mode);self.manual_move()
                self.set_pin(False)
                self.input(self.target('clipboard'))
                self.wait(self.popup,'real clipboard open');time.sleep(.25)
                self.leave_and_assert(mode,placement)
        except BaseException as error:
            failure=error;raise
        finally:
            restore_error=None
            for restore in (self.restore_producer,lambda:self.restore(original)):
                try:restore()
                except Exception as error:
                    self.mark('restore-failed',error_type=type(error).__name__,error=str(error))
                    if restore_error is None:restore_error=error
            if failure is None and restore_error is not None:raise restore_error


def run(backend,recipe,*,placement='default-edge'):
    if recipe not in RECIPES:raise ValueError('Unsupported final motion recipe')
    runner=FinalMotion(backend);runner.recipe=recipe
    if recipe=='host-real-leave-close':
        # Two real modes plus measured UIA queries need a bounded overall
        # budget. Individual popup/physical-leave deadlines remain unchanged.
        runner.budget_seconds=45;runner.deadline=runner.started+runner.budget_seconds
    runner.mark('recipe-start',placement=placement,soft_deadline_seconds=runner.budget_seconds)
    if recipe=='header-fixed-expand':runner.header_fixed_expand()
    else:runner.host_real_leave_close(placement)
    runner.originals_unchanged()
    return dict(recipe=recipe,placement=placement,events=runner.events,elapsed=time.monotonic()-runner.started,
                normal_exe_patched=False,manual_visual_review='required',actual_display_fps='not inferred from UIA')
