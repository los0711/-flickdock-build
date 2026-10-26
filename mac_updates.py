"""Validated macOS .app ZIP staging and an after-exit atomic update helper.

No operation is performed on import. Windows tests use disposable app bundles
and explicit test_only=True; they never launch or install an application.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from uuid import uuid4
import zipfile

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from update_client import compare_versions, version_tuple

BUNDLE_ID = 'com.flickdock.app'
MAX_ZIP_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBERS = 100000
_STAGE = re.compile(r'\.flickdock-update-[0-9a-f]{32}\Z')
_MACHO = (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf')


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('更新准备已取消。')


def _sha(path, cancel_event=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while True:
            _check_cancel(cancel_event)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def bundle_metadata(app, *, bundle_id=BUNDLE_ID, version=None):
    app = Path(app)
    if app.is_symlink() or not app.is_dir() or app.name != 'FlickDock.app':
        raise ValueError('更新目标必须是普通的 FlickDock.app 应用目录。')
    info = app / 'Contents' / 'Info.plist'
    if info.stat().st_size > 1024 * 1024:
        raise ValueError('应用元数据过大。')
    data = plistlib.loads(info.read_bytes())
    if (not isinstance(data, dict) or data.get('CFBundleIdentifier') != bundle_id
            or data.get('CFBundlePackageType') != 'APPL'
            or data.get('CFBundleExecutable') != 'FlickDock'):
        raise ValueError('应用身份或可执行文件元数据不匹配。')
    version_tuple(data.get('CFBundleShortVersionString'))
    if version is not None and data['CFBundleShortVersionString'] != version:
        raise ValueError('应用内版本号与更新清单不一致。')
    executable = app / 'Contents' / 'MacOS' / 'FlickDock'
    if executable.is_symlink() or not executable.is_file():
        raise ValueError('更新包缺少主程序。')
    with executable.open('rb') as stream:
        if stream.read(4) not in _MACHO:
            raise ValueError('更新包主程序不是受支持的 macOS Mach-O 文件。')
    return data


def _target(path):
    path = Path(path).absolute()
    if path.name != 'FlickDock.app' or path.is_symlink():
        raise ValueError('更新目标必须是已安装的 FlickDock.app。')
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_dir():
        raise ValueError('应用安装位置不能包含目录链接。')
    return resolved


def _safe_name(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or '\\' in value or '\x00' in value or ':' in value
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.rstrip('/').split('/'))):
        raise ValueError('更新 ZIP 包含不安全路径。')
    path = PurePosixPath(value.rstrip('/'))
    if any(len(part) > 255 for part in path.parts):
        raise ValueError('更新 ZIP 路径过长。')
    return path


def _extract(archive, stage, cancel_event):
    seen, members, links, total = set(), [], {}, 0
    entries = archive.infolist()
    if len(entries) > MAX_MEMBERS:
        raise ValueError('更新 ZIP 文件数量超过限制。')
    for item in entries:
        _check_cancel(cancel_event)
        # ZipInfo normalizes backslashes on Windows; inspect the original
        # archive spelling too so host-independent QA catches the same attack.
        path = _safe_name(item.orig_filename)
        identity = unicodedata.normalize('NFC', str(path)).casefold()
        if identity in seen:
            raise ValueError('更新 ZIP 包含重复或大小写冲突路径。')
        seen.add(identity)
        if path.parts[0] == '__MACOSX':
            continue
        if path.parts[0] != 'FlickDock.app':
            raise ValueError('更新 ZIP 只能包含一个 FlickDock.app。')
        kind = stat.S_IFMT(item.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK) or item.flag_bits & 1:
            raise ValueError('更新 ZIP 包含不支持的特殊文件或加密文件。')
        total += item.file_size
        if total > MAX_UNPACKED_BYTES or item.file_size > 512 * 1024 * 1024:
            raise ValueError('更新 ZIP 解包大小超过限制。')
        if kind == stat.S_IFLNK:
            if item.file_size > 4096:
                raise ValueError('更新 ZIP 链接过长。')
            target = archive.read(item).decode('utf-8')
            if not target or '\\' in target or '\x00' in target or ':' in target or target.startswith('/'):
                raise ValueError('更新 ZIP 链接目标不安全。')
            normalized = posixpath.normpath(str(path.parent / target))
            if normalized != 'FlickDock.app' and not normalized.startswith('FlickDock.app/'):
                raise ValueError('更新 ZIP 链接越过应用目录。')
            links[str(path)] = target
        members.append((item, path, kind))
    if not members:
        raise ValueError('更新 ZIP 为空。')
    link_keys = {unicodedata.normalize('NFC', key).casefold() for key in links}
    for item, path, kind in members:
        if any(unicodedata.normalize('NFC', str(parent)).casefold() in link_keys for parent in path.parents):
            raise ValueError('更新 ZIP 不能向链接目录内写入文件。')
        destination = stage.joinpath(*path.parts)
        if kind == stat.S_IFLNK:
            continue
        if item.is_dir() or kind == stat.S_IFDIR:
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item) as source, destination.open('xb') as target:
            while True:
                _check_cancel(cancel_event)
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        mode = (item.external_attr >> 16) & 0o777
        destination.chmod(mode or 0o644)
    for value, target in links.items():
        _check_cancel(cancel_event)
        destination = stage.joinpath(*PurePosixPath(value).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(target, target_is_directory=(destination.parent / target).is_dir())
    bundle = stage / 'FlickDock.app'
    root = bundle.resolve(strict=True)
    for value in links:
        try:
            resolved = stage.joinpath(*PurePosixPath(value).parts).resolve(strict=True)
            if resolved != root and root not in resolved.parents:
                raise ValueError('更新 ZIP 链接越过应用目录。')
        except (OSError, RuntimeError):
            raise ValueError('更新 ZIP 包含无效或循环链接。') from None
    executable = bundle / 'Contents' / 'MacOS' / 'FlickDock'
    exec_entry = next((item for item, path, kind in members if str(path) == 'FlickDock.app/Contents/MacOS/FlickDock'), None)
    if exec_entry is None or not ((exec_entry.external_attr >> 16) & 0o111):
        raise ValueError('更新 ZIP 主程序缺少可执行权限。')
    return bundle


def _tree_hash(bundle, cancel_event=None):
    digest = hashlib.sha256()
    for path in sorted(Path(bundle).rglob('*'), key=lambda item: item.as_posix()):
        _check_cancel(cancel_event)
        name = path.relative_to(bundle).as_posix().encode('utf-8')
        if path.is_symlink():
            value = b'L' + os.readlink(path).encode('utf-8')
        elif path.is_file():
            value = b'F' + bytes.fromhex(_sha(path, cancel_event)) + str(stat.S_IMODE(path.stat().st_mode)).encode('ascii')
        elif path.is_dir():
            value = b'D'
        else:
            raise ValueError('更新目录出现不支持的文件。')
        digest.update(len(name).to_bytes(4, 'big') + name + value)
    return digest.hexdigest()


@dataclass
class PreparedMacUpdate:
    stage: Path
    app_path: Path
    installed_app: Path
    version: str
    current_version: str
    tree_sha256: str
    owner_pid: int
    bundle_id: str = BUNDLE_ID

    def cleanup(self):
        if self.stage.parent == self.installed_app.parent and _STAGE.fullmatch(self.stage.name):
            if self.stage.is_symlink():
                raise ValueError('更新暂存目录发生变化，未清理。')
            shutil.rmtree(self.stage, ignore_errors=True)


def prepare_update(zip_path, manifest, *, current_version, installed_app,
                   staging_parent=None, expected_bundle_id=BUNDLE_ID, cancel_event=None):
    """Background-only IO. Originals stay untouched until install_prepared()."""
    version = manifest.get('version') if isinstance(manifest, dict) else None
    if compare_versions(version, current_version) <= 0:
        raise ValueError('更新版本必须高于当前版本。')
    installer = manifest.get('installer', {})
    size, checksum = installer.get('size'), installer.get('sha256')
    if (type(size) is not int or not 0 < size <= MAX_ZIP_BYTES or not isinstance(checksum, str)
            or re.fullmatch(r'[0-9a-f]{64}', checksum) is None):
        raise ValueError('更新 ZIP 的大小或 SHA256 信息无效。')
    target = _target(installed_app)
    bundle_metadata(target, bundle_id=expected_bundle_id, version=current_version)
    parent = Path(staging_parent).resolve(strict=True) if staging_parent is not None else target.parent
    if parent != target.parent:
        raise ValueError('更新暂存目录必须与已安装应用在同一目录，以便原子替换。')
    zip_path = Path(zip_path)
    if zip_path.is_symlink() or zip_path.stat().st_size != size or _sha(zip_path, cancel_event) != checksum:
        raise ValueError('更新 ZIP 大小或 SHA256 校验失败。')
    stage = parent / ('.flickdock-update-' + uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bundle = _extract(archive, stage, cancel_event)
        if zip_path.stat().st_size != size or _sha(zip_path, cancel_event) != checksum:
            raise ValueError('更新 ZIP 在解包期间发生变化。')
        bundle_metadata(bundle, bundle_id=expected_bundle_id, version=version)
        _check_cancel(cancel_event)
        return PreparedMacUpdate(stage, bundle, target, version, current_version,
                                 _tree_hash(bundle, cancel_event), os.getpid(), expected_bundle_id)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _validate_prepared(prepared, *, verify_tree=True):
    if not isinstance(prepared, PreparedMacUpdate):
        raise ValueError('尚未准备有效的 macOS 更新。')
    if (not prepared.stage.is_absolute() or prepared.stage.resolve(strict=True) != prepared.stage
            or not _STAGE.fullmatch(prepared.stage.name) or prepared.stage.is_symlink()
            or prepared.stage.parent != prepared.installed_app.parent
            or prepared.app_path != prepared.stage / 'FlickDock.app'):
        raise ValueError('macOS 更新暂存位置无效。')
    if _target(prepared.installed_app) != prepared.installed_app:
        raise ValueError('macOS 更新目标位置无效。')
    if compare_versions(prepared.version, prepared.current_version) <= 0:
        raise ValueError('更新不能降级。')
    bundle_metadata(prepared.installed_app, bundle_id=prepared.bundle_id, version=prepared.current_version)
    bundle_metadata(prepared.app_path, bundle_id=prepared.bundle_id, version=prepared.version)
    if verify_tree and _tree_hash(prepared.app_path) != prepared.tree_sha256:
        raise ValueError('更新暂存文件在校验后发生变化。')


def _pid_alive(pid):
    if os.name == 'nt':
        # os.kill(pid, 0) is not a harmless probe on Windows Python.
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def install_prepared(prepared, *, installed_app=None, replace=os.replace, relaunch=None, test_only=False):
    """After-exit helper only. Tests explicitly operate on temporary fixtures."""
    if not test_only and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)):
        raise ValueError('仅 macOS 正式版辅助进程可以安装更新。')
    if not test_only and _pid_alive(prepared.owner_pid):
        raise ValueError('FlickDock 尚未退出，未替换应用。')
    _validate_prepared(prepared)
    target = prepared.installed_app
    if installed_app is not None and Path(installed_app).resolve(strict=True) != target:
        raise ValueError('更新目标与校验时的应用位置不一致。')
    backup = target.parent / ('.FlickDock.rollback-' + uuid4().hex + '.app')
    replace(target, backup)
    replaced = False
    try:
        replace(prepared.app_path, target)
        replaced = True
        bundle_metadata(target, bundle_id=prepared.bundle_id, version=prepared.version)
        if relaunch is not None:
            relaunch(target)
    except BaseException:
        if replaced:
            replace(target, prepared.app_path)
        replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    prepared.cleanup()
    return target


def launch_update_helper(prepared, *, executable=None, popen=None, quit_callback):
    """Launch a copy of the bundled binary in helper mode, then request quit."""
    if popen is None and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)):
        raise ValueError('仅 macOS 正式版可以启动更新辅助进程。')
    _validate_prepared(prepared, verify_tree=False)
    executable = Path(executable or sys.executable).resolve(strict=True)
    if executable != prepared.installed_app / 'Contents' / 'MacOS' / 'FlickDock':
        raise ValueError('更新辅助进程必须来自当前应用。')
    receipt = prepared.stage / 'update-receipt.json'
    payload = {key: str(value) if isinstance(value, Path) else value for key, value in vars(prepared).items()}
    receipt.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    receipt.chmod(0o600)
    process = (popen or subprocess.Popen)([str(executable), '--apply-mac-update', str(receipt)],
                      cwd=str(prepared.installed_app.parent), start_new_session=True,
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      shell=False)
    quit_callback()
    return process


def run_update_helper(receipt_path, *, timeout=30, test_only=False, pid_alive=_pid_alive,
                      replace=os.replace, relaunch=None):
    """Call before constructing QApplication when --apply-mac-update is present."""
    if not test_only and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)):
        raise ValueError('macOS 更新辅助进程当前不可用。')
    receipt = Path(receipt_path).absolute()
    if receipt.is_symlink() or receipt.name != 'update-receipt.json' or receipt.stat().st_size > 16384:
        raise ValueError('更新辅助进程收据无效。')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    for key in ('stage', 'app_path', 'installed_app'):
        data[key] = Path(data[key])
    prepared = PreparedMacUpdate(**data)
    if receipt != prepared.stage / 'update-receipt.json' or type(prepared.owner_pid) is not int or prepared.owner_pid <= 0:
        raise ValueError('更新辅助进程收据身份无效。')
    deadline = time.monotonic() + max(0, timeout)
    while pid_alive(prepared.owner_pid):
        if time.monotonic() >= deadline:
            raise TimeoutError('等待 FlickDock 退出超时；原应用未替换。')
        time.sleep(0.1)
    if relaunch is None and not test_only:
        relaunch = lambda path: subprocess.run(['/usr/bin/open', '-n', str(path)], shell=False, check=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return install_prepared(prepared, replace=replace, relaunch=relaunch, test_only=test_only)


class _PrepareSignals(QObject):
    done = Signal(object, str, bool)


class _CleanupWorker(QRunnable):
    def __init__(self, prepared):
        super().__init__()
        self.prepared = prepared

    def run(self): self.prepared.cleanup()


class _PrepareWorker(QRunnable):
    def __init__(self, args, options):
        super().__init__()
        self.args, self.options = args, options
        self.cancel_event, self.complete = threading.Event(), threading.Event()
        self.signals = _PrepareSignals()

    def run(self):
        result, error, cancelled = None, '', False
        try:
            result = prepare_update(*self.args, cancel_event=self.cancel_event, **self.options)
        except InterruptedError:
            cancelled = True
        except (OSError, ValueError, TypeError, zipfile.BadZipFile, plistlib.InvalidFileException) as problem:
            error = str(problem)
        if self.cancel_event.is_set():
            if result is not None:
                result.cleanup()
                result = None
            cancelled = True
        self.complete.set()
        self.signals.done.emit(result, error, cancelled)


class PrepareMacUpdateJob(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, zip_path, manifest, parent=None, *, current_version, installed_app,
                 staging_parent=None, expected_bundle_id=BUNDLE_ID):
        super().__init__(parent)
        self.worker = _PrepareWorker((zip_path, manifest), dict(current_version=current_version,
                            installed_app=installed_app, staging_parent=staging_parent,
                            expected_bundle_id=expected_bundle_id))
        self.worker.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        self._started = False

    def start(self):
        if self._started:
            return False
        self._started = True
        QThreadPool.globalInstance().start(self.worker)
        return True

    def cancel(self): self.worker.cancel_event.set()

    def wait(self, milliseconds=0):
        return self.worker.complete.wait(max(0, milliseconds) / 1000) if self._started else True

    def _done(self, prepared, error, cancelled):
        if cancelled or self.worker.cancel_event.is_set():
            if prepared is not None:
                QThreadPool.globalInstance().start(_CleanupWorker(prepared))
            self.cancelled.emit()
        elif error:
            self.failed.emit(error)
        else:
            self.finished.emit(prepared)
