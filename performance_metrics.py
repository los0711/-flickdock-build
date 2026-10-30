"""Opt-in diagnostics only. Unsupported metrics are explicit, never zeroes."""
import ctypes
import os
import sys
import threading
import time
import tracemalloc


def process_topology():
    """Explicit diagnostic snapshot only; no arguments, paths or user content."""
    if sys.platform!='win32':
        import subprocess
        try:
            text=subprocess.run(['/bin/ps','-axo','pid=,ppid=,comm='],capture_output=True,text=True,timeout=2,check=True).stdout
            rows=[dict(pid=int(a),parent_pid=int(b),name=c.rsplit('/',1)[-1])
                  for a,b,c in (line.strip().split(None,2) for line in text.splitlines() if line.strip())]
        except (OSError,ValueError,subprocess.SubprocessError):return dict(available=False,processes=[])
    else:
        from ctypes import wintypes as w
        class Entry(ctypes.Structure):
            _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('exe',w.WCHAR*260)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
        kernel.Process32FirstW.argtypes=kernel.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        kernel.CloseHandle.argtypes=[w.HANDLE]
        handle=kernel.CreateToolhelp32Snapshot(2,0)
        if handle==ctypes.c_void_p(-1).value:return dict(available=False,processes=[])
        rows=[]
        try:
            entry=Entry();entry.size=ctypes.sizeof(entry);valid=kernel.Process32FirstW(handle,ctypes.byref(entry))
            while valid:
                rows.append(dict(pid=entry.pid,parent_pid=entry.parent,native_threads=entry.threads,name=entry.exe))
                valid=kernel.Process32NextW(handle,ctypes.byref(entry))
        finally:kernel.CloseHandle(handle)
    included={os.getpid()}
    while True:
        previous=len(included);included.update(row['pid'] for row in rows if row['parent_pid'] in included)
        if len(included)==previous:break
    return dict(available=True,processes=[row for row in rows if row['pid'] in included])


def process_sample():
    sample=dict(timestamp=time.time(),pid=os.getpid(),platform=sys.platform,
        process_cpu_seconds=sum(os.times()[:2]),python_threads=threading.active_count(),
        gpu_usage=None,gpu_memory_bytes=None,qt_native_allocated_bytes=None,
        unavailable=['gpu_usage','gpu_memory_bytes','qt_native_allocated_bytes'])
    if tracemalloc.is_tracing():
        sample['python_traced_bytes'],sample['python_peak_bytes']=tracemalloc.get_traced_memory()
    else:sample['python_traced_bytes']=None
    if sys.platform=='win32':
        from ctypes import wintypes as w
        class Memory(ctypes.Structure):
            _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(name,ctypes.c_size_t) for name in
                ('peak_working_set','working_set','peak_paged_pool','paged_pool','peak_nonpaged_pool',
                 'nonpaged_pool','pagefile','peak_pagefile','private')]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in
                      ('read_operations','write_operations','other_operations','read_bytes','write_bytes','other_bytes')]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);psapi=ctypes.WinDLL('psapi',use_last_error=True)
        kernel.GetCurrentProcess.restype=w.HANDLE;handle=kernel.GetCurrentProcess()
        psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.POINTER(Memory),w.DWORD]
        memory=Memory();memory.cb=ctypes.sizeof(memory)
        if psapi.GetProcessMemoryInfo(handle,ctypes.byref(memory),memory.cb):
            sample.update(rss_bytes=memory.working_set,working_set_bytes=memory.working_set,
                          private_bytes=memory.private,commit_bytes=memory.pagefile)
        kernel.GetProcessHandleCount.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
        count=w.DWORD()
        if kernel.GetProcessHandleCount(handle,ctypes.byref(count)):sample['handles']=count.value
        kernel.GetProcessIoCounters.argtypes=[w.HANDLE,ctypes.POINTER(IO)]
        io=IO()
        if kernel.GetProcessIoCounters(handle,ctypes.byref(io)):
            sample['io']={name:getattr(io,name) for name,_ in io._fields_}
        user=ctypes.WinDLL('user32',use_last_error=True);user.GetGuiResources.argtypes=[w.HANDLE,w.DWORD]
        sample['gdi_handles']=user.GetGuiResources(handle,0);sample['user_handles']=user.GetGuiResources(handle,1)
    elif sys.platform=='darwin':
        import resource,subprocess
        sample['peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        try:
            result=subprocess.run(['/bin/ps','-o','rss=','-p',str(os.getpid())],capture_output=True,text=True,timeout=2,check=True)
            sample['rss_bytes']=int(result.stdout.strip())*1024
        except (OSError,ValueError,subprocess.SubprocessError):sample['rss_bytes']=None
        sample['unavailable']+=['windows_private_bytes','windows_commit_size']
    return sample


def qt_sample(app,manager=None):
    """Call only on Qt's GUI thread, and only when diagnostics are enabled."""
    from PySide6.QtCore import QObject,QTimer,QThread
    from PySide6.QtWidgets import QApplication
    if QThread.currentThread()!=app.thread():raise RuntimeError('Qt diagnostics require the GUI thread')
    from shiboken6 import getCppPointer
    widgets=QApplication.allWidgets();objects={}
    for root in [app,*QApplication.topLevelWidgets()]:
        for child in [root,*root.findChildren(QObject)]:objects[getCppPointer(child)[0]]=child
    timers=[child for child in objects.values() if isinstance(child,QTimer)]
    result=dict(widgets=len(widgets),windows=len(QApplication.topLevelWidgets()),
                visible_windows=sum(w.isVisible() for w in QApplication.topLevelWidgets()),
                tracked_qobjects=len(objects),parented_timers=len(timers),parented_active_timers=sum(t.isActive() for t in timers))
    if manager:
        shelf=manager.shelf;cache=shelf.image_cache
        result.update(image_cache_bytes=cache._image_bytes,image_decode_jobs=len(cache._pending),
            poster_cache_bytes=sum(image.sizeInBytes() for image in shelf.preview_cache.values()),
            cloud_requests=len(manager.account.api._requests),sync_jobs=len(manager.sync.jobs),
            active_transfers=sum(t.active for service in (manager.lan,manager.wan) for t in service._transfers.values()))
    return result
