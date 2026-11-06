"""Build-only selection of pinned, unmodified official Syncthing artifacts."""
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import sys
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent
VERSION='v2.1.7'
RELEASE='https://github.com/syncthing/syncthing/releases/download/'+VERSION+'/'
ARTIFACTS={
    'windows-amd64':dict(name='syncthing-windows-amd64-v2.1.7.zip',bytes=14281765,
                         sha256='3ce7da770f546a8d02b65b2e7ee31f2a3b8b7c3f990ede045590cca4172ecfe1'),
    'macos-arm64':dict(name='syncthing-macos-arm64-v2.1.7.zip',bytes=13372993,
                      sha256='2df075b463231a307c91da657157beaed06383a941ba7ea04d33d536e3c4af38'),
}


def checksum(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def target():
    machine=platform.machine().lower()
    if sys.platform=='win32' and machine in ('amd64','x86_64'):return 'windows-amd64'
    if sys.platform=='darwin' and machine in ('arm64','aarch64'):return 'macos-arm64'
    raise RuntimeError('This release supports Windows x64 and Apple silicon macOS only')


def prepare(*,cache=None):
    selected=target();asset=ARTIFACTS[selected]
    if cache is None:
        configured=os.environ.get('FLICKDOCK_BUILD_CACHE')
        cache=Path(configured) if configured else (Path('E:/Codex/Cache') if os.name=='nt' else ROOT/'build/cache')
    cache=Path(cache)/'syncthing';cache.mkdir(parents=True,exist_ok=True)
    path=cache/asset['name']
    if not path.exists():
        partial=path.with_suffix('.part')
        try:
            with urllib.request.urlopen(RELEASE+asset['name'],timeout=90) as reply,partial.open('wb') as output:
                count=0
                while block:=reply.read(1024*1024):
                    count+=len(block)
                    if count>asset['bytes']:raise ValueError('Official kernel archive exceeds its pinned size')
                    output.write(block)
            if count!=asset['bytes'] or checksum(partial)!=asset['sha256']:raise ValueError('Official kernel archive SHA256 mismatch')
            os.replace(partial,path)
        finally:partial.unlink(missing_ok=True)
    if path.stat().st_size!=asset['bytes'] or checksum(path)!=asset['sha256']:raise ValueError('Official kernel archive SHA256 mismatch')
    runtime=ROOT/'syncthing/runtime';runtime.mkdir(parents=True,exist_ok=True)
    executable='syncthing.exe' if selected=='windows-amd64' else 'syncthing'
    names=(executable,'LICENSE.txt','AUTHORS.txt','README.txt');prefix=path.stem+'/'
    with zipfile.ZipFile(path) as archive:
        for name in names:
            member=archive.getinfo(prefix+name)
            if member.is_dir() or not 0<member.file_size<100*1024*1024:raise ValueError('Invalid official kernel archive member')
            with archive.open(member) as source,(runtime/name).open('wb') as output:shutil.copyfileobj(source,output)
    (runtime/executable).chmod(0o755)
    manifest=dict(version=VERSION,platform=selected,archive_sha256=asset['sha256'],
                  files=[dict(name=name,bytes=(runtime/name).stat().st_size,sha256=checksum(runtime/name)) for name in names])
    (runtime/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    return manifest


def bundle_assets():
    runtime=ROOT/'syncthing/runtime';manifest=json.loads((runtime/'manifest.json').read_bytes())
    selected=target();asset=ARTIFACTS[selected]
    if manifest['version']!=VERSION or manifest['platform']!=selected or manifest['archive_sha256']!=asset['sha256']:
        raise ValueError('Kernel assets do not match the selected build platform')
    executable='syncthing.exe' if selected=='windows-amd64' else 'syncthing'
    if {row['name'] for row in manifest['files']}!={executable,'LICENSE.txt','AUTHORS.txt','README.txt'}:
        raise ValueError('Kernel asset list is invalid')
    binaries=[];data=[]
    for row in manifest['files']:
        source=runtime/row['name']
        if source.is_symlink() or not source.is_file() or source.stat().st_size!=row['bytes'] or checksum(source)!=row['sha256']:
            raise ValueError('Kernel asset checksum mismatch')
        (binaries if row['name']==executable else data).append((str(source),'syncthing/runtime'))
    data.append((str(runtime/'manifest.json'),'syncthing/runtime'))
    return binaries,data


if __name__=='__main__':
    result=prepare();print(json.dumps(dict(ok=True,version=result['version'],platform=result['platform'],files=len(result['files']))))
