"""Bounded native icon lookup, isolated from the application's GUI thread.

The helper owns its native QFileIconProvider on its own GUI thread and never
shows a window. A slow shell extension can be terminated without freezing the
shelf. Workers return QImages; no QPixmap crosses application threads.
"""
from collections import OrderedDict
import json,logging,os
from pathlib import Path
import subprocess,sys,tempfile,threading,time

from PySide6.QtCore import QObject,QRunnable,QThreadPool,Qt,Signal,QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication,QStyle
from shiboken6 import isValid


def generic_icon(size=96):
    app=QApplication.instance()
    return app.style().standardIcon(QStyle.SP_FileIcon).pixmap(QSize(size,size)).toImage()


def read_native_icon(ref,size,cancel):
    """A one-shot native helper, time-bounded and never invoked by a paint."""
    if cancel.is_set():return QImage()
    with tempfile.TemporaryDirectory(prefix='flickdock-edge-icon-') as directory:
        request=Path(directory)/'request.json'
        request.write_text(json.dumps(dict(path=ref,size=size)),encoding='utf-8')
        command=([sys.executable,'--edge-icon',str(request)] if getattr(sys,'frozen',False) else
                 [sys.executable,str(Path(__file__).with_name('flickdock.py')),'--edge-icon',str(request)])
        env=dict(os.environ);env.pop('QT_QPA_PLATFORM',None)
        if sys.platform=='win32':env['QT_QPA_PLATFORM']='windows'
        elif sys.platform=='darwin':env['QT_QPA_PLATFORM']='cocoa'
        flags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 env=env,creationflags=flags)
        deadline=time.monotonic()+2.5
        try:
            while process.poll() is None and not cancel.is_set() and time.monotonic()<deadline:
                cancel.wait(.025)
            if process.poll() is None:process.kill()
            process.wait(timeout=2)
            output=Path(directory)/'icon.png'
            if cancel.is_set():return QImage()
            if process.returncode!=0 or not output.is_file():
                failure=Path(directory)/'failure.txt'
                if failure.is_file():logging.error('Native icon helper failed:\n%s',failure.read_text(encoding='utf-8').replace(ref,'<file>'))
                else:logging.warning('Native icon helper exit code: %s',process.returncode)
                return QImage()
            data=output.read_bytes()
            return QImage.fromData(data) if len(data)<=1024*1024 else QImage()
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)


class _Signals(QObject):done=Signal(str,int,QImage)

class _IconJob(QRunnable):
    def __init__(self,ref,generation,reader):
        super().__init__();self.ref=ref;self.generation=generation;self.reader=reader
        self.cancel=threading.Event();self.signals=_Signals()
    def run(self):
        image=QImage()
        try:image=self.reader(self.ref,96,self.cancel)
        except (OSError,ValueError,subprocess.SubprocessError):logging.exception('Native edge icon lookup failed')
        except Exception:logging.exception('Unexpected native edge icon worker failure')
        if isValid(self.signals):self.signals.done.emit(self.ref,self.generation,image)


class NativeIconCache(QObject):
    ready=Signal(str)
    def __init__(self,parent=None,*,reader=None):
        super().__init__(parent);self.closed=False;self._reader=reader or read_native_icon
        self._native=reader is not None or QApplication.platformName() in ('windows','cocoa')
        self._cache=OrderedDict();self._jobs={};self._sequence=0;self._failed_until={}
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.pool.setExpiryTimeout(1500)
    def image(self,ref):
        value=self._cache.get(ref)
        if value is not None:self._cache.move_to_end(ref);return QImage(value)
        return None
    def request(self,ref):
        if self.closed:return None
        ref=str(ref);value=self.image(ref)
        if value is not None and value.isNull() and time.monotonic()>=self._failed_until.get(ref,0):
            self._cache.pop(ref,None);self._failed_until.pop(ref,None);value=None
        if value is not None:return value
        if not self._native:
            # Explicit offscreen test double: never launches native services.
            image=generic_icon();self._remember(ref,image);return image
        if ref not in self._jobs and len(self._jobs)<4:
            self._sequence+=1;job=_IconJob(ref,self._sequence,self._reader)
            job.signals.done.connect(self._completed,Qt.QueuedConnection);self._jobs[ref]=job;self.pool.start(job)
        return None
    def _remember(self,ref,image):
        self._cache[ref]=QImage(image);self._cache.move_to_end(ref)
        if image.isNull():self._failed_until[ref]=time.monotonic()+2
        else:self._failed_until.pop(ref,None)
        while len(self._cache)>64 or sum(i.sizeInBytes() for i in self._cache.values())>4*1024*1024:
            retired,_=self._cache.popitem(last=False);self._failed_until.pop(retired,None)
    def _completed(self,ref,generation,image):
        job=self._jobs.get(ref)
        if job is None or job.generation!=generation:return
        del self._jobs[ref]
        if self.closed:return
        if image.isNull():logging.warning('Native edge icon unavailable; retaining normal file intake')
        self._remember(ref,image);self.ready.emit(ref)
    def close(self):
        if self.closed:return
        self.closed=True
        for job in self._jobs.values():job.cancel.set()
        self.pool.clear();self._jobs.clear();self._cache.clear();self._failed_until.clear()


def helper_main(request_path):
    import traceback
    try:return _helper(request_path)
    except Exception:
        Path(request_path).with_name('failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
        return 3

def _helper(request_path):
    """Windowless CLI path, before ShelfManager/accounts/hotkeys are imported."""
    from PySide6.QtCore import QFileInfo
    from PySide6.QtWidgets import QFileIconProvider
    request=Path(request_path)
    if request.stat().st_size>65536:return 2
    value=json.loads(request.read_text(encoding='utf-8'));ref=value.get('path');size=value.get('size')
    if not isinstance(ref,str) or not Path(ref).is_absolute() or type(size) is not int or not 16<=size<=256:return 2
    if sys.platform=='darwin':
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app=QApplication.instance() or QApplication([])
    from flickdock_icons import _native_icon,document_pixmap
    artwork=_native_icon(Path(ref),size)
    if artwork.isNull():artwork=document_pixmap(Path(ref).suffix.lstrip('.') or 'file',size,dpr=1.)
    image=artwork.toImage()
    if max(image.width(),image.height())>256:image=image.scaled(256,256,Qt.KeepAspectRatio,Qt.SmoothTransformation)
    request.with_name('info.json').write_text(json.dumps(dict(platform=app.platformName(),top_level_widgets=len(app.topLevelWidgets()))),encoding='utf-8')
    return 0 if not image.isNull() and image.save(str(request.with_name('icon.png')),'PNG') else 3
