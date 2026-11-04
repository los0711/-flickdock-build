"""Qt ownership and cancellation for isolated media workers and their children."""
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import signal
import sys

from PySide6.QtCore import QObject,QProcess,QTimer,Signal

MAX_OUTPUT_BYTES=1024*1024
EXTRACT_TIMEOUT_MS=150000
DOWNLOAD_IDLE_TIMEOUT_MS=45000
PROCESSING_TIMEOUT_MS=300000


class ProcessTree:
    def __init__(self,pid):
        self.pid=pid;self.handle=None
        if sys.platform!='win32':return
        size=ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),
                ('min_working',size),('max_working',size),('process_limit',wintypes.DWORD),('affinity',size),
                ('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',ctypes.c_ulonglong*6),('process_memory',size),('job_memory',size),('peak_process',size),('peak_job',size)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel=kernel;handle=kernel.CreateJobObjectW(None,None)
        if not handle:raise ctypes.WinError(ctypes.get_last_error())
        process=kernel.OpenProcess(0x101,False,pid)
        try:
            limits=Extended();limits.basic.flags=0x2000
            if not process or not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,process):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle=handle
        except BaseException:kernel.CloseHandle(handle);raise
        finally:
            if process:kernel.CloseHandle(process)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle);self.handle=None
        elif sys.platform!='win32' and self.pid:
            try:os.killpg(self.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        self.pid=0


class VideoJob(QObject):
    changed=Signal(object)
    finished=Signal(object)

    def __init__(self,request,parent=None):
        super().__init__(parent);self.request=dict(request);self.process=QProcess(self)
        self.priority=False
        self.buffer=bytearray();self.stderr=bytearray();self.result=None;self.error='';self.cancelled=False;self.done=False;self.tree=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._timeout)
        if sys.platform!='win32':self.process.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        self.process.started.connect(self._started);self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._read_error);self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)

    def start(self):
        worker=Path(__file__).with_name('video_worker.py')
        arguments=['--video-worker'] if getattr(sys,'frozen',False) else [str(worker)]
        from PySide6.QtCore import QProcessEnvironment
        environment=QProcessEnvironment.systemEnvironment()
        for key in ('NODE_OPTIONS','NODE_PATH','NODE_EXTRA_CA_CERTS','PYTHONPATH','PYTHONSTARTUP'):environment.remove(key)
        self.process.setProcessEnvironment(environment)
        self.process.start(sys.executable,arguments)
        self.timer.start(EXTRACT_TIMEOUT_MS)

    def _started(self):
        try:self.tree=ProcessTree(int(self.process.processId()))
        except OSError as error:
            self.error='无法建立可取消的视频任务：'+str(error);self.process.kill();return
        self.process.write((json.dumps(self.request,ensure_ascii=False)+'\n').encode('utf-8'));self.set_priority(self.priority)

    def set_priority(self,active):
        self.priority=bool(active)
        if self.process.state()==QProcess.ProcessState.Running:
            self.process.write((json.dumps({'clipboard_priority':self.priority})+'\n').encode('utf-8'))

    def _read_error(self):
        self.stderr.extend(bytes(self.process.readAllStandardError()));self.stderr=self.stderr[-8192:]

    def _read(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.buffer)>MAX_OUTPUT_BYTES:self.error='视频组件响应超过限制。';self.cancel();return
        while b'\n' in self.buffer:
            line,_,tail=self.buffer.partition(b'\n');self.buffer=bytearray(tail)
            try:
                value=json.loads(line)
                if not isinstance(value,dict) or value.get('type') not in ('progress','result','warning','error'):raise ValueError()
                if value['type']=='result':self.result=value
                elif value['type']=='error':self.error=str(value.get('message') or '视频任务失败。')[:1000]
                elif value['type']=='progress':
                    for key in ('bytes','total','speed'):
                        number=value.get(key)
                        if number is not None and (type(number) not in (float,int) or not math.isfinite(number) or number<0):raise ValueError()
                    self.changed.emit(value)
                else:self.changed.emit(value)
                if self.request['action']=='download':
                    timeout=PROCESSING_TIMEOUT_MS if value.get('phase')=='processing' else DOWNLOAD_IDLE_TIMEOUT_MS
                    self.timer.start(timeout)
            except (ValueError,TypeError,UnicodeError):
                self.error='视频组件返回了无效响应。';self.cancel();return

    def _timeout(self):
        self.error='视频任务长时间没有响应，请检查网络后重试。';self.cancel()

    def cancel(self):
        if self.done:return
        self.cancelled=True
        if self.tree:self.tree.close()
        self.process.kill()

    def _process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.error='无法启动视频组件：'+self.process.errorString();self._finished(-1,QProcess.ExitStatus.CrashExit)

    def _finished(self,code,status):
        if self.done:return
        self._read();self._read_error();self.done=True;self.timer.stop()
        if self.tree:self.tree.close()
        if not self.error and not self.cancelled and (code!=0 or status!=QProcess.ExitStatus.NormalExit or self.result is None):
            self.error='视频组件异常退出。'+self.stderr.decode('utf-8','replace')[-500:]
        self.finished.emit(dict(result=self.result,error=self.error or ('已取消。' if self.cancelled else ''),cancelled=self.cancelled))
