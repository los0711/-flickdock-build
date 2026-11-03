"""Review candidate only: hosted real cleanup/stdio/reap, never OS clipboard.

No runtime adapters, native editor, GUI window, installer or existing-process
inspection. The expected EXE digest must come from the independently verified
fresh build manifest; a digest supplied here alone does not verify its source.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import shutil
import sys
import time

REPOSITORY='los0711/-flickdock-build'
JOBS={'Windows':frozenset({'frozen-normal-entry','cleanup-boundaries'}),
      'macOS':frozenset({'build','cleanup-boundaries'})}
PLATFORMS={'Windows':'win32','macOS':'darwin'}
RUNTIME={
    'clipboard_sync.py':'6eb2e683b3ce24c892307bb02dc78043f7ffcfd70f67d33f690ce40d42adb306',
    'clipboard_cleanup.py':'569d3066bf9936a2cd92386b7a8d64efbe014a2f523f611097b86c9d11ba4ea6',
    'clipboard_cleanup_worker.py':'fd631a9451ca62aaee90f9c2f5e1bb5c068fdc78fcda15883d34631a963f6552',
}
PREFIX='flickdock-cleanup-hosted-'
HEX=re.compile(r'[0-9a-f]{64}\Z')
NONCE=re.compile(r'[0-9a-f]{32}\Z')
WAIT_SECONDS=8


def guard(explicit,nonce,env=None,platform=None):
    """Pure, first operation: invalid environments cannot reach filesystem/API."""
    env=os.environ if env is None else env
    platform=sys.platform if platform is None else platform
    runner=env.get('RUNNER_OS')
    run_id=env.get('GITHUB_RUN_ID');temporary=env.get('RUNNER_TEMP')
    if (explicit is not True or not isinstance(nonce,str) or not NONCE.fullmatch(nonce)
            or env.get('GITHUB_ACTIONS')!='true' or env.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or env.get('GITHUB_REPOSITORY')!=REPOSITORY or runner not in JOBS
            or platform!=PLATFORMS[runner] or env.get('GITHUB_JOB')not in JOBS[runner]
            or not isinstance(temporary,str) or not temporary
            or not (ntpath.isabs(temporary) if runner=='Windows' else posixpath.isabs(temporary))
            or not isinstance(run_id,str) or not run_id.isdigit() or int(run_id)<=0):
        raise RuntimeError('HOSTED_CLEANUP_SCOPE_REQUIRED')
    return runner


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(262144),b''):digest.update(block)
    return digest.hexdigest()


def own_root(nonce):
    # Resolve only the trusted runner temporary base, then reject links in the
    # newly owned namespace. Never scan an existing application cache.
    import stat
    base=Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    root=base/(PREFIX+nonce)
    root.mkdir(mode=0o700,exist_ok=False)
    for path in (root,*root.parents):
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise RuntimeError('REPARSE_PROFILE_REJECTED')
    (root/'fixture-owner.json').write_text(json.dumps({'purpose':'cleanup-hosted-v1','nonce':nonce}),encoding='ascii')
    return root


def check(condition,code):
    if not condition:raise RuntimeError(code)


def request(cache,worker,*,mode='plan',targets=None,refs=None,stamp=None):
    return dict(mode=mode,cache=str(cache),targets=targets or [],references=refs or [],
                protected=[],inventory=mode=='plan',cursor='',cache_stamp=stamp)


def await_operation(app,controller,value,operations):
    receipts=[]
    token=controller.submit(value,receipts.append)
    check(token is not None,'SUBMIT_REJECTED')
    operation=controller.active;operations.append(operation)
    until=time.monotonic()+WAIT_SECONDS
    while controller.active is not None and time.monotonic()<until:
        app.processEvents();time.sleep(.005)
    if controller.active is not None:
        controller.cancel()
        until=time.monotonic()+2
        while not operation.done.is_set() and time.monotonic()<until:time.sleep(.005)
        app.processEvents()
        raise RuntimeError('OPERATION_NOT_TERMINAL')
    check(operation.done.is_set() and operation.reaped and controller.quarantined is None,'CHILD_NOT_REAPED')
    check(len(receipts)==1,'RECEIPT_COUNT')
    return receipts[0]


def attribute_only_control(worker,root):
    import ctypes
    from ctypes import wintypes as w
    directory=root/'attribute-only-control';directory.mkdir();moved=root/'attribute-only-moved'
    kernel=worker.windows_directory_api()
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
    kernel.CreateFileW.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    handle=kernel.CreateFileW(str(directory),0x80,3,None,3,0x02200000,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    allowed=False
    try:
        try:directory.rename(moved);allowed=True
        except PermissionError:pass
    finally:
        if not kernel.CloseHandle(handle):raise ctypes.WinError(ctypes.get_last_error())
    if allowed:moved.rename(directory)
    return allowed


def run_cases(app,lane,worker,root,exe,operations):
    results=[]
    original=root/'original.bin';original.write_bytes(b'owned-original-do-not-delete')
    original_sha=sha(original)
    for frozen in ((False,True) if exe is not None else (False,)):
        cache=root/('frozen' if frozen else 'source')/'clipboard-cache';cache.mkdir(parents=True)
        expired=cache/'clip-expired';expired.mkdir()
        current=cache/'clip-current';current.mkdir()
        created=time.time()-90000
        for directory in (expired,current):
            (directory/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':created}),encoding='ascii')
            (directory/'body.bin').write_bytes(b'owned-cache-original')
        current_sha=sha(current/'body.bin')
        options={'command':[str(exe),'--clipboard-cleaner']} if frozen else {}
        controller=lane.ClipboardCleanup(**options)  # Production default spawn.
        try:
            plan=await_operation(app,controller,request(cache,worker,refs=[str(current/'body.bin'),str(original)]),operations)
            check(plan.get('status')=='ready' and not plan.get('errors'),'PLAN_FAILED')
            check([item['name'] for item in plan['candidates']]==[expired.name],'REFERENCE_NOT_PRESERVED')
            deleted=await_operation(app,controller,request(cache,worker,mode='delete',targets=plan['candidates'],
                refs=[str(current/'body.bin'),str(original)],stamp=plan['cache_stamp']),operations)
            check(deleted.get('status')=='ready' and not deleted.get('errors') and deleted.get('deleted')==[expired.name],'DELETE_FAILED')
            check(not expired.exists() and sha(current/'body.bin')==current_sha and sha(original)==original_sha,'ORIGINAL_CHANGED')
            results.append({'scope':'frozen-early-entry' if frozen else 'source-default-entry',
                            'expired_deleted':True,'current_kept':True,'original_kept':True,'actual_children_reaped':2})
        finally:controller.close()

    # A controlled sleeping purpose fixture tests the real default process owner,
    # independent timer and reap. It is not mislabeled a frozen worker hang.
    timeout=lane.ClipboardCleanup(command=[sys.executable,'-c','import time; time.sleep(30)'],deadline_ms=300)
    try:
        outcome=await_operation(app,timeout,request(root/'source'/'clipboard-cache',worker),operations)
        check(outcome.get('status')=='deferred' and outcome.get('code')=='DEADLINE','TIMEOUT_NOT_ENFORCED')
        operation=operations[-1]
        check(operation.process is not None and operation.process.poll()is not None,'TIMED_CHILD_STILL_LIVE')
        results.append({'scope':'default-owner-synthetic-sleep-fixture','deadline_killed':True,'reaped':True})
    finally:timeout.close()

    if os.name=='nt':
        old_rename_allowed=attribute_only_control(worker,root)
        directory=root/'share-pin';directory.mkdir();moved=root/'share-pin-moved'
        denied=False
        with worker.held_directory(directory):
            try:directory.rename(moved)
            except PermissionError:denied=True
        check(denied and directory.exists() and not moved.exists(),'NATIVE_SHARE_DELETE_NOT_DENIED')
        directory.rename(moved);moved.rename(directory)
        results.append({'scope':'actual-Windows-owned-directory','attribute_only_control_rename_allowed':old_rename_allowed,
                        'rename_denied_while_held':True,'rename_after_release':True,
                        'requested_directory_access':worker.WINDOWS_DIRECTORY_READ_ACCESS})
    else:
        needed=(os.open,os.stat,os.unlink,os.rmdir,os.link)
        check(all(function in os.supports_dir_fd for function in needed),'POSIX_DIR_FD_UNAVAILABLE')
        check(hasattr(os,'O_NOFOLLOW'),'POSIX_NOFOLLOW_UNAVAILABLE')
        results.append({'scope':'actual-POSIX-kernel','dir_fd_backend_exercised_by_both_delete_children':True})
    return results


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--hosted-cleanup',action='store_true')
    parser.add_argument('--nonce',required=True)
    parser.add_argument('--repo',required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--exe')
    mode.add_argument('--source-only',action='store_true')
    parser.add_argument('--exe-sha256')
    args=parser.parse_args(argv)
    # No filesystem, Qt, default process owner or native import before guard.
    runner=guard(args.hosted_cleanup,args.nonce)
    check((args.exe is None and args.exe_sha256 is None) or
          (isinstance(args.exe_sha256,str) and bool(HEX.fullmatch(args.exe_sha256))),'INVALID_EXE_DIGEST')
    repo=Path(args.repo).resolve(strict=True);exe=Path(args.exe).resolve(strict=True) if args.exe is not None else None
    before={name:sha(repo/name) for name in RUNTIME}
    check(before==RUNTIME and (exe is None or exe.is_file() and sha(exe)==args.exe_sha256),'FROZEN_SOURCE_OR_EXE_MISMATCH')
    root=own_root(args.nonce);operations=[]
    report={'ok':False,'observation_only':True,'installer_acceptance':False,'runner':runner,
            'scope':'hosted-owned-TEMP-cleanup-only-no-clipboard-no-GUI','source_before':before,
            'exe_sha256':args.exe_sha256,'frozen_entry_tested':exe is not None,'checks':[]}
    try:
        temp=root/'temp';temp.mkdir()
        for key in ('TEMP','TMP','TMPDIR','APPDATA','LOCALAPPDATA','XDG_CONFIG_HOME'):os.environ[key]=str(temp)
        import tempfile
        tempfile.tempdir=str(temp)
        os.environ['QT_QPA_PLATFORM']='offscreen'
        sys.path.insert(0,str(repo))
        from PySide6.QtCore import QCoreApplication
        import clipboard_cleanup as lane
        import clipboard_cleanup_worker as worker
        app=QCoreApplication.instance()or QCoreApplication([])
        report['checks']=run_cases(app,lane,worker,root,exe,operations)
        report['source_after']={name:sha(repo/name) for name in RUNTIME}
        check(report['source_after']==before and (exe is None or sha(exe)==args.exe_sha256),'SOURCE_CHANGED_DURING_GATE')
        report['ok']=True
    except Exception as error:
        # This QA report has only fixed check codes/types, never arbitrary paths
        # or native exception messages. No failure is converted to acceptance.
        report['error_type']=type(error).__name__
        report['error_code']=error.args[0] if type(error)is RuntimeError and error.args and re.fullmatch('[A-Z_]+',str(error.args[0]))else 'GATE_FAILED'
    finally:
        for operation in operations:
            if not operation.done.is_set():operation.cancel()
        end=time.monotonic()+3
        while any(not item.done.is_set() for item in operations) and time.monotonic()<end:time.sleep(.005)
        report['all_children_reaped']=all(item.done.is_set() and item.reaped for item in operations)
        if not report['all_children_reaped']:report['ok']=False
        try:
            if report['all_children_reaped']:
                owner=json.loads((root/'fixture-owner.json').read_text(encoding='ascii'))
                check(owner=={'purpose':'cleanup-hosted-v1','nonce':args.nonce},'PROFILE_OWNER_CHANGED')
                shutil.rmtree(root)
                report['owned_profile_removed']=True
            else:report['owned_profile_retained_for_unreaped_owner']=True
        except Exception as error:
            report['ok']=False;report['cleanup_error_type']=type(error).__name__
        print(json.dumps(report,separators=(',',':')),flush=True)
    return 0 if report['ok']else 1


if __name__=='__main__':raise SystemExit(main())
