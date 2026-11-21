"""Build and verify a Windows release on a disposable GitHub-hosted runner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from app_version import APP_VERSION
from release_manifest import validate_versions


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / 'outputs'
TESTS = (
    'test_release_signing_ci', 'test_trigger_reliability_v1810', 'test_crash_diagnostics_v1838', 'test_display_frames', 'test_ui_feedback_v1815',
    'test_feedback_v1818', 'test_surface_source_cache', 'test_lan_transfer_v181',
    'test_wan_transfer_v1816', 'test_sync_priority_v1823', 'test_transfer_ui_v181',
    'test_architecture',
)


def run(command, *, env, timeout):
    subprocess.run([str(value) for value in command], cwd=ROOT, env=env, check=True, timeout=timeout)


def inno_compiler():
    directory = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))
    path = directory / 'Inno Setup 6' / 'ISCC.exe'
    if not path.is_file():
        raise RuntimeError('Inno Setup 6 is unavailable on the Windows build host.')
    # ISCC 6 can report a zero PE version and does not support --version.
    # Ask its own preprocessor to enforce the required compiler version.
    with tempfile.TemporaryDirectory(prefix='flickdock-inno-version-') as directory:
        probe = Path(directory) / 'probe.iss'
        probe.write_text('#if Ver < EncodeVer(6,5,0)\n#error Inno Setup 6.5 or newer required\n'
                         '#endif\n[Setup]\nAppName=Version probe\nAppVersion=1\n'
                         'DefaultDirName={autopf}\\VersionProbe\nOutput=no\n', encoding='utf-8-sig')
        result = subprocess.run([str(path), str(probe)], capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Inno Setup 6.5 or newer is required for the Chinese language file.')
    return path


def main():
    if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Run this builder only on a disposable Windows GitHub Actions runner.')
    for name in ('FlickDock.iss', 'ChineseSimplified.isl'):
        if not (ROOT / name).read_bytes().startswith(b'\xef\xbb\xbf'):
            raise RuntimeError(name + ' must retain its UTF-8 BOM.')
    compiler = inno_compiler()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks = []
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUTF8='1')
    for name, command, timeout in (
        ('assets', [sys.executable, 'ci_assets.py'], 120),
        ('webrtc assets', [sys.executable, 'p2p_assets.py'], 600),
    ):
        run(command, env=env, timeout=timeout)
        checks.append(dict(name=name, exit_code=0))
    # Qt tests own application state, exception hooks and native threads.
    # Separate processes keep one suite's lifecycle from affecting another.
    for module in TESTS:
        run([sys.executable, '-m', 'unittest', module, '-q'], env=env, timeout=600)
        checks.append(dict(name=module, exit_code=0))
    for name, command, timeout in (
        ('frozen build', [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', 'FlickDock.spec'], 900),
        ('installer build', [compiler, ROOT / 'FlickDock.iss'], 600),
    ):
        run(command, env=env, timeout=timeout)
        checks.append(dict(name=name, exit_code=0))
    binary = ROOT / 'dist' / 'FlickDock' / 'FlickDock.exe'
    setup = OUTPUT / ('FlickDock-Setup-' + APP_VERSION + '.exe')
    if not binary.is_file() or not setup.is_file():
        raise RuntimeError('The frozen application or installer was not produced.')
    with tempfile.TemporaryDirectory(prefix='flickdock-release-verify-') as directory:
        profile = Path(directory)
        reports = {}
        for option, name in (('--verify-overview','overview'),('--verify-notices','notices'),
                             ('--verify-transfers','transfers'),('--verify-translator','translator')):
            path = profile / (name + '.json')
            print('Verifying frozen ' + name, flush=True)
            try:
                run([binary, option, '--verify-output', path], env=dict(env, APPDATA=str(profile),
                    LOCALAPPDATA=str(profile), XDG_CONFIG_HOME=str(profile)), timeout=180)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if path.is_file():
                    print(path.read_text(encoding='utf-8'), flush=True)
                raise
            report = json.loads(path.read_text(encoding='utf-8'))
            if report.get('ok') is not True or report.get('frozen') is not True or report.get('version') != APP_VERSION:
                raise RuntimeError('Frozen application verification failed: ' + name)
            reports[name] = report
            checks.append(dict(name='frozen ' + name, exit_code=0))
    validate_versions(ROOT, setup, APP_VERSION)
    with setup.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    report = dict(version=APP_VERSION, checks=checks, frozen=reports['overview'],
                  setup=dict(name=setup.name, bytes=setup.stat().st_size, sha256=digest))
    (OUTPUT / ('windows-build-' + APP_VERSION + '.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Windows installer and frozen checks passed for', APP_VERSION, flush=True)


if __name__ == '__main__':
    main()
