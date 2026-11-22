"""Install a higher-version fixture, reproduce the skip and verify the replacement PKG."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from app_version import APP_VERSION
from build_macos import _checksum, _create_pkg


APP = Path('/Applications/FlickDock.app')
EXECUTABLE = APP / 'Contents/MacOS/FlickDock'
IDENTIFIER = 'com.flickdock.app'
RECEIPT = 'com.flickdock.installer'
PRIOR_VERSION = '1.8.46'


def command(*arguments, check=True, capture=False, env=None, timeout=90):
    return subprocess.run([str(argument) for argument in arguments], check=check, text=True,
                          capture_output=capture, env=env, timeout=timeout)


def processes():
    result = command('/usr/bin/pgrep', '-f', r'^/Applications/FlickDock\.app/Contents/MacOS/FlickDock( |$)',
                     check=False, capture=True)
    assert result.returncode in (0, 1), result.stderr
    return tuple(map(int, result.stdout.split()))


def wait_for(probe):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError('Installed FlickDock did not reach the expected process state')


def app_version(app=APP):
    return plistlib.loads((app / 'Contents/Info.plist').read_bytes())['CFBundleShortVersionString']


def install(path, *, check=True):
    return command('sudo', '/usr/sbin/installer', '-pkg', path, '-target', '/', check=check, timeout=100)


def newer_fixture(folder):
    app = folder / 'FlickDock.app'
    binary = app / 'Contents/MacOS/FlickDock'
    binary.parent.mkdir(parents=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': IDENTIFIER, 'CFBundleName': 'FlickDock',
        'CFBundleExecutable': 'FlickDock', 'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': PRIOR_VERSION, 'CFBundleVersion': PRIOR_VERSION,
        'LSUIElement': True,
    }))
    source = folder.parent / 'fixture.swift'
    source.write_text('import AppKit\nlet app = NSApplication.shared\napp.setActivationPolicy(.accessory)\napp.run()\n')
    command('/usr/bin/swiftc', source, '-o', binary)
    command('/usr/bin/codesign', '--sign', '-', '--force', app)
    return app


def verify(legacy, retained, output):
    if sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('This installation check requires a disposable macOS runner.')
    assert not APP.exists() and not processes(), 'Refusing to overwrite an existing application'
    output.mkdir(parents=True)
    scratch = output.parent
    assert app_version(retained) == APP_VERSION
    expected_hash = _checksum(retained / 'Contents/MacOS/FlickDock')
    staged = output / f'FlickDock-{APP_VERSION}-macOS-arm64-cover.pkg'
    _create_pkg(retained, staged, APP_VERSION)
    expanded = scratch / 'verified-cover'
    command('/usr/sbin/pkgutil', '--expand-full', staged, expanded)
    assert _checksum(expanded / 'Payload/FlickDock.app/Contents/MacOS/FlickDock') == expected_hash

    fixture = newer_fixture(scratch / 'old-source')
    old_hash = _checksum(fixture / 'Contents/MacOS/FlickDock')
    baseline = scratch / 'simulated-1.8.46.pkg'
    command('/usr/bin/pkgbuild', '--root', fixture.parent, '--identifier', RECEIPT,
            '--version', PRIOR_VERSION, '--install-location', '/Applications', baseline)
    install(baseline)
    receipt = command('/usr/sbin/pkgutil', '--pkg-info-plist', RECEIPT, capture=True)
    assert plistlib.loads(receipt.stdout.encode())['pkg-version'] == PRIOR_VERSION
    assert app_version() == PRIOR_VERSION and _checksum(EXECUTABLE) == old_hash

    skipped = install(legacy, check=False)
    assert app_version() == PRIOR_VERSION and _checksum(EXECUTABLE) == old_hash, 'Old package did not reproduce the skip'
    old_pids = wait_for(processes) if skipped.returncode == 0 else processes()
    for pid in old_pids:
        command('/bin/kill', '-TERM', pid)
    wait_for(lambda: not processes())

    data_folder = Path.home() / 'Library/Application Support/FlickDock'
    data_folder.mkdir(parents=True, exist_ok=True)
    marker = data_folder / 'package-overwrite-qa.txt'
    marker.write_text('existing local profile', encoding='utf-8')

    install(staged)
    restarted = wait_for(processes)
    assert len(restarted) == 1 and not set(restarted).intersection(old_pids)
    assert app_version() == APP_VERSION and _checksum(EXECUTABLE) == expected_hash
    assert marker.read_text(encoding='utf-8') == 'existing local profile'
    command('/usr/bin/codesign', '--verify', '--deep', '--strict', APP)
    profile = scratch / 'translator-profile'
    profile.mkdir()
    smoke = output / 'installed-translator.json'
    env = dict(os.environ, HOME=str(profile), APPDATA=str(profile), XDG_CONFIG_HOME=str(profile),
               QT_QPA_PLATFORM='cocoa', FLICKDOCK_VERIFY_NATIVE_UI='1')
    command(EXECUTABLE, '--verify-translator', '--verify-output', smoke, env=env, timeout=95)
    translator = json.loads(smoke.read_text(encoding='utf-8'))
    assert translator['ok'] and translator['frozen'] and translator['version'] == APP_VERSION
    assert translator['mac_leading_controls']
    report = dict(ok=True, fixture_version=PRIOR_VERSION, legacy_exit=skipped.returncode, legacy_kept_old_app=True,
                  installed_version=APP_VERSION, executable_matches=True, automatically_restarted=True,
                  profile_preserved=True, installed_translator_verified=True,
                  pkg_bytes=staged.stat().st_size, sha256=_checksum(staged))
    (output / 'mac-cover-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    verify(*(Path(value).resolve() for value in sys.argv[1:]))
