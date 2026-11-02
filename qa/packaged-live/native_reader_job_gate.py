"""Hosted-only real Windows Job gate. Never accesses any clipboard.

Owns exactly one fixture parent and its protected fixture child. Not a full
native-reader, frozen-windowed-stdio, renderer or cross-platform acceptance.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[2]
EXPECTED_REPOSITORY='los0711/-flickdock-build'
PROFILE_PREFIX='flickdock-reader-job-'
NONCE_PATTERN=re.compile('[0-9a-f]{32}')
READY_TIMEOUT_SECONDS=10
EXIT_TIMEOUT_SECONDS=5
POLL_INTERVAL_SECONDS=.01
WAIT_OBJECT_0=0
WAIT_TIMEOUT=258
DUPLICATE_SAME_ACCESS=2


def hosted_root(profile,nonce,*,create=False):
    # Fail before any native import, process launch or fixture file operation.
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('GITHUB_REPOSITORY')!=EXPECTED_REPOSITORY
            or not os.environ.get('RUNNER_TEMP') or not NONCE_PATTERN.fullmatch(nonce)):
        raise RuntimeError('HOSTED_RUNNER_REQUIRED')
    base=Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    root=Path(profile).absolute()
    if root.name!=PROFILE_PREFIX+nonce or root.parent.resolve(strict=True)!=base:
        raise RuntimeError('OWNED_PROFILE_REQUIRED')
    for path in (root,*root.parents):
        if path.is_symlink() or path.is_junction():raise RuntimeError('REPARSE_PROFILE_REJECTED')
    marker=root/'fixture-owner.json'
    if create:
        root.mkdir(exist_ok=False)
        marker.write_text(json.dumps({'nonce':nonce,'fixture':'reader-job-v1'}),encoding='ascii')
    elif json.loads(marker.read_text(encoding='ascii'))!={'nonce':nonce,'fixture':'reader-job-v1'}:
        raise RuntimeError('WRONG_FIXTURE_OWNER')
    return root


def fixture_child(nonce):
    import ctypes
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)]
    kernel.IsProcessInJob.restype=w.BOOL
    inside=w.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(),None,ctypes.byref(inside)):
        raise ctypes.WinError(ctypes.get_last_error())
    print(json.dumps({'nonce':nonce,'pid':os.getpid(),'in_job':bool(inside.value)}),flush=True)
    # The only termination under test is closure of the parent's Job handle.
    while True:time.sleep(1)


def fixture_parent(root,nonce):
    sys.path.insert(0,str(REPO))
    from win_owned_process import spawn_owned
    child=None
    try:
        child=spawn_owned([sys.executable,str(Path(__file__).resolve()),'--role','child',
                           '--profile',str(root),'--nonce',nonce])
        row=json.loads(child.stdout.readline().decode('ascii'))
        if row.get('nonce')!=nonce or row.get('pid')!=child.pid or row.get('in_job') is not True:
            raise RuntimeError('CHILD_JOB_MEMBERSHIP_FAILED')
        # Keep this exact kernel object open until the gate duplicates it.
        row['source_handle']=int(child._process)
        temp=root/'parent-ready.partial'
        temp.write_text(json.dumps(row),encoding='ascii');temp.replace(root/'parent-ready.json')
        while True:time.sleep(1)
    finally:
        # Normal/error cleanup only; forced termination intentionally bypasses
        # finally and must still kill the child through the noninherited Job.
        if child is not None:
            child.kill()
            try:child.wait(EXIT_TIMEOUT_SECONDS)
            finally:
                if child.stdout is not None:child.stdout.close()
                child.close()


def run_gate(root,nonce):
    import ctypes
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.GetProcessId.argtypes=[w.HANDLE];kernel.GetProcessId.restype=w.DWORD
    kernel.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
    kernel.DuplicateHandle.restype=w.BOOL
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    kernel.TerminateProcess.argtypes=[w.HANDLE,w.UINT];kernel.TerminateProcess.restype=w.BOOL
    parent=None;child_handle=None;child_verified=False
    report={'ok':False,'scope':'hosted-real-Windows-Job-only-no-clipboard','checks':[]}
    started=time.monotonic()
    try:
        parent=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--role','parent',
            '--profile',str(root),'--nonce',nonce],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+READY_TIMEOUT_SECONDS
        ready=root/'parent-ready.json'
        while not ready.exists():
            failure=root/'parent-error.json'
            if failure.exists():
                report['fixture_error']=json.loads(failure.read_text(encoding='ascii'))
                raise RuntimeError('FIXTURE_NATIVE_START_FAILED')
            if parent.poll() is not None:raise RuntimeError('FIXTURE_PARENT_EXITED_'+str(parent.returncode))
            if time.monotonic()>=deadline:raise TimeoutError('CHILD_READY_TIMEOUT')
            time.sleep(POLL_INTERVAL_SECONDS)
        row=json.loads(ready.read_text(encoding='ascii'))
        if (row.get('nonce')!=nonce or type(row.get('pid')) is not int or row['pid']<=0
                or type(row.get('source_handle')) is not int or row['source_handle']<=0 or row.get('in_job') is not True):
            raise RuntimeError('INVALID_CHILD_RECEIPT')
        # Duplicate the parent's exact child kernel object. Reopening a PID
        # could target an unrelated process after child exit and PID reuse.
        duplicated=w.HANDLE()
        if not kernel.DuplicateHandle(int(parent._handle),row['source_handle'],kernel.GetCurrentProcess(),
                                      ctypes.byref(duplicated),0,False,DUPLICATE_SAME_ACCESS):
            raise ctypes.WinError(ctypes.get_last_error())
        child_handle=duplicated.value
        if kernel.GetProcessId(child_handle)!=row['pid']:raise RuntimeError('CHILD_KERNEL_IDENTITY_MISMATCH')
        child_verified=True
        if kernel.WaitForSingleObject(child_handle,0)!=WAIT_TIMEOUT:raise RuntimeError('CHILD_NOT_ALIVE_BEFORE_PARENT_EXIT')
        report['checks'].append('actual_atomic_job_membership')
        killed=time.monotonic();parent.kill();parent.wait(timeout=EXIT_TIMEOUT_SECONDS)
        if kernel.WaitForSingleObject(child_handle,EXIT_TIMEOUT_SECONDS*1000)!=WAIT_OBJECT_0:raise RuntimeError('CHILD_SURVIVED_PARENT_DEATH')
        report['parent_death_to_child_exit_ms']=(time.monotonic()-killed)*1000
        report['checks'].append('child_reaped_after_forced_parent_death')
        report['ok']=True
    except Exception as error:
        report['error']={'type':type(error).__name__,'reason':str(error)[:160],
                         'winerror':getattr(error,'winerror',None)}
    finally:
        cleanup=[]
        if parent is not None:
            try:
                if parent.poll() is None:parent.kill()
                parent.wait(timeout=EXIT_TIMEOUT_SECONDS)
            except Exception as error:cleanup.append(type(error).__name__)
        if child_handle:
            try:
                # Failure teardown targets only the retained exact child handle.
                if child_verified and kernel.WaitForSingleObject(child_handle,0)==WAIT_TIMEOUT:
                    if not kernel.TerminateProcess(child_handle,1):raise ctypes.WinError(ctypes.get_last_error())
                    if kernel.WaitForSingleObject(child_handle,EXIT_TIMEOUT_SECONDS*1000)!=WAIT_OBJECT_0:raise RuntimeError('OWNED_CHILD_CLEANUP_TIMEOUT')
            except Exception as error:cleanup.append(type(error).__name__)
            finally:
                try:
                    if not kernel.CloseHandle(child_handle):raise ctypes.WinError(ctypes.get_last_error())
                except Exception as error:cleanup.append(type(error).__name__)
        if cleanup:report.update(ok=False,cleanup_errors=cleanup)
        report['seconds']=time.monotonic()-started
        report['source_sha256']={name:hashlib.sha256((REPO/name).read_bytes()).hexdigest()
                                 for name in ('win_owned_process.py','win_clipboard_native.py')}
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return 0 if report['ok'] else 1


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--role',choices=('gate','parent','child'),default='gate')
    parser.add_argument('--profile',required=True);parser.add_argument('--nonce',required=True)
    args=parser.parse_args(argv)
    root=hosted_root(args.profile,args.nonce,create=args.role=='gate')
    if args.role=='child':fixture_child(args.nonce)
    elif args.role=='parent':
        try:fixture_parent(root,args.nonce)
        except Exception as error:
            # A startup failure must retain safe API diagnostics rather than
            # disappear into the deliberately noninteractive fixture stdio.
            sys.path.insert(0,str(REPO))
            from win_clipboard_native import safe_failure
            data=safe_failure(getattr(error,'phase','spawn'),error)
            (root/'parent-error.json').write_text(json.dumps(data),encoding='ascii')
            return 1
    else:return run_gate(root,args.nonce)
    return 0


if __name__=='__main__':raise SystemExit(main())
