# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import json
import sys
import ssl

root=Path(SPECPATH)
sys.path.insert(0,str(root))
from bundle_policy import filter_bundle

font_names=['HarmonyOS_Sans_SC_Regular.ttf']
license_names=['HarmonyOS-Sans-LICENSE.txt','Python-LICENSE.txt',
               'LGPL-3.0.txt','GPL-3.0.txt','PyInstaller-COPYING.txt',
               'OpenSSL-LICENSE.txt','Cloud-Client-LICENSES.txt']
asset_files=[(root/'fonts'/name,'fonts') for name in font_names]
asset_files += [(root/'licenses'/name,'licenses') for name in license_names]
asset_files += [(root/'THIRD_PARTY_NOTICES.md','.'),(root/'font_manifest.json','.')]
# This build runtime's SSLContext loads an auxiliary public CA policy file
# beside ssl.py. Preserve that policy in the frozen layout as well.
ssl_policy=Path(ssl.__file__).with_name('codex-rejected-cas.pem')
if ssl_policy.is_file():
    asset_files.append((ssl_policy,'.'))
for source,_ in asset_files:
    if not source.is_file():
        raise FileNotFoundError(f'Required distribution asset is missing: {source}')

unused_qt=['QtWebEngineCore','QtWebEngineWidgets','QtWebEngineQuick','QtPdf','QtPdfWidgets',
           'QtQml','QtQuick','QtQuickWidgets','QtQuickControls2','QtQmlMeta','QtQmlModels',
           'QtQmlWorkerScript','QtVirtualKeyboard','QtOpenGL','QtOpenGLWidgets',
           'QtMultimediaWidgets','QtSvgWidgets','QtSql','QtPrintSupport','QtHelp','QtTest']
a=Analysis(
    [str(root/'flickdock.py')],pathex=[str(root)],binaries=[],
    datas=[(str(source),destination) for source,destination in asset_files],
    # scrypt for cloud accounts needs _hashlib/libcrypto. Keep the native
    # dependency closure; the network transport still uses Qt/Schannel.
    hiddenimports=['PySide6.QtSvg','PySide6.QtMultimedia',
                   '_md5','_sha1','_sha2','_sha3','_blake2','_hashlib',
                   'keyring.backends.Windows'],
    hookspath=[],hooksconfig={},runtime_hooks=[str(root/'runtime_media.py')],
    excludes=['PySide6.'+name for name in unused_qt]+['keyring.backends.SecretService','keyring.backends.kwallet'],
    noarchive=False,optimize=0,
)
a.binaries,a.datas,report=filter_bundle(a.binaries,a.datas)
(root/'package-audit-analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
if report['required_dynamic_roots_missing']:
    raise RuntimeError('Required Qt runtime components missing: '+', '.join(report['required_dynamic_roots_missing']))
if report['required_plugin_paths_missing']:
    raise RuntimeError('Required Qt plugin destinations missing: '+', '.join(report['required_plugin_paths_missing']))
if report['unresolved_non_system_imports']:
    raise RuntimeError('Retained binaries have unresolved non-system imports: '+', '.join(report['unresolved_non_system_imports']))
print('FlickDock dependency audit: '+str(report['removed_bytes'])+' unused bytes removed; '+str(report['after_bytes'])+' retained bytes')
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='FlickDock',debug=False,
        bootloader_ignore_signals=False,strip=False,upx=False,console=False,
        icon=[str(root/'flickdock.ico')],version=str(root/'version_info.txt'))
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='FlickDock')
