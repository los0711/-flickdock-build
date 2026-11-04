"""Prepare and verify the separate FFmpeg command-line runtime at build time."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent
ARCHIVE_NAME='ffmpeg-n9.0.2-39-g7adfbe40dc-win64-lgpl-shared-9.0.zip'
ARCHIVE_URL='https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/'+ARCHIVE_NAME
ARCHIVE_SHA256='e907b01fa4783913151237e37df2a10fa97d99a1598a021bf59d4ae62ffed4c0'
TOOLS=ROOT/'assets/video-tools'


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def prepare():
    TOOLS.mkdir(parents=True,exist_ok=True)
    manifest_path=TOOLS/'manifest.json'
    if manifest_path.is_file():
        previous=json.loads(manifest_path.read_text(encoding='utf-8'))
        if previous.get('platform')!=sys.platform:
            for row in previous['files']:
                name=row['name'];path=TOOLS/name
                if Path(name).name!=name or not path.is_file() or path.is_symlink() or digest(path)!=row['sha256']:
                    raise ValueError('Cannot replace modified video runtime asset')
            for row in previous['files']:(TOOLS/row['name']).unlink()
    if sys.platform=='win32':
        cache=Path('E:/Codex/Cache/flickdock-video') if Path('E:/Codex').is_dir() else ROOT/'build/video-cache'
        cache.mkdir(parents=True,exist_ok=True);archive=cache/ARCHIVE_NAME
        if not archive.is_file():
            temporary=archive.with_suffix('.download')
            try:
                with urllib.request.urlopen(ARCHIVE_URL,timeout=45) as source,temporary.open('wb') as output:shutil.copyfileobj(source,output)
                if digest(temporary)!=ARCHIVE_SHA256:raise ValueError('FFmpeg archive checksum mismatch')
                temporary.replace(archive)
            finally:temporary.unlink(missing_ok=True)
        if digest(archive)!=ARCHIVE_SHA256:raise ValueError('FFmpeg archive checksum mismatch')
        with zipfile.ZipFile(archive) as bundle:
            names=[name for name in bundle.namelist() if '/bin/' in name and (name.endswith('.dll') or name.endswith(('/ffmpeg.exe','/ffprobe.exe'))) or name.endswith('/LICENSE.txt')]
            for name in names:
                with bundle.open(name) as source,(TOOLS/Path(name).name).open('wb') as output:shutil.copyfileobj(source,output)
        selected=[TOOLS/Path(name).name for name in names]
        origin=dict(url=ARCHIVE_URL,sha256=ARCHIVE_SHA256,license='LGPL-3.0-or-later')
    elif sys.platform=='darwin':
        import imageio_ffmpeg
        source=Path(imageio_ffmpeg.get_ffmpeg_exe())
        target=TOOLS/'ffmpeg';shutil.copy2(source,target);target.chmod(0o755)
        shutil.copy2(ROOT/'licenses/GPL-3.0.txt',TOOLS/'LICENSE.txt')
        selected=[target,TOOLS/'LICENSE.txt']
        origin=dict(package='imageio-ffmpeg==0.6.0',url='https://github.com/imageio/imageio-ffmpeg',license='GPL-3.0-or-later')
    else:raise RuntimeError('FlickDock bundles video tools for Windows and macOS only')
    files=[dict(name=path.name,bytes=path.stat().st_size,sha256=digest(path)) for path in sorted(selected)]
    manifest=dict(platform=sys.platform,origin=origin,files=files)
    (TOOLS/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(ok=True,platform=sys.platform,files=len(files))))


def bundle_assets():
    manifest=json.loads((TOOLS/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('platform')!=sys.platform:raise RuntimeError('Run video_assets.py for the current build platform')
    files=[]
    for row in manifest['files']:
        name=row['name']
        if Path(name).name!=name:raise ValueError('Invalid video asset name')
        path=TOOLS/name
        if not path.is_file() or path.is_symlink() or path.stat().st_size!=row['bytes'] or digest(path)!=row['sha256']:raise ValueError('Video runtime asset mismatch: '+name)
        files.append((str(path),'assets/video-tools'))
    required='ffmpeg.exe' if sys.platform=='win32' else 'ffmpeg'
    if required not in {Path(path).name for path,_ in files}:raise ValueError('Missing video runtime')
    binaries=[entry for entry in files if Path(entry[0]).suffix in ('.exe','.dll') or Path(entry[0]).name=='ffmpeg']
    data=[entry for entry in files if entry not in binaries]+[(str(TOOLS/'manifest.json'),'assets/video-tools')]
    if sys.platform=='win32':
        # PyInstaller otherwise recopies same-named FFmpeg DLLs into the app's
        # root, where they can shadow the different Qt playback build. The PE
        # policy still audits these data entries and their full import closure.
        return [],data+binaries
    return binaries,data


if __name__=='__main__':prepare()
