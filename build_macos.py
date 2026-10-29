"""Native macOS build, offscreen verification and optional signing/notarization."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import copy
import stat
import time
import venv
import zipfile

ROOT = Path(__file__).resolve().parent


def run(arguments, *, env=None):
    subprocess.run([str(value) for value in arguments], cwd=ROOT, env=env, check=True)


def make_icon(destination):
    # Offscreen SVG rendering uses the same original mark as the Windows app.
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtCore import QByteArray, QRectF, Qt
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
            inset=pixels*.065
            renderer.render(painter,QRectF(inset,inset,pixels-2*inset,pixels-2*inset))
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


def _bundle_inventory(app):
    records={}
    for path in [app,*app.rglob('*')]:
        key=path.relative_to(app).as_posix()
        mode=stat.S_IMODE(path.lstat().st_mode)
        records[key]=('link',os.readlink(path),mode) if path.is_symlink() else (
            ('directory',mode) if path.is_dir() else ('file',_checksum(path),mode))
    return records


def _create_zip(app,archive,*,python,env):
    # Retain ditto's symlink and permission metadata. LZMA compresses the
    # bundled runtime without removing payload. Shipped 1.8.15 clients contain
    # _lzma and use mac_updates._extract, rather than ditto, for automatic ZIPs.
    with tempfile.TemporaryDirectory(prefix='flickdock-zip-') as directory:
        folder=Path(directory);original=folder/'original.zip'
        run(['/usr/bin/ditto','-c','-k','--keepParent','--zlibCompressionLevel','9',app,original])
        with zipfile.ZipFile(original) as source,zipfile.ZipFile(archive,'x') as target:
            target.comment=source.comment
            for member in source.infolist():
                info=copy.copy(member)
                compression=zipfile.ZIP_LZMA if member.file_size>=4096 else zipfile.ZIP_DEFLATED
                target.writestr(info,source.read(member),compress_type=compression,compresslevel=9)
        extracted=folder/'extracted'
        run([python,'-c',
             'import sys,zipfile;from pathlib import Path;from mac_updates import _extract;'
             'from build_macos import _bundle_inventory;'
             'archive,app,stage=map(Path,sys.argv[1:]);'
             'z=zipfile.ZipFile(archive);bundle=_extract(z,stage,None);z.close();'
             'assert _bundle_inventory(app)==_bundle_inventory(bundle),'
             '"Automatic ZIP round trip changed bundle bytes, symlinks or permissions"',
             archive,app,extracted],env=env)


def _create_dmg(staging,dmg):
    arguments=['/usr/bin/hdiutil','create','-volname','FlickDock','-srcfolder',str(staging),
               '-ov','-format','ULMO',str(dmg)]
    for attempt in range(3):
        result=subprocess.run(arguments,cwd=ROOT,capture_output=True,text=True,
                              env=dict(os.environ,LC_ALL='C'))
        if result.stdout:print(result.stdout,flush=True)
        if result.stderr:print(result.stderr,file=sys.stderr,flush=True)
        if result.returncode==0:return
        if attempt==2 or 'resource busy' not in (result.stdout+result.stderr).lower():
            raise subprocess.CalledProcessError(result.returncode,arguments)
        # Only this unpublished destination may exist after a failed create.
        if dmg.exists():dmg.unlink()
        time.sleep(2*(attempt+1))


PKG_PREINSTALL='''#!/bin/sh
set -eu
# Never replace the executable of a running instance, including older copies.
while /usr/bin/pgrep -x FlickDock >/dev/null 2>&1; do
  console_user=$(/usr/bin/stat -f %Su /dev/console)
  case "$console_user" in root|loginwindow|'') echo '请先退出 FlickDock，再重新安装。' >&2; exit 1;; esac
  console_uid=$(/usr/bin/id -u "$console_user")
  /bin/launchctl asuser "$console_uid" /usr/bin/sudo -u "$console_user" /usr/bin/osascript <<'APPLESCRIPT' || exit 1
display dialog "FlickDock 正在运行。请先退出已打开的 FlickDock，然后点击继续。安装程序会覆盖 /Applications 中的旧版本。" buttons {"取消安装", "继续"} default button "继续" cancel button "取消安装" with title "安装 FlickDock" with icon caution
APPLESCRIPT
done
exit 0
'''


def _create_pkg(app,installer,version):
    with tempfile.TemporaryDirectory(prefix='flickdock-pkg-') as directory:
        folder=Path(directory);payload=folder/'payload';payload.mkdir()
        shutil.copytree(app,payload/app.name,symlinks=True)
        components=folder/'components.plist'
        components.write_bytes(plistlib.dumps([dict(RootRelativeBundlePath=app.name,
            BundleIsRelocatable=False,BundleHasStrictIdentifier=True,
            BundleIsVersionChecked=True,BundleOverwriteAction='upgrade')]))
        scripts=folder/'scripts';scripts.mkdir();preinstall=scripts/'preinstall'
        preinstall.write_text(PKG_PREINSTALL,encoding='utf8');preinstall.chmod(0o755)
        run(['/bin/sh','-n',preinstall])
        run(['/usr/bin/pkgbuild','--root',payload,'--component-plist',components,
             '--install-location', '/Applications','--scripts',scripts,
             '--identifier','com.flickdock.installer','--version',version,
             '--compression','latest','--min-os-version','14.4',installer])


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
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_MEDIA_BACKEND='darwin',PYTHONFAULTHANDLER='1',
               FLICKDOCK_TARGET_ARCH=options.target_arch)
    # This workspace may contain Windows wheel files in py/. Never import them
    # into the macOS interpreter or reuse a Windows virtual environment.
    env.pop('PYTHONPATH', None)
    # Exercise the actual automatic updater before running the longer gates.
    with tempfile.TemporaryDirectory(prefix='flickdock-codec-probe-') as directory:
        folder=Path(directory);probe=folder/'FlickDock.app'
        executable=probe/'Contents/MacOS/FlickDock';executable.parent.mkdir(parents=True)
        executable.write_bytes(b'\xcf\xfa\xed\xfe'+bytes(range(256))*64);executable.chmod(0o755)
        (probe/'alias').symlink_to('Contents/MacOS/FlickDock')
        _create_zip(probe,folder/'probe.zip',python=python,env=env)
    with tempfile.TemporaryDirectory(prefix='flickdock-cocoa-preflight-') as directory:
        run([python,ROOT/'test_mac_cocoa_spaces.py','--output',Path(directory)/'cocoa.json'],
            env=dict(env,HOME=directory,QT_QPA_PLATFORM='cocoa',FLICKDOCK_CI_COCOA_SMOKE='1'))
    run([python,ROOT/'p2p_assets.py'],env=env)
    if options.sign_identity:
        env['FLICKDOCK_CODESIGN_IDENTITY'] = options.sign_identity
    if options.entitlements:
        env['FLICKDOCK_ENTITLEMENTS'] = str(options.entitlements.resolve())
    run([python, '-m', 'unittest', 'test_platform_startup', 'test_platform_native', 'test_mac_window_order_v1810', 'test_mac_updates',
         'test_macos_native_entry', 'test_update_macos_controller', 'test_mac_build',
         'test_shelf_sync_v180', 'test_sync_review_v180', 'test_account_security_v180',
         'test_lan_transfer_v181', 'test_lan_dialog_v183', 'test_shelf_transfers_v181',
         'test_account_profile_layout_v1814', 'test_lan_hig_layout_v1814',
         'test_ui_feedback_v1815', 'test_directory_sync_v1815', 'test_feedback_v1816','test_feedback_v1818',
         'test_shelf_raise_v1810', 'test_intake_first_frame_v1810',
         'test_sync_remote_commit_wakeup', 'test_cloud_file_status_v1810',
         'test_trigger_reliability_v1810', 'test_event_stream_wiring_v1810',
         'test_pointer_paste_v1819','test_presentation_v1819','test_clipboard_sync_v1819','test_wan_account_v1819','test_webrtc_close_v1819','test_update_notice_v1813','-v'], env=env)
    # Native WebRTC and QWebSocket lifetimes are verified in their own Qt
    # process, matching the standalone frozen-client gate below.
    run([python,'-m','unittest','test_wan_transfer_v1816','-v'],env=env)
    run([python,'-m','unittest','test_nearby_transport_v1818','-v'],env=env)
    run([python,'-m','unittest','test_wan_signaling_v1818','-v'],env=env)
    run([python,'-m','unittest','test_cloud_failover_v1818','-v'],env=env)
    run([python,'-m','unittest','test_stream_upload_v1816','-v'],env=env)
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
        update_path=Path(profile)/'public-update.json'
        run([binary,'--verify-update','--verify-current','1.8.14','--verify-output',update_path],env=dict(env,XDG_CONFIG_HOME=profile,APPDATA=profile))
        update=json.loads(update_path.read_text(encoding='utf8'))
        if not update.get('ok') or not update.get('frozen') or not update.get('verified_download') or update.get('installer_executed'):
            raise RuntimeError('Frozen Mac public update check/download failed')
        frozen['public_updates']=update
        frozen['wan_transfers']={}
        for mode in ('direct','relay'):
            wan_path=Path(profile)/('wan-'+mode+'.json')
            arguments=[binary,'--verify-wan','--verify-output',wan_path]
            if mode=='relay':arguments.append('--wan-force-relay')
            run(arguments,env=dict(env,XDG_CONFIG_HOME=profile,APPDATA=profile))
            wan=json.loads(wan_path.read_text(encoding='utf8'))
            if not wan.get('ok') or not wan.get('frozen') or wan.get('route_source')!='native-selected-candidate-pair' or mode=='relay' and wan.get('route')!='relay':
                raise RuntimeError('Frozen Mac native WAN '+mode+' transfer failed')
            frozen['wan_transfers'][mode]=wan
    # Run the real Cocoa plugin and construct the shelf on the hosted Mac,
    # but with a disposable HOME and native/network side effects disabled.
    # This catches launch exits that an offscreen import check cannot see.
    with tempfile.TemporaryDirectory(prefix='flickdock-cocoa-smoke-') as profile:
        smoke_path = Path(profile) / 'cocoa.json'
        cocoa_env = dict(env, HOME=profile, QT_QPA_PLATFORM='cocoa',
                         FLICKDOCK_CI_COCOA_SMOKE='1')
        try:
            # Stage the exact native flag reported on macOS 27. Hosted Macs
            # may choose different Qt defaults, so launch success alone does
            # not verify that edge/shelf native ordering avoids the conflict.
            spaces_path = Path(profile) / 'cocoa-spaces.json'
            run([python, ROOT / 'test_mac_cocoa_spaces.py', '--output', spaces_path],
                env=cocoa_env)
            spaces = json.loads(spaces_path.read_text(encoding='utf-8'))
            if (not spaces.get('ok') or spaces.get('qt_platform') != 'cocoa'
                    or {case.get('window') for case in spaces.get('cases', [])} != {'edge', 'shelf'}):
                raise RuntimeError('Native macOS Spaces collection behavior verification failed')
            frozen['cocoa_window_order'] = spaces
            # Hosted Intel Cocoa event dispatch can spend >20s in a native
            # readiness call. Keep the assertions intact with a bounded guard.
            subprocess.run([str(binary), '--verify-cocoa-startup', str(smoke_path)],
                           cwd=ROOT, env=cocoa_env, check=True, timeout=120)
            cocoa = json.loads(smoke_path.read_text(encoding='utf-8'))
            if (not cocoa.get('ok') or cocoa.get('version') != APP_VERSION
                    or cocoa.get('qt_platform') != 'cocoa'
                    or not cocoa.get('shelf_constructed')
                    or not cocoa.get('native_window_order', {}).get('ok')
                    or not cocoa.get('native_window_order', {}).get('passive_drag_probe')
                    or not cocoa.get('native_window_order', {}).get('stable_native_canvas')
                    or {case.get('window') for case in cocoa.get('native_window_order', {}).get('cases', [])} != {'edge', 'shelf'}
                    or any(cocoa.get(name) for name in ('native_hotkeys', 'lan_listener',
                                                       'cloud_session', 'startup_mutation'))):
                raise RuntimeError('Frozen macOS Cocoa shelf startup verification failed')
            frozen['cocoa_startup'] = cocoa
        except BaseException:
            startup_log = Path(profile) / 'Library/Logs/FlickDock/startup-last-error.log'
            if startup_log.is_file():
                shutil.copy2(startup_log, report.parent / f'mac-startup-error-{options.target_arch}.log')
            raise
    if options.sign_identity:
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    name = f'FlickDock-{APP_VERSION}-macOS-{options.target_arch}'
    archive = report.parent / (name + '.zip')
    if archive.exists():
        raise FileExistsError('Refusing to overwrite an existing versioned macOS release ZIP')
    _create_zip(app,archive,python=python,env=env)
    if options.notary_profile:
        run(['/usr/bin/xcrun', 'notarytool', 'submit', archive, '--keychain-profile', options.notary_profile, '--wait'])
        run(['/usr/bin/xcrun', 'stapler', 'staple', app])
        archive.unlink()
        _create_zip(app,archive,python=python,env=env)
    dmg = report.parent / (name + '.dmg')
    if dmg.exists():
        raise FileExistsError('Refusing to overwrite an existing versioned macOS DMG')
    with tempfile.TemporaryDirectory(prefix='flickdock-dmg-') as folder:
        staging = Path(folder)
        shutil.copytree(app, staging / 'FlickDock.app', symlinks=True)
        (staging / 'Applications').symlink_to('/Applications', target_is_directory=True)
        _create_dmg(staging,dmg)
    if options.sign_identity:
        run(['/usr/bin/codesign', '--sign', options.sign_identity, '--timestamp', dmg])
    if options.notary_profile:
        run(['/usr/bin/xcrun', 'notarytool', 'submit', dmg, '--keychain-profile', options.notary_profile, '--wait'])
        run(['/usr/bin/xcrun', 'stapler', 'staple', dmg])
    run(['/usr/bin/hdiutil', 'verify', dmg])
    with tempfile.TemporaryDirectory(prefix='flickdock-dmg-verify-') as folder:
        mount = Path(folder) / 'mounted'; mount.mkdir()
        run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', mount, dmg])
        try:
            mounted_app = mount / 'FlickDock.app'
            metadata = plistlib.loads((mounted_app / 'Contents/Info.plist').read_bytes())
            if (metadata.get('CFBundleIdentifier') != 'com.flickdock.app'
                    or metadata.get('CFBundleShortVersionString') != APP_VERSION
                    or not (mounted_app / 'Contents/MacOS/FlickDock').is_file()
                    or (mount / 'Applications').resolve() != Path('/Applications')):
                raise RuntimeError('DMG payload identity, version or install destination is invalid')
        finally:
            run(['/usr/bin/hdiutil', 'detach', mount])
    installer = report.parent / (name + '.pkg')
    if installer.exists():
        raise FileExistsError('Refusing to overwrite an existing versioned macOS installer')
    _create_pkg(app,installer,APP_VERSION)
    # Check the package payload and metadata without installing on this Mac.
    with tempfile.TemporaryDirectory(prefix='flickdock-pkg-verify-') as directory:
        expanded = Path(directory) / 'expanded'
        run(['/usr/sbin/pkgutil', '--expand-full', installer, expanded])
        package_info = (expanded / 'PackageInfo').read_text(encoding='utf-8')
        if 'com.flickdock.installer' not in package_info or APP_VERSION not in package_info:
            raise RuntimeError('macOS installer metadata does not match this build')
        if _bundle_inventory(app)!=_bundle_inventory(expanded/'Payload'/app.name):
            raise RuntimeError('Native PKG round trip changed bundle bytes, symlinks or permissions')
    if any(item.stat().st_size>100000000 for item in (archive,dmg,installer)):
        raise RuntimeError('Native package exceeds the public download service 100 MB attachment limit')
    artifacts = [dict(name=item.name, bytes=item.stat().st_size, sha256=_checksum(item))
                 for item in (archive, dmg, installer)]
    report.write_text(json.dumps(dict(ok=True, host=platform.platform(), version=APP_VERSION,
          architecture=options.target_arch, signed=bool(options.sign_identity), dmg_verified=True,
          notarized=bool(options.notary_profile),zip_roundtrip_verified=True,pkg_payload_verified=True,
          frozen=frozen, artifacts=artifacts),
          ensure_ascii=False, indent=2), encoding='utf-8')
    print(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
