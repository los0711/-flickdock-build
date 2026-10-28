"""macOS bundle entry: handle update helper before constructing a GUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback


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
                  lan_tls_certificate_generated=True, lan_openssl_runtime=ssl.OPENSSL_VERSION,
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
