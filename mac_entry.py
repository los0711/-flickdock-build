"""macOS bundle entry: handle update helper before constructing a GUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback
import plistlib
import subprocess
import re


def _dmg_launch(source,target,*,runner=subprocess.run,launcher=subprocess.Popen):
    """Install/redirect an explicit disk-image launch before any GUI hooks run."""
    def metadata(path):
        try:return plistlib.loads((path/'Contents/Info.plist').read_bytes())
        except (OSError,ValueError):return {}
    current=metadata(source);installed=metadata(target)
    if current.get('CFBundleIdentifier')!='com.flickdock.app':return False
    if target.is_symlink():raise ValueError('应用程序中的 FlickDock 是链接，请先检查安装位置。')
    if target.exists() and installed.get('CFBundleIdentifier')!='com.flickdock.app':raise ValueError('应用程序中已有不同的 FlickDock.app，请先检查安装位置。')
    version=lambda value:tuple(map(int,value.split('.'))) if isinstance(value,str) and re.fullmatch(r'\d+\.\d+\.\d+',value) else (0,0,0)
    if installed.get('CFBundleIdentifier')!='com.flickdock.app' or version(installed.get('CFBundleShortVersionString'))<version(current.get('CFBundleShortVersionString')):
        # Finder supplies its standard replacement/authorization UI. Arguments
        # remain data; neither application path is interpolated into script code.
        script='''on run argv
display dialog "将 FlickDock 安装到“应用程序”后打开？安装完成后会退出安装镜像。" buttons {"取消", "安装并打开"} default button "安装并打开" cancel button "取消" with title "安装 FlickDock"
tell application "Finder" to duplicate (POSIX file (item 1 of argv)) to (POSIX file (item 2 of argv)) with replacing
end run'''
        running=runner(['/usr/bin/pgrep','-x','FlickDock'],capture_output=True,text=True)
        if any(pid.isdigit() and int(pid)!=os.getpid() for pid in running.stdout.split()):
            close='display dialog "请先退出正在运行的 FlickDock，再打开安装镜像安装新版本。" buttons {"好"} default button "好" with title "FlickDock 正在运行"'
            runner(['/usr/bin/osascript','-e',close],capture_output=True);return True
        copied=runner(['/usr/bin/osascript','-e',script,str(source),str(target.parent)],capture_output=True)
        if copied.returncode:return True
        installed=metadata(target)
        if installed.get('CFBundleIdentifier')!='com.flickdock.app' or installed.get('CFBundleShortVersionString')!=current.get('CFBundleShortVersionString'):
            raise ValueError('安装未完成，请从“应用程序”检查 FlickDock。')
    # Only detach a confirmed disk-image mount, never an external physical disk.
    result=runner(['/usr/bin/hdiutil','info','-plist'],capture_output=True)
    try:images=plistlib.loads(result.stdout).get('images',[])
    except (ValueError,TypeError):images=[]
    mount=source.parent
    image=any(str(image.get('image-path','')).lower().endswith('.dmg') and
           any(entity.get('mount-point')==str(mount) for entity in image.get('system-entities',[])) for image in images)
    launcher(['/bin/sh','-c','cd /; while /bin/kill -0 "$1" 2>/dev/null; do /bin/sleep 0.2; done; /usr/bin/open -a "$2"; if [ -n "$3" ]; then /usr/bin/hdiutil detach "$3" >/dev/null 2>&1; fi',
              'flickdock-installed-launch',str(os.getpid()),str(target),str(mount) if image else ''],
              cwd='/',stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    return True


def _relocate_disk_image_launch():
    if sys.platform!='darwin' or not getattr(sys,'frozen',False) or os.environ.get('QT_QPA_PLATFORM') in ('offscreen','minimal'):return False
    from platform_native import app_bundle
    source=app_bundle()
    return bool(source and source.as_posix().startswith('/Volumes/') and _dmg_launch(source,Path('/Applications/FlickDock.app')))


def verify_cocoa_window_order(edge, shelf):
    """Exercise the reported Spaces conflict inside the frozen Cocoa runtime.

    Hosted macOS runners need not choose the same default NSWindow behavior
    as the affected Mac. Stage MoveToActiveSpace explicitly, then verify both
    native reveal paths normalize it and the shelf restores its prior state.
    This entry is restricted to the existing isolated CI startup smoke mode.
    """
    if (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
            or os.environ.get('FLICKDOCK_CI_COCOA_SMOKE') != '1'
            or os.environ.get('QT_QPA_PLATFORM', '').lower() != 'cocoa'):
        raise RuntimeError('Native window-order verification requires frozen Cocoa CI mode.')
    import ctypes
    import AppKit
    import objc
    from platform_native import (lower_shelf_after_reveal, raise_edge_target,
                                 raise_shelf_for_reveal)

    move = int(AppKit.NSWindowCollectionBehaviorMoveToActiveSpace)
    can_join = int(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
    ignores_cycle = int(AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    staged = move | ignores_cycle
    results = []
    for name, widget, reveal in (('edge', edge, raise_edge_target),
                                  ('shelf', shelf, raise_shelf_for_reveal)):
        view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
        window = view.window()
        if window is None:
            raise AssertionError(f'{name}: frozen Qt did not create an NSWindow')
        original = (window.level(), window.collectionBehavior())
        try:
            window.setCollectionBehavior_(staged)
            if not int(window.collectionBehavior()) & move:
                raise AssertionError(f'{name}: failed to stage MoveToActiveSpace')
            if not reveal(widget, platform='darwin'):
                raise AssertionError(f'{name}: frozen native reveal failed')
            active = int(window.collectionBehavior())
            if not active & can_join or active & move or not active & ignores_cycle:
                raise AssertionError(f'{name}: frozen Spaces behavior is invalid: {active:#x}')
            if not reveal(widget, platform='darwin') or int(window.collectionBehavior()) != active:
                raise AssertionError(f'{name}: repeated frozen native reveal changed behavior')
            if name == 'shelf':
                if not lower_shelf_after_reveal(widget, platform='darwin'):
                    raise AssertionError('shelf: frozen native order did not restore')
                if int(window.collectionBehavior()) != staged or window.level() != original[0]:
                    raise AssertionError('shelf: frozen native order restored the wrong state')
            results.append(dict(window=name, staged_behavior=staged,
                                reveal_completed=True, active_behavior=active))
        finally:
            window.setLevel_(original[0])
            window.setCollectionBehavior_(original[1])
    return dict(ok=True, cases=results)


def verify_cocoa_interactions(edge, shelf):
    if (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
            or os.environ.get('FLICKDOCK_CI_COCOA_SMOKE') != '1'
            or os.environ.get('QT_QPA_PLATFORM', '').lower() != 'cocoa'):
        raise RuntimeError('Native interaction verification requires frozen Cocoa CI mode.')
    import ctypes
    import AppKit
    import objc
    from PySide6.QtCore import QPoint,Qt
    from PySide6.QtWidgets import QApplication
    from drag_probe import ContentDragGate
    import time
    app=QApplication.instance()
    probe=ContentDragGate(lambda _:None)
    try:
        if not probe.probe.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            raise AssertionError('Cocoa drag probe may hide when the source app stays active')
        probe.arm(QPoint(120,120));app.processEvents()
        probe_view=objc.objc_object(c_void_p=ctypes.c_void_p(int(probe.probe.winId())))
        if not probe_view.window().isVisible() or probe_view.window().level()!=AppKit.NSFloatingWindowLevel:
            raise AssertionError('Cocoa drag probe did not become a passive floating target')
    finally:probe.close()
    area=edge.screen_area()
    shelf._show_at(QPoint(area.right()-359,area.top()+4),'dock',QPoint())
    app.processEvents()
    canvas=shelf.geometry()
    def settle():
        deadline=time.monotonic()+4
        while shelf.geometry_morph.active and time.monotonic()<deadline:
            app.processEvents()
            if shelf.geometry()!=canvas:raise AssertionError('Cocoa changed the native canvas during a morph')
            time.sleep(.005)
        if shelf.geometry_morph.active:raise AssertionError('Cocoa morph did not settle')
    shelf.show_contents();settle();shelf.collapse_contents();settle()
    if shelf.geometry()!=canvas:raise AssertionError('Cocoa changed the native origin after collapse')
    shelf.hide_immediately()
    # A mode switch must retain the same actual NSWindow, rather than merely
    # recreating a Qt window at matching coordinates between two paints.
    import tempfile
    from app_settings import SettingsStore
    from lan_panel import LanDialog
    from lan_transfer import LanTransferService
    from wan_transfer import WanTransferService
    with tempfile.TemporaryDirectory(prefix='flickdock-cocoa-transfer-') as directory:
        lan=LanTransferService(discovery=False)
        wan=WanTransferService(Path(directory)/'wan',enabled=False)
        dialog=LanDialog(lan,SettingsStore(Path(directory)/'settings'))
        try:
            dialog.show();app.processEvents();geometry=dialog.geometry()
            view=objc.objc_object(c_void_p=ctypes.c_void_p(int(dialog.winId())))
            number=view.window().windowNumber()
            for service in (wan,lan,wan,lan):
                dialog.set_service(service);app.processEvents()
                current=objc.objc_object(c_void_p=ctypes.c_void_p(int(dialog.winId()))).window()
                if current.windowNumber()!=number or not current.isVisible() or dialog.geometry()!=geometry:
                    raise AssertionError('Transfer mode switch replaced or moved its Cocoa native window')
        finally:dialog.close();dialog.deleteLater();lan.close();wan.close();app.processEvents()
    if AppKit.NSApplication.sharedApplication().activationPolicy()!=AppKit.NSApplicationActivationPolicyAccessory:
        raise AssertionError('FlickDock acquired an unexpected Dock application tile')
    return dict(passive_drag_probe=True,stable_native_canvas=True,menu_bar_accessory=True,stable_transfer_window=True)


def verify_platform(report_path):
    if os.environ.get('QT_QPA_PLATFORM', '').lower() != 'offscreen':
        raise ValueError('平台验证必须显式使用 offscreen。')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtMultimedia import QMediaPlayer
    import hashlib
    from keyring.backends.macOS import Keyring
    import Quartz
    import flickdock
    from app_version import APP_VERSION
    from mac_updates import bundle_metadata
    from platform_native import app_bundle, native_input_available
    from platform_startup import startup_runtime_allowed
    from lan_transfer import _ephemeral_certificate
    import ssl
    application = QApplication.instance() or QApplication(['FlickDock-platform-verification'])
    flickdock.load_font(application)
    bundle = app_bundle()
    if bundle is None:
        raise ValueError('平台验证需要实际打包的 .app。')
    data = bundle_metadata(bundle, version=APP_VERSION)
    if data.get('LSUIElement') is not True:raise ValueError('Mac 应用缺少菜单栏常驻配置。')
    media = QMediaPlayer()
    media.deleteLater()
    derived = hashlib.scrypt(b'build-fixture', salt=b'FlickDock-build-fixture', n=2, r=1, p=1, dklen=16)
    if len(derived) != 16 or Keyring is None:
        raise ValueError('账号加密运行时或系统凭据后端缺失。')
    certificate, private_key, fingerprint = _ephemeral_certificate()
    if not certificate.startswith(b'-----BEGIN CERTIFICATE-----') or b'PRIVATE KEY' not in private_key.partition(b'\n')[0] or len(fingerprint) != 64:
        raise ValueError('局域网 TLS 加密运行时缺失。')
    report = dict(ok=True, version=APP_VERSION, platform=sys.platform,
                  qt_platform=application.platformName(), bundle_id=data['CFBundleIdentifier'],
                  native_hooks=False, startup_mutation=False, quartz_imported=True,
                  scrypt_available=True, keychain_backend_imported=True, keychain_accessed=False,
                  lan_tls_certificate_generated=True, lan_openssl_runtime=ssl.OPENSSL_VERSION,menu_bar_agent=True,
                  media_backend=os.getenv('QT_MEDIA_BACKEND', ''), bundle=str(bundle))
    if native_input_available() or startup_runtime_allowed():
        raise ValueError('离屏验证意外启用了本机输入或登录启动写入。')
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if '--elevated-mac-update' in arguments:
        from mac_updates import run_elevated_update_helper, write_privileged_update_error
        index = arguments.index('--elevated-mac-update')
        if index + 1 >= len(arguments):
            return 2
        receipt = Path(arguments[index + 1])
        try:
            run_elevated_update_helper(receipt)
            return 0
        except Exception as error:
            if receipt.name == 'update-receipt.json' and receipt.parent.name.startswith('.flickdock-update-'):
                try:
                    write_privileged_update_error(receipt.parent, error)
                except OSError:
                    pass
            return 1
    if '--apply-mac-update' in arguments:
        from mac_updates import run_update_helper
        index = arguments.index('--apply-mac-update')
        if index + 1 >= len(arguments):
            return 2
        receipt = Path(arguments[index + 1])
        try:
            run_update_helper(receipt)
            return 0
        except Exception as error:
            # Only the helper's own verified stage may receive diagnostics.
            if receipt.name == 'update-receipt.json' and receipt.parent.name.startswith('.flickdock-update-'):
                try:
                    (receipt.parent / 'install-error.txt').write_text(str(error), encoding='utf-8')
                except OSError:
                    pass
            return 1
    if '--verify-platform' in arguments:
        index = arguments.index('--verify-platform')
        return verify_platform(arguments[index + 1]) if index + 1 < len(arguments) else 2
    try:
        if not arguments and _relocate_disk_image_launch():return 0
        from platform_native import configure_media_backend
        configure_media_backend()
        from flickdock import main as application_main
        return application_main()
    except Exception:
        # Python startup errors do not necessarily create an Apple .ips crash
        # report. Keep a private, bounded traceback for diagnosing a launch
        # that briefly shows its icon and then exits.
        try:
            log = Path.home() / 'Library' / 'Logs' / 'FlickDock' / 'startup-last-error.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write(traceback.format_exc()[-32768:])
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
