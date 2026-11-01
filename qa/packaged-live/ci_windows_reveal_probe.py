"""Hosted-only independent Shell selection comparison; no UIA/mouse selection.

Each case opens a different Unicode/space temporary folder. It records the
Shell's actual Folder/SelectedItems at real times; S_OK is not visual success.
The parent bounds every COM case in its own known subprocess, never /IM kills.
"""
from __future__ import annotations
import argparse,ctypes,hashlib,json,os,subprocess,sys,tempfile,threading,time,traceback
from pathlib import Path

MODES=('absolute-zero','parent-child-one','absolute-zero-sta-pump')


def require_hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or os.environ.get('FD_QA_NATIVE')!='1'):
        raise RuntimeError('Only an ephemeral GitHub-hosted Windows native QA job is permitted')
    return Path(os.environ['RUNNER_TEMP']).resolve(strict=True)


def inside(path,root):
    path,root=Path(path).resolve(),Path(root).resolve()
    return path!=root and path.is_relative_to(root)


def save(path,report):
    path=Path(path);temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');os.replace(temporary,path)


def snapshot(shell,folder,target,before):
    """Read only documents at the exact own fixture path. Never selects."""
    rows=[]
    for index,item in enumerate(shell.Windows()):
        if index>=64:raise RuntimeError('Shell enumeration exceeds diagnostic bound')
        try:
            location=item.Document.Folder.Self.Path
            if os.path.normcase(os.path.realpath(location))!=os.path.normcase(str(folder.resolve())):continue
            selected=item.Document.SelectedItems();count=int(selected.Count)
            paths=[str(selected.Item(i).Path) for i in range(min(count,16))]
            exact=count==1 and [os.path.normcase(os.path.realpath(p)) for p in paths]==[os.path.normcase(str(target.resolve()))]
            rows.append(dict(hwnd=int(item.HWND),new_hwnd=int(item.HWND) not in before,folder=str(folder),
                             selected_count=count,selected_paths=paths,exact_target_selected=exact))
        except (AttributeError,TypeError):continue
    return rows


def _native_call(mode,target,release,returned,done,state,lock):
    shell=ctypes.WinDLL('shell32',use_last_error=True);ole=ctypes.WinDLL('ole32',use_last_error=True)
    ptr=ctypes.c_void_p;uint=ctypes.c_uint32;hr=ctypes.c_int32
    shell.SHParseDisplayName.argtypes=[ctypes.c_wchar_p,ptr,ctypes.POINTER(ptr),uint,ctypes.POINTER(uint)];shell.SHParseDisplayName.restype=hr
    shell.SHOpenFolderAndSelectItems.argtypes=[ptr,uint,ctypes.POINTER(ptr),uint];shell.SHOpenFolderAndSelectItems.restype=hr
    shell.ILClone.argtypes=[ptr];shell.ILClone.restype=ptr
    shell.ILRemoveLastID.argtypes=[ptr];shell.ILRemoveLastID.restype=ctypes.c_int32
    shell.ILFindLastID.argtypes=[ptr];shell.ILFindLastID.restype=ptr
    ole.CoInitializeEx.argtypes=[ptr,uint];ole.CoInitializeEx.restype=hr
    ole.CoUninitialize.argtypes=[];ole.CoUninitialize.restype=None
    ole.CoTaskMemFree.argtypes=[ptr];ole.CoTaskMemFree.restype=None
    item=ptr();parent=ptr();initialized=False
    def result(name,value):
        code=int(value)&0xffffffff
        with lock:state[name]=dict(signed=ctypes.c_int32(code).value,hex=f'0x{code:08X}',elapsed=time.perf_counter()-started)
        if code&0x80000000:raise OSError(name+' '+f'0x{code:08X}')
        return code
    started=time.perf_counter()
    try:
        code=int(ole.CoInitializeEx(None,2))&0xffffffff
        if code==0x80010106:
            with lock:state['CoInitializeEx']=dict(hex=f'0x{code:08X}',existing_apartment=True)
        else:result('CoInitializeEx',code);initialized=True
        result('SHParseDisplayName',shell.SHParseDisplayName(str(target),None,ctypes.byref(item),0,None))
        if not item.value:raise RuntimeError('Shell returned a null item PIDL')
        if mode=='parent-child-one':
            parent=ptr(shell.ILClone(item));child=ptr(shell.ILFindLastID(item))
            if not parent.value or not child.value:raise RuntimeError('Cannot clone parent/find child PIDL')
            if not shell.ILRemoveLastID(parent):raise RuntimeError('ILRemoveLastID failed')
            children=(ptr*1)(child)
            result('SHOpenFolderAndSelectItems',shell.SHOpenFolderAndSelectItems(parent,1,children,0))
        else:result('SHOpenFolderAndSelectItems',shell.SHOpenFolderAndSelectItems(item,0,None,0))
        returned.set()
    except Exception as error:
        with lock:state.update(error=type(error).__name__+': '+str(error),traceback=traceback.format_exc())
        returned.set()
    finally:
        if parent.value:ole.CoTaskMemFree(parent)
        if item.value:ole.CoTaskMemFree(item)
        with lock:state['pidls_freed_before_pump']=True
        if mode=='absolute-zero-sta-pump' and initialized:
            # Keep ONLY the initialized STA alive. PIDLs were already freed,
            # matching the zero case's allocation lifetime. No input generated.
            import pythoncom
            count=0;deadline=time.monotonic()+12
            while not release.is_set() and time.monotonic()<deadline:
                pythoncom.PumpWaitingMessages();count+=1;time.sleep(.01)
            with lock:state['pump_iterations']=count
        if initialized:ole.CoUninitialize()
        with lock:state['com_uninitialized']=initialized;state['worker_finished']=True
        done.set()


def run_case(mode,fixture,output):
    runner=require_hosted();fixture=fixture.resolve(strict=True)
    if not inside(fixture,runner) or not fixture.name.startswith('flickdock-shell-probe-'):
        raise RuntimeError('Case fixture must be the generated unique RUNNER_TEMP directory')
    if mode not in MODES:raise ValueError('Unknown comparison mode')
    folder=fixture/mode/'Unicode 空格目录';target=folder/'目标 tiny 样本.bin'
    if not inside(target,fixture) or not target.is_file():raise RuntimeError('Missing own synthetic fixture')
    import pythoncom
    from win32com.client import Dispatch
    # Explicitly initialize the controller thread too; it only observes COM.
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    report=dict(mode=mode,ok=False,folder=str(folder),expected_target=str(target),
                input_generated=False,selection_modified_by_observer=False,samples=[])
    release=threading.Event();returned=threading.Event();done=threading.Event();state={};lock=threading.Lock()
    shell=None;before=set();native=None
    def persist():
        with lock:report['native']=dict(state)
        save(output,report)
    try:
        shell=Dispatch('Shell.Application');before={int(item.HWND) for item in shell.Windows()}
        report['before_hwnds']=sorted(before);persist()
        native=threading.Thread(target=_native_call,args=(mode,target,release,returned,done,state,lock),daemon=True,name='FlickDock-Shell-probe')
        native.start()
        if not returned.wait(8):raise TimeoutError('Native Shell request did not return within 8 seconds')
        epoch=time.perf_counter()
        for delay in (.2,1.,3.,6.):
            remaining=delay-(time.perf_counter()-epoch)
            if remaining>0:time.sleep(remaining)
            docs=snapshot(shell,folder,target,before)
            report['samples'].append(dict(requested_seconds=delay,actual_seconds=time.perf_counter()-epoch,documents=docs))
            persist()
        release.set()
        if not done.wait(2):raise TimeoutError('Native worker did not finish cleanup within 2 seconds')
        with lock:
            if state.get('error'):raise RuntimeError(state['error'])
        report['selection_observed']=any(doc['exact_target_selected'] for sample in report['samples'] for doc in sample['documents'])
        report['new_window_selection_observed']=any(doc['new_hwnd'] and doc['exact_target_selected'] for sample in report['samples'] for doc in sample['documents'])
        report['ok']=True  # Probe completed; selection_observed is independent.
    except Exception as error:report.update(error=type(error).__name__+': '+str(error),traceback=traceback.format_exc())
    finally:
        release.set()
        if shell is not None:
            # No process kill. Close only newly created exact own-folder HWNDs.
            try:
                user=ctypes.WinDLL('user32',use_last_error=True)
                user.PostMessageW.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_size_t,ctypes.c_ssize_t];user.PostMessageW.restype=ctypes.c_int32
                report['closed_own_windows']=[]
                for doc in snapshot(shell,folder,target,before):
                    if doc['new_hwnd']:
                        if not user.PostMessageW(doc['hwnd'],0x0010,0,0):raise OSError(ctypes.get_last_error(),'Cannot close own probe window')
                        report['closed_own_windows'].append(doc['hwnd'])
            except Exception as error:report['cleanup_error']=type(error).__name__+': '+str(error);report['ok']=False
        persist();pythoncom.CoUninitialize()
    return 0 if report['ok'] else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--case-mode',choices=MODES);parser.add_argument('--fixture',type=Path)
    args=parser.parse_args();runner=require_hosted()
    if args.case_mode:
        if args.fixture is None:parser.error('--fixture is required for a case worker')
        return run_case(args.case_mode,args.fixture,args.output)
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    fixture=Path(tempfile.mkdtemp(prefix='flickdock-shell-probe-',dir=runner)).resolve()
    report=dict(ok=False,scope='Official Shell API independent diagnostic; no normal EXE validation',
                fixture=str(fixture),source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),cases=[])
    for mode in MODES:
        folder=fixture/mode/'Unicode 空格目录';folder.mkdir(parents=True)
        for name in ('aaa other.bin','目标 tiny 样本.bin','zzz other.bin'):(folder/name).write_bytes(b'original bytes '+name.encode('utf8'))
        destination=output/(mode+'.json')
        with (output/(mode+'.log')).open('wb') as log:
            child=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--output',str(destination),
                                    '--case-mode',mode,'--fixture',str(fixture)],stdout=log,stderr=subprocess.STDOUT)
            try:code=child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=5);code=None
        result=json.loads(destination.read_text(encoding='utf8')) if destination.exists() else {}
        report['cases'].append(dict(mode=mode,exit_code=code,complete=result.get('ok') is True,
                                    selection_observed=result.get('selection_observed'),report=destination.name,
                                    new_window_selection_observed=result.get('new_window_selection_observed'),
                                    case_timeout=code is None))
        save(output/'comparison.json',report)
    report['ok']=all(case['complete'] and case['exit_code']==0 for case in report['cases'])
    report['all_modes_selected']=all(case['selection_observed'] is True for case in report['cases'])
    report['fixture_retained']=True;save(output/'comparison.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
