# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

if sys.platform != 'darwin':
    raise RuntimeError('FlickDock.app must be built and verified on a macOS host.')
root = Path(SPECPATH)
sys.path.insert(0, str(root))
from app_version import APP_VERSION

assets = [(root / 'fonts' / 'HarmonyOS_Sans_SC_Regular.ttf', 'fonts')]
assets += [(root / 'licenses' / name, 'licenses') for name in (
    'HarmonyOS-Sans-LICENSE.txt', 'Python-LICENSE.txt', 'LGPL-3.0.txt',
    'GPL-3.0.txt', 'PyInstaller-COPYING.txt','OpenSSL-LICENSE.txt','Cloud-Client-LICENSES.txt',
    'Lucide-LICENSE.txt')]
assets += [(root / 'THIRD_PARTY_NOTICES.md', '.'), (root / 'font_manifest.json', '.')]
for path, destination in assets:
    if not path.is_file():
        raise FileNotFoundError(path)
identity = os.environ.get('FLICKDOCK_CODESIGN_IDENTITY') or None
entitlements = os.environ.get('FLICKDOCK_ENTITLEMENTS') or None
architecture = os.environ.get('FLICKDOCK_TARGET_ARCH') or None
icon = root / 'build' / 'macos' / 'FlickDock.icns'
if not icon.is_file():
    raise FileNotFoundError('Run build_macos.py first to generate the original SVG icon.')
analysis = Analysis([str(root / 'mac_entry.py')], pathex=[str(root)], binaries=[],
    datas=[(str(path), destination) for path, destination in assets],
    hiddenimports=['Quartz', 'Quartz.CoreGraphics', 'Foundation', 'objc', '_hashlib',
                   'ssl', '_ssl', 'ctypes', 'ctypes.util', 'verify_transfers',
                   'keyring.backends.macOS', 'keyring.backends.macOS.api',
                   'PySide6.QtSvg', 'PySide6.QtMultimedia', 'PySide6.QtWebSockets'],
    runtime_hooks=[str(root / 'runtime_macos.py')],
    excludes=['winreg', 'pefile', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtWebEngineQuick', 'PySide6.QtQml', 'PySide6.QtQuick',
              'PySide6.QtQuickWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets'],
    noarchive=False, optimize=0)
pyz = PYZ(analysis.pure)
executable = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='FlickDock',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    target_arch=architecture, codesign_identity=identity, entitlements_file=entitlements)
collection = COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name='FlickDock')
app = BUNDLE(collection, name='FlickDock.app', icon=str(icon), bundle_identifier='com.flickdock.app',
    version=APP_VERSION, target_arch=architecture, codesign_identity=identity,
    entitlements_file=entitlements,
    info_plist=dict(CFBundleDisplayName='FlickDock', CFBundleName='FlickDock',
                   CFBundleShortVersionString=APP_VERSION, CFBundleVersion=APP_VERSION,
                   CFBundlePackageType='APPL', CFBundleExecutable='FlickDock',
                   LSUIElement=False, NSHighResolutionCapable=True,
                   NSLocalNetworkUsageDescription='FlickDock 用于发现同一局域网的设备，并在你确认后加密传输文件。',
                   LSMinimumSystemVersion='14.4', NSPrincipalClass='NSApplication'))
