"""Per-user login startup; Windows uses one Run value, macOS one LaunchAgent.

Importing this module never reads or changes system configuration. Tests must
inject CallbackBackend and runtime_allowed=True, never register real startup.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from uuid import uuid4

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
APPROVED_KEY = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
RUN_VALUE = 'FlickDock'
LAUNCH_LABEL = 'com.flickdock.app'


@dataclass(frozen=True)
class StartupStatus:
    enabled: bool
    registered: bool
    blocked: bool
    command: str
    reason: str = ''
    supported: bool = True
    changed: bool = False


def startup_runtime_allowed(*, platform=None, frozen=None, qt_platform=None):
    platform = sys.platform if platform is None else platform
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
    if platform not in ('win32', 'darwin') or not frozen:
        return False
    if qt_platform is None:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        qt_platform = app.platformName() if app is not None else ''
    return str(qt_platform).lower() == ('windows' if platform == 'win32' else 'cocoa')


def startup_command(executable=None, *, frozen=None, script=None, platform=None):
    executable = str(Path(executable or sys.executable).resolve())
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    if frozen:
        return '"' + executable + '"'
    script = str(Path(script or Path(__file__).with_name('flickdock.py')).resolve())
    return subprocess.list2cmdline([executable, script])


class WindowsRunBackend:
    def __init__(self, registry=None):
        if registry is None:
            import winreg
            registry = winreg
        self.registry = registry

    def _read(self, key_path):
        registry = self.registry
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_QUERY_VALUE) as key:
                return registry.QueryValueEx(key, RUN_VALUE)
        except FileNotFoundError:
            return None

    def _write(self, key_path, value):
        registry = self.registry
        if value is None:
            try:
                with registry.OpenKey(registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_SET_VALUE) as key:
                    registry.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
            return
        with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_SET_VALUE) as key:
            registry.SetValueEx(key, RUN_VALUE, 0, value[1], value[0])

    def read(self): return self._read(RUN_KEY)
    def write(self, value): self._write(RUN_KEY, value)
    def read_approval(self): return self._read(APPROVED_KEY)
    def write_approval(self, value): self._write(APPROVED_KEY, value)


class CallbackBackend:
    """Compatibility hooks; omitted approval callbacks never touch the OS."""
    def __init__(self, read, write, read_approval=None, write_approval=None):
        self.read, self.write = read, write
        self.read_approval = read_approval or (lambda: None)
        self.write_approval = write_approval or (lambda value: None)


class LaunchAgentBackend:
    def __init__(self, home=None, *, runner=None, system_actions=None, uid=None):
        self.path = Path(home or Path.home()) / 'Library' / 'LaunchAgents' / (LAUNCH_LABEL + '.plist')
        self.runner = runner or subprocess.run
        self.system_actions = (home is None and sys.platform == 'darwin' and startup_runtime_allowed()) if system_actions is None else system_actions
        self.uid = getattr(os, 'getuid', lambda: 0)() if uid is None else uid

    def read(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > 64 * 1024:
            raise ValueError('登录启动文件过大，未覆盖。')
        try:
            data = plistlib.loads(raw)
            arguments = data.get('ProgramArguments')
            if (data.get('Label') != LAUNCH_LABEL or not isinstance(arguments, list)
                    or len(arguments) != 1 or not isinstance(arguments[0], str)):
                raise ValueError()
            return ('"' + arguments[0] + '"', 1)
        except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            raise ValueError('同名登录启动文件不是 FlickDock 自有配置，未覆盖。') from None

    def read_approval(self):
        exists = self.path.exists()
        data = plistlib.loads(self.path.read_bytes()) if exists else {}
        disabled = bool(exists and (data.get('Disabled') or not data.get('RunAtLoad', False)))
        if self.system_actions:
            result = self.runner(['/bin/launchctl', 'print-disabled', 'gui/' + str(self.uid)],
                                 capture_output=True, text=True, check=True, timeout=2)
            match = re.search(r'(?m)^\s*"' + re.escape(LAUNCH_LABEL) + r'"\s*=>\s*(true|false)', result.stdout)
            disabled = disabled or bool(match and match.group(1) == 'true')
        return (b'\x03' + b'\x00' * 11, 3) if disabled else None

    def write_approval(self, value):
        # write() always creates an enabled RunAtLoad job. No launchctl bootout:
        # unloading the running job would terminate the user's current app.
        if self.path.exists():
            if self.system_actions:
                action = 'disable' if value is not None and bool(bytes(value[0])[0] & 1) else 'enable'
                self.runner(['/bin/launchctl', action, 'gui/' + str(self.uid) + '/' + LAUNCH_LABEL],
                            capture_output=True, text=True, check=True, timeout=2)
            data = plistlib.loads(self.path.read_bytes())
            data['Disabled'] = value is not None and bool(bytes(value[0])[0] & 1)
            if value is None:
                data['RunAtLoad'] = True
            self._atomic(data)

    def _atomic(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.flickdock-' + uuid4().hex + '.plist')
        try:
            with temporary.open('xb') as stream:
                plistlib.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, value):
        if value is None:
            self.path.unlink(missing_ok=True)
            return
        executable = _command_path(value[0])
        if executable is None or executable.name != 'FlickDock':
            raise ValueError('macOS 登录启动需要已打包的 FlickDock.app。')
        self._atomic(dict(Label=LAUNCH_LABEL, ProgramArguments=[str(executable)],
                         RunAtLoad=True, KeepAlive=False, ProcessType='Interactive',
                         LimitLoadToSessionType='Aqua', WorkingDirectory=str(executable.parent),
                         AssociatedBundleIdentifiers=[LAUNCH_LABEL]))


def read_startup_value():
    if sys.platform == 'win32':
        return WindowsRunBackend().read()
    if sys.platform == 'darwin':
        return LaunchAgentBackend().read()
    return None


def write_startup_value(value):
    if sys.platform == 'win32':
        WindowsRunBackend().write(value)
    elif sys.platform == 'darwin':
        LaunchAgentBackend().write(value)


def _command_path(command):
    if not isinstance(command, str):
        return None
    command = command.strip()
    if command.startswith('"') and command.endswith('"') and command.count('"') == 2:
        command = command[1:-1]
    elif '"' in command or any(character.isspace() for character in command):
        return None
    if not command or any(character in command for character in '\r\n<>|*?'):
        return None
    path = Path(command)
    return path if path.is_absolute() else None


def _blocked(approval):
    if approval is None:
        return False
    value = approval[0]
    # StartupApproved enabled states use even first bytes (2/6), disabled 3/7.
    # Unknown formats are not silently treated as an enabled startup entry.
    return not isinstance(value, (bytes, bytearray)) or not value or bool(value[0] & 1)


class StartupManager:
    def __init__(self, executable=None, *, platform=None, frozen=None, qt_platform=None,
                 backend=None, runtime_allowed=None):
        self.platform = sys.platform if platform is None else platform
        self.executable = Path(executable or sys.executable).resolve()
        self.frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
        self._runtime_allowed = runtime_allowed
        self.qt_platform = qt_platform
        self.backend = backend

    def _allowed(self):
        if self._runtime_allowed is not None:
            return bool(self._runtime_allowed() if callable(self._runtime_allowed) else self._runtime_allowed)
        return startup_runtime_allowed(platform=self.platform, frozen=self.frozen, qt_platform=self.qt_platform)

    def _backend(self):
        if self.backend is None:
            self.backend = WindowsRunBackend() if self.platform == 'win32' else LaunchAgentBackend()
        return self.backend

    def _owned(self, value):
        path = _command_path(value[0]) if value and value[1] == 1 else None
        expected_name = 'flickdock.exe' if self.platform == 'win32' else 'flickdock'
        return path is not None and path.name.casefold() == expected_name

    def status(self):
        if not self._allowed() or self.platform not in ('win32', 'darwin'):
            return StartupStatus(False, False, False, '', '仅正式安装版本可设置登录启动。', supported=False)
        try:
            backend = self._backend()
            value, approval = backend.read(), backend.read_approval()
            command = value[0] if value is not None and isinstance(value[0], str) else ''
            blocked = _blocked(approval)
            desired = startup_command(self.executable, frozen=True)
            enabled = bool(value == (desired, 1) and self.executable.is_file() and not blocked)
            reason = ''
            if blocked:
                reason = 'Windows 已禁用此启动项，请重新勾选并保存以启用。' if self.platform == 'win32' else '此登录启动项已被禁用，请重新勾选并保存。'
            elif value is None:
                reason = '尚未注册登录启动。'
            elif not self._owned(value):
                reason = '同名启动项不属于 FlickDock，未覆盖。'
            elif not enabled:
                reason = '登录启动仍指向旧安装位置。' if self.executable.is_file() else '当前程序文件不存在。'
            return StartupStatus(enabled, value is not None, blocked, command, reason)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            return StartupStatus(False, False, False, '', '无法读取登录启动状态：' + str(error))

    def reconcile(self, enabled, *, explicit=False):
        if type(enabled) is not bool:
            raise ValueError('登录启动设置必须为布尔值。')
        previous_status = self.status()
        if not previous_status.supported:
            return previous_status
        backend = self._backend()
        before, approval = None, None
        mutated = False
        try:
            before, approval = backend.read(), backend.read_approval()
            if before is not None and not self._owned(before):
                return previous_status
            if not enabled and before is None:
                return previous_status
            if enabled and self.executable.name.casefold() != ('flickdock.exe' if self.platform == 'win32' else 'flickdock'):
                return replace(previous_status, reason='当前程序名称不是 FlickDock，未注册。')
            if enabled and not self.executable.is_file():
                return replace(previous_status, reason='当前程序文件不存在，未注册。')
            desired = (startup_command(self.executable, frozen=True), 1) if enabled else None
            changed = before != desired
            if changed:
                mutated = True
                backend.write(desired)
            if enabled and explicit and _blocked(approval):
                mutated = True
                backend.write_approval(None)
                changed = True
            result = self.status()
            return replace(result, changed=changed)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            if mutated:
                try:
                    backend.write(before)
                    backend.write_approval(approval)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    pass
            return replace(self.status(), reason='无法保存登录启动：' + str(error), changed=False)
