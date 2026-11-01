"""Hosted Windows QA: exact production installer, isolated normal EXE and RM.

This is NOT the signed-manifest/download/update-offer end-to-end test. It builds
the unchanged production ISS, initially installs without launching or optional
tasks, then exercises FDOnlineUpdate/FDParentPid, Restart Manager and relaunch.
Never run this on a development desktop. It refuses non-hosted environments.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback


def require_hosted():
    if (sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_OS') != 'Windows'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('FD_QA_NATIVE') != '1'):
        raise RuntimeError('Only an ephemeral GitHub-hosted Windows native QA job is permitted')
    runner = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    if not runner.is_dir():
        raise RuntimeError('RUNNER_TEMP must be an existing directory')
    return runner


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path != root and path.is_relative_to(root)


def read_log(path):
    raw = Path(path).read_bytes()
    return raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8', errors='replace')


def find_compiler():
    candidates = [shutil.which('ISCC.exe')]
    for base in (Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')),
                 Path(os.environ['LOCALAPPDATA']) / 'Programs'):
        if base.is_dir():
            candidates.extend(base.glob('Inno Setup*/ISCC.exe'))
    compiler = next((Path(value).resolve() for value in candidates if value and Path(value).is_file()), None)
    if compiler is None:
        raise RuntimeError('ISCC.exe is required; the hosted workflow must install Inno Setup before running this QA')
    return compiler


class Native:
    """Handles are acquired only after checking the process image path."""
    def __init__(self, owned):
        self.owned = Path(owned).resolve()
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def open(self, pid, *, terminate=False):
        handle = self.kernel.OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, int(pid))
        if not handle:
            raise OSError(ctypes.get_last_error(), 'Cannot open owned process')
        text = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(text))
        if not self.kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(length)):
            self.kernel.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), 'Cannot verify owned process image')
        image = Path(text.value).resolve()
        if not inside(image, self.owned):
            self.kernel.CloseHandle(handle)
            raise RuntimeError('Refusing to operate on a process outside the unique QA prefix')
        return handle, image

    def running(self, handle):
        result = self.kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise OSError(ctypes.get_last_error(), 'Owned process wait failed')
        return result == 258

    def stop(self, pid):
        handle, image = self.open(pid, terminate=True)
        try:
            if self.running(handle):
                if not self.kernel.TerminateProcess(handle, 97):
                    raise OSError(ctypes.get_last_error(), 'Cannot terminate owned fixture process')
                if self.kernel.WaitForSingleObject(handle, 10000) != 0:
                    raise TimeoutError('Owned fixture did not terminate')
            return dict(pid=pid, image=str(image))
        finally:
            self.kernel.CloseHandle(handle)

    def locked_image(self, exe):
        if not inside(exe, self.owned):
            raise RuntimeError('Lock probe outside QA prefix')
        # Probe access only. No data is written or truncated.
        handle = self.kernel.CreateFileW(str(exe), 0x40000000, 0, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error != 32:
                raise OSError(error, 'Expected actual image sharing lock, not a permissions error')
            return True
        self.kernel.CloseHandle(handle)
        return False

    def windows(self, pid):
        result = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            owner = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and self.user.IsWindowVisible(hwnd):
                rect = wintypes.RECT()
                self.user.GetWindowRect(hwnd, ctypes.byref(rect))
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(hwnd, name, len(name))
                if name.value.startswith('Qt') and rect.right > rect.left and rect.bottom > rect.top:
                    result.append(dict(hwnd=int(hwnd), window_class=name.value,
                                       bounds=[rect.left, rect.top, rect.right, rect.bottom]))
            return True
        callback = callback_type(visit)
        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        if not self.user.EnumWindows(callback, 0):
            raise OSError(ctypes.get_last_error(), 'EnumWindows failed')
        return result


def processes(prefix):
    """CIM outputs only processes inside this unique fixture, never all PIDs."""
    environment = dict(os.environ, FD_INSTALLER_QA_PREFIX=str(Path(prefix).resolve()) + os.sep)
    command = ("$ErrorActionPreference='Stop'; $prefix=$env:FD_INSTALLER_QA_PREFIX; "
               "@((Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
               "$_.ExecutablePath.StartsWith($prefix,[System.StringComparison]::OrdinalIgnoreCase) } | "
               "Select-Object ProcessId,ParentProcessId,ExecutablePath,CreationDate)) | ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, timeout=10, env=environment, check=True)
    body = result.stdout.strip()
    rows = json.loads(body) if body else []
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        if not inside(row['ExecutablePath'], prefix):
            raise RuntimeError('CIM returned an out-of-prefix image')
    return rows


def wait_for(action, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(.15)
    raise TimeoutError(label)


def exe_install_entry(log, exe):
    """Require the actual EXE file entry to say it was installed, not skipped."""
    expected = ntpath.normcase(ntpath.normpath(str(exe)))
    for block in re.split(r'-- File entry --', log):
        for line in block.splitlines():
            matched = re.search(r'\bDest filename:\s*(.*?)\s*$', line, re.I)
            if matched and ntpath.normcase(ntpath.normpath(matched.group(1))) == expected:
                return block if 'Successfully installed the file.' in block else None
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--normal-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    runner = require_hosted()
    root, exe, output = args.root.resolve(strict=True), args.exe.resolve(strict=True), args.output.resolve()
    expected = args.sha256.lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or sha256(exe) != expected:
        raise AssertionError('The immutable dist EXE SHA does not match')
    if exe != root / 'dist' / 'FlickDock' / 'FlickDock.exe':
        raise AssertionError('The production ISS must package this exact dist/FlickDock/FlickDock.exe')
    normal = json.loads(args.normal_report.read_text(encoding='utf8'))
    if (normal.get('ok') is not True or normal.get('normal_entry') is not True
            or normal.get('exe_sha256') != expected or normal.get('verify_switches') is not False
            or normal.get('frozen_executable_patched') is not False):
        raise AssertionError('A successful same-SHA normal frozen GUI receipt is required first')
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='flickdock-exact-installer-', dir=runner)).resolve()
    if not inside(folder, runner):
        raise RuntimeError('Fixture must remain under RUNNER_TEMP')
    target = folder / 'installed'
    target.mkdir()
    build = folder / 'setup'
    build.mkdir()
    native = Native(folder)
    report = dict(ok=False, scope='Exact production installer / parent PID / Restart Manager / relaunch only',
                  signed_manifest_download_offer_verified=False, publication_authorized=False,
                  exe_sha256=expected, source_iss_sha256=sha256(root / 'FlickDock.iss'),
                  normal_gui_report_sha256=sha256(args.normal_report), fixture=str(folder), checks=[], stages=[])
    old_handle = new_handle = None
    streams = []
    def record(stage, **details):
        row = dict(stage=stage, monotonic=time.monotonic(), **details)
        report['stages'].append(row)
        (output / 'exact-installer.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        print(json.dumps(row), flush=True)
    def run_installer(command, name, env):
        with (output / (name + '.stdout')).open('wb') as stream:
            child = subprocess.Popen(command, cwd=folder, env=env, stdout=stream, stderr=subprocess.STDOUT)
            record(name + '-started', pid=child.pid, flags=command[1:])
            try:
                code = child.wait(timeout=120)
            except subprocess.TimeoutExpired:
                raise TimeoutError(name + ' installer exceeded 120 seconds; see untouched raw log')
        if code != 0:
            raise RuntimeError(name + ' installer exit ' + str(code))
        record(name + '-exited', exit_code=code)
    try:
        import winreg
        # Production AppId is deliberately unchanged. Refuse a contaminated
        # runner rather than changing another installation's shared registry.
        uninstall_key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{C51D7C62-61AE-45D7-A96D-6DC63B8495E7}_is1'
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    previous = winreg.OpenKey(hive, uninstall_key, 0, winreg.KEY_READ | view)
                except FileNotFoundError:
                    continue
                else:
                    previous.Close()
                    raise RuntimeError('A pre-existing production AppId is registered; require a fresh hosted runner')
        definition = (root / 'FlickDock.iss').read_text(encoding='utf8')
        if (not re.search(r'^CloseApplications=force\s*$', definition, re.M)
                or not re.search(r'^CloseApplicationsFilter=FlickDock.exe,node.exe\s*$', definition, re.M)):
            raise AssertionError('Production Restart Manager policy changed; this QA must be reviewed')
        compiler = find_compiler()
        # No copied/edited ISS, fake EXE or build-time define changes.
        with (output / 'iscc.log').open('wb') as stream:
            subprocess.run([str(compiler), '/O' + str(build), str(root / 'FlickDock.iss')],
                           cwd=root, stdout=stream, stderr=subprocess.STDOUT, timeout=180, check=True)
        installers = list(build.glob('*.exe'))
        if len(installers) != 1:
            raise AssertionError('Expected exactly one newly compiled production installer')
        setup = installers[0]
        if sha256(exe) != expected or sha256(root / 'FlickDock.iss') != report['source_iss_sha256']:
            raise AssertionError('Production input changed while compiling')
        report['installer_sha256'] = sha256(setup)
        shutil.copy2(setup, output / setup.name)
        base = folder / 'profile'
        profile = base / 'FlickDock'
        profile.mkdir(parents=True)
        settings = dict(version=1, theme='dark', autostart=False, startup_initialized=True,
                        automatic_updates=False, clipboard_notice_seen=True, clipboard_sync_enabled=False,
                        clipboard_success_notice=False, sync_upload_enabled=False, sync_receive_enabled=False,
                        trigger_visible=True, keep_empty_handle=True)
        (profile / 'settings.json').write_text(json.dumps(settings), encoding='utf8')
        runtime_temp = folder / 'runtime-temp'
        runtime_temp.mkdir()
        environment = dict(os.environ, APPDATA=str(base), LOCALAPPDATA=str(base / 'local'),
                           TEMP=str(runtime_temp), TMP=str(runtime_temp),
                           QT_QPA_PLATFORM='windows', QT_ACCESSIBILITY='1')
        # No desktop shortcut / firewall task, no postinstall auto-launch.
        common = ['/VERYSILENT', '/NORESTART', '/SP-', '/NOICONS', '/TASKS=', '/DIR=' + str(target)]
        run_installer([str(setup), *common, '/LOG=' + str(output / 'initial-install.log')], 'initial-install', environment)
        installed = target / 'FlickDock.exe'
        if sha256(installed) != expected:
            raise AssertionError('Initial installed EXE differs from immutable dist')
        time.sleep(1)
        initial_processes = processes(target)
        if initial_processes:
            raise AssertionError('Initial silent install unexpectedly launched an application')
        record('initial-install-no-launch', matching_processes=initial_processes)
        report['checks'].append('Initial silent exact-production install: no launch, same dist SHA')
        for name in ('installed.stdout', 'installed.stderr'):
            stream = (output / name).open('wb')
            streams.append(stream)
        application = subprocess.Popen([str(installed)], cwd=folder, env=environment,
                                       stdout=streams[0], stderr=streams[1])
        old_handle, old_image = native.open(application.pid)
        if old_image != installed:
            raise AssertionError('Initial process is not the exact installed executable')
        old_windows = wait_for(lambda: native.windows(application.pid) if native.running(old_handle) else None,
                               30, 'Installed normal entry did not expose a native Qt window')
        time.sleep(3)
        if not native.running(old_handle) or not native.locked_image(installed):
            raise AssertionError('Installed normal process did not survive startup with an actual image lock')
        record('installed-normal-ready', pid=application.pid, image=str(old_image), windows=old_windows,
               image_locked=True, args=[], matching_processes=processes(target))
        before_identity = dict(inode=installed.stat().st_ino, size=installed.stat().st_size)
        # Keep the real parent running: InitializeSetup's five-second wait must
        # hand its actual installed file owner to production Restart Manager.
        run_installer([str(setup), *common, '/FDOnlineUpdate=1', '/FDParentPid=' + str(application.pid),
                       '/LOG=' + str(output / 'online-install.log')], 'online-install', environment)
        if native.running(old_handle):
            raise AssertionError('The exact old normal application PID is still alive')
        log = read_log(output / 'online-install.log')
        if 'handing exact installed file owners to Restart Manager.' not in log:
            raise AssertionError('Expected the live parent to reach the actual Restart Manager handoff')
        entry = exe_install_entry(log, installed)
        if entry is None:
            raise AssertionError('No successful overwrite entry for the exact installed EXE in raw Inno log')
        (output / 'exact-exe-install-entry.txt').write_text(entry, encoding='utf8')
        if sha256(installed) != expected:
            raise AssertionError('Post-update installed EXE differs from dist SHA')
        def relaunched():
            for row in processes(target):
                if Path(row['ExecutablePath']).resolve() == installed and row['ProcessId'] != application.pid:
                    return row
            return None
        new = wait_for(relaunched, 35, 'Online production installer did not relaunch its installed EXE')
        new_handle, new_image = native.open(new['ProcessId'])
        if new_image != installed:
            raise AssertionError('Relaunched process is not the exact installed executable')
        new_windows = wait_for(lambda: native.windows(new['ProcessId']) if native.running(new_handle) else None,
                               30, 'Relaunched normal entry did not expose a native Qt window')
        time.sleep(3)
        if not native.running(new_handle) or not native.locked_image(installed):
            raise AssertionError('Relaunched process did not survive startup with its actual image lock')
        persisted = json.loads((profile / 'settings.json').read_text(encoding='utf8'))
        for name in ('autostart', 'automatic_updates', 'clipboard_sync_enabled'):
            if persisted.get(name) is not False:
                raise AssertionError('Disposable profile safety setting changed: ' + name)
        if (profile / 'account-session.json').exists():
            raise AssertionError('Expected the unique installed application profile to remain unsigned-in')
        if sha256(exe) != expected or sha256(root / 'FlickDock.iss') != report['source_iss_sha256']:
            raise AssertionError('Original frozen EXE or production ISS changed during QA')
        report['checks'].extend(['Old actual normal PID exited; actual production RM handoff logged',
                                 'Exact installed EXE successfully overwritten and remains same immutable SHA',
                                 'New PID at same installed path exposes native Qt window and survives startup'])
        record('relaunch-ready', old_pid=application.pid, new_pid=new['ProcessId'], image=str(new_image),
               windows=new_windows, image_locked=True, before_identity=before_identity,
               after_identity=dict(inode=installed.stat().st_ino, size=installed.stat().st_size),
               matching_processes=processes(target), isolated_profile_unsigned_in=True)
        report['ok'] = True
    except Exception as error:
        report.update(error=type(error).__name__ + ': ' + str(error), traceback=traceback.format_exc())
    finally:
        cleanup_errors = []
        try:
            # No /IM, global process kill or unverified arbitrary PID. Re-query
            # each cleanup round; every opened handle revalidates its image.
            for _ in range(3):
                rows = processes(folder)
                if not rows:
                    break
                for row in rows:
                    try:
                        native.stop(row['ProcessId'])
                    except OSError as error:
                        if getattr(error, 'errno', None) != 87:
                            raise
            if processes(folder):
                raise RuntimeError('Owned process remains after bounded cleanup')
        except Exception as error:
            cleanup_errors.append(type(error).__name__ + ': ' + str(error))
        for handle in (old_handle, new_handle):
            if handle:
                native.kernel.CloseHandle(handle)
        for stream in streams:
            stream.close()
        if cleanup_errors:
            report.update(ok=False, cleanup_errors=cleanup_errors)
        # Retain the unique TEMP fixture for hosted artifact diagnostics. Do
        # not invoke production uninstaller (its runas firewall cleanup is a
        # separate flow), delete registry keys, or touch other installations.
        report['fixture_retained'] = True
        record('finished', ok=report['ok'])
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
