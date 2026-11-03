"""Explicit test-only process/filesystem providers; never imported by runtime."""
from contextlib import contextmanager,nullcontext
import ctypes,os,subprocess,sys,threading
from pathlib import Path
from unittest.mock import patch
from clipboard_cleanup_worker import held_directory as production_held_directory

TREE=Path(__file__).absolute().parent.parent
COUNTERS={'parent_native_attempts':0,'child_native_attempts':0,'spawned':0,'reaped':0}
_LOCK=threading.Lock()
_SCOPE_DEPTH=0


@contextmanager
def modeled_directory(path):
    import clipboard_cleanup_worker as worker
    before=worker.stamp(path)
    yield None
    # This is a provider model, not a Windows no-share-delete handle. Actual
    # native pinning and POSIX dir-fd guarantees remain hosted acceptance gates.
    if path.exists() and worker.stamp(path)!=before:raise worker.CleanupBoundary('Modeled directory changed')


def configure_child():
    import clipboard_cleanup_worker as worker
    counter=[0];request=None
    if '--request'in sys.argv:
        index=sys.argv.index('--request')
        try:request=worker.parse_arguments(sys.argv[index:])[0]
        except (ValueError,TypeError):pass # Malformed purpose is still rejected by real main.
    def blocked(*args,**kwargs):
        counter[0]+=1
        if request is not None:(request.parent/'qa-native-count').write_text(str(counter[0]),encoding='ascii')
        raise RuntimeError('Native API forbidden by explicit synthetic provider')
    ctypes.WinDLL=blocked
    if os.name=='nt':worker.held_directory=modeled_directory
    def send(raw):
        if counter[0]:raise RuntimeError('Synthetic child attempted a native API')
        worker.write_posix(raw)
    worker.write_response=send


class MonitoredProcess:
    def __init__(self,process,request):self.process=process;self.request=request;self.collected=False
    def __getattr__(self,name):return getattr(self.process,name)
    def wait(self,*args,**kwargs):
        code=self.process.wait(*args,**kwargs)
        if not self.collected:
            self.collected=True;count=0
            if self.request is not None:
                marker=self.request.parent/'qa-native-count'
                if marker.exists():count=int(marker.read_text(encoding='ascii'));marker.unlink()
            with _LOCK:COUNTERS['child_native_attempts']+=count;COUNTERS['reaped']+=1
        return code


def synthetic_spawn(command,**options):
    if command[0]!=sys.executable:raise AssertionError('Synthetic cleanup accepts only the owned source interpreter')
    bootstrap="import sys;sys.path.insert(0,"+repr(str(TREE))+ ");from qa.clipboard_cleanup_synthetic import configure_child;configure_child();\n"
    if len(command)>2 and command[1]=='-c':argv=[command[0],'-c',bootstrap+command[2],*command[3:]]
    elif len(command)>1 and Path(command[1]).name=='clipboard_cleanup_worker.py':
        argv=[command[0],'-c',bootstrap+"import clipboard_cleanup_worker as w;raise SystemExit(w.main(sys.argv[1:]))",*command[2:]]
    else:raise AssertionError('Unexpected cleanup purpose in synthetic provider')
    request=Path(command[command.index('--request')+1]) if '--request'in command else None
    process=subprocess.Popen(argv,**options)
    with _LOCK:COUNTERS['spawned']+=1
    return MonitoredProcess(process,request)


@contextmanager
def synthetic_scope():
    global _SCOPE_DEPTH
    if _SCOPE_DEPTH:
        _SCOPE_DEPTH+=1
        try:yield COUNTERS
        finally:_SCOPE_DEPTH-=1
        return
    import clipboard_cleanup as lane
    import clipboard_cleanup_worker as worker
    before=dict(COUNTERS)
    def blocked(*args,**kwargs):
        with _LOCK:COUNTERS['parent_native_attempts']+=1
        raise AssertionError('Native API forbidden by explicit synthetic provider')
    directory=patch.object(worker,'held_directory',modeled_directory) if os.name=='nt' else nullcontext()
    with patch.object(lane,'spawn',side_effect=synthetic_spawn),directory,patch.object(lane,'native_spawn',side_effect=blocked),patch.object(worker,'windows_directory_api',side_effect=blocked):
        _SCOPE_DEPTH=1
        try:yield COUNTERS
        finally:
            _SCOPE_DEPTH=0
            if COUNTERS['parent_native_attempts']!=before['parent_native_attempts'] or COUNTERS['child_native_attempts']!=before['child_native_attempts']:raise AssertionError('Synthetic cleanup attempted a native API')
            if COUNTERS['spawned']-before['spawned']!=COUNTERS['reaped']-before['reaped']:raise AssertionError('Owned synthetic child was not reaped before scope exit')
