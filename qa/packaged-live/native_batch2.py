"""Additional bounded normal-EXE recipes, called inside the existing UIA worker.

Only UIA reads and physical OS input; no Qt/application import, Invoke, Toggle,
selection API or internal callback. The parent Recorder must capture the whole
call. Returned timestamps/bounds locate actual frames, not visual approval.
"""
import ctypes
import hashlib
import json
import os
import sys
import time
from pathlib import Path


RECIPES=('selection-grid-list','detail-feedback-rename','context-transfer',
         'history-folder','settings-theme','grid-delete-width','empty-zones-last')
ORIGINALS=('original-image.png','original-text.txt','tiny.bin','original-8MiB.bin')
SELECTORS={
 'overview':('FlickDock','Window','ShelfWindow'),
 'transfer':('FlickDock · 文件互传','Window','LanDialog'),
 'settings_window':('FlickDock 设置','Window','SettingsDialog'),
 'rename_window':('重命名文件','Window','RenameDialog'),
 'grid':('图标视图','CheckBox','IconTextButton'),
 'list':('列表视图','CheckBox','IconTextButton'),
 'grid_text':('original-text.txt','Group','IconItem'),
 'package':('打包所选','Button','IconTextButton'),
 'select_all':('全选','Button','IconTextButton'),
 'deselect_all':('弃选','Button','IconTextButton'),
 'open':('打开','Button','IconTextButton'),
 'rename':('重命名文件','Button','IconTextButton'),
 'rename_edit':(None,'Edit','QLineEdit'),
 'cancel':('取消','Button',None),
 'list_check':('选择 original-text.txt','CheckBox','SelectionCheck'),
 'transfer_nav':('文件互传','Button','TransferNavButton'),
 # Actual normal Windows UIA tree, run 41268257366 (not the Qt base class).
 'lan':('局域网传输','CheckBox','_PressButton'),
 'wan':('远程传输','CheckBox','_PressButton'),
 'queue_clear':('清除','Button',None),
 'back':('返回总览','Button',None),
 'history_folder':('在文件夹中显示 Completed synthetic 0.bin','Button','_HistoryRemoveButton'),
 'history_delete':('删除这条传输记录','Button','_HistoryRemoveButton'),
 'settings':('设置','Button','IconTextButton'),
 'general':('通用与外观设置','CheckBox','QPushButton'),
 'account':('账号与设备设置','CheckBox','QPushButton'),
 'sync':('搁架与剪贴板设置','CheckBox','QPushButton'),
 'light':('浅色','CheckBox','QPushButton'),
 'dark':('暗色','CheckBox','QPushButton'),
 'settings_close':('关闭设置','Button','_WindowButton'),
 'clear_all':('全部删除','Button','IconTextButton'),
 'clear_confirm':('删除全部搁架内容','Button','_PointerConfirmButton'),
 'remove_tiny':('从搁架移除 tiny.bin','Button','ToolbarButton'),
 'empty_overview':('搁架为空，点击选择文件，或拖入文件、图片、文字或链接',None,'OverviewEmptyDropZone'),
 'mini':('搁架文件预览',None,'MiniStack'),
 'empty_transfer':('选择要发送的文件；也可拖入文件或文件夹',None,'_DropZone'),
}


def require_hosted():
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS')!='Windows':
        raise RuntimeError('Native batch2 refuses non-ephemeral Windows desktops')


def bounds(control):
    r=control.rectangle();return [r.left,r.top,r.right,r.bottom]


def centre(control,ratio=(.5,.5)):
    x,y,r,b=bounds(control)
    if r<=x or b<=y:raise AssertionError('Native target has empty actual bounds')
    return (round(x+(r-x)*ratio[0]),round(y+(b-y)*ratio[1]))


def selected(control):
    # UIA read only. Never call Toggle/Select to manufacture an input result.
    try:return int(control.get_toggle_state())
    except AttributeError:return int(control.iface_toggle.CurrentToggleState)


class Batch2:
    def __init__(self,backend):
        require_hosted()
        from pywinauto import mouse
        self.b=backend;self.mouse=mouse;self.events=[];self.started=time.monotonic()
        self.deadline=self.started+65;self.recipe='';self.original_hashes=self.hash_originals()

    def mark(self,step,control=None,**values):
        row=dict(step=step,t=round(time.monotonic()-self.started,4),**values)
        if control is not None:
            info=control.element_info
            row.update(name=info.name,role=info.control_type,class_name=info.class_name,bounds=bounds(control))
        self.events.append(row)
        with (self.b.base/'batch2-events.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(dict(recipe=self.recipe,**row),ensure_ascii=False)+'\n')
        return row

    def wait(self,action,label,seconds=6):
        end=min(self.deadline,time.monotonic()+seconds)
        while time.monotonic()<end:
            value=action()
            if value:return value
            time.sleep(.025)
        raise AssertionError('BATCH2 bounded wait: '+label)

    def scope(self,key):
        name,role,cls=SELECTORS[key]
        rows=[w for w in self.b.windows() if w.element_info.name==name and
              w.element_info.control_type==role and w.element_info.class_name==cls]
        if len(rows)>1:raise AssertionError('Ambiguous actual native window '+key)
        return rows[0] if rows else None

    def matches(self,key,scope=None,*,enabled=True):
        name,role,cls=SELECTORS[key];roots=[scope] if scope is not None else self.b.windows()
        rows=[]
        for root in roots:
            for c in [root,*root.descendants()]:
                info=c.element_info
                if (c.is_visible() and (not enabled or c.is_enabled()) and
                    (name is None or info.name==name) and (role is None or info.control_type==role) and
                    (cls is None or info.class_name==cls)):rows.append(c)
        return rows

    def resolve(self,key,scope='overview',*,enabled=True):
        def read():
            root=self.scope(scope) if isinstance(scope,str) else scope
            if root is None:return None
            rows=self.matches(key,root,enabled=enabled)
            if len(rows)>1:raise AssertionError('Ambiguous actual UIA selector '+key)
            return rows[0] if rows else None
        c=self.wait(read,'unique visible '+key);self.mark('preflight:'+key,c);return c

    def root_at(self,point):
        from ctypes import wintypes
        api=self.b._input_api();query=api.GetAncestor
        query.argtypes=[wintypes.HWND,wintypes.UINT];query.restype=wintypes.HWND
        return query(api.WindowFromPoint(wintypes.POINT(*point)),2)

    def guard(self,c,point):
        self.b._producer_topmost(False);self.b._assert_pointer_owner(point,self.b.pid)
        window=c.top_level_parent();api=self.b._input_api()
        if self.b._window_pid(window.handle)!=self.b.pid:raise AssertionError('Target root PID is not frozen application')
        if self.root_at(point)!=api.GetAncestor(window.handle,2):raise AssertionError('Native root at input point is not expected scoped window')

    def input(self,c,*,kind='click',point=None):
        point=point or centre(c);self.guard(c,point);before=self.mark('before:'+kind,c,point=list(point))
        if kind=='hover':self.mouse.move(coords=point)
        elif kind=='right':self.mouse.click(button='right',coords=point)
        elif kind=='double':self.mouse.double_click(button='left',coords=point,interval=.07)
        elif kind=='click':self.mouse.click(button='left',coords=point)
        else:raise ValueError('Physical input kind')
        # A real delete may legitimately destroy this wrapper. Record its
        # measured input bounds without dereferencing it after the click.
        time.sleep(.28);self.mark('after:'+kind,target=before['name'],input_bounds=before['bounds'],point=list(point))

    def click(self,key,scope='overview'):self.input(self.resolve(key,scope))
    def ancestor(self,c,cls):
        for _ in range(24):
            if c.element_info.class_name==cls:return c
            c=c.parent()
            if c is None:break
        raise AssertionError('Actual ancestor '+cls+' missing')

    def hash_originals(self):
        return {name:hashlib.sha256((self.b.base/'files'/name).read_bytes()).hexdigest() for name in ORIGINALS}
    def originals_unchanged(self):
        if self.hash_originals()!=self.original_hashes:raise AssertionError('Synthetic original bytes changed')
        self.mark('original-file-hashes-unchanged')

    def overview(self):
        if self.scope('overview') is None:self.b.hotkey('^%+{SPACE}')
        self.wait(lambda:self.scope('overview'),'overview normal window')

    def transfer(self,mode):
        if self.scope('transfer') is None:self.overview();self.click('transfer_nav')
        self.click(mode,'transfer')

    def queue(self,expected,*,exact=False):
        root=self.wait(lambda:self.scope('transfer'),'transfer after physical action')
        def names():
            return sorted(c.element_info.name.removeprefix('移除待发送文件 ') for c in root.descendants()
                          if c.is_visible() and c.element_info.control_type=='Button' and c.element_info.name.startswith('移除待发送文件 '))
        found=self.wait(lambda:(found if set(expected)<=set(found) else None) if (found:=names()) else None,
                        'actual original names in queue',seconds=8)
        self.mark('actual-queue',files=found,expected=sorted(expected))
        if exact and found!=sorted(expected):raise AssertionError('Mini queue differs from actual complete shelf card inventory')

    def no_queue(self):
        root=self.wait(lambda:self.scope('transfer'),'transfer queue scope')
        self.wait(lambda:not any(c.is_visible() and c.element_info.name.startswith('移除待发送文件 ') for c in root.descendants()),'empty transfer queue')

    def package_enabled(self,value):
        c=self.resolve('package',enabled=False)
        if bool(c.is_enabled())!=value:raise AssertionError('Selection via actual package-enabled differs')
        self.mark('package-enabled',c,value=value)

    def selection(self):
        self.overview();self.click('grid')
        # Reset pre-existing baseline selection using only its real control.
        root=self.scope('overview')
        if not self.matches('deselect_all',root):self.click('select_all')
        self.click('deselect_all');self.package_enabled(False)
        self.mark('double-click-interval',seconds=.07)
        before=self.b._window_pid(self.b._input_api().GetForegroundWindow())
        for kind,value in (('click',True),('click',False),('double',False)):
            c=self.resolve('grid_text');self.input(c,kind=kind,point=centre(c,(.5,.55)));self.package_enabled(value)
        if self.scope('overview') is None or self.b._window_pid(self.b._input_api().GetForegroundWindow())!=before:
            raise AssertionError('Grid body clicks opened/activated another application')
        self.click('list')
        for kind,value in (('click',1),('click',0),('double',0)):
            check=self.resolve('list_check');row=self.ancestor(check,'FileRow');self.input(row,kind=kind)
            check=self.resolve('list_check')
            if selected(check)!=value:raise AssertionError('List body did not toggle the actual checkbox')
            self.mark('list-selection',check,value=value)
        if self.scope('overview') is None or self.b._window_pid(self.b._input_api().GetForegroundWindow())!=before:
            raise AssertionError('List body clicks opened/activated another application')
        self.click('grid')

    def detail(self):
        self.overview();self.click('grid');c=self.resolve('grid_text');self.input(c,point=centre(c,(.5,.55)))
        foreground=self.b._window_pid(self.b._input_api().GetForegroundWindow())
        c=self.resolve('open');self.input(c,kind='hover');time.sleep(.15)
        point=centre(c);r=bounds(c);outside=(r[0]-8,point[1]);window=c.top_level_parent()
        # Leave inside the same real window, and release outside the button.
        self.guard(window,outside);self.guard(c,point);self.mark('open-pressed-begin',c)
        try:self.mouse.press(button='left',coords=point);time.sleep(.18);self.mouse.move(coords=outside)
        finally:self.mouse.release(button='left',coords=outside)
        time.sleep(.3);self.mark('open-cancelled-release',c,release=list(outside))
        if self.scope('overview') is None or self.b._window_pid(self.b._input_api().GetForegroundWindow())!=foreground:
            raise AssertionError('Cancelled Open hid overview or activated another application')
        self.click('rename');edit=self.resolve('rename_edit','rename_window');self.guard(edit,centre(edit))
        self.mark('rename-modal-on-top',edit,native_root=int(self.root_at(centre(edit))))
        self.click('cancel','rename_window');self.wait(lambda:self.scope('rename_window') is None,'rename closed')

    def menu_action(self,mode):
        name='使用局域网传输' if mode=='lan' else '使用远程传输'
        def read():
            rows=[c for w in self.b.windows() for c in [w,*w.descendants()] if c.is_visible() and c.is_enabled() and
                  c.element_info.control_type=='MenuItem' and c.element_info.name==name]
            if len(rows)>1:raise AssertionError('Ambiguous actual context MenuItem')
            return rows[0] if rows else None
        self.input(self.wait(read,'actual context '+name))

    def context(self):
        self.overview();self.click('grid')
        for mode in ('lan','wan'):
            c=self.resolve('grid_text');self.input(c,kind='right',point=centre(c,(.5,.55)))
            self.menu_action(mode);self.queue(['original-text.txt']);self.click('queue_clear','transfer');self.click('back','transfer')
        # Mini's menu sends its entire shelf, including any real peer fixture
        # objects already received by core QA. Record exact queue; don't assert
        # that such a shelf contains exactly four files.
        all_names=sorted(c.element_info.name for c in self.scope('overview').descendants()
                         if c.is_visible() and c.element_info.class_name=='IconItem')
        if not set(ORIGINALS)<=set(all_names):raise AssertionError('Complete shelf card fixture unavailable for mini-all')
        if not any(c.is_visible() and c.element_info.control_type=='Text' and c.element_info.name==f'{len(all_names)} 项'
                   for c in self.scope('overview').descendants()):
            raise AssertionError('Observed card inventory does not match actual overview total; mini-all proof requires complete fixture')
        self.mark('current-shelf-card-inventory',files=all_names)
        for mode in ('lan','wan'):
            self.b.hotkey('^%{SPACE}');mini=self.wait(lambda:self.matches('mini') or None,'mini stack')[0]
            self.input(mini,kind='right');self.menu_action(mode);self.queue(all_names,exact=True)
            self.click('queue_clear','transfer');self.click('back','transfer')

    def theme(self):
        self.overview();self.click('settings');self.click('general','settings_window')
        initial={key:selected(self.resolve(key,'settings_window')) for key in ('light','dark')}
        if sorted(initial.values())!=[0,1]:raise AssertionError('Initial theme lacks exclusive selection')
        restore=next(key for key,value in initial.items() if value==1)
        try:
            for theme in ('light','dark'):
                self.click(theme,'settings_window');self.click(theme,'settings_window')
                states={key:selected(self.resolve(key,'settings_window')) for key in ('light','dark')}
                if states[theme]!=1 or sum(states.values())!=1:raise AssertionError('Repeated theme click cleared/duplicated selection')
                self.mark('actual-theme-selection',theme=theme,states=states);time.sleep(.3)
            self.click('account','settings_window');time.sleep(.35)
            self.click('sync','settings_window');time.sleep(.35)
        finally:
            if self.scope('settings_window') is not None:
                self.click('general','settings_window');self.click(restore,'settings_window');self.click('settings_close','settings_window')
        self.wait(lambda:self.scope('settings_window') is None,'settings close')

    def grid_width(self):
        self.overview();self.click('grid')
        def cards():
            root=self.scope('overview')
            return {c.element_info.name:bounds(c) for c in root.descendants() if c.is_visible() and c.element_info.class_name=='IconItem'}
        before=cards();self.mark('grid-width-before',rectangles=before)
        if not set(ORIGINALS)<=set(before):raise AssertionError('Original card fixture missing before deletion')
        self.click('remove_tiny');self.wait(lambda:not self.matches('remove_tiny',self.scope('overview')),'removed tiny shelf reference')
        time.sleep(.4);after=cards();self.mark('grid-width-after',rectangles=after)
        for name,r in before.items():
            if name=='tiny.bin':continue
            if name not in after or abs((r[2]-r[0])-(after[name][2]-after[name][0]))>1:
                raise AssertionError('Surviving card width changed: '+name)

    def hover_leave(self,key,scope):
        c=self.resolve(key,scope);self.input(c,kind='hover');time.sleep(.42)
        root=c.top_level_parent()
        # A frameless native host includes transparent compositor padding.
        # Leave to a measured real control, never that transparent corner.
        targets=[x for x in root.descendants() if x.is_visible() and x.is_enabled() and
                 x.element_info.control_type=='Button' and x.element_info.name in ('设置','返回总览','展开详情')]
        if not targets:raise AssertionError('No actual safe control for hover exit')
        target=targets[0];p=centre(target)
        self.guard(target,p);self.mouse.move(coords=p);self.mark('physical-leave:'+key,target,point=list(p));time.sleep(.4)

    def empty(self):
        self.overview();self.click('clear_all');self.click('clear_confirm')
        self.resolve('empty_overview');self.hover_leave('empty_overview','overview')
        self.b.hotkey('^%{SPACE}');self.wait(lambda:self.matches('mini') or None,'empty mini')
        mini=self.matches('mini')[0];self.hover_leave('mini',mini.top_level_parent())
        self.b.hotkey('^%+{SPACE}');time.sleep(.4)
        self.transfer('lan');self.no_queue();self.hover_leave('empty_transfer','transfer');self.click('back','transfer')

    def history(self):
        # Each mode must still have synthetic record0. No fallback to another
        # history row after baseline clear: that is a missing ordering fixture.
        for mode in ('lan','wan'):
            self.transfer(mode);folder=self.resolve('history_folder','transfer');row=self.ancestor(folder,'_HistoryRow')
            remove=self.resolve('history_delete',row)
            one,two=bounds(folder),bounds(remove)
            sizes=[(r[2]-r[0],r[3]-r[1]) for r in (one,two)]
            if sizes[0]!=sizes[1] or any(abs(w-h)>1 for w,h in sizes):raise AssertionError('History folder/delete are not equal squares')
            self.mark('equal-square-history-buttons',folder,other_bounds=two)
            self.input(folder,kind='hover');self.input(remove,kind='hover')
            self.reveal(folder);self.click('back','transfer')

    def reveal(self,folder):
        from pywinauto import Desktop
        from win32com.client import Dispatch
        from pywinauto.keyboard import send_keys
        desktop=Desktop(backend='uia');before={w.handle for w in desktop.windows(class_name='CabinetWClass')}
        expected=(self.b.base/'files').resolve();self.input(folder)
        shell=Dispatch('Shell.Application')
        def actual():
            matches=[]
            for window in desktop.windows(class_name='CabinetWClass'):
                if not window.is_visible() or window.handle in before:continue
                # COM may enumerate inactive tabs with the same frame HWND.
                # Require the current frame's address toolbar as well as the
                # Shell document/selected full path. Hidden extensions and a
                # matching background tab alone cannot satisfy this check.
                if not any((c.element_info.name or '').rstrip('\\/').casefold().endswith(str(expected).casefold())
                           for c in window.descendants(control_type='ToolBar') if c.is_visible()):continue
                for item in shell.Windows():
                    if int(item.HWND)!=window.handle:continue
                    document=item.Document
                    if os.path.normcase(os.path.realpath(document.Folder.Self.Path))!=os.path.normcase(str(expected)):continue
                    selected_items=document.SelectedItems()
                    paths=[os.path.normcase(os.path.realpath(selected_items.Item(index).Path)) for index in range(selected_items.Count)]
                    if paths==[os.path.normcase(str(expected/'tiny.bin'))]:matches.append(window)
            if len(matches)>1:raise AssertionError('Multiple new Explorer windows show exact selected fixture')
            return matches[0] if matches else None
        explorer=self.wait(actual,'new unique Explorer full fixture path and selected tiny.bin',seconds=10)
        self.mark('validated-explorer-reveal',explorer,handle=int(explorer.handle),selected='tiny.bin')
        api=self.b._input_api();explorer_pid=self.b._window_pid(explorer.handle)
        if explorer_pid==self.b.pid or explorer.handle in before:raise AssertionError('Explorer ownership guard failed')
        # Close only this new, positively validated Explorer window, not its
        # process, any old tab/window, or the global desktop shell.
        explorer.set_focus();self.b._assert_foreground(explorer_pid)
        if api.GetAncestor(api.GetForegroundWindow(),2)!=api.GetAncestor(explorer.handle,2):
            raise AssertionError('Validated Explorer is not actual foreground native root')
        send_keys('%{F4}',pause=.02)
        self.wait(lambda:not any(w.handle==explorer.handle for w in desktop.windows(class_name='CabinetWClass')),'validated Explorer closed')

    def run(self,recipe):
        if recipe not in RECIPES:raise ValueError('Unknown batch2 recipe')
        self.recipe=recipe;self.mark('begin');self.mark('preflight-tree',controls=self.b.tree())
        actions={'selection-grid-list':self.selection,'detail-feedback-rename':self.detail,
                 'context-transfer':self.context,'history-folder':self.history,'settings-theme':self.theme,
                 'grid-delete-width':self.grid_width,'empty-zones-last':self.empty}
        actions[recipe]();self.originals_unchanged();self.mark('end')
        return dict(recipe=recipe,events=self.events,actual_native_input=True,application_imported=False,
                    normal_exe_patched=False,manual_visual_review='required',elapsed=time.monotonic()-self.started)


def run(backend,recipe):
    """Worker entry; outer RPC must have a <=75s hard process timeout."""
    return Batch2(backend).run(recipe)
