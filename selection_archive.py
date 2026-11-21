"""Cancellable ZIP export of selected shelf items, without UI-thread I/O.

``destination`` is the desired ZIP file path. Existing files are never replaced;
the returned path may have a `` (2)`` suffix. Text items are ``(label, text)``
pairs and go straight into UTF-8 entries inside the ZIP, without disk TXT files.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Iterable
import zipfile

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


CHUNK_SIZE = 256 * 1024


class ArchiveCancelled(Exception):
    """The caller cancelled before the completed archive was published."""


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ArchiveCancelled("已取消压缩。")


@dataclass(frozen=True)
class _Source:
    path: Path
    canonical: Path
    directory: bool
    link: bool


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _sources(paths: Iterable[os.PathLike | str], cancel_event) -> list[_Source]:
    """Keep each selected path once; selected folders already contain children."""
    sources = []
    seen = set()
    for value in paths:
        _check_cancelled(cancel_event)
        path = Path(value).expanduser().absolute()
        link = _is_link(path)
        # A link is an item in its own right, not a copy of the target. In
        # particular, selecting a junction must not discard its real target
        # folder or a separately selected file inside that target.
        canonical = path.parent.resolve(strict=True) / path.name if link else path.resolve(strict=True)
        key = (link, _path_key(canonical))
        if key in seen:
            continue
        mode = path.lstat().st_mode if link else path.stat().st_mode
        if not (link or stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"无法压缩此类型的项目：{path.name}")
        sources.append(_Source(path, canonical, stat.S_ISDIR(mode) and not link, link))
        seen.add(key)
    directories = [source.canonical for source in sources if source.directory]
    return [source for source in sources
            if not any(source.canonical != folder and source.canonical.is_relative_to(folder)
                       for folder in directories)]


def _unique_name(name: str, used: set[str], *, directory=False) -> str:
    """Names are unique even in Windows ZIP viewers with case-insensitive paths."""
    original = Path(name)
    stem, suffix = (name, "") if directory else (original.stem, original.suffix)
    candidate, number = name, 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({number}){suffix}"
        number += 1
    used.add(candidate.casefold())
    return candidate


def _text_name(label: str) -> str:
    # These names must also be extractable with the Windows built-in ZIP viewer.
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(label)).strip(" .")[:120]
    label = label or "文本"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", label):
        label = "_" + label
    return label if label.lower().endswith(".txt") else label + ".txt"


@contextmanager
def _source_reader(source: Path):
    """Read stable bytes while allowing Windows users to move/delete a source.

    The C runtime's default file open does not share DELETE on Windows. A ZIP
    worker may take seconds, so use an explicit read/delete-sharing handle.
    WRITE sharing remains disabled to prevent concurrent content modification.
    Ownership passes from HANDLE to CRT descriptor to the Python file exactly
    once; every error path closes whichever layer still owns it.
    """
    if os.name != "nt":
        with source.open("rb") as stream:
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    value = os.path.abspath(source)
    if len(value) >= 248 and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    handle = kernel.CreateFileW(value, 0x80000000, 0x00000001 | 0x00000004,
                                None, 3, 0x08000000 | 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = str(source)
        raise error
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def _write_file(archive: zipfile.ZipFile, source: Path, name: str, cancel_event):
    info = zipfile.ZipInfo.from_file(source, name, strict_timestamps=False)
    info.compress_type = zipfile.ZIP_DEFLATED
    with _source_reader(source) as incoming, archive.open(info, "w", force_zip64=True) as outgoing:
        while True:
            _check_cancelled(cancel_event)
            chunk = incoming.read(CHUNK_SIZE)
            if not chunk:
                break
            outgoing.write(chunk)
            _check_cancelled(cancel_event)


def _write_link(archive: zipfile.ZipFile, source: Path, name: str, cancel_event):
    """Do not recursively follow directory links/junctions or create link cycles."""
    _check_cancelled(cancel_event)
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, os.readlink(source).encode("utf-8"))


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _write_source(archive: zipfile.ZipFile, source: Path, name: str,
                  part: Path, cancel_event):
    # Iterative traversal also supports deep folders without Python recursion.
    pending = [(source, name)]
    part_key = _path_key(part.resolve())
    while pending:
        _check_cancelled(cancel_event)
        path, entry = pending.pop()
        if path.name.casefold() == part.name.casefold() and _path_key(path.resolve()) == part_key:
            continue
        if _is_link(path):
            _write_link(archive, path, entry, cancel_event)
        elif path.is_dir():
            archive.write(path, entry.rstrip("/") + "/")
            with os.scandir(path) as iterator:
                children = []
                for child in iterator:
                    _check_cancelled(cancel_event)
                    children.append(child)
                children.sort(key=lambda child: (child.name.casefold(), child.name))
            for child in reversed(children):
                _check_cancelled(cancel_event)
                pending.append((Path(child.path), entry + "/" + child.name))
        else:
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(f"无法压缩此类型的项目：{path.name}")
            _write_file(archive, path, entry, cancel_event)


def _write_text(archive: zipfile.ZipFile, label: str, text: str, cancel_event):
    info = zipfile.ZipInfo(label)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    with archive.open(info, "w", force_zip64=True) as outgoing:
        # Slice the string before encoding so large text never needs an extra
        # full-size UTF-8 copy and can be cancelled between chunks.
        for offset in range(0, len(text), CHUNK_SIZE // 4):
            _check_cancelled(cancel_event)
            outgoing.write(text[offset:offset + CHUNK_SIZE // 4].encode("utf-8"))
        _check_cancelled(cancel_event)


def _publish_part(part: Path, destination: Path, cancel_event) -> Path:
    number = 1
    while True:
        _check_cancelled(cancel_event)
        candidate = destination if number == 1 else destination.with_name(
            f"{destination.stem} ({number}){destination.suffix}")
        try:
            if os.name == "nt":
                # Windows rename is atomic and fails if the target exists.
                part.rename(candidate)
            else:
                # rename() replaces existing files on Unix. An atomic link
                # publishes complete bytes with the same no-overwrite contract.
                os.link(part, candidate)
                part.unlink()
            return candidate
        except FileExistsError:
            number += 1


def create_archive(paths: Iterable[os.PathLike | str], destination: os.PathLike | str,
                   texts: Iterable[tuple[str, str]] = (), cancel_event=None) -> Path:
    """Return the actual complete ZIP path, or raise on error/cancellation.

    Cancellation accepts a ``threading.Event``-compatible ``is_set()`` object.
    It is checked while walking folders and between 256 KiB file blocks.
    Only this call's random ``.part`` is removed after failure or cancellation.
    The destination parent must already exist. Directory links are archived as
    links, not followed; regular selected folder contents and empty dirs remain.
    """
    _check_cancelled(cancel_event)
    sources = _sources(paths, cancel_event)
    text_items = []
    for label, text in texts:
        _check_cancelled(cancel_event)
        text_items.append((str(label), str(text)))
    if not sources and not text_items:
        raise ValueError("没有可压缩的内容。")
    target = Path(destination).expanduser().absolute()
    if target.suffix.lower() != ".zip":
        target = target.with_name(target.name + ".zip")
    _check_cancelled(cancel_event)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.stem[:60]}-", suffix=".part", dir=target.parent)
    part = Path(temporary)
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True, strict_timestamps=False) as archive:
                names = set()
                for source in sources:
                    _check_cancelled(cancel_event)
                    name = _unique_name(source.path.name or "文件夹", names, directory=source.directory)
                    _write_source(archive, source.path, name, part, cancel_event)
                for label, text in text_items:
                    _check_cancelled(cancel_event)
                    name = _unique_name(_text_name(label), names)
                    _write_text(archive, name, text, cancel_event)
            stream.flush()
            os.fsync(stream.fileno())
        return _publish_part(part, target, cancel_event)
    finally:
        part.unlink(missing_ok=True)


class _ArchiveSignals(QObject):
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()


class _ArchiveWorker(QRunnable):
    def __init__(self, paths, destination, texts, cancel_event, completed):
        super().__init__()
        self.paths, self.destination, self.texts = paths, destination, texts
        self.cancel_event, self.completed = cancel_event, completed
        self.signals = _ArchiveSignals()

    def run(self):
        try:
            path = create_archive(self.paths, self.destination, self.texts, self.cancel_event)
        except ArchiveCancelled:
            self.signals.cancelled.emit()
        except Exception as error:
            self.signals.failed.emit(str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(str(path))
        finally:
            self.completed.set()


class ZipArchiveJob(QObject):
    """One-shot pool job. Keep it alive until one terminal signal is delivered.

    All public signals are delivered on this QObject's thread. ``cancel()`` is
    nonblocking; ``wait(timeout_ms)`` is available for orderly application exit
    and waits for disk cleanup, without needing the Qt event loop to be running.
    """
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, paths, destination, texts=(), parent=None, *, pool=None):
        super().__init__(parent)
        self.paths = tuple(paths)
        self.destination = destination
        self.texts = tuple((str(label), str(text)) for label, text in texts)
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._cancel_event = threading.Event()
        self._completed = threading.Event()
        self._started = False
        self._delivered = False
        self._worker = None
        self.result_path = ""

    @property
    def is_running(self) -> bool:
        return self._started and not self._completed.is_set()

    def start(self) -> bool:
        if self._started:
            return False
        self._started = True
        self._worker = _ArchiveWorker(self.paths, self.destination, self.texts,
                                      self._cancel_event, self._completed)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.signals.failed.connect(self._on_failed)
        self._worker.signals.cancelled.connect(self._on_cancelled)
        self._pool.start(self._worker)
        return True

    def cancel(self):
        self._cancel_event.set()

    def wait(self, timeout_ms: int = -1) -> bool:
        if not self._started:
            return True
        return self._completed.wait(None if timeout_ms < 0 else timeout_ms / 1000)

    @Slot(str)
    def _on_finished(self, path):
        if not self._delivered:
            self._delivered = True
            self.result_path = path
            self.finished.emit(path)

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
