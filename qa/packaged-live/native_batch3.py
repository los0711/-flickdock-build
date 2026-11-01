"""Supplemental hosted-only normal EXE observations and physical input.

This module imports only the QA helper, never Qt or shipped application code.
The outer RPC/Recorder owns a hard process timeout and full-screen capture.
Receipts identify actual actions; visual approval still requires raw frames.
"""
import ctypes
import json
import ntpath
import os
import re
import subprocess
import sys
import time

from native_batch2 import Batch2, bounds, centre, selected

RECIPES=('tray-menu-only','recent-device-delete','open-original-text')
RECENT_NAME='Native QA recent offline'
TRAY_NAME=re.compile(r'FlickDock \d+\.\d+\.\d+(?:[^ ]*)? · 文件搁架\Z')
TRAY_ROOTS=('Shell_TrayWnd','Shell_SecondaryTrayWnd','NotifyIconOverflowWindow',
            'TopLevelWindowForOverflowXamlIsland')
OVERFLOW_NAMES=('Show hidden icons','显示隐藏的图标')
TRAY_ACTIONS=('显示搁架','粘贴到搁架','设置…','检查更新…','退出 FlickDock')


def require_hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'):
        raise RuntimeError('Native batch3 refuses non-ephemeral hosted Windows desktops')


def command_has_exact_path(command,expected):
    """Exact, bounded argument boundary; never accept a basename substring."""
    value=str(command or '').replace('/','\\').casefold()
    expected=ntpath.normpath(str(expected)).replace('/','\\').casefold()
    return bool(re.search(r'(?:^|[\s"])'+re.escape(expected)+r'(?=$|[\s"])',value))


class Batch3(Batch2):
    def __init__(self,backend):
        require_hosted();super().__init__(backend)
        self.deadline=self.started+45

    def mark(self,step,control=None,**values):
        now=time.monotonic();row=dict(step=step,t=round(now-self.started,6),monotonic=now,**values)
        if control is not None:
            info=control.element_info
            row.update(name=info.name,role=info.control_type,class_name=info.class_name,bounds=bounds(control))
        self.events.append(row)
        with (self.b.base/'batch3-events.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(dict(recipe=self.recipe,**row),ensure_ascii=False)+'\n')
        return row

    def wait(self,action,label,seconds=3):
        return super().wait(action,'BATCH3 '+label,seconds=seconds)

    def desktop(self):
        from pywinauto import Desktop
        return Desktop(backend='uia')

    def unique(self,rows,label):
        if len(rows)>1:raise AssertionError('Ambiguous actual native '+label)
        return rows[0] if rows else None

    def process_image(self,pid):
        """Read one observed process image, closing the native handle always."""
        from ctypes import wintypes
        api=ctypes.windll.kernel32
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
        api.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        api.QueryFullProcessImageNameW.restype=wintypes.BOOL
        api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle.restype=wintypes.BOOL
        handle=api.OpenProcess(0x1000,False,int(pid))
        if not handle:raise OSError('Cannot observe native target process image')
        try:
            text=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(text))
            if not api.QueryFullProcessImageNameW(handle,0,text,ctypes.byref(size)):
                raise OSError('Cannot query native target process image')
            return text.value
        finally:api.CloseHandle(handle)

    def ancestry(self,control):
        rows=[];current=control
        for _ in range(24):
            if current is None:break
            rows.append(current);current=current.parent()
        else:raise AssertionError('Native ancestry exceeded bounded depth')
        return rows

    def system_tray_targets(self,*,overflow=False):
        rows=[];seen=set()
        for root in self.desktop().windows():
            if not root.is_visible() or root.element_info.class_name not in TRAY_ROOTS:continue
            for control in root.descendants():
                info=control.element_info
                if not control.is_visible() or not control.is_enabled():continue
                if overflow:
                    match=(info.control_type=='Button' and info.name in OVERFLOW_NAMES)
                else:match=bool(TRAY_NAME.fullmatch(info.name or ''))
                if not match:continue
                rectangle=bounds(control);key=(info.name,tuple(rectangle))
                if key not in seen:seen.add(key);rows.append(control)
        return rows

    def tray_input(self,control):
        # Explorer is the sole allowed extra input owner, only for a positively
        # identified tray/notification descendant on the disposable runner.
        require_hosted();chain=self.ancestry(control)
        if not any(c.element_info.class_name in TRAY_ROOTS for c in chain):
            raise AssertionError('Native tray target has no actual taskbar/notification ancestry')
        is_icon=bool(TRAY_NAME.fullmatch(control.element_info.name or ''))
        is_overflow=control.element_info.control_type=='Button' and control.element_info.name in OVERFLOW_NAMES
        if not (is_icon or is_overflow):raise AssertionError('Not an observed exact tray/overflow target')
        point=centre(control);self.b._producer_topmost(False)
        from ctypes import wintypes
        hit=self.b._input_api().WindowFromPoint(wintypes.POINT(*point));pid=self.b._window_pid(hit)
        declared=int(control.element_info.process_id)
        if pid!=declared:raise AssertionError('Actual tray point is covered by a different native owner')
        expected=ntpath.join(os.environ['WINDIR'],'explorer.exe')
        if ntpath.normcase(self.process_image(pid))!=ntpath.normcase(expected):
            raise AssertionError('Observed native tray target is not system Explorer')
        before=self.mark('before:tray-physical-click',control,point=list(point),native_owner_pid=pid,
                         ancestry=[c.element_info.class_name for c in chain])
        self.mouse.click(button='left',coords=point)
        self.mark('after:tray-physical-click',target=before['name'],point=list(point),input_bounds=before['bounds'])

    def menu(self):
        rows=[]
        for root in self.b.windows():
            for control in [root,*root.descendants()]:
                if control.is_visible() and control.element_info.control_type=='MenuItem' and control.element_info.name in TRAY_ACTIONS:
                    rows.append(control)
        names=[c.element_info.name for c in rows]
        if len(set(names))!=len(names):raise AssertionError('Duplicate visible owned tray menu actions')
        return rows if set(names)==set(TRAY_ACTIONS) else None

    def tray(self):
        self.overview()
        closes=[c for c in self.scope('overview').descendants() if c.is_visible() and c.is_enabled() and
                c.element_info.name=='收起到屏幕边缘' and c.element_info.control_type=='Button' and c.element_info.class_name=='ToolbarButton']
        self.input(self.wait(lambda:self.unique(closes,'shelf close'),'shelf close'))
        self.wait(lambda:self.scope('overview') is None,'shelf actually hidden before tray click')
        controls=self.system_tray_targets()
        if not controls:
            overflow=self.unique(self.system_tray_targets(overflow=True),'visible overflow button')
            if overflow is None:raise AssertionError('No actual visible tray icon or native overflow button; no guessed input sent')
            self.tray_input(overflow)
            controls=self.wait(lambda:self.system_tray_targets(),'exact actual FlickDock tray tooltip',seconds=3)
        self.tray_input(self.unique(controls,'FlickDock tray tooltip'))
        menu=self.wait(self.menu,'owned tray menu')
        for c in menu:self.mark('actual-tray-menu-action',c)
        if self.scope('overview') is not None:raise AssertionError('Native tray click unexpectedly opened the shelf')
        self.mark('actual-tray-menu-no-shelf')
        # Escape is permitted only when the actual application owns foreground
        # and its complete menu is still visible. No command invokes Show shelf.
        self.b._assert_foreground(self.b.pid)
        from pywinauto.keyboard import send_keys
        send_keys('{ESC}')
        self.wait(lambda:not self.menu(),'actual tray menu dismissed')
        if self.scope('overview') is not None:raise AssertionError('Dismissing tray menu opened shelf')
        self.overview()

    def popup_rows(self):
        """Actual visible QListView rows belonging only to the app PID."""
        rows=[];seen=set()
        for root in self.b.windows():
            for control in [root,*root.descendants()]:
                if not control.is_visible() or control.element_info.control_type!='ListItem':continue
                chain=self.ancestry(control)
                if not any(c.element_info.class_name=='QListView' for c in chain):continue
                rectangle=bounds(control);key=(control.element_info.name,tuple(rectangle))
                if key not in seen:rows.append(control);seen.add(key)
        return rows

    def row_remove_point(self,row):
        rectangle=bounds(row);point=(rectangle[2]-30,(rectangle[1]+rectangle[3])//2)
        # Delegate remove_rect is 28px square at QRect.right()-43, centred
        # vertically; derive only from this actually observed row, never from
        # a guessed global coordinate. All clipping ancestors must contain it.
        if rectangle[2]-rectangle[0]<120 or rectangle[3]-rectangle[1]<28:
            raise AssertionError('Recent native row is too small for its actual remove target')
        for ancestor in self.ancestry(row):
            if ancestor.element_info.control_type=='Window':break
            r=bounds(ancestor)
            if not ancestor.is_visible() or not (r[0]<=point[0]<r[2] and r[1]<=point[1]<r[3]):
                raise AssertionError('Recent remove target is clipped by actual native popup ancestor')
        return point

    def recent(self):
        self.transfer('wan');root=self.scope('transfer')
        def combo():
            return self.unique([c for c in root.descendants() if c.is_visible() and c.is_enabled() and
                                c.element_info.control_type=='ComboBox' and c.element_info.class_name=='_DevicesCombo'], 'WAN device dropdown')
        self.input(self.wait(combo,'actual WAN device dropdown'))
        def target():return self.unique([c for c in self.popup_rows() if c.element_info.name==RECENT_NAME], 'offline recent row')
        row=self.wait(target,'real offline recent-only fixture row')
        # The fixture is established via real local signaling before the app
        # launches; never seed/alter a live client model or simulate online.
        initial=[c.element_info.name for c in self.popup_rows()]
        if RECENT_NAME not in initial:raise AssertionError('No actual recent record to delete')
        self.mark('recent-dropdown-initial',names=initial,fixture_online=False)
        point=self.row_remove_point(row);self.input(row,kind='hover',point=point);time.sleep(.2)
        self.input(row,point=point)
        self.wait(lambda:target() is None,'recent fixture row removed without closing popup',seconds=5)
        remaining=[c.element_info.name for c in self.popup_rows()]
        if sorted(remaining)!=sorted(name for name in initial if name!=RECENT_NAME):
            raise AssertionError('Recent deletion closed popup or changed another native device row')
        if not remaining:raise AssertionError('Meaningful recent deletion requires a surviving real peer row')
        self.mark('actual-recent-delete-stayed-open',remaining=remaining)
        # Physical same-combo click dismisses the popup; reopen verifies that
        # the real server response, not a single painted frame, removed it.
        self.input(self.wait(combo,'same dropdown after deletion'))
        self.wait(lambda:not self.popup_rows(),'recent popup dismissed')
        self.input(self.wait(combo,'same dropdown reopen'))
        self.wait(lambda:bool(self.popup_rows()),'recent popup reopened')
        if target() is not None:raise AssertionError('Deleted recent record returned on dropdown reopen')
        if sorted(c.element_info.name for c in self.popup_rows())!=sorted(remaining):
            raise AssertionError('Recent survivors changed on reopen')
        self.mark('actual-recent-removal-persists-on-reopen',remaining=remaining)
        self.input(self.wait(combo,'same dropdown final close'));self.click('back','transfer')
        self.wait(lambda:self.scope('overview'),'overview restored after recent deletion')

    def process_command(self,pid):
        # Only a newly observed external foreground PID. No raw command line is
        # written to receipts; retain just whether the synthetic full path matched.
        pid=int(pid)
        if pid<=0:raise AssertionError('No observed external editor PID')
        script=f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'
        completed=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',script],
                                 capture_output=True,text=True,timeout=5,check=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        data=json.loads(completed.stdout)
        if int(data['ProcessId'])!=pid:raise AssertionError('Observed editor process query returned wrong PID')
        return data

    def observe_editor(self,before,expected):
        api=self.b._input_api();foreground=api.GetForegroundWindow();pid=self.b._window_pid(foreground)
        if pid in (0,self.b.pid,self.b.producer_pid):return None
        rows=[w for w in self.desktop().windows() if w.is_visible() and int(w.handle)==int(foreground) and
              int(w.handle) not in before and self.b._window_pid(w.handle)==pid]
        window=self.unique(rows,'new external foreground editor')
        if window is None:return None
        info=self.process_command(pid);image=ntpath.basename(str(info.get('ExecutablePath') or '')).casefold()
        if image not in ('notepad.exe','wordpad.exe','write.exe'):
            self.mark('external-editor-observed-without-input',pid=pid,image_allowed=False)
            raise AssertionError('Observed default editor is not an allowed passive native text editor')
        matched=command_has_exact_path(info.get('CommandLine'),expected)
        self.mark('external-editor-exact-path-observation',pid=pid,handle=int(window.handle),
                  image=image,full_expected_path=matched,bounds=bounds(window))
        if not matched:raise AssertionError('Actual external editor command does not prove the full original file path')
        return dict(pid=pid,handle=int(window.handle),bounds=bounds(window),full_expected_path=True,image=image)

    def open_original(self):
        self.overview();self.click('list');root=self.scope('overview')
        if not self.matches('deselect_all',root):self.click('select_all')
        self.click('deselect_all');self.package_enabled(False)
        check=self.resolve('list_check');row=self.ancestor(check,'FileRow')
        self.input(row,point=self.body_point(row))
        self.wait(lambda:selected(self.resolve('list_check'))==1,'original text selected by physical file body')
        self.package_enabled(True)
        expected=(self.b.base/'files'/'original-text.txt').resolve()
        if not expected.is_file():raise AssertionError('Original text fixture missing')
        before={int(w.handle) for w in self.desktop().windows() if w.is_visible()}
        self.mark('before:actual-open-original',original='original-text.txt',original_sha256=self.original_hashes['original-text.txt'])
        self.input(self.resolve('open'))
        self.wait(lambda:self.scope('overview') is None,'shelf actually hides after opening original',seconds=4)
        observed=self.wait(lambda:self.observe_editor(before,expected),'external native editor with exact original path',seconds=7)
        self.mark('actual-open-original-and-shelf-hidden',**observed)
        # Passive observation only: never type, close or terminate the editor.
        self.overview();self.mark('overview-restored-editor-left-unmodified')

    def run(self,recipe):
        if recipe not in RECIPES:raise ValueError('Unknown batch3 recipe')
        self.recipe=recipe;self.mark('begin')
        {'tray-menu-only':self.tray,'recent-device-delete':self.recent,'open-original-text':self.open_original}[recipe]()
        self.originals_unchanged();self.mark('end')
        return dict(recipe=recipe,events=self.events,actual_native_input=True,application_imported=False,
                    normal_exe_patched=False,manual_visual_review='required',elapsed=time.monotonic()-self.started)


def run(backend,recipe):
    """Worker entry; outer RPC must bound the entire process to <=55 seconds."""
    return Batch3(backend).run(recipe)
