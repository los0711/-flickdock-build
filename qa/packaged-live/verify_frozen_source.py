"""Exact reviewed source -> compiled normal entry provenance, no app execution."""
import argparse,hashlib,json,marshal,sys
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--before-build',action='store_true');parser.add_argument('--after-build',action='store_true');args=parser.parse_args()
root=Path.cwd();sys.path.insert(0,str(root));out=root/'build/normal-source-verification.json';out.parent.mkdir(exist_ok=True)
manifest=json.loads((root/'SOURCE_MANIFEST.json').read_text(encoding='utf8'))
rows=manifest.get('files')
if isinstance(rows,dict):selected=rows
elif isinstance(rows,list):selected={row['path']:row['sha256'] for row in rows}
else:raise AssertionError('Unsupported reviewed manifest schema')
for name,expected in selected.items():
    path=root/name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise AssertionError('Reviewed source differs: '+name)
if args.before_build:
    out.write_text(json.dumps(dict(source_manifest_sha256=hashlib.sha256((root/'SOURCE_MANIFEST.json').read_bytes()).hexdigest(),reviewed_files=len(selected),before=True),indent=2))
if args.after_build:
    from PyInstaller.archive.readers import CArchiveReader
    from release_manifest import RUNTIME_SOURCES
    exe=root/'dist/FlickDock/FlickDock.exe';archive=CArchiveReader(str(exe));pyz=archive.open_embedded_archive('PYZ.pyz')
    provenance={}
    for filename in RUNTIME_SOURCES:
        module=filename.removesuffix('.py')
        if module not in pyz.toc:continue
        compiled=pyz.extract(module)
        provenance[filename]=compile((root/filename).read_bytes(),compiled.co_filename,'exec',dont_inherit=True,optimize=0)==compiled
    entry=marshal.loads(archive.extract('flickdock'))
    provenance['normal-flickdock-entry']=compile((root/'flickdock.py').read_bytes(),entry.co_filename,'exec',dont_inherit=True,optimize=0)==entry
    assert provenance and all(provenance.values()),provenance
    result=json.loads(out.read_text());result.update(compiled=provenance,after=True,exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
    # The COM loader is Python code; a correct PYZ alone does not prove its
    # runtime-loaded DLL survived PE dependency trimming.
    bindings={}
    for name in ('pythoncom312.dll','pywintypes312.dll'):
        matches=[path for path in exe.parent.rglob(name) if path.is_file()]
        assert len(matches)==1,('Required frozen Shell binding is missing or ambiguous',name)
        path=matches[0]
        bindings[name]=dict(path=path.relative_to(exe.parent).as_posix(),bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    result['windows_shell_bindings']=bindings
    out.write_text(json.dumps(result,indent=2))
print('Reviewed source / frozen normal entry verification passed')
