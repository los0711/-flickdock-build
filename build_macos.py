"""Native macOS build, offscreen verification and optional signing/notarization."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parent


def run(arguments, *, env=None):
    subprocess.run([str(value) for value in arguments], cwd=ROOT, env=env, check=True)


def make_icon(destination):
    # Offscreen SVG rendering uses the same original mark as the Windows app.
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication
    from flickdock_icons import app_svg
    application = QApplication.instance() or QApplication(['FlickDock-icon-builder'])
    iconset = destination.parent / 'FlickDock.iconset'
    iconset.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(QByteArray(app_svg().encode('utf-8')))
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            renderer.render(painter)
            painter.end()
            name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
            if not image.save(str(iconset / name)):
                raise OSError('Cannot save build icon')
    run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(destination)])


def _checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-arch', choices=('arm64', 'x86_64', 'universal2'), default=platform.machine())
    parser.add_argument('--sign-identity', default='')
    parser.add_argument('--notary-profile', default='')
    parser.add_argument('--entitlements', type=Path)
    parser.add_argument('--use-current-python', action='store_true')
    parser.add_argument('--icon-only', type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    if sys.platform != 'darwin':
        parser.error('This build requires a macOS host. Windows cannot produce or verify a usable .app/.dmg.')
    if options.icon_only is not None:
        make_icon(options.icon_only)
        return 0
    if options.notary_profile and not options.sign_identity:
        parser.error('--notary-profile requires --sign-identity')
    if options.entitlements and not options.entitlements.is_file():
        parser.error('--entitlements file does not exist')
    build = ROOT / 'build' / 'macos'
    build.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    if not options.use_current_python:
        environment = ROOT / 'build' / 'macos-venv'
        python = environment / 'bin' / 'python'
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(environment)
        run([python, '-m', 'pip', 'install', 'PySide6==6.12.0', 'PyInstaller==6.22.3',
             'pyobjc-framework-Quartz>=11,<13', 'keyring==25.7.0', 'cryptography>=42'])
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_MEDIA_BACKEND='darwin',
               FLICKDOCK_TARGET_ARCH=options.target_arch)
    # This workspace may contain Windows wheel files in py/. Never import them
    # into the macOS interpreter or reuse a Windows virtual environment.
    env.pop('PYTHONPATH', None)
    if options.sign_identity:
        env['FLICKDOCK_CODESIGN_IDENTITY'] = options.sign_identity
    if options.entitlements:
        env['FLICKDOCK_ENTITLEMENTS'] = str(options.entitlements.resolve())
    run([python, '-m', 'unittest', 'test_platform_startup', 'test_platform_native', 'test_mac_updates',
         'test_macos_native_entry', 'test_update_macos_controller', 'test_mac_build',
         'test_shelf_sync_v180', 'test_sync_review_v180', 'test_account_security_v180',
         'test_lan_transfer_v181', 'test_shelf_transfers_v181', '-v'], env=env)
    run([python, ROOT / 'test_update_macos.py'], env=env)
    run([python, ROOT / 'build_macos.py', '--icon-only', build / 'FlickDock.icns'], env=env)
    run([python, '-m', 'PyInstaller', '--noconfirm', '--clean', ROOT / 'FlickDock-mac.spec'], env=env)
    app = ROOT / 'dist' / 'FlickDock.app'
    binary = app / 'Contents' / 'MacOS' / 'FlickDock'
    if not binary.is_file():
        raise FileNotFoundError('PyInstaller did not produce the expected app bundle')
    media_plugins = list(app.rglob('libdarwinmediaplugin.dylib'))
    if not media_plugins:
        raise RuntimeError('Native Qt AVFoundation media plugin is missing')
    from app_version import APP_VERSION
    report = ROOT.parent / 'outputs' / f'mac-build-{APP_VERSION}-{options.target_arch}.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='flickdock-mac-verify-') as profile:
        smoke = Path(profile) / 'platform.json'
        run([binary, '--verify-platform', smoke], env=dict(env, XDG_CONFIG_HOME=profile, APPDATA=profile))
        frozen = json.loads(smoke.read_text(encoding='utf-8'))
        if not frozen.get('ok') or frozen.get('version') != APP_VERSION:
            raise RuntimeError('Frozen macOS platform verification failed')
        transfers_path = Path(profile) / 'transfers.json'
        run([binary, '--verify-transfers', '--verify-output', transfers_path], env=dict(env, XDG_CONFIG_HOME=profile, APPDATA=profile))
        transfers = json.loads(transfers_path.read_text(encoding='utf-8'))
        if not transfers.get('ok') or not transfers.get('frozen') or transfers.get('version') != APP_VERSION:
            raise RuntimeError('Frozen macOS original-file/TLS/cancellation verification failed')
        frozen['lan_transfers'] = transfers
    if options.sign_identity:
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    name = f'FlickDock-{APP_VERSION}-macOS-{options.target_arch}'
    archive = report.parent / (name + '.zip')
    if archive.exists():
        raise FileExistsError('Refusing to overwrite an existing versioned macOS release ZIP')
    run(['/usr/bin/ditto', '-c', '-k', '--keepParent', app, archive])
    if options.notary_profile:
        run(['/usr/bin/xcrun', 'notarytool', 'submit', archive, '--keychain-profile', options.notary_profile, '--wait'])
        run(['/usr/bin/xcrun', 'stapler', 'staple', app])
        archive.unlink()
        run(['/usr/bin/ditto', '-c', '-k', '--keepParent', app, archive])
    dmg = report.parent / (name + '.dmg')
    if dmg.exists():
        raise FileExistsError('Refusing to overwrite an existing versioned macOS DMG')
    with tempfile.TemporaryDirectory(prefix='flickdock-dmg-') as folder:
        staging = Path(folder)
        shutil.copytree(app, staging / 'FlickDock.app', symlinks=True)
        (staging / 'Applications').symlink_to('/Applications', target_is_directory=True)
        run(['/usr/bin/hdiutil', 'create', '-volname', 'FlickDock', '-srcfolder', staging,
             '-ov', '-format', 'UDZO', dmg])
    if options.sign_identity:
        run(['/usr/bin/codesign', '--sign', options.sign_identity, '--timestamp', dmg])
    if options.notary_profile:
        run(['/usr/bin/xcrun', 'notarytool', 'submit', dmg, '--keychain-profile', options.notary_profile, '--wait'])
        run(['/usr/bin/xcrun', 'stapler', 'staple', dmg])
    installer = report.parent / (name + '.pkg')
    if installer.exists():
        raise FileExistsError('Refusing to overwrite an existing versioned macOS installer')
    run(['/usr/bin/pkgbuild', '--component', app, '--install-location', '/Applications',
         '--identifier', 'com.flickdock.installer', '--version', APP_VERSION, installer])
    # Check the package payload and metadata without installing on this Mac.
    with tempfile.TemporaryDirectory(prefix='flickdock-pkg-verify-') as directory:
        expanded = Path(directory) / 'expanded'
        run(['/usr/sbin/pkgutil', '--expand', installer, expanded])
        package_info = (expanded / 'PackageInfo').read_text(encoding='utf-8')
        if 'com.flickdock.installer' not in package_info or APP_VERSION not in package_info:
            raise RuntimeError('macOS installer metadata does not match this build')
    artifacts = [dict(name=item.name, bytes=item.stat().st_size, sha256=_checksum(item))
                 for item in (archive, dmg, installer)]
    report.write_text(json.dumps(dict(ok=True, host=platform.platform(), version=APP_VERSION,
          architecture=options.target_arch, signed=bool(options.sign_identity),
          notarized=bool(options.notary_profile), frozen=frozen, artifacts=artifacts),
          ensure_ascii=False, indent=2), encoding='utf-8')
    print(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
