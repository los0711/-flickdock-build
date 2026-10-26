"""Read-only package-policy regressions; no build, install or desktop actions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from bundle_policy import filter_bundle, pe_dependencies

root=Path(__file__).resolve().parent
hashlib_pyd=Path(importlib.util.find_spec('_hashlib').origin)
crypto_name=next(name for name in pe_dependencies(hashlib_pyd) if name.startswith('libcrypto'))
runtime_directories=[hashlib_pyd.parent,Path(sys.base_prefix)/'DLLs',Path(sys.base_prefix)]
crypto=next(directory/crypto_name for directory in runtime_directories if (directory/crypto_name).is_file())
# A large DLL remains whenever a retained extension actually imports it,
# including when PyInstaller classified a library as data rather than binary.
binaries=[('_hashlib.pyd',str(hashlib_pyd),'EXTENSION')]
datas=[(crypto_name,str(crypto),'DATA')]
keep_b,keep_d,report=filter_bundle(binaries,datas)
assert any(e[0]==crypto_name for e in keep_d)
_,keep_d2,report2=filter_bundle([],datas)
assert not keep_d2 and report2['removed_bytes']==crypto.stat().st_size
# The bundled font is the original one recorded by its license manifest.
manifest=json.loads((root/'font_manifest.json').read_text(encoding='utf-8'))
assert len(manifest['fonts'])==1
font=manifest['fonts'][0]
assert font['file']=='fonts/HarmonyOS_Sans_SC_Regular.ttf'
assert hashlib.sha256((root/font['file']).read_bytes()).hexdigest()==font['sha256']
# Syntax check only: do not execute the PyInstaller spec or make an installer.
for name in ['FlickDock.spec','bundle_policy.py','runtime_media.py','package_source.py']:
    compile((root/name).read_text(encoding='utf-8-sig'),str(root/name),'exec')
print('BUNDLE_POLICY_STATIC_OK dependency-preservation/data-TOC/font-hash/config-syntax')
