"""One disposable hosted-CI desktop fixture; never imported by product code.

No OOBE input, settings, ACL edits, elevation, cross-session UI, hooks, or shell.
The non-Qt supervisor owns exactly one child and restores before its outer timeout.
A hard CI cancellation can destroy the supervisor before finally; that is not a
successful restore. The hosted VM is disposable, and raw lifecycle evidence is kept.
"""
import argparse,ctypes,hashlib,json,os,subprocess,sys,uuid
from pathlib import Path

READ=0x0001
SWITCH=0x0100
OWN_ACCESS=0x0001|0x0002|0x0004|0x0040|0x0080|0x0100
APPROVED_REPOSITORY='los0711/-flickdock-build'
APPROVED_REF='refs/heads/qa/material-probe-1823-20261031'


def require_hosted_fixture(env=None,platform=None):
    env=os.environ if env is None else env;platform=sys.platform if platform is None else platform
    expected={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':APPROVED_REPOSITORY,'GITHUB_REF':APPROVED_REF,
              'RUNNER_ENVIRONMENT':'github-hosted','RUNNER_OS':'Windows','RUNNER_ARCH':'ARM64',
              'FLICKDOCK_MATERIAL_RUNNER_LABEL':'windows-11-arm'}
    if platform!='win32' or any(env.get(key)!=value for key,value in expected.items()):
        raise RuntimeError('Restricted to the explicitly approved Windows 11 Arm hosted QA job')
    if env.get('ImageOS') not in ('win11-arm64','win11-vs2026-arm64'):
        raise RuntimeError('Unrecognized Windows 11 client hosted image; no desktop changes allowed')


class DesktopApi:
    """Public Win32 calls only. Construction does not create or switch desktops."""
    def __init__(self):
        from ctypes import wintypes as w
        self.w=w;self.user=ctypes.WinDLL('user32',use_last_error=True);self.kernel=ctypes.WinDLL('kernel32',use_last_error=True);self.dwm=ctypes.WinDLL('dwmapi',use_last_error=True)
        self._configure(self.user,'GetThreadDesktop',[w.DWORD],w.HANDLE)
        self._configure(self.user,'GetProcessWindowStation',[],w.HANDLE)
        self._configure(self.user,'OpenInputDesktop',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE)
        self._configure(self.user,'OpenDesktopW',[w.LPCWSTR,w.DWORD,w.BOOL,w.DWORD],w.HANDLE)
        self._configure(self.user,'GetUserObjectInformationW',[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD)],w.BOOL)
        self._configure(self.user,'CreateDesktopW',[w.LPCWSTR,w.LPCWSTR,w.LPVOID,w.DWORD,w.DWORD,w.LPVOID],w.HANDLE)
        self._configure(self.user,'SwitchDesktop',[w.HANDLE],w.BOOL)
        self._configure(self.user,'SetThreadDesktop',[w.HANDLE],w.BOOL)
        self._configure(self.user,'CloseDesktop',[w.HANDLE],w.BOOL)
        self._configure(self.user,'GetForegroundWindow',[],w.HWND)
        self._configure(self.user,'GetWindowThreadProcessId',[w.HWND,ctypes.POINTER(w.DWORD)],w.DWORD)
        self._configure(self.user,'GetWindowLongPtrW',[w.HWND,ctypes.c_int],ctypes.c_ssize_t)
        self._configure(self.user,'GetWindowRect',[w.HWND,ctypes.POINTER(w.RECT)],w.BOOL)
        self._configure(self.user,'IsWindowVisible',[w.HWND],w.BOOL)
        self._configure(self.user,'GetWindow',[w.HWND,w.UINT],w.HWND)
        self._configure(self.kernel,'GetCurrentThreadId',[],w.DWORD)
        self._configure(self.kernel,'GetCurrentProcessId',[],w.DWORD)
        self._configure(self.kernel,'ProcessIdToSessionId',[w.DWORD,ctypes.POINTER(w.DWORD)],w.BOOL)
        self._configure(self.kernel,'WTSGetActiveConsoleSessionId',[],w.DWORD)
        self._configure(self.kernel,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD)
        self._configure(self.kernel,'GetExitCodeProcess',[w.HANDLE,ctypes.POINTER(w.DWORD)],w.BOOL)
        self._configure(self.kernel,'TerminateProcess',[w.HANDLE,w.UINT],w.BOOL)
        self._configure(self.kernel,'CloseHandle',[w.HANDLE],w.BOOL)
        self._configure(self.dwm,'DwmGetWindowAttribute',[w.HWND,w.DWORD,w.LPVOID,w.DWORD],ctypes.c_long)
        class Startup(ctypes.Structure):
            _fields_=[('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),
                      ('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),('dwYSize',w.DWORD),('dwXCountChars',w.DWORD),('dwYCountChars',w.DWORD),
                      ('dwFillAttribute',w.DWORD),('dwFlags',w.DWORD),('wShowWindow',w.WORD),('cbReserved2',w.WORD),('lpReserved2',w.LPVOID),
                      ('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]
        class Process(ctypes.Structure):
            _fields_=[('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]
        self.Startup=Startup;self.Process=Process
        self._configure(self.kernel,'CreateProcessW',[w.LPCWSTR,w.LPWSTR,w.LPVOID,w.LPVOID,w.BOOL,w.DWORD,w.LPVOID,w.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)],w.BOOL)

    @staticmethod
    def _configure(library,name,args,result):
        function=getattr(library,name);function.argtypes=args;function.restype=result

    @staticmethod
    def _check(value,operation):
        if not value:raise OSError(ctypes.get_last_error(),operation)
        return value

    def object_name(self,handle):
        buffer=ctypes.create_unicode_buffer(512);needed=self.w.DWORD()
        self._check(self.user.GetUserObjectInformationW(handle,2,buffer,ctypes.sizeof(buffer),ctypes.byref(needed)),'GetUserObjectInformation(UOI_NAME)')
        return buffer.value

    def desktop(self,handle):
        active=self.w.BOOL();needed=self.w.DWORD()
        self._check(self.user.GetUserObjectInformationW(handle,6,ctypes.byref(active),ctypes.sizeof(active),ctypes.byref(needed)),'GetUserObjectInformation(UOI_IO)')
        return dict(name=self.object_name(handle),receiving_input=bool(active.value))

    def thread_desktop(self):
        return self._check(self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId()),'GetThreadDesktop(current)')

    def open_input(self,access=READ):
        return self._check(self.user.OpenInputDesktop(0,False,access),'OpenInputDesktop')

    def session(self,pid):
        result=self.w.DWORD()
        self._check(self.kernel.ProcessIdToSessionId(pid,ctypes.byref(result)),'ProcessIdToSessionId')
        return result.value

    def window(self,hwnd,own=False):
        pid=self.w.DWORD();tid=int(self.user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid)))
        result=dict(hwnd=int(hwnd),pid=pid.value,tid=tid)
        try:result['session']=self.session(pid.value)
        except OSError as error:result['session_error']=str(error)
        try:result['desktop']=self.desktop(self._check(self.user.GetThreadDesktop(tid),'GetThreadDesktop(window)'))
        except OSError as error:result['desktop_error']=str(error)
        if own:
            if pid.value!=self.kernel.GetCurrentProcessId():raise RuntimeError('Refusing style/geometry inspection of a foreign HWND')
            rect=self.w.RECT();self._check(self.user.GetWindowRect(hwnd,ctypes.byref(rect)),'GetWindowRect(own)')
            cloaked=self.w.DWORD();code=self.dwm.DwmGetWindowAttribute(hwnd,14,ctypes.byref(cloaked),ctypes.sizeof(cloaked))
            result.update(visible=bool(self.user.IsWindowVisible(hwnd)),rect=[rect.left,rect.top,rect.right,rect.bottom],style=int(self.user.GetWindowLongPtrW(hwnd,-16)),ex_style=int(self.user.GetWindowLongPtrW(hwnd,-20)),
                          previous_hwnd=int(self.user.GetWindow(hwnd,3) or 0),next_hwnd=int(self.user.GetWindow(hwnd,2) or 0),cloaked_hresult=code,cloaked=cloaked.value if code>=0 else None)
        return result

    def evidence(self,input_handle=None,own_hwnds=()):
        pid=int(self.kernel.GetCurrentProcessId());tid=int(self.kernel.GetCurrentThreadId());station=self._check(self.user.GetProcessWindowStation(),'GetProcessWindowStation')
        class Flags(ctypes.Structure):_fields_=[('inherit',self.w.BOOL),('reserved',self.w.BOOL),('flags',self.w.DWORD)]
        flags=Flags();needed=self.w.DWORD();self._check(self.user.GetUserObjectInformationW(station,1,ctypes.byref(flags),ctypes.sizeof(flags),ctypes.byref(needed)),'GetUserObjectInformation(UOI_FLAGS)')
        result=dict(pid=pid,tid=tid,session=self.session(pid),active_console_session=int(self.kernel.WTSGetActiveConsoleSessionId()),station=dict(name=self.object_name(station),visible=bool(flags.flags&1),raw_flags=flags.flags),thread_desktop=self.desktop(self.thread_desktop()),windows_build=sys.getwindowsversion().build,windows_product_type=sys.getwindowsversion().product_type)
        foreground=int(self.user.GetForegroundWindow() or 0)
        result['foreground']=self.window(foreground) if foreground else None
        opened=None
        try:
            if input_handle is None:opened=self.open_input();input_handle=opened
            result['input_desktop']=self.desktop(input_handle)
        except OSError as error:result['input_desktop_error']=str(error)
        finally:
            if opened:self.close_desktop(opened)
        result['own_windows']=[self.window(hwnd,own=True) for hwnd in own_hwnds]
        return result

    def create_owned(self,name):
        ctypes.set_last_error(0);existing=self.user.OpenDesktopW(name,0,False,READ)
        if existing:
            self.close_desktop(existing);raise RuntimeError('Refusing an existing desktop name')
        if ctypes.get_last_error()!=2:raise OSError(ctypes.get_last_error(),'Unique-desktop absence check')
        ctypes.set_last_error(0);handle=self._check(self.user.CreateDesktopW(name,None,None,0,OWN_ACCESS,None),'CreateDesktopW(own)')
        if ctypes.get_last_error()==183:
            self.close_desktop(handle);raise RuntimeError('Desktop creation collided with an existing name')
        return handle

    def switch(self,handle):return bool(self.user.SwitchDesktop(handle))
    @staticmethod
    def last_error():return ctypes.get_last_error()
    def restore_thread(self,handle):return bool(self.user.SetThreadDesktop(handle))
    def close_desktop(self,handle):return bool(self.user.CloseDesktop(handle))

    def launch(self,arguments,desktop):
        startup=self.Startup();startup.cb=ctypes.sizeof(startup);startup.lpDesktop='WinSta0\\'+desktop;startup.dwFlags=0x80
        process=self.Process();command=ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
        self._check(self.kernel.CreateProcessW(arguments[0],command,None,None,False,0x08000000,None,str(Path(__file__).resolve().parent),ctypes.byref(startup),ctypes.byref(process)),'CreateProcessW(own child)')
        return dict(process=process.hProcess,thread=process.hThread,pid=process.dwProcessId,tid=process.dwThreadId)

    def wait(self,child,timeout_ms):
        result=int(self.kernel.WaitForSingleObject(child['process'],timeout_ms))
        if result not in (0,258):raise OSError(ctypes.get_last_error(),'WaitForSingleObject(own child)')
        return result==0

    def terminate(self,child):return bool(self.kernel.TerminateProcess(child['process'],124))

    def exit_code(self,child):
        code=self.w.DWORD();self._check(self.kernel.GetExitCodeProcess(child['process'],ctypes.byref(code)),'GetExitCodeProcess(own child)');return code.value

    def close_child(self,child):
        return dict(thread=bool(self.kernel.CloseHandle(child['thread'])),process=bool(self.kernel.CloseHandle(child['process'])))


def read_desktop_evidence(own_hwnds=()):
    """Read-only diagnostics called by the native probe, with no station edits."""
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':return dict(error='Not a native Windows CI process')
    try:return DesktopApi().evidence(own_hwnds=own_hwnds)
    except Exception as error:return dict(error=type(error).__name__+': '+str(error))


def validate_preflight(evidence):
    station=evidence.get('station',{});desktop=evidence.get('input_desktop',{});foreground=evidence.get('foreground')
    if station.get('name','').casefold()!='winsta0' or not station.get('visible'):raise RuntimeError('Not the visible interactive WinSta0; no desktop changes allowed')
    if evidence.get('session')!=evidence.get('active_console_session') or evidence.get('session') in (None,0,0xffffffff):raise RuntimeError('Current process is not in the active interactive console session')
    if foreground and foreground.get('session')!=evidence['session']:raise RuntimeError('Foreground/current session mismatch or inaccessible foreground session')
    if evidence.get('windows_product_type')!=1 or evidence.get('windows_build',0)<22621:raise RuntimeError('Not the approved Windows 11 client OS')
    # We do not classify arbitrary OOBE/private/secure desktops as safe to replace.
    if desktop.get('name','').casefold()!='default' or not desktop.get('receiving_input'):raise RuntimeError('Original input desktop is not the known nonsecure Default desktop')


def execute_fixture(api,arguments,report,timeout_ms=40000,desktop_name=None):
    """Bounded state machine; injected API is used by tests without any desktop."""
    original_input=None;own=None;child=None;original_thread=None;switch_attempted=False;child_exited=False
    report.update(verified=False,status='environment-blocked',restore_verified=False)
    try:
        report['before']=api.evidence()
        original_input=api.open_input(READ|SWITCH)
        report['restore_access_granted']=True
        report['preflight']=api.evidence(original_input);validate_preflight(report['preflight'])
        original_name=report['preflight']['input_desktop']['name'];original_thread=api.thread_desktop()
        name=desktop_name or 'FlickDockMaterialQA_'+uuid.uuid4().hex
        if not name.startswith('FlickDockMaterialQA_') or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for character in name):raise RuntimeError('Invalid own desktop name')
        report['own_desktop_name']=name;own=api.create_owned(name)
        report['created']=api.desktop(own)
        if report['created']['name']!=name:raise RuntimeError('Created desktop identity differs from our unique name')
        switch_attempted=True;report['switch_to_own_ok']=api.switch(own)
        if not report['switch_to_own_ok']:
            report['switch_to_own_error']=api.last_error();raise RuntimeError('SwitchDesktop(own) denied; no escalation attempted')
        report['after_switch']=api.evidence()
        if report['after_switch'].get('input_desktop')!={'name':name,'receiving_input':True}:raise RuntimeError('Input-desktop readback did not confirm our fixture')
        child=api.launch(arguments+['--expected-desktop',name],name);report['child_identity']={key:child[key] for key in ('pid','tid')}
        child_exited=api.wait(child,timeout_ms)
        if not child_exited:
            report['status']='failed-child-timeout';report['child_timeout']=True;report['terminate_own_child_ok']=api.terminate(child)
            if not report['terminate_own_child_ok']:report['terminate_own_child_error']=api.last_error()
            child_exited=api.wait(child,3000)
            raise RuntimeError('Owned probe child exceeded its bounded deadline')
        report['child_exit']=api.exit_code(child);report['status']='completed-native-result'
    except Exception as error:report['error']=type(error).__name__+': '+str(error)
    finally:
        if child and not child_exited:
            try:
                report['cleanup_terminate_own_child_ok']=api.terminate(child);child_exited=api.wait(child,2000)
            except Exception as error:report['cleanup_child_error']=str(error)
        report['child_exited']=child_exited if child else None
        if switch_attempted and original_input:
            try:
                report['restore_switch_ok']=api.switch(original_input)
                if not report['restore_switch_ok']:report['restore_switch_error']=api.last_error()
                restored=api.evidence();report['after_restore']=restored
                report['restore_verified']=bool(report['restore_switch_ok'] and restored.get('input_desktop')=={'name':original_name,'receiving_input':True})
            except Exception as error:report['restore_error']=str(error)
        if original_thread:
            try:report['restore_thread_ok']=api.restore_thread(original_thread)
            except Exception as error:report['restore_thread_error']=str(error)
        if own:
            try:report['close_own_desktop_ok']=api.close_desktop(own)
            except Exception as error:report['close_own_error']=str(error)
        if original_input:
            try:report['close_original_input_ok']=api.close_desktop(original_input)
            except Exception as error:report['close_original_input_error']=str(error)
        if child:
            try:report['close_child_handles']=api.close_child(child)
            except Exception as error:report['close_child_error']=str(error)
        if own and (not report['restore_verified'] or not report.get('restore_thread_ok') or not report.get('close_own_desktop_ok') or not report.get('close_original_input_ok') or child and (not child_exited or not all(report.get('close_child_handles',{}).values()) or set(report.get('close_child_handles',{}))!={'thread','process'})):report['status']='failed-fixture-cleanup'
    return report


def child_main(build_json,output,log,expected_desktop):
    require_hosted_fixture()
    # Child-created file handles only; no inherited pipes that could fill/block.
    original_stdout,original_stderr=sys.stdout,sys.stderr
    with Path(log).open('w',encoding='utf-8',buffering=1) as stream:
        try:
            sys.stdout=stream;sys.stderr=stream
            for descriptor in (1,2):
                try:os.dup2(stream.fileno(),descriptor)
                except OSError:pass
            before=read_desktop_evidence();Path(output).with_name('child-bootstrap.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
            if not expected_desktop.startswith('FlickDockMaterialQA_') or before.get('thread_desktop',{}).get('name')!=expected_desktop or before.get('input_desktop')!={'name':expected_desktop,'receiving_input':True}:
                print('Child was not attached to the expected active own desktop',flush=True);return 3
            from ci_windows_material_probe import verify
            result=verify(build_json,output)
            return 0 if result['verified'] else 2
        finally:sys.stdout=original_stdout;sys.stderr=original_stderr


def fixture_verified(report,pixels):
    context=pixels.get('desktop_context_before_qt',{});name=report.get('own_desktop_name');closed=report.get('close_child_handles',{})
    return bool(report.get('status')=='completed-native-result' and report.get('child_exit')==0 and report.get('child_exited') and report.get('restore_verified') and report.get('restore_thread_ok') and report.get('close_own_desktop_ok') and report.get('close_original_input_ok')
                and set(closed)=={'thread','process'} and all(closed.values()) and pixels.get('verified') and pixels.get('github_sha')==report['github_sha']
                and context.get('thread_desktop',{}).get('name')==name and context.get('input_desktop')=={'name':name,'receiving_input':True})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--build-json',required=True);parser.add_argument('--output-dir');parser.add_argument('--child',action='store_true');parser.add_argument('--output');parser.add_argument('--log');parser.add_argument('--expected-desktop')
    args=parser.parse_args()
    if args.child:return child_main(args.build_json,args.output,args.log,args.expected_desktop)
    out=Path(args.output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    report=dict(verified=False,status='environment-blocked',hard_cancellation_boundary='Finally cannot be guaranteed after supervisor/VM hard termination; only recorded restore readback counts as success',github_sha=os.environ.get('GITHUB_SHA',''),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    try:
        require_hosted_fixture()
        build_path=Path(args.build_json).resolve(strict=True);build=json.loads(build_path.read_text(encoding='utf-8'))
        if not build.get('ok') or build.get('source_hashes',{}).get(Path(__file__).name)!=report['fixture_sha256']:raise RuntimeError('Build evidence does not bind this fixture source')
        arguments=[str(Path(sys.executable).resolve(strict=True)),str(Path(__file__).resolve()),'--child','--build-json',str(build_path),'--output',str(out/'pixels.json'),'--log',str(out/'child-bootstrap.log')]
        execute_fixture(DesktopApi(),arguments,report)
        pixels=json.loads((out/'pixels.json').read_text(encoding='utf-8')) if (out/'pixels.json').is_file() else {}
        report['verified']=fixture_verified(report,pixels)
    except Exception as error:report['error']=type(error).__name__+': '+str(error)
    (out/'desktop-fixture.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if report['verified'] else 2


if __name__=='__main__':raise SystemExit(main())
