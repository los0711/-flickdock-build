"""macOS bundle entry: handle update helper before constructing a GUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


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
    report = dict(ok=True, version=APP_VERSION, platform=sys.platform,
                  qt_platform=application.platformName(), bundle_id=data['CFBundleIdentifier'],
                  native_hooks=False, startup_mutation=False, quartz_imported=True,
                  scrypt_available=True, keychain_backend_imported=True, keychain_accessed=False,
                  media_backend=os.getenv('QT_MEDIA_BACKEND', ''), bundle=str(bundle))
    if native_input_available() or startup_runtime_allowed():
        raise ValueError('离屏验证意外启用了本机输入或登录启动写入。')
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
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
    from platform_native import configure_media_backend
    configure_media_backend()
    from flickdock import main as application_main
    return application_main()


if __name__ == '__main__':
    raise SystemExit(main())
