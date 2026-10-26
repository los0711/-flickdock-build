"""Update presentation policy and explicit, revalidated installer handoff."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication

from update_client import (DEFAULT_ALLOWED_HOSTS, DEFAULT_MANIFEST_URL, UpdateClient,
                           compare_versions, validate_manifest, version_tuple)
from platform_native import app_bundle
from mac_updates import PrepareMacUpdateJob, launch_update_helper


def _identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _installer_path(path, updates_dir, version):
    root = Path(updates_dir).absolute()
    candidate = Path(path).absolute()
    expected = root / version / 'setup.exe'
    if candidate != expected:
        raise ValueError('安装包必须位于本程序的 updates/版本号/setup.exe。')
    canonical_root = root.resolve(strict=True)
    canonical = candidate.resolve(strict=True)
    if canonical != canonical_root / version / 'setup.exe':
        raise ValueError('安装包路径不能重定向到更新目录之外。')
    # Refuse reparse points at the version/file level, including Windows junctions.
    for item in (root / version, candidate):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('安装包路径不能使用链接或目录联接。')
    if not stat.S_ISREG(canonical.stat().st_mode):
        raise ValueError('安装包不是普通文件。')
    return canonical


def _locked_reader(path):
    """On Windows keep the verified file immutable until the child opens it."""
    if os.name != 'nt':
        return path.open('rb')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    create = api.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    # FILE_SHARE_READ permits execution reads, but denies writes and delete/rename.
    handle = create(str(path), 0x80000000, 0x1, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        api.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, 'rb')


class VerifiedInstaller:
    """A short-lived read lease, never constructed from unverified UI data."""
    def __init__(self, path, manifest, updates_dir, reader):
        self.path, self.manifest = path, manifest
        self.updates_dir, self.reader = Path(updates_dir), reader
        self.identity = _identity(os.fstat(reader.fileno()))

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def __del__(self):
        self.close()


def verify_installer(path, manifest, *, current_version, updates_dir,
                     allowed_hosts=DEFAULT_ALLOWED_HOSTS, cancelled=None):
    """Read and hash on a worker. The returned lease must always be closed."""
    candidate = validate_manifest(manifest, allowed_hosts, platform='win32')
    if compare_versions(candidate['version'], current_version) <= 0:
        raise ValueError('更新版本必须高于当前版本。')
    canonical = _installer_path(path, updates_dir, candidate['version'])
    reader = _locked_reader(canonical)
    try:
        before = os.fstat(reader.fileno())
        if before.st_size != candidate['installer']['size']:
            raise ValueError('安装包大小已变化，请重新下载。')
        digest, total, prefix = hashlib.sha256(), 0, b''
        while True:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('安装校验已取消。')
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            if not total:
                prefix = chunk[:2]
            total += len(chunk)
            digest.update(chunk)
        if (total != candidate['installer']['size'] or prefix != b'MZ'
                or digest.hexdigest() != candidate['installer']['sha256']
                or _identity(before) != _identity(os.fstat(reader.fileno()))):
            raise ValueError('安装包校验失败，请重新下载。')
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('安装校验已取消。')
        return VerifiedInstaller(canonical, copy.deepcopy(candidate), updates_dir, reader)
    except BaseException:
        reader.close()
        raise


def launch_installer(verified, *, installed_exe, quit_callback, popen=None,
                     parent_pid=None):
    """Spawn only a still-valid lease; quit only after successful process creation."""
    if not isinstance(verified, VerifiedInstaller) or verified.reader is None:
        raise ValueError('安装包尚未通过校验。')
    executable = Path(installed_exe).resolve(strict=True)
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise ValueError('无法确定当前程序的安装位置。')
    if popen is None or popen is subprocess.Popen:
        if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
            raise ValueError('仅已安装的 Windows 正式版可以启动在线安装。')
        if not os.path.samefile(executable, sys.executable):
            raise ValueError('更新目标与当前程序不一致。')
        launcher = subprocess.Popen
    else:
        launcher = popen
    path = _installer_path(verified.path, verified.updates_dir, verified.manifest['version'])
    if (_identity(path.stat()) != verified.identity
            or _identity(os.fstat(verified.reader.fileno())) != verified.identity):
        raise ValueError('安装包在校验后已变化，请重新下载。')
    arguments = [str(path), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                 '/CLOSEAPPLICATIONS', '/DIR=' + str(executable.parent),
                 '/FDOnlineUpdate=1', '/FDParentPid=' + str(os.getpid() if parent_pid is None else int(parent_pid)),
                 '/LOG=' + str(path.parent / 'install.log')]
    options = dict(cwd=str(executable.parent), shell=False)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    process = launcher(arguments, **options)
    verified.close()
    quit_callback()
    return process


def install_update(path, manifest, *, current_version, updates_dir, installed_exe,
                   quit_callback, allowed_hosts=DEFAULT_ALLOWED_HOSTS, popen=None,
                   parent_pid=None):
    """Standalone synchronous entry for callers already running on a worker.

    The controller instead runs verify_installer on QThreadPool and hands its
    lease to launch_installer on the GUI thread, keeping the quit callback there.
    """
    verified = verify_installer(path, manifest, current_version=current_version,
                                updates_dir=updates_dir, allowed_hosts=allowed_hosts)
    try:
        return launch_installer(verified, installed_exe=installed_exe,
                                quit_callback=quit_callback, popen=popen, parent_pid=parent_pid)
    finally:
        verified.close()


class _VerificationSignals(QObject):
    finished = Signal(int, object, str)


class _Verification(QRunnable):
    def __init__(self, generation, path, manifest, options):
        super().__init__()
        self.generation, self.path, self.manifest = generation, path, manifest
        self.options, self.cancelled = options, threading.Event()
        self.signals = _VerificationSignals()

    def run(self):
        result, error = None, ''
        try:
            result = verify_installer(self.path, self.manifest, cancelled=self.cancelled, **self.options)
        except (OSError, ValueError, TypeError) as problem:
            error = str(problem)
        self.signals.finished.emit(self.generation, result, error)


class _DisposeMacPrepared(QRunnable):
    def __init__(self, prepared):
        super().__init__()
        self.prepared = prepared

    def run(self): self.prepared.cleanup()


class UpdateController(QObject):
    """Headless update state: explicit authorization covers download and install."""
    state_changed = Signal(str, dict)
    installation_started = Signal(str)
    installation_failed = Signal(str)
    notification_shown = Signal(str)

    def __init__(self, app_version, data_dir, parent=None, *, settings=None, theme=None,
                 installed_exe=None, quit_callback=None, is_busy=None,
                 endpoint=None, allowed_hosts=DEFAULT_ALLOWED_HOSTS,
                 client=None, popen=None, defer_ms=1000, platform=None, machine=None):
        super().__init__(parent)
        version_tuple(app_version)
        self.app_version, self.data_dir = app_version, Path(data_dir).absolute()
        self.updates_dir = self.data_dir / 'updates'
        self.settings = settings
        self._theme = theme if theme is not None else getattr(settings, 'theme', 'light')
        self._installed_exe = Path(installed_exe or sys.executable)
        self._quit = quit_callback or QApplication.instance().quit
        self._is_busy, self._popen = is_busy or (lambda: False), popen
        self._closed, self._generation = False, 0
        self._installing = self._authorized = False
        self._jobs, self._notified = {}, set()
        self._pending_manifest = None
        self._pending_check = False
        self._manifest, self._verified_path = None, ''
        self._operation, self._retry_operation = 'check', 'check'
        self._state = 'idle'
        self._payload = dict(version='', received=0, total=0, error='')
        self._defer_ms = max(20, int(defer_ms))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._resume_automatic)
        self.client = client if client is not None else UpdateClient(app_version, self.updates_dir, self,
                                    endpoint=endpoint, allowed_hosts=allowed_hosts, platform=platform, machine=machine)
        self.platform = platform or getattr(self.client, 'platform', sys.platform)
        self.machine = machine or getattr(self.client, 'machine', None)
        self.allowed_hosts = frozenset(getattr(self.client, 'allowed_hosts', allowed_hosts))
        for name, handler in (('checking', self._checking), ('update_available', self._available),
                ('up_to_date', self._latest), ('failed', self._failed),
                ('progress', self._progress), ('verified_file', self._download_verified),
                ('cancelled', self._cancelled)):
            getattr(self.client, name).connect(handler)
        if settings is not None and hasattr(settings, 'changed'):
            settings.changed.connect(self._settings_changed)

    @property
    def state(self):
        return self._state

    @property
    def state_payload(self):
        return dict(self._payload)

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    @property
    def busy(self):
        return bool(self._installing or self.client.busy)

    @property
    def can_start_update(self):
        if self._closed or self.busy or self._manifest is None:
            return False
        return (self._state in ('available', 'ready', 'cancelled')
                or self._state == 'error' and self._retry_operation in ('download', 'install'))

    def _set_state(self, state, *, version=None, received=0, total=0, error=''):
        if self._closed:
            return
        if version is None:
            version = self._manifest.get('version', '') if self._manifest else ''
        self._state = state
        self._payload = dict(version=version, received=max(0, int(received)),
                             total=max(0, int(total)), error=str(error))
        self.state_changed.emit(state, dict(self._payload))

    def _settings_changed(self):
        self.apply_theme(getattr(self.settings, 'theme', self._theme))

    def apply_theme(self, theme):
        self._theme = 'dark' if theme == 'dark' else 'light'

    def _busy(self):
        try:
            application = QApplication.instance()
            return (bool(self._is_busy()) or application.activeModalWidget() is not None
                    or application.activePopupWidget() is not None
                    or bool(application.mouseButtons()))
        except (RuntimeError, TypeError):
            return True

    def activate(self):
        """The settings button checks, updates, or retries its current action."""
        return self.start_update() if self.can_start_update else self.open(manual=True)

    def open(self, manual=True):
        """Check without creating, opening, or activating any window."""
        if not manual:
            return self.check_automatically()
        if self._closed:
            return False
        if self.busy:
            return True
        self._pending_manifest, self._pending_check = None, False
        self._timer.stop()
        self._operation = 'check'
        return self.client.check(manual=True)

    def check_automatically(self):
        if self._closed or self._installing:
            return False
        if self._busy() or self.client.busy:
            self._pending_check = True
            self._timer.start(self._defer_ms)
            return True
        self._pending_check = False
        self._operation = 'check'
        return self.client.check(manual=False)

    def _checking(self, manual=False):
        if self._closed:
            return
        self._authorized = False
        self._manifest, self._verified_path = None, ''
        self._operation = self._retry_operation = 'check'
        self._set_state('checking')

    def _available(self, manifest, manual=False):
        if self._closed:
            return
        self._manifest, self._verified_path = copy.deepcopy(manifest), ''
        self._authorized = False
        self._set_state('available')
        if not manual:
            self._pending_manifest = copy.deepcopy(manifest)
            self._resume_automatic()

    def _resume_automatic(self):
        if self._closed:
            return
        if self._busy() or self.busy:
            self._timer.start(self._defer_ms)
            return
        if self._pending_manifest is not None:
            manifest, self._pending_manifest = self._pending_manifest, None
            version = manifest.get('version', '')
            if version not in self._notified:
                self._notified.add(version)
                # Retained signal name for tray integration; no popup is created.
                self.notification_shown.emit(version)
        elif self._pending_check:
            self._pending_check = False
            self._operation = 'check'
            self.client.check(manual=False)

    def _latest(self, manual=False):
        if self._closed:
            return
        self._authorized = False
        self._manifest, self._verified_path = None, ''
        self._set_state('latest')

    def _failed(self, message, manual=False):
        if self._closed:
            return
        self._authorized = False
        self._retry_operation = self._operation
        self._set_state('error', error=message)

    def open_error(self, message):
        """Compatibility entry: errors are reported in the existing settings UI."""
        self._failed(message, True)

    def start_update(self):
        """One explicit click authorizes this download, verification and handoff."""
        if not self.can_start_update:
            return False
        self._authorized = True
        self._pending_manifest, self._pending_check = None, False
        self._timer.stop()
        if self._verified_path:
            return self._start_verification()
        self._operation = 'download'
        self._set_state('downloading')
        if not self._authorized or self._closed:
            return False
        try:
            if self.client.download(copy.deepcopy(self._manifest), manual=True) is False:
                self._failed('正在处理更新，请稍后再试。', True)
                return False
        except (OSError, ValueError, RuntimeError) as problem:
            self._failed(str(problem), True)
            return False
        return True

    def _progress(self, received, total):
        if not self._closed and self._authorized and self._operation == 'download':
            self._set_state('downloading', received=received, total=total)

    def _download_verified(self, path, manifest, manual=False):
        if self._closed or not self._authorized or self._operation != 'download':
            return
        self._manifest, self._verified_path = copy.deepcopy(manifest), str(path)
        self._set_state('ready', received=manifest['installer']['size'],
                        total=manifest['installer']['size'])
        if self._authorized and not self._closed:
            self._start_verification()

    def _start_verification(self):
        if self._closed or self._installing or not self._authorized:
            return False
        self._operation = 'install'
        self._installing = True
        self._generation += 1
        generation = self._generation
        if self.platform == 'darwin':
            return self._start_mac_verification(generation)
        job = _Verification(generation, self._verified_path, copy.deepcopy(self._manifest),
                            dict(current_version=self.app_version, updates_dir=self.updates_dir,
                                 allowed_hosts=self.allowed_hosts))
        self._jobs[generation] = job
        job.signals.finished.connect(self._verified, Qt.ConnectionType.QueuedConnection)
        self._set_state('verifying')
        QThreadPool.globalInstance().start(job)
        return True

    def _start_mac_verification(self, generation):
        try:
            manifest = validate_manifest(self._manifest, self.allowed_hosts,
                                          platform='darwin', machine=self.machine)
            path = Path(self._verified_path).absolute()
            root = self.updates_dir.resolve(strict=True)
            expected = self.updates_dir / manifest['version'] / 'update.zip'
            if (path != expected or path.resolve(strict=True) != root / manifest['version'] / 'update.zip'
                    or path.is_symlink() or path.parent.is_symlink()
                    or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
                    or getattr(path.parent.lstat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('Mac 更新包必须位于本程序 updates/版本号/update.zip。')
            bundle = app_bundle(self._installed_exe)
            if bundle is None:
                raise ValueError('Mac 在线更新需要已安装的 FlickDock.app。')
            job = PrepareMacUpdateJob(path, manifest, self, current_version=self.app_version,
                                      installed_app=bundle)
            self._jobs[generation] = job
            job.finished.connect(lambda prepared: self._mac_verified(generation, prepared, ''))
            job.failed.connect(lambda error: self._mac_verified(generation, None, error))
            job.cancelled.connect(lambda: self._mac_cancelled(generation))
            self._set_state('verifying')
            if self._closed or generation != self._generation or not self._authorized:
                job.cancel()
            job.start()
            return True
        except (OSError, ValueError, TypeError) as error:
            self._mac_verified(generation, None, str(error))
            return False

    def _mac_cancelled(self, generation):
        job = self._jobs.pop(generation, None)
        if job is not None:
            job.deleteLater()
        if not self._closed and generation == self._generation:
            self._installing = self._authorized = False
            self._set_state('cancelled')

    def _mac_verified(self, generation, prepared, error):
        job = self._jobs.pop(generation, None)
        if job is not None:
            job.deleteLater()
        if self._closed or generation != self._generation or not self._authorized:
            if prepared is not None:
                QThreadPool.globalInstance().start(_DisposeMacPrepared(prepared))
            return
        self._installing = False
        if error:
            self._authorized = False
            self._verified_path = ''
            self._retry_operation = 'download'
            self._set_state('error', error=error)
            self.installation_failed.emit(error)
            return
        handed_off = False
        try:
            self._set_state('installing')
            if self._closed or generation != self._generation or not self._authorized:
                return
            def quit_after_spawn():
                self._authorized = False
                self.installation_started.emit(str(prepared.installed_app))
                self._quit()
            launch_update_helper(prepared, executable=self._installed_exe,
                                 popen=self._popen, quit_callback=quit_after_spawn)
            handed_off = True
        except (OSError, ValueError, TypeError) as problem:
            self._authorized = False
            self._retry_operation = 'install'
            self._set_state('error', error=str(problem))
            self.installation_failed.emit(str(problem))
        finally:
            if not handed_off and prepared is not None:
                QThreadPool.globalInstance().start(_DisposeMacPrepared(prepared))

    def _verified(self, generation, result, error):
        self._jobs.pop(generation, None)
        if self._closed or generation != self._generation or not self._authorized:
            if result is not None:
                result.close()
            return
        self._installing = False
        if error:
            self._authorized = False
            self._verified_path = ''
            self._retry_operation = 'download'
            self._set_state('error', error=error)
            self.installation_failed.emit(error)
            return
        try:
            path = str(result.path)
            self._set_state('installing')
            if self._closed or generation != self._generation or not self._authorized:
                return
            def quit_after_spawn():
                self._authorized = False
                self.installation_started.emit(path)
                self._quit()
            launch_installer(result, installed_exe=self._installed_exe,
                              quit_callback=quit_after_spawn, popen=self._popen)
        except (OSError, ValueError, TypeError) as problem:
            self._authorized = False
            self._retry_operation = 'install'
            self._set_state('error', error=str(problem))
            self.installation_failed.emit(str(problem))
        finally:
            if result is not None:
                result.close()

    def _cancelled(self, manual=False):
        if not self._closed:
            self._set_state('cancelled')

    def cancel(self):
        if self._closed:
            return False
        active = self.busy or self._authorized
        self._authorized = False
        self._generation += 1
        self._installing = False
        self._timer.stop()
        self._pending_manifest, self._pending_check = None, False
        for job in self._jobs.values():
            if isinstance(job, _Verification):
                job.cancelled.set()
            else:
                job.cancel()
        if self.client.busy:
            self.client.cancel()
        if active:
            self._set_state('cancelled')
        return bool(active)

    def close(self):
        if self._closed:
            return
        self.cancel()
        self._closed = True
        self.client.close()
