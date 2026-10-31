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
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from uuid import uuid4
import zipfile

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QCoreApplication, QTimer, Qt, Signal
from update_client import (DEFAULT_ALLOWED_HOSTS, MAX_MANIFEST_BYTES, _json_object,
                           compare_versions, manifest_endpoint, trusted_url,
                           update_architecture, validate_manifest, version_tuple)

BUNDLE_ID = 'com.flickdock.app'
MAX_ZIP_BYTES = 1024 * 1024 * 1024
_deferred_trees=set()
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
    source_archive: Path | None = None
    ready_token: str = ''
    helper_app: Path | None = None

    def cleanup(self):
        if _STAGE.fullmatch(self.stage.name) and self.stage.is_absolute():
            if self.stage.is_symlink():
                raise ValueError('更新暂存目录发生变化，未清理。')
            if not _defer_running_runtime_cleanup(self.stage):shutil.rmtree(self.stage, ignore_errors=True)


def _defer_running_runtime_cleanup(directory):
    """Never remove the executable/PYZ from which this helper still imports."""
    directory=Path(directory).absolute()
    if sys.platform!='darwin' or not getattr(sys,'frozen',False):return False
    if not Path(sys.executable).absolute().is_relative_to(directory):return False
    if (directory.is_symlink() or directory.resolve()!=directory
            or not (_STAGE.fullmatch(directory.name) or re.fullmatch(r'\.flickdock-updater-runtime-[a-f0-9]{32}',directory.name))):
        raise ValueError('更新运行目录身份无效，未自动清理。')
    details=directory.stat();identity=f'{details.st_dev}:{details.st_ino}'
    if (str(directory),identity) in _deferred_trees:return True
    # Paths are separate argv, and stat rechecks the original directory inode
    # after process exit. The fixed script never follows a replacement symlink.
    script='while /bin/kill -0 "$1" 2>/dev/null; do /bin/sleep 0.2; done; if [ ! -L "$2" ] && [ "$(/usr/bin/stat -f "%d:%i" "$2" 2>/dev/null)" = "$3" ]; then /bin/rm -rf -- "$2"; fi'
    try:
        subprocess.Popen(['/bin/sh','-c',script,'flickdock-update-cleanup',str(os.getpid()),str(directory),identity],
            cwd='/',start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        _deferred_trees.add((str(directory),identity))
    except OSError:pass # Preserve a live runtime if deferred cleanup cannot start.
    return True


def prepare_update(zip_path, manifest, *, current_version, installed_app,
                   staging_parent=None, expected_bundle_id=BUNDLE_ID, cancel_event=None,copy_runtime=True):
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
    zip_path = Path(zip_path)
    if zip_path.is_symlink() or zip_path.stat().st_size != size or _sha(zip_path, cancel_event) != checksum:
        raise ValueError('更新 ZIP 大小或 SHA256 校验失败。')
    zip_path = zip_path.resolve(strict=True)
    if staging_parent is not None:
        parent = Path(staging_parent).resolve(strict=True)
        if parent not in (target.parent, zip_path.parent):
            raise ValueError('更新暂存目录必须在应用目录或已验证下载目录。')
        stage = parent / ('.flickdock-update-' + uuid4().hex)
        stage.mkdir(mode=0o700)
    else:
        # /Applications is normally owned by root. Prepare without elevated
        # rights in the already user-owned, verified download directory.
        parent = target.parent
        stage = parent / ('.flickdock-update-' + uuid4().hex)
        try:
            stage.mkdir(mode=0o700)
        except PermissionError:
            parent = zip_path.parent
            stage = parent / ('.flickdock-update-' + uuid4().hex)
            stage.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bundle = _extract(archive, stage, cancel_event)
        if zip_path.stat().st_size != size or _sha(zip_path, cancel_event) != checksum:
            raise ValueError('更新 ZIP 在解包期间发生变化。')
        bundle_metadata(bundle, bundle_id=expected_bundle_id, version=version)
        _check_cancel(cancel_event)
        helper_app=None
        if copy_runtime:
            required=sum(path.stat().st_size for path in target.rglob('*') if path.is_file() and not path.is_symlink())
            if shutil.disk_usage(stage).free<required+32*1024*1024:
                raise ValueError('磁盘空间不足，无法准备独立更新程序；原应用未替换。')
            helper_app=stage/'updater-runtime'/'FlickDock.app'
            shutil.copytree(target,helper_app,symlinks=True)
            _check_cancel(cancel_event)
            bundle_metadata(helper_app,bundle_id=expected_bundle_id,version=current_version)
        return PreparedMacUpdate(stage, bundle, target, version, current_version,
                                 _tree_hash(bundle, cancel_event), os.getpid(), expected_bundle_id,
                                 zip_path, uuid4().hex,helper_app)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _validate_prepared(prepared, *, verify_tree=True):
    if not isinstance(prepared, PreparedMacUpdate):
        raise ValueError('尚未准备有效的 macOS 更新。')
    if (not prepared.stage.is_absolute() or prepared.stage.resolve(strict=True) != prepared.stage
            or not _STAGE.fullmatch(prepared.stage.name) or prepared.stage.is_symlink()
            or prepared.source_archive is None
            or prepared.source_archive.is_symlink()
            or prepared.stage.parent not in (prepared.installed_app.parent,
                                             prepared.source_archive.parent)
            or prepared.app_path != prepared.stage / 'FlickDock.app'):
        raise ValueError('macOS 更新暂存位置无效。')
    if not re.fullmatch(r'[0-9a-f]{32}', prepared.ready_token):
        raise ValueError('macOS 更新就绪令牌无效。')
    if prepared.helper_app is not None:
        if prepared.helper_app!=prepared.stage/'updater-runtime'/'FlickDock.app':
            raise ValueError('更新运行程序位置无效。')
        bundle_metadata(prepared.helper_app,bundle_id=prepared.bundle_id,version=prepared.current_version)
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


def _retry_exit_prompt():
    """The independent native updater owns this prompt, not the quitting GUI."""
    import AppKit
    app=AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    alert=AppKit.NSAlert.alloc().init()
    alert.setMessageText_('FlickDock 尚未完全退出')
    alert.setInformativeText_('后台任务仍在结束，更新尚未替换任何文件。重试会关闭当前安装的 FlickDock 后继续安装；取消则保留原版本。')
    alert.addButtonWithTitle_('重试退出并安装');alert.addButtonWithTitle_('取消更新')
    return alert.runModal()==AppKit.NSAlertFirstButtonReturn


def _request_owner_exit(prepared):
    """Terminate only the exact installed app identified by this receipt.

    Never quit by bundle name: another copy or the independent updater may
    share that name and must remain untouched.
    """
    if prepared.bundle_id!=BUNDLE_ID:raise ValueError('更新进程身份无效。')
    import AppKit
    application=AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(prepared.owner_pid)
    if application is None:
        if not _pid_alive(prepared.owner_pid):return
        raise RuntimeError('无法确认原进程身份，未结束进程或替换应用。')
    executable=application.executableURL()
    expected=prepared.installed_app/'Contents/MacOS/FlickDock'
    if executable is None or Path(str(executable.path())).resolve()!=expected:
        raise ValueError('原进程与更新目标不一致，未结束进程。')
    application.terminate()
    deadline=time.monotonic()+2
    while not application.isTerminated() and time.monotonic()<deadline:time.sleep(.1)
    if not application.isTerminated():application.forceTerminate()


def _wait_for_owner_exit(prepared,timeout,pid_alive,*,retry=None,request_exit=None):
    deadline=time.monotonic()+max(0,timeout)
    while pid_alive(prepared.owner_pid):
        if time.monotonic()>=deadline:
            if retry is None or not retry():
                raise TimeoutError('FlickDock 尚未退出，已取消本次安装；原应用未替换。')
            if request_exit is not None:request_exit(prepared)
            deadline=time.monotonic()+max(.1,timeout)
        time.sleep(.1)


def install_prepared(prepared, *, installed_app=None, replace=os.replace, relaunch=None,
                     replacement_ready=None, await_readiness=None, test_only=False):
    """After-exit helper only. Tests explicitly operate on temporary fixtures."""
    if not test_only and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)):
        raise ValueError('仅 macOS 正式版辅助进程可以安装更新。')
    if not test_only and _pid_alive(prepared.owner_pid):
        raise ValueError('FlickDock 尚未退出，未替换应用。')
    _validate_prepared(prepared)
    target = prepared.installed_app
    if installed_app is not None and Path(installed_app).resolve(strict=True) != target:
        raise ValueError('更新目标与校验时的应用位置不一致。')
    install_stage = prepared.stage
    install_app = prepared.app_path
    copied_stage = None
    if prepared.stage.parent != target.parent:
        # The source lives in the user's download cache. Make a verified copy
        # beside the installed app so both renames below stay atomic even if
        # the home directory is on another volume.
        copied_stage = target.parent / ('.flickdock-update-' + uuid4().hex)
        copied_stage.mkdir(mode=0o700)
        try:
            shutil.copytree(prepared.app_path, copied_stage / 'FlickDock.app', symlinks=True)
            install_stage = copied_stage
            install_app = copied_stage / 'FlickDock.app'
            bundle_metadata(install_app, bundle_id=prepared.bundle_id, version=prepared.version)
            if _tree_hash(install_app) != prepared.tree_sha256:
                raise ValueError('安装暂存副本与已验证应用不一致。')
        except BaseException:
            shutil.rmtree(copied_stage, ignore_errors=True)
            raise
    backup = target.parent / ('.FlickDock.rollback-' + uuid4().hex + '.app')
    try:
        replace(target, backup)
    except BaseException:
        if copied_stage is not None:
            shutil.rmtree(copied_stage, ignore_errors=True)
        raise
    replaced = False
    try:
        replace(install_app, target)
        replaced = True
        bundle_metadata(target, bundle_id=prepared.bundle_id, version=prepared.version)
        if replacement_ready is not None:
            replacement_ready()
        if relaunch is not None:
            relaunch(target)
        if await_readiness is not None:
            await_readiness()
    except BaseException:
        if replaced:
            replace(target, install_app)
        replace(backup, target)
        raise
    finally:
        if copied_stage is not None:
            shutil.rmtree(copied_stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    prepared.cleanup()
    return target


def _ack_helper_startup(prepared,receipt,*,pid=None):
    marker=prepared.stage/'helper-started.json'
    payload=dict(receipt_sha256=_sha(Path(receipt)),owner_pid=prepared.owner_pid,helper_pid=os.getpid() if pid is None else pid)
    temporary=marker.with_name('.helper-started-'+uuid4().hex+'.tmp')
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w',encoding='utf8') as stream:
            json.dump(payload,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,marker)
    finally:temporary.unlink(missing_ok=True)


class _HelperRetirementSignals(QObject):
    finished=Signal(bool)


class _RetireHelper(QRunnable):
    def __init__(self,prepared,process):
        super().__init__();self.prepared=prepared;self.process=process;self.signals=_HelperRetirementSignals()

    def run(self):
        safe=False
        try:
            poll=getattr(self.process,'poll',None)
            if not callable(poll):return
            if poll() is None:
                self.process.terminate()
                wait=getattr(self.process,'wait',None)
                if not callable(wait):return
                try:wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:wait(timeout=2)
                    except subprocess.TimeoutExpired:return
            safe=poll() is not None
            if safe:self.prepared.cleanup()
        except (OSError,RuntimeError,ValueError):pass
        finally:self.signals.finished.emit(safe)


class _HelperStartup(QObject):
    """A finite asynchronous handshake. A spawned PID is not install readiness."""
    def __init__(self,prepared,process,receipt,quit_callback,failed_callback,*,timeout=20):
        super().__init__(QCoreApplication.instance());self.prepared=prepared;self.process=process
        self.receipt_hash=_sha(receipt);self.quit_callback=quit_callback;self.failed_callback=failed_callback
        self.deadline=time.monotonic()+timeout;self.done=False;self.acknowledged=False;self.safe_to_retry=False
        self.timer=QTimer(self);self.timer.setInterval(40);self.timer.timeout.connect(self.check)

    def check(self):
        if self.done:return
        poll=getattr(self.process,'poll',None);code=poll() if callable(poll) else None
        if code is not None:return self.fail(f'安装助手未能启动（退出码 {code}），请重试安装。')
        marker=self.prepared.stage/'helper-started.json'
        try:
            if marker.exists():
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_size>2048:raise ValueError('启动确认无效。')
                value=json.loads(marker.read_text(encoding='utf8'))
                if (not isinstance(value,dict) or value.get('receipt_sha256')!=self.receipt_hash or value.get('owner_pid')!=self.prepared.owner_pid
                        or value.get('helper_pid')!=getattr(self.process,'pid',None)):raise ValueError('安装助手身份确认失败。')
                self.done=True;self.acknowledged=True;self.timer.stop();self.quit_callback();self.deleteLater();return
        except (OSError,ValueError,TypeError) as error:return self.fail('安装助手启动确认失败，请重试安装。')
        if time.monotonic()>=self.deadline:self.fail('安装助手启动超时，当前版本保持运行；请重试安装。')

    def fail(self,message,*,cancelled=False):
        if self.done:return
        self.done=True;self.timer.stop()
        if not cancelled:_record_update_failure(self.prepared,message)
        record=_failure_record_path(self.prepared);source=self.prepared.stage/'helper-startup.log'
        if record is not None and not cancelled:
            try:
                if not source.is_symlink():
                    with source.open('rb') as stream:data=stream.read(8192)
                    diagnostic=record.with_name('mac-update-helper-startup.log')
                    descriptor=os.open(diagnostic,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
                    with os.fdopen(descriptor,'wb') as stream:stream.write(data)
            except OSError:pass
        self._reaper=_RetireHelper(self.prepared,self.process)
        def retired(safe):
            self.safe_to_retry=bool(safe)
            detail=message if safe or cancelled else message+' 安装助手尚未确认退出，已保留运行文件；请重启电脑后重试安装。'
            if self.failed_callback:self.failed_callback(detail)
            self.deleteLater()
        self._reaper.signals.finished.connect(retired,Qt.QueuedConnection)
        QThreadPool.globalInstance().start(self._reaper)

    def cancel(self):
        if not self.done:self.fail('更新准备已取消。',cancelled=True)


def launch_update_helper(prepared, *, executable=None, popen=None, quit_callback, failed_callback=None):
    """Quit only after the actual helper has validated its launch receipt."""
    if popen is None and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)):
        raise ValueError('仅 macOS 正式版可以启动更新辅助进程。')
    if QCoreApplication.instance() is None:raise ValueError('安装助手需要正在运行的更新界面。')
    _validate_prepared(prepared, verify_tree=False)
    executable = Path(executable or sys.executable).resolve(strict=True)
    if executable != prepared.installed_app / 'Contents' / 'MacOS' / 'FlickDock':
        raise ValueError('更新辅助进程必须来自当前应用。')
    if prepared.helper_app is None:raise ValueError('独立更新程序尚未准备。')
    helper_executable=prepared.helper_app/'Contents'/'MacOS'/'FlickDock'
    receipt = prepared.stage / 'update-receipt.json'
    payload = {key: str(value) if isinstance(value, Path) else value for key, value in vars(prepared).items()}
    receipt.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    receipt.chmod(0o600)
    (prepared.stage/'helper-started.json').unlink(missing_ok=True)
    log=prepared.stage/'helper-startup.log';log.unlink(missing_ok=True)
    monitor=_HelperStartup(prepared,None,receipt,quit_callback,failed_callback)
    try:
        descriptor=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            process = (popen or subprocess.Popen)([str(helper_executable), '--apply-mac-update', str(receipt)],
                              cwd=str(prepared.installed_app.parent), start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,shell=False)
    except BaseException:
        monitor.done=True;monitor.deleteLater();raise
    monitor.process=process;monitor.timer.start()
    process._flickdock_startup_monitor=monitor
    monitor.check()
    return process


def _update_marker(prepared, name):
    return prepared.stage / name


def _write_privileged_marker(prepared, name):
    # Never follow a symlink in the user-writable staging directory while
    # running as root. A fresh stage must not already contain this marker.
    directory = os.open(prepared.stage,
                        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_NOFOLLOW', 0), 0o644, dir_fd=directory)
        try:
            # The directory is private to the update owner (0700), but this
            # marker is created by root and must be readable by the original
            # unprivileged supervisor, irrespective of root's umask.
            os.fchmod(descriptor, 0o644)
            os.write(descriptor, prepared.ready_token.encode('ascii'))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _safe_marker_text(stage, name):
    directory = os.open(stage, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                        getattr(os, 'O_NOFOLLOW', 0))
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                             getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > 128:
                raise ValueError('更新就绪标记无效。')
            return os.read(descriptor, 129).decode('ascii')
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _wait_for_ready(prepared, *, timeout=30, secure=False):
    marker = _update_marker(prepared, 'launch-ready')
    failed = _update_marker(prepared, 'launch-failed')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if secure:
                failure = _safe_marker_text(prepared.stage, 'launch-failed')
            else:
                failure = failed.read_text(encoding='ascii')
            if failure:
                raise RuntimeError('新版本未能启动，已恢复原应用。')
        except FileNotFoundError:
            pass
        try:
            value = (_safe_marker_text(prepared.stage, 'launch-ready') if secure else
                     marker.read_text(encoding='ascii'))
            if value == prepared.ready_token:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    raise TimeoutError('新版本启动未确认，已恢复原应用。')


def _open_app(path, prepared=None):
    arguments = ['/usr/bin/open', '-n', str(path)]
    if prepared is not None:
        arguments += ['--args', '--post-update-ready',
                      str(_update_marker(prepared, 'launch-ready')), prepared.ready_token]
    subprocess.run(arguments, shell=False, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _reopen_previous(path):
    # The original GUI has already exited by this point. An authorization
    # cancellation must not leave the user with no running shelf at all.
    try:
        _open_app(path)
    except (OSError, subprocess.SubprocessError):
        pass


def _failure_record_path(prepared):
    archive = prepared.source_archive
    if archive is None or archive.parent.parent.name != 'updates':
        return None
    base = archive.parent.parent.parent
    return base / 'mac-update-failure.json' if base.name == 'FlickDock' else None


def _record_update_failure(prepared, message):
    path = _failure_record_path(prepared)
    if path is None:
        return
    detail = str(message).strip()[:500] or 'macOS 更新未能完成。'
    root_error = prepared.stage / 'install-error.txt'
    try:
        details = root_error.lstat()
        if stat.S_ISREG(details.st_mode) and details.st_size <= 2048:
            detail = root_error.read_text(encoding='utf-8').strip()[:500] or detail
    except (OSError, UnicodeError):
        pass
    temporary = path.with_name('.mac-update-failure-' + uuid4().hex + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(dict(version=prepared.version, error=detail, recorded_at=time.time()),
                      output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def consume_update_failure(base,current_version=None):
    """Read one failed Mac handoff once, from the current user profile only."""
    path = Path(base) / 'mac-update-failure.json'
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > 4096:
            return ''
        value = json.loads(path.read_text(encoding='utf-8'))
        path.unlink(missing_ok=True)
        if (not isinstance(value, dict) or not isinstance(value.get('error'), str)
                or len(value['error']) > 500 or not isinstance(value.get('recorded_at'), (int, float))
                or not 0 <= time.time() - value['recorded_at'] <= 7 * 86400):
            return ''
        if current_version is not None and isinstance(value.get('version'),str):
            if compare_versions(current_version,value['version'])>=0:return ''
        return value['error']
    except (OSError, ValueError, TypeError, UnicodeError):
        return ''


def write_privileged_update_error(stage, message):
    """Root may report an error, but must never follow user-owned paths."""
    stage = Path(stage)
    if (not _STAGE.fullmatch(stage.name) or stage.parent.parent.name != 'updates'
            or stage.parent.parent.parent.name != 'FlickDock'):
        raise ValueError('更新错误记录路径无效。')
    directory = os.open(stage, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                        getattr(os, 'O_NOFOLLOW', 0))
    try:
        descriptor = os.open('install-error.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_NOFOLLOW', 0), 0o644, dir_fd=directory)
        try:
            os.fchmod(descriptor, 0o644)
            os.write(descriptor, str(message).encode('utf-8')[:2000])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _administrator_script(prepared, executable):
    """Only the fixed, verified helper command is given to Apple's auth UI."""
    command = ' '.join(shlex.quote(str(part)) for part in
                       (executable, '--elevated-mac-update',
                        prepared.stage / 'update-receipt.json'))
    return 'do shell script ' + json.dumps(command, ensure_ascii=False) + ' with administrator privileges'


class _TrustedManifestRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, request, fp, code, message, headers, newurl):
        url = urllib.parse.urljoin(request.full_url, newurl)
        trusted_url(url, DEFAULT_ALLOWED_HOSTS)
        return super().redirect_request(request, fp, code, message, headers, url)


def _trusted_manifest_for(version, *, machine=None, opener=None):
    """Root independently checks the current official index, not the receipt."""
    endpoint = manifest_endpoint('darwin', machine)
    if opener is None:
        # Use the same OS certificate trust, system proxy and authenticated
        # public mirror policy as the GUI. Frozen OpenSSL's Python-framework
        # cert.pem is not present on a clean Mac and must not be relied upon.
        from PySide6.QtCore import QCoreApplication,QEventLoop,QTimer
        from update_client import UpdateClient
        global _helper_qt_app
        _helper_qt_app=QCoreApplication.instance() or QCoreApplication(['FlickDock-update-verification'])
        loop=QEventLoop();answer=[];errors=[]
        client=UpdateClient('0.0.0',platform='darwin',machine=machine)
        def available(manifest,_manual):answer.append(manifest);loop.quit()
        def failed(message,_manual):errors.append(message);loop.quit()
        client.update_available.connect(available);client.failed.connect(failed)
        timer=QTimer();timer.setSingleShot(True)
        timer.timeout.connect(lambda:failed('官方清单验证超时。',False));timer.start(35000)
        try:
            client.check(manual=False)
            if not answer and not errors:loop.exec()
            if not answer:raise ValueError(errors[-1] if errors else '无法验证官方更新清单。')
            manifest=answer[0]
            if manifest['version']!=version or not manifest['installer']['url'].lower().split('?',1)[0].endswith('.zip'):
                raise ValueError('官方自动更新索引已变更，请重新检查更新。')
            return manifest
        finally:timer.stop();client.close();client.deleteLater()
    request = urllib.request.Request(endpoint, headers={'Accept': 'application/json',
                                 'Cache-Control': 'no-cache', 'User-Agent': 'FlickDock-Mac-Update'})
    with opener(request, timeout=15) as response:
        trusted_url(response.geturl(), DEFAULT_ALLOWED_HOSTS)
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError('官方更新清单超过大小限制。')
    parsed = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_json_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('更新清单包含无效数值。')))
    manifest = validate_manifest(parsed, platform='darwin', machine=machine)
    if manifest['version'] != version or not manifest['installer']['url'].lower().split('?', 1)[0].endswith('.zip'):
        raise ValueError('官方自动更新索引已变更，请重新检查更新。')
    return manifest


def _root_runtime_parent():
    # /Applications can be renamed by an administrator group member. Keep
    # privileged executable paths under root's non-user-writable ancestry.
    parent=Path('/private/var/root/Library/Caches/FlickDock/updater-runtimes')
    parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    _verify_root_ancestry(parent)
    return parent


def _verify_root_ancestry(path):
    if path.resolve(strict=True)!=path:raise ValueError('管理员更新运行路径包含链接。')
    for node in (path,*path.parents):
        details=node.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid!=0 or stat.S_IMODE(details.st_mode)&0o022:
            raise ValueError('管理员更新运行路径可被其他用户修改。')


def _current_root_runtime(prepared):
    executable=Path(sys.executable).absolute()
    if executable==prepared.installed_app/'Contents/MacOS/FlickDock':return None
    app=executable.parent.parent.parent;runtime=app.parent
    if (app.name!='FlickDock.app' or executable!=app/'Contents/MacOS/FlickDock'
            or not re.fullmatch(r'\.flickdock-updater-runtime-[a-f0-9]{32}',runtime.name)
            or runtime.parent!=Path('/private/var/root/Library/Caches/FlickDock/updater-runtimes')):
        raise ValueError('管理员更新程序必须来自已安装应用或独立安全运行目录。')
    _verify_root_ancestry(runtime)
    record_path=runtime/'provenance.json';details=record_path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid!=0 or details.st_size>4096:
        raise ValueError('管理员运行目录来源记录无效。')
    record=json.loads(record_path.read_text(encoding='utf8'))
    if record!={'target':str(prepared.installed_app),'current_version':prepared.current_version,
                'receipt':str(prepared.stage/'update-receipt.json'),'tree_sha256':_tree_hash(app)}:
        raise ValueError('管理员运行目录与本次更新不匹配。')
    bundle_metadata(app,version=prepared.current_version)
    return runtime


def _ensure_stable_root_runtime(prepared):
    runtime=_current_root_runtime(prepared)
    if runtime is not None:
        return runtime
    parent=_root_runtime_parent();runtime=parent/('.flickdock-updater-runtime-'+uuid4().hex)
    runtime.mkdir(mode=0o700)
    try:
        app=runtime/'FlickDock.app';shutil.copytree(prepared.installed_app,app,symlinks=True)
        bundle_metadata(app,version=prepared.current_version)
        record={'target':str(prepared.installed_app),'current_version':prepared.current_version,
                'receipt':str(prepared.stage/'update-receipt.json'),'tree_sha256':_tree_hash(app)}
        (runtime/'provenance.json').write_text(json.dumps(record),encoding='utf8')
        executable=app/'Contents/MacOS/FlickDock'
        os.execv(str(executable),[str(executable),'--elevated-mac-update',str(prepared.stage/'update-receipt.json')])
    except BaseException:
        shutil.rmtree(runtime,ignore_errors=True);raise


def finish_update_helper(receipt_path):
    """Run before interpreter teardown; retain runtime through the final import."""
    if sys.platform!='darwin' or not getattr(sys,'frozen',False):return
    if os.geteuid()==0:
        runtime=Path(sys.executable).absolute().parent.parent.parent.parent
        if runtime.parent!=Path('/private/var/root/Library/Caches/FlickDock/updater-runtimes'):return
        _verify_root_ancestry(runtime);_defer_running_runtime_cleanup(runtime)
    else:
        prepared=_read_receipt(receipt_path)
        if (prepared.stage.stat().st_uid==os.geteuid() and prepared.helper_app is not None
                and Path(sys.executable).absolute()==prepared.helper_app/'Contents/MacOS/FlickDock'):
            prepared.cleanup()


def _privileged_receipt(prepared, *, test_only):
    if prepared.stage.parent == prepared.installed_app.parent:
        raise ValueError('用户可写应用无需管理员授权。')
    if (not prepared.stage.is_absolute() or prepared.stage.is_symlink()
            or prepared.stage.resolve(strict=True) != prepared.stage
            or not _STAGE.fullmatch(prepared.stage.name)
            or prepared.source_archive is None or prepared.source_archive.is_symlink()
            or prepared.source_archive.resolve(strict=True) != prepared.source_archive
            or prepared.app_path != prepared.stage / 'FlickDock.app'
            or prepared.bundle_id != BUNDLE_ID
            or not re.fullmatch(r'[0-9a-f]{32}', prepared.ready_token)):
        raise ValueError('更新辅助进程收据位置或身份无效。')
    version_tuple(prepared.version)
    version_tuple(prepared.current_version)
    if compare_versions(prepared.version, prepared.current_version) <= 0:
        raise ValueError('更新不能降级。')
    target = _target(prepared.installed_app)
    if not test_only:
        if target != Path('/Applications/FlickDock.app'):
            raise ValueError('管理员授权仅可更新 /Applications/FlickDock.app。')
        if Path(sys.executable).absolute() != target / 'Contents' / 'MacOS' / 'FlickDock':
            _current_root_runtime(prepared)
        import pwd
        owner_home = Path(pwd.getpwuid(prepared.stage.stat().st_uid).pw_dir)
        expected = owner_home / 'Library' / 'Application Support' / 'FlickDock' / 'updates' / prepared.version
        if prepared.stage.parent != expected or prepared.source_archive != expected / 'update.zip':
            raise ValueError('更新下载目录不属于当前用户的 FlickDock。')
    elif prepared.stage.parent != prepared.source_archive.parent:
        raise ValueError('测试更新暂存目录与下载目录不一致。')
    bundle_metadata(target, version=prepared.current_version)
    return target


def _copy_verified_archive(source, destination, manifest):
    """Copy once into root-owned storage, then hash the immutable copy."""
    expected = manifest['installer']
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(source, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('更新下载文件不是普通文件。')
        if os.fstat(descriptor).st_size != expected['size']:
            raise ValueError('更新 ZIP 大小与官方索引不符。')
        digest, copied = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'rb', closefd=False) as stream, destination.open('xb') as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected['size']:
                    raise ValueError('更新 ZIP 超过官方索引大小。')
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != expected['size'] or digest.hexdigest() != expected['sha256']:
            raise ValueError('更新 ZIP 与官方索引校验不符。')
        destination.chmod(0o600)
    finally:
        os.close(descriptor)


def run_elevated_update_helper(receipt_path, *, timeout=30, test_only=False,
                               pid_alive=_pid_alive, replace=os.replace,
                               trusted_manifest=None):
    """Privileged copy/swap only. The user-owned supervisor relaunches the GUI."""
    if not test_only and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
                          or os.geteuid() != 0):
        raise ValueError('仅经 macOS 管理员授权的更新进程可以替换系统应用。')
    prepared = _read_receipt(receipt_path)
    target = _privileged_receipt(prepared, test_only=test_only)
    if trusted_manifest is not None and not test_only:
        raise ValueError('管理员更新不能使用外部提供的更新清单。')
    deadline = time.monotonic() + max(0, timeout)
    while pid_alive(prepared.owner_pid):
        if time.monotonic() >= deadline:
            raise TimeoutError('等待 FlickDock 退出超时；原应用未替换。')
        time.sleep(0.1)
    if not test_only:_ensure_stable_root_runtime(prepared)
    manifest = trusted_manifest if trusted_manifest is not None else _trusted_manifest_for(
                    prepared.version, machine=update_architecture())
    if (manifest.get('version') != prepared.version or
            manifest.get('installer', {}).get('sha256') is None):
        raise ValueError('官方更新清单与下载版本不一致。')
    # A second extraction from the user-writable stage would be unsafe even
    # after hashing it. The privileged process seals a copy of the ZIP in a
    # root-owned 0700 directory and only extracts that protected copy.
    protected = target.parent / ('.flickdock-update-' + uuid4().hex)
    protected.mkdir(mode=0o700)
    try:
        sealed_zip = protected / 'update.zip'
        _copy_verified_archive(prepared.source_archive, sealed_zip, manifest)
        fresh = prepare_update(sealed_zip, manifest, current_version=prepared.current_version,
                    installed_app=target, staging_parent=target.parent,copy_runtime=False)
        try:
            # The sealed extraction happens inside this privileged helper.
            # Preserve the original GUI PID for install_prepared's exit guard;
            # using fresh.owner_pid would reject the still-running helper itself.
            fresh.owner_pid = prepared.owner_pid
            signal_replacement = (lambda: _update_marker(prepared, 'replacement-ready').write_text(
                    prepared.ready_token, encoding='ascii')) if test_only else (
                    lambda: _write_privileged_marker(prepared, 'replacement-ready'))
            install_prepared(fresh, replace=replace, test_only=test_only,
                    replacement_ready=signal_replacement,
                    await_readiness=lambda: _wait_for_ready(prepared, secure=not test_only))
        finally:
            fresh.cleanup()
    finally:
        shutil.rmtree(protected, ignore_errors=True)
    return prepared.installed_app


def _read_receipt(receipt_path):
    receipt = Path(receipt_path).absolute()
    if receipt.is_symlink() or receipt.name != 'update-receipt.json' or receipt.stat().st_size > 16384:
        raise ValueError('更新辅助进程收据无效。')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    for key in ('stage', 'app_path', 'installed_app', 'source_archive'):
        data[key] = Path(data[key])
    if data.get('helper_app') is not None:data['helper_app']=Path(data['helper_app'])
    prepared = PreparedMacUpdate(**data)
    if receipt != prepared.stage / 'update-receipt.json' or type(prepared.owner_pid) is not int or prepared.owner_pid <= 0:
        raise ValueError('更新辅助进程收据身份无效。')
    return prepared


def run_update_helper(receipt_path, *, timeout=30, test_only=False, pid_alive=_pid_alive,
                      replace=os.replace, relaunch=None,retry_exit=None,request_exit=None):
    """Call before constructing QApplication when --apply-mac-update is present."""
    if not test_only and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)):
        raise ValueError('macOS 更新辅助进程当前不可用。')
    prepared = _read_receipt(receipt_path)
    _validate_prepared(prepared,verify_tree=False)
    if not test_only:_ack_helper_startup(prepared,receipt_path)
    try:
        _wait_for_owner_exit(prepared,timeout,pid_alive,
            retry=retry_exit if test_only else (retry_exit or _retry_exit_prompt),
            request_exit=request_exit if test_only else (request_exit or _request_owner_exit))
    except (TimeoutError,RuntimeError,ValueError):
        if not test_only:_record_update_failure(prepared,'FlickDock 尚未退出或用户取消，原应用未替换；可重试退出并安装。')
        raise
    if not test_only and prepared.stage.parent != prepared.installed_app.parent:
        # In /Applications, only Apple can authorize the privileged swap. The
        # GUI is always relaunched by this original, unprivileged process.
        script = _administrator_script(prepared,
                    prepared.installed_app / 'Contents' / 'MacOS' / 'FlickDock')
        try:
            process = subprocess.Popen(['/usr/bin/osascript', '-e', script], shell=False,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
        except OSError:
            _record_update_failure(prepared, '无法打开 macOS 管理员授权界面，更新未安装。')
            _reopen_previous(prepared.installed_app)
            raise
        marker = _update_marker(prepared, 'replacement-ready')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if marker.read_text(encoding='ascii') == prepared.ready_token:
                    break
            except FileNotFoundError:
                pass
            if process.poll() is not None:
                _record_update_failure(prepared, '管理员授权已取消或更新失败，原应用未替换。')
                _reopen_previous(prepared.installed_app)
                raise RuntimeError('管理员授权已取消或更新失败，原应用未替换。')
            time.sleep(0.1)
        else:
            process.terminate()
            _record_update_failure(prepared, '等待 macOS 管理员授权超时，原应用未替换。')
            _reopen_previous(prepared.installed_app)
            raise TimeoutError('等待 macOS 管理员授权超时；原应用未替换。')
        try:
            (relaunch or (lambda path: _open_app(path, prepared)))(prepared.installed_app)
        except BaseException:
            _update_marker(prepared, 'launch-failed').write_text('launch failed', encoding='ascii')
            process.wait(timeout=40)
            _record_update_failure(prepared, '新版本未能启动，已恢复原应用。')
            _reopen_previous(prepared.installed_app)
            raise
        if process.wait(timeout=40) != 0:
            _record_update_failure(prepared, '新版本未能启动，已恢复原应用。')
            _reopen_previous(prepared.installed_app)
            raise RuntimeError('新版本未能启动，已恢复原应用。')
        prepared.cleanup()
        return prepared.installed_app
    if relaunch is None and not test_only:
        relaunch = lambda path: _open_app(path, prepared)
    runtime_probe=None
    if (os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('FLICKDOCK_CI_RUNTIME_READER')=='1'
            and sys.platform=='darwin' and getattr(sys,'frozen',False) and os.geteuid()!=0):
        # Read a previously unimported module without executing it. This
        # diagnostic has no effect outside the explicit ephemeral CI gate.
        reader=__loader__._pyz_archive
        entry=next(name for name in ('wan_transfer','shelf_sync','release_history') if name not in sys.modules)
        before=hashlib.sha256(reader.extract(entry,raw=True)).hexdigest()
        assert Path(sys.executable).absolute()==prepared.helper_app/'Contents/MacOS/FlickDock'
        def runtime_probe():
            after=hashlib.sha256(reader.extract(entry,raw=True)).hexdigest()
            assert before==after
            (prepared.stage.parent/'runtime-reader.json').write_text(json.dumps(dict(
                ok=True,independent_executable=True,cold_pyz_read_after_swap=True,module=entry,sha256=after)))
    try:
        return install_prepared(prepared, replace=replace, relaunch=relaunch,
                 replacement_ready=runtime_probe,
                 await_readiness=(lambda: _wait_for_ready(prepared)) if not test_only else None,
                 test_only=test_only)
    except BaseException:
        if not test_only:
            _record_update_failure(prepared, 'macOS 更新失败，原应用已保留。')
            _reopen_previous(prepared.installed_app)
        raise


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
