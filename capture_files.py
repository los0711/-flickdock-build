"""Cancellable copies owned by the shelf; originals are never changed."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from selection_archive import _source_reader


CHUNK_SIZE = 256 * 1024


class CopyCancelled(Exception):
    pass


def _check(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CopyCancelled()


def _is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _copy_file(source, target, cancel_event):
    _check(cancel_event)
    with _source_reader(source) as incoming, target.open('xb') as outgoing:
        while True:
            _check(cancel_event)
            block = incoming.read(CHUNK_SIZE)
            if not block:
                break
            outgoing.write(block)
        _check(cancel_event)


def _copy_tree(source, target, cancel_event):
    pending = [(source, target)]
    while pending:
        _check(cancel_event)
        incoming, outgoing = pending.pop()
        if _is_link(incoming):
            raise ValueError(f'暂不支持复制符号链接或目录联接：{incoming.name}')
        mode = incoming.stat().st_mode
        if stat.S_ISDIR(mode):
            outgoing.mkdir()
            with os.scandir(incoming) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.casefold(), reverse=True)
            pending.extend((Path(child.path), outgoing / child.name) for child in children)
        elif stat.S_ISREG(mode):
            _copy_file(incoming, outgoing, cancel_event)
        else:
            raise ValueError(f'无法复制此类型的项目：{incoming.name}')


def copy_files(paths, directory, cancel_event=None):
    """Copy in order into one uniquely owned imports directory, or clean it up.

    This function performs disk I/O and is called only by a worker in the UI.
    Sources that contain the destination are rejected before any copying.
    """
    _check(cancel_event)
    sources = [Path(value).expanduser().absolute() for value in paths]
    if not sources:
        return []
    imports = Path(directory).expanduser().absolute() / 'imports'
    canonical_imports = imports.resolve()
    for source in sources:
        _check(cancel_event)
        if _is_link(source):
            raise ValueError(f'暂不支持复制符号链接或目录联接：{source.name}')
        canonical_source = source.resolve(strict=True)
        if source.is_dir() and canonical_imports.is_relative_to(canonical_source):
            raise ValueError(f'源文件夹包含搁架保存目录，无法复制：{source.name}')
    imports.mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix='capture-', dir=imports))
    # Only this exact freshly created directory may be removed on failure.
    if owned.parent.resolve() != canonical_imports:
        raise RuntimeError('复制暂存目录越过保存目录。')
    result, names = [], set()
    try:
        for source in sources:
            _check(cancel_event)
            directory_source = source.is_dir()
            name = source.name or 'folder'
            original = Path(name)
            stem, suffix = (name, '') if directory_source else (original.stem, original.suffix)
            number = 1
            while name.casefold() in names:
                number += 1
                name = f'{stem} ({number}){suffix}'
            names.add(name.casefold())
            target = owned / name
            _copy_tree(source, target, cancel_event)
            result.append(str(target))
        _check(cancel_event)
        return result
    except BaseException:
        shutil.rmtree(owned)
        raise


class _Signals(QObject):
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()


class _CopyWorker(QRunnable):
    def __init__(self, paths, directory, cancel_event, completed):
        super().__init__()
        self.paths, self.directory = paths, directory
        self.cancel_event, self.completed = cancel_event, completed
        self.signals = _Signals()

    def run(self):
        try:
            result = copy_files(self.paths, self.directory, self.cancel_event)
        except CopyCancelled:
            self.signals.cancelled.emit()
        except Exception as error:
            self.signals.failed.emit(str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(result)
        finally:
            self.completed.set()


class CopyFilesJob(QObject):
    """One-shot background copy with GUI-thread terminal signals.

    Keep the job alive until its terminal signal. cancel() is nonblocking;
    wait(ms) also waits for failure/cancellation cleanup without a Qt event loop.
    """
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, paths, directory, parent=None, *, pool=None):
        super().__init__(parent)
        self.paths, self.directory = tuple(paths), directory
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._cancel_event, self._completed = threading.Event(), threading.Event()
        self._started = self._delivered = False
        self._worker = None
        self.result_paths = []

    @property
    def is_running(self):
        return self._started and not self._completed.is_set()

    def start(self):
        if self._started:
            return False
        self._started = True
        self._worker = _CopyWorker(self.paths, self.directory, self._cancel_event, self._completed)
        self._worker.signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.signals.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._worker.signals.cancelled.connect(self._on_cancelled, Qt.ConnectionType.QueuedConnection)
        self._pool.start(self._worker)
        return True

    def cancel(self):
        self._cancel_event.set()

    def wait(self, timeout_ms=-1):
        return not self._started or self._completed.wait(None if timeout_ms < 0 else timeout_ms / 1000)

    @Slot(list)
    def _on_finished(self, paths):
        if not self._delivered:
            self._delivered = True
            self.result_paths = list(paths)
            self.finished.emit(self.result_paths)

    @Slot(str)
    def _on_failed(self, message):
        if not self._delivered:
            self._delivered = True
            self.failed.emit(message)

    @Slot()
    def _on_cancelled(self):
        if not self._delivered:
            self._delivered = True
            self.cancelled.emit()
