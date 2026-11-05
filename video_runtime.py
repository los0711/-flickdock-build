"""Fetch one pinned FFmpeg executable on demand, outside the application bundle."""
import hashlib
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from PySide6.QtCore import QLockFile
from platform_native import data_directory
from transfer_priority import yield_bulk_payload

RUNTIME_CATALOG=[{'url': 'https://files.pythonhosted.org/packages/da/58/87ef68ac83f4c7690961bce288fd8e382bc5f1513860fc7f90a9c1c1c6bf/imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl', 'archive_sha256': '9d2baaf867088508d4a3458e61eeb30e945c4ad8016025545f66c4b5aaef0a61', 'archive_bytes': 24932969, 'member': 'imageio_ffmpeg/binaries/ffmpeg-macos-x86_64-v7.1', 'sha256': '4a4a968b98859588e98500ae25973d80a5ca5eed0724222b9f76360dcb72a001', 'bytes': 75991688, 'platform': 'darwin', 'architecture': 'x86_64'}, {'url': 'https://files.pythonhosted.org/packages/40/5c/f3d8a657d362cc93b81aab8feda487317da5b5d31c0e1fdfd5e986e55d17/imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl', 'archive_sha256': 'b1ae3173414b5fc5f538a726c4e48ea97edc0d2cdc11f103afee655c463fa742', 'archive_bytes': 21113891, 'member': 'imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1', 'sha256': '6d175a4743ca50256e89a8cdd731100f9cee33bd79aeea46894d209410dc6617', 'bytes': 49368728, 'platform': 'darwin', 'architecture': 'arm64'}, {'url': 'https://files.pythonhosted.org/packages/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl', 'archive_sha256': '02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a', 'archive_bytes': 31246824, 'member': 'imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe', 'sha256': '2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3', 'bytes': 87638016, 'platform': 'win32', 'architecture': 'x86_64'}]
BLOCK_BYTES=64*1024
PREPARE_TIMEOUT_SECONDS=180

def _digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def ensure_video_runtime(progress=lambda **_:None):
    architecture=platform.machine().lower()
    if architecture in ('amd64','x64'):architecture='x86_64'
    if architecture=='aarch64':architecture='arm64'
    record=next((row for row in RUNTIME_CATALOG if (row['platform'],row['architecture'])==(sys.platform,architecture)),None)
    if record is None:raise ValueError('当前系统没有适用的视频下载组件。')
    folder=data_directory()/'video-runtime'/record['sha256'];folder.mkdir(parents=True,exist_ok=True)
    if folder.is_symlink():raise ValueError('视频组件缓存目录不能是链接。')
    target=folder/('ffmpeg.exe' if sys.platform=='win32' else 'ffmpeg')
    def verified():
        if target.is_symlink():raise ValueError('视频组件不能是链接。')
        return target.is_file() and target.stat().st_size==record['bytes'] and _digest(target)==record['sha256']
    if verified():return folder
    lock=QLockFile(str(folder/'prepare.lock'));lock.setStaleLockTime(PREPARE_TIMEOUT_SECONDS*1000)
    deadline=time.monotonic()+PREPARE_TIMEOUT_SECONDS;last_notice=0.
    while not lock.tryLock(0):
        if time.monotonic()>=deadline:raise TimeoutError('等待视频下载组件超时，请重试。')
        if time.monotonic()-last_notice>=1:
            progress(phase='preparing',bytes=0,total=record['archive_bytes'],speed=None);last_notice=time.monotonic()
        time.sleep(.05)
    temporary=[]
    try:
        if verified():return folder
        # A killed worker can leave only bounded, unpublished files in this owned cache.
        for path in folder.glob('.runtime-*'):
            if path.is_file() and not path.is_symlink():path.unlink()
        with tempfile.NamedTemporaryFile(prefix='.runtime-',dir=folder,delete=False) as output:
            archive=Path(output.name);temporary.append(archive);received=0;digest=hashlib.sha256();started=time.monotonic();last_progress=started
            progress(phase='preparing',bytes=0,total=record['archive_bytes'],speed=None)
            request=urllib.request.Request(record['url'],headers={'User-Agent':'FlickDock/1.8.23','Accept-Encoding':'identity'})
            with urllib.request.urlopen(request,timeout=30) as response:
                if response.geturl()!=record['url']:raise ValueError('视频组件下载来源发生变化。')
                while True:
                    yield_bulk_payload(threading.Event())
                    if time.monotonic()>=deadline:raise TimeoutError('下载视频组件超时，请重试。')
                    block=response.read(BLOCK_BYTES)
                    if not block:break
                    received+=len(block)
                    if received>record['archive_bytes']:raise ValueError('视频组件下载大小不符。')
                    output.write(block);digest.update(block)
                    now=time.monotonic()
                    if now-last_progress>=.1 or received==record['archive_bytes']:
                        progress(phase='preparing',bytes=received,total=record['archive_bytes'],speed=received/max(.001,now-started));last_progress=now
        if received!=record['archive_bytes'] or digest.hexdigest()!=record['archive_sha256']:raise ValueError('视频组件下载校验失败，请重试。')
        with zipfile.ZipFile(archive) as bundle:
            matches=[row for row in bundle.infolist() if row.filename==record['member']]
            if len(matches)!=1 or matches[0].file_size!=record['bytes']:raise ValueError('视频组件归档内容不符。')
            with bundle.open(matches[0]) as source,tempfile.NamedTemporaryFile(prefix='.runtime-',dir=folder,delete=False) as output:
                executable=Path(output.name);temporary.append(executable);digest=hashlib.sha256()
                for block in iter(lambda:source.read(BLOCK_BYTES),b''):output.write(block);digest.update(block)
        if digest.hexdigest()!=record['sha256']:raise ValueError('视频组件可执行文件校验失败。')
        executable.chmod(0o755);os.replace(executable,target)
        return folder
    finally:
        for path in temporary:path.unlink(missing_ok=True)
        lock.unlock()
