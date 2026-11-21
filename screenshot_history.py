"""Observe newly saved macOS screenshots and retain them in local history."""
from __future__ import annotations

from collections import OrderedDict
import ctypes
from functools import cache
import logging
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
import time

from PySide6.QtCore import QObject, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QImageReader
from platform_native import native_input_available
from sync_files import DiskWork


LOG = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.heic', '.heif', '.webp', '.bmp'}
SCREENSHOT_NAME = re.compile(
    r'^(?:screen[\s_-]?(?:shot|capture)|截[屏图]|螢幕截圖|屏幕快照|屏幕截图|'
    r'QQ截图|微信截图|WeChat|CleanShot|Snipaste|PixPin|iShot|Shottr|SCR-\d{8}|Snip\d{8})', re.I)
CAPTURE_APP = re.compile(r'(?:screencapture|screenshot|cleanshot|shottr|snipaste|pixpin|ishot|snagit|monosnap|flameshot|snip)', re.I)
MAX_PENDING = 128
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
SETTLE_SECONDS = .18


def screenshot_hint(path, capture_app=False):
    if capture_app or SCREENSHOT_NAME.search(path.name):
        return True
    if any(SCREENSHOT_NAME.search(part) for part in path.parts[:-1]):
        return True
    try:
        marker = os.getxattr(path, 'com.apple.metadata:kMDItemIsScreenCapture')
        return bool(plistlib.loads(marker))
    except (OSError, AttributeError, ValueError, plistlib.InvalidFileException):
        return False


def read_screenshot(path, started, capture_app, previous, cancel, folders=()):
    """Read a finished file in a worker, without modifying it or the clipboard."""
    path = Path(path)
    try:
        before = path.lstat()
        stamp = (before.st_size, before.st_mtime_ns, before.st_ino)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES
                or before.st_mtime < started - 1 or stamp == previous):
            return None
        configured=bool(folders) and any(path.resolve().is_relative_to(folder) for folder in folders)
        if not screenshot_hint(path, capture_app or configured):
            return None
        if not before.st_size or cancel.wait(SETTLE_SECONDS):
            return 'retry'
        settled = path.stat()
        if (settled.st_size, settled.st_mtime_ns) != stamp[:2]:
            return 'retry'
        reader = QImageReader(str(path))
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > MAX_IMAGE_PIXELS:
            return 'retry'
        image = reader.read()
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != stamp[:2]:
            return 'retry'
        if image.isNull() or cancel.is_set():
            return 'retry'
        return 'image', dict(image=image, _screenshot_stamp=stamp)
    except (FileNotFoundError, PermissionError):
        return 'retry'


def save_folders(base,folders,cancel):
    from account_store import atomic_json
    if cancel.is_set():return False
    atomic_json(Path(base)/'screenshot-folders.json',folders)
    return True


def load_folders(base):
    try:
        value=json.loads((Path(base)/'screenshot-folders.json').read_text(encoding='utf8'))
        return [Path(path) for path in value if isinstance(path,str) and Path(path).is_absolute()] if isinstance(value,list) else []
    except (OSError,ValueError,TypeError):
        return []


@cache
def _file_events_api():
    pointer = ctypes.c_void_p
    core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    events = ctypes.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
    callback = ctypes.CFUNCTYPE(None, pointer, pointer, ctypes.c_size_t, pointer,
                               ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64))
    definitions = (
        (core, 'CFStringCreateWithCString', pointer, [pointer, ctypes.c_char_p, ctypes.c_uint32]),
        (core, 'CFArrayCreate', pointer, [pointer, ctypes.POINTER(pointer), ctypes.c_long, pointer]),
        (core, 'CFRunLoopGetMain', pointer, []),
        (core, 'CFRelease', None, [pointer]),
        (events, 'FSEventStreamCreate', pointer,
         [pointer, callback, pointer, pointer, ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32]),
        (events, 'FSEventStreamScheduleWithRunLoop', None, [pointer, pointer, pointer]),
        (events, 'FSEventStreamStart', ctypes.c_bool, [pointer]),
        (events, 'FSEventStreamStop', None, [pointer]),
        (events, 'FSEventStreamInvalidate', None, [pointer]),
        (events, 'FSEventStreamRelease', None, [pointer]),
    )
    for library, name, result, arguments in definitions:
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    return core, events, callback


class SavedScreenshotHistory(QObject):
    received = Signal(object)
    failed = Signal(str)

    def __init__(self, parent, *, home=None,base=None):
        super().__init__(parent)
        self.home = Path(home or Path.home()).resolve()
        self.base=Path(base) if base is not None else None
        self._folders=[]
        self._stream = None
        self._callback = None
        self._workspace = None
        self._observer = None
        self._capture_until = 0.
        self._pending = OrderedDict()
        self._recent = OrderedDict()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._job = None
        self._epoch = 0
        self._enabled = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._next)

    def start(self):
        if self._enabled or sys.platform != 'darwin' or not native_input_available():
            return
        import AppKit
        from Foundation import NSUserDefaults
        self._started = time.time()
        self._enabled = True
        self._epoch += 1
        self._workspace = AppKit.NSWorkspace.sharedWorkspace()
        self._observer = self._workspace.notificationCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidActivateApplicationNotification, None, None, self._app_activated)
        self._record_app(self._workspace.frontmostApplication())
        defaults = NSUserDefaults.standardUserDefaults().persistentDomainForName_('com.apple.screencapture') or {}
        location = Path(str(defaults.get('location') or self.home / 'Desktop')).expanduser()
        self._folders=[folder.resolve() for folder in load_folders(self.base)] if self.base is not None else []
        roots = [self.home, Path('/Volumes')]
        for folder in [location.resolve(),*self._folders]:
            if folder.is_absolute() and not any(folder.is_relative_to(root) for root in roots):
                roots.append(folder)
        try:
            core, events, callback = _file_events_api()
            self._callback = callback(self._events)
            strings = [core.CFStringCreateWithCString(None, os.fsencode(root), 0x08000100) for root in roots]
            if not all(strings):
                raise OSError('Could not create screenshot observation roots')
            array = core.CFArrayCreate(None, (ctypes.c_void_p * len(strings))(*strings), len(strings), None)
            try:
                # File events, ignore our own history writes, since now.
                self._stream = events.FSEventStreamCreate(None, self._callback, None, array,
                                                          0xffffffffffffffff, .10, 0x1E)
            finally:
                core.CFRelease(array)
                for string in strings:
                    core.CFRelease(string)
            if not self._stream:
                raise OSError('Could not start screenshot observation')
            mode = ctypes.c_void_p.in_dll(core, 'kCFRunLoopCommonModes').value
            events.FSEventStreamScheduleWithRunLoop(self._stream, core.CFRunLoopGetMain(), mode)
            if not events.FSEventStreamStart(self._stream):
                raise OSError('Screenshot observation did not start')
        except (OSError, RuntimeError) as error:
            self.stop()
            LOG.warning('Saved screenshot observer failed (%s)', type(error).__name__)
            self.failed.emit('无法监测新截图，请检查截图保存文件夹的读取权限。')

    def _record_app(self, app):
        if app and CAPTURE_APP.search(str(app.bundleIdentifier()) + ' ' + str(app.localizedName())):
            self._capture_until = time.monotonic() + 30

    def _app_activated(self, notice):
        self._record_app(notice.userInfo().get('NSWorkspaceApplicationKey'))

    def _events(self, stream, context, count, paths, flags, ids):
        try:
            names = ctypes.cast(paths, ctypes.POINTER(ctypes.c_char_p))
            self._record_app(self._workspace.frontmostApplication())
            capture = time.monotonic() < self._capture_until
            for index in range(count):
                if flags[index] & 0x10000 and flags[index] & 0x9900:
                    self.consider(os.fsdecode(names[index]), capture_app=capture)
        except Exception:
            # Keep the full stack at the native callback boundary.
            LOG.exception('Saved screenshot file-event callback failed')

    def consider(self, filename, *, capture_app=False):
        if not self._enabled:
            return
        path = Path(filename)
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            return
        if any(part.startswith('.') or part in ('Library', 'node_modules', 'FlickDock.app') for part in path.parts[1:]):
            return
        key = str(path)
        if key not in self._pending and len(self._pending) >= MAX_PENDING:
            return
        self._pending[key] = (capture_app, time.monotonic() + 8)
        if self._job is None and not self._timer.isActive():
            self._timer.start(100)

    def _next(self):
        if not self._enabled or self._job is not None or not self._pending:
            return
        filename, (capture, deadline) = self._pending.popitem(last=False)
        epoch = self._epoch
        folders=tuple(self._folders)
        job = DiskWork(lambda cancel: read_screenshot(filename, self._started, capture, self._recent.get(filename), cancel, folders))
        self._job = job

        def ready(value, error):
            self._job = None
            if epoch != self._epoch or not self._enabled:
                if self._enabled and self._pending:self._timer.start(100)
                return
            if error:
                LOG.warning('Saved screenshot read failed (%s)', type(error).__name__,
                            exc_info=(type(error),error,error.__traceback__))
            elif value == 'retry' and time.monotonic() < deadline:
                self._pending.setdefault(filename, (capture, deadline))
            elif value and value != 'retry':
                self.received.emit(value)
                self._recent[filename] = value[1]['_screenshot_stamp']
                while len(self._recent) > 128:
                    self._recent.popitem(last=False)
            if self._pending:
                self._timer.start(100)

        job.signals.done.connect(ready, Qt.QueuedConnection)
        self._pool.start(job)

    def stop(self):
        self._enabled = False
        self._epoch += 1
        self._timer.stop()
        self._pending.clear()
        if self._job is not None:
            self._job.cancel.set()
        if self._stream is not None:
            _, events, _ = _file_events_api()
            events.FSEventStreamStop(self._stream)
            events.FSEventStreamInvalidate(self._stream)
            events.FSEventStreamRelease(self._stream)
            self._stream = None
        self._callback = None
        if self._observer is not None:
            self._workspace.notificationCenter().removeObserver_(self._observer)
            self._observer = None

    def close(self):
        self.stop()
        self._pool.waitForDone(2000)

