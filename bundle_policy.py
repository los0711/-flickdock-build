"""FlickDock's small Windows bundle policy and reproducible PE dependency audit.

No DLL is trimmed solely because its name looks large. After removing unused
Qt plugin entry points, retained binaries are the import closure of Python
extension modules, the Python runtime, and the selected dynamic Qt plugins.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import pefile

PLUGIN_ALLOWLIST = {
    'platforms': {'qwindows.dll', 'qoffscreen.dll', 'qminimal.dll'},
    'styles': {'qmodernwindowsstyle.dll', 'qwindowsvistastyle.dll'},
    'imageformats': {'qgif.dll', 'qico.dll', 'qjpeg.dll', 'qsvg.dll', 'qtiff.dll', 'qwebp.dll'},
    'iconengines': {'qsvgicon.dll'},
    'tls': {'qcertonlybackend.dll', 'qschannelbackend.dll'},
    'networkinformation': {'qnetworklistmanager.dll'},
    'multimedia': {'windowsmediaplugin.dll'},
}
UNUSED_DATA_PARTS = {'qml', 'translations', 'resources', 'typesystems', 'include', 'glue'}
FONT_KEEP = {'harmonyos_sans_sc_regular.ttf'}
# This unrelated Poppler library shadows Windows' own ICU API. Core.dll's
# unversioned ICU imports are satisfied by the OS, not by Poppler ICU 78.
SYSTEM_ONLY = {'icuuc.dll'}
RUNTIME_ROOTS = {'python3.dll', 'python312.dll'}
REQUIRED_DYNAMIC_ROOTS = {
    'qt6core.dll','qt6gui.dll','qt6widgets.dll','qt6network.dll','qt6websockets.dll','qt6svg.dll','qt6multimedia.dll',
    'qwindows.dll','qoffscreen.dll','qsvgicon.dll','windowsmediaplugin.dll',
    'qcertonlybackend.dll','qschannelbackend.dll','qnetworklistmanager.dll',
    'qgif.dll','qico.dll','qjpeg.dll','qsvg.dll','qtiff.dll','qwebp.dll',
}
REQUIRED_PLUGIN_PATHS = {
    f'pyside6/plugins/{group}/{name}'
    for group,names in PLUGIN_ALLOWLIST.items()
    for name in names
    if name in REQUIRED_DYNAMIC_ROOTS
}


def _normal(value):
    return str(value).replace('\\','/').lower()


def _policy_reason(destination, source):
    dest = _normal(destination)
    name = Path(source).name.lower()
    parts = dest.split('/')
    if name in SYSTEM_ONLY:
        return 'uses Windows system ICU; do not shadow it with Poppler ICU'
    if '/plugins/' in '/'+dest:
        index = parts.index('plugins')
        group = parts[index+1] if len(parts)>index+1 else ''
        if name not in PLUGIN_ALLOWLIST.get(group,set()):
            return f'unused Qt plugin entry point: {group}/{name}'
    if 'pyside6' in parts and any(part in UNUSED_DATA_PARTS for part in parts):
        return 'unused Qt QML/resources/translations/development data'
    if dest.startswith('fonts/') and name not in FONT_KEEP:
        return 'only the unmodified Regular font is needed; Qt synthesizes weights'
    return None


def pe_dependencies(path):
    """Direct, delay-loaded and export-forwarded library names from a PE file."""
    image = pefile.PE(str(path),fast_load=True)
    directories = [pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
                   pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT'],
                   pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']]
    try:
        image.parse_data_directories(directories=directories)
        dependencies = {entry.dll.decode('ascii',errors='replace').lower()
                        for field in ('DIRECTORY_ENTRY_IMPORT','DIRECTORY_ENTRY_DELAY_IMPORT')
                        for entry in getattr(image,field,[])}
        for symbol in getattr(getattr(image,'DIRECTORY_ENTRY_EXPORT',None),'symbols',[]):
            if symbol.forwarder:
                forwarded = symbol.forwarder.decode('ascii',errors='replace').split('.')[0].lower()
                if forwarded:
                    dependencies.add(forwarded+'.dll')
        return dependencies
    finally:
        image.close()


def _system_import_exists(name):
    if name.startswith(('api-ms-','ext-ms-')):
        return True  # Windows API-set contracts need not have a physical DLL.
    windows=Path(os.environ.get('SystemRoot',r'C:\Windows'))
    return any((directory/name).is_file() for directory in (windows/'System32',windows))


def filter_bundle(binaries,datas):
    all_entries = [('binary',tuple(e)) for e in binaries]+[('data',tuple(e)) for e in datas]
    candidates,removed,non_pe = [],[],[]
    for kind,entry in all_entries:
        destination,source = entry[:2]
        reason = _policy_reason(destination,source)
        record = {'destination':str(destination),'source':str(source),
                  'bytes':Path(source).stat().st_size if Path(source).is_file() else 0,
                  'kind':kind}
        if reason:
            removed.append(dict(record,reason=reason))
        elif Path(source).suffix.lower() in {'.dll','.pyd','.exe','.node'}:
            candidates.append((kind,entry,record))
        else:
            non_pe.append((kind,entry,record))
    by_name = defaultdict(list)
    for index,(_,entry,_) in enumerate(candidates):
        by_name[Path(entry[1]).name.lower()].append(index)
    roots,root_reasons = set(),{}
    for index,(_,entry,_) in enumerate(candidates):
        name = Path(entry[1]).name.lower()
        if name.endswith('.pyd') or name in RUNTIME_ROOTS:
            roots.add(index);root_reasons[index]='Python runtime or analysed extension module'
        elif '/plugins/' in '/'+_normal(entry[0]):
            roots.add(index);root_reasons[index]='selected runtime-loaded Qt plugin'
        elif _normal(entry[0]).startswith('p2p/') and name in ('node.exe','node_datachannel.node'):
            roots.add(index);root_reasons[index]='native WebRTC data-channel runtime'
    keep,queue,dependencies,unbundled = set(roots),list(roots),{},set()
    while queue:
        index = queue.pop()
        source = candidates[index][1][1]
        try:
            imported = pe_dependencies(source)
        except (OSError,pefile.PEFormatError) as error:
            raise RuntimeError(f'Cannot safely audit retained binary {source}: {error}') from error
        dependencies[index] = sorted(imported)
        for dependency in imported:
            matches = by_name.get(dependency,[])
            if not matches:
                unbundled.add(dependency)
            for child in matches:
                if child not in keep:
                    keep.add(child);queue.append(child)
                    root_reasons[child] = f'PE dependency of {candidates[index][0]}:{candidates[index][1][0]}'
    retained = list(non_pe)
    for index,(kind,entry,record) in enumerate(candidates):
        if index in keep:
            retained.append((kind,entry,dict(record,reason=root_reasons[index],imports=dependencies.get(index,[]))))
        else:
            removed.append(dict(record,reason='not reachable from selected runtime roots in PE import graph'))
    result_binaries = [entry for kind,entry,_ in retained if kind=='binary']
    result_datas = [entry for kind,entry,_ in retained if kind=='data']
    found = {Path(entry[1]).name.lower() for _,entry,_ in retained}
    destinations={_normal(entry[0]) for _,entry,_ in retained}
    plugin_paths_missing=sorted(path for path in REQUIRED_PLUGIN_PATHS
                                if not any(dest.endswith(path) for dest in destinations))
    unresolved=sorted(name for name in unbundled if not _system_import_exists(name))
    before = sum(r['bytes'] for _,_,r in candidates+non_pe)+sum(r['bytes'] for r in removed if not r['reason'].startswith('not reachable'))
    report = {
        'policy_version':'1.6.0','method':'Qt plugin allowlist followed by direct/delayed/forwarded PE import closure',
        'before_bytes':before,'after_bytes':sum(r['bytes'] for _,_,r in retained),
        'removed_bytes':sum(r['bytes'] for r in removed),
        'required_dynamic_roots_missing':sorted(REQUIRED_DYNAMIC_ROOTS-found),
        'required_plugin_paths_missing':plugin_paths_missing,
        'unbundled_imports_system_provided':sorted(unbundled-set(unresolved)),
        'unresolved_non_system_imports':unresolved,
        'retained':[r for _,_,r in retained],
        'removed':sorted(removed,key=lambda r:r['bytes'],reverse=True),
        'runtime_note':'Qt Multimedia uses Windows Media Foundation; no FFmpeg backend or software OpenGL fallback is packaged.',
    }
    return result_binaries,result_datas,report


def audit_distribution(directory,output):
    directory=Path(directory).resolve()
    entries=[]
    for path in directory.rglob('*'):
        if path.is_file() and path.name.lower()!='flickdock.exe':
            relative=path.relative_to(directory).as_posix().removeprefix('_internal/')
            entries.append((relative,str(path),'BINARY' if path.suffix.lower() in {'.dll','.pyd'} else 'DATA'))
    binaries=[e for e in entries if e[2]=='BINARY'];datas=[e for e in entries if e[2]=='DATA']
    _,_,report=filter_bundle(binaries,datas)
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory');parser.add_argument('--output',default='package-audit.json')
    args=parser.parse_args();result=audit_distribution(args.directory,args.output)
    print(json.dumps({k:result[k] for k in ['before_bytes','after_bytes','removed_bytes','required_dynamic_roots_missing']},indent=2))
