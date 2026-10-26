"""Bounded disk work for shelf sync; never keeps an original open between jobs."""
from __future__ import annotations
import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import threading
from uuid import uuid4
import zipfile

from PySide6.QtCore import QObject, QRunnable, Signal
from selection_archive import _source_reader, _write_file, _is_link

# Overall service space is bounded; no separate 64 MB intake restriction.
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
DIRECTORY_MIME = 'application/x-flickdock-directory+zip'


def safe_name(name):
    # Same portable basename on Windows/macOS; never interpret a remote path.
    cleaned = ''.join('_' if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in str(name))
    cleaned = cleaned.rstrip(' .')[:160] or '未命名'
    if cleaned.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        cleaned = '_' + cleaned
    return cleaned


def fingerprint(ref, text=None, name=''):
    if text is not None:
        return hashlib.sha256((name+'\0'+text).encode('utf8')).hexdigest()
    try:
        info = Path(ref).stat()
        return f'{info.st_size}:{info.st_mtime_ns}:{stat.S_ISDIR(info.st_mode)}:{name}'
    except OSError:
        return 'missing:'+name


def stage_item(ref, text, name, directory, cancel, limit=MAX_FILE_BYTES):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    target = directory / (uuid4().hex + '.blob')
    try:
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        if text is not None:
            data = text.encode('utf8')
            if len(data) > limit: raise ValueError('这段文字超过云同步大小限制。')
            target.write_bytes(data); mime = 'text/plain'
        elif Path(ref).is_dir():
            mime = DIRECTORY_MIME
            count, total = 0, 0
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
                for folder, directories, files in os.walk(ref, followlinks=False):
                    if any(_is_link(Path(folder,d)) for d in directories):
                        raise ValueError('文件夹含有链接，请移除链接后同步。')
                    directories[:] = sorted(directories)
                    if Path(folder) != Path(ref):
                        archive.writestr(Path(folder).relative_to(ref).as_posix()+'/',b''); count += 1
                    if count > 10000: raise ValueError('文件夹超过云同步数量限制。')
                    for filename in sorted(files):
                        source = Path(folder,filename)
                        if cancel.is_set(): raise InterruptedError()
                        if _is_link(source): raise ValueError('文件夹含有链接，请移除链接后同步。')
                        total += source.stat().st_size; count += 1
                        if total > limit or count > 10000: raise ValueError('文件夹超过云同步大小或数量限制。')
                        _write_file(archive,source,source.relative_to(ref).as_posix(),cancel)
                        if target.stat().st_size > limit: raise ValueError('文件夹压缩包超过云同步限制。')
        else:
            total = 0
            with _source_reader(Path(ref)) as original, target.open('xb') as copy:
                while chunk := original.read(1024*1024):
                    if cancel.is_set(): raise InterruptedError()
                    total += len(chunk)
                    if total > limit: raise ValueError('此项超过当前云空间容量；原文件仍保留在本机。')
                    copy.write(chunk)
        digest = hashlib.sha256(); size = 0
        with target.open('rb') as stream:
            while chunk := stream.read(1024*1024):
                if cancel.is_set(): raise InterruptedError()
                digest.update(chunk); size += len(chunk)
                if size > limit: raise ValueError('此项超过云同步大小限制。')
        return {'path':str(target),'size':size,'sha256':digest.hexdigest(),'mime_type':mime}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def read_part(path, index, chunk_size, cancel):
    if cancel.is_set(): raise InterruptedError()
    with _source_reader(Path(path)) as stream:
        stream.seek(index*chunk_size); data = stream.read(chunk_size)
    return data, hashlib.sha256(data).hexdigest()


def assemble_parts(parts, target, expected, cancel):
    target = Path(target); staged = target.with_name('.'+uuid4().hex+'.part')
    digest = hashlib.sha256(); size = 0
    try:
        with staged.open('xb') as output:
            for part in parts:
                with Path(part).open('rb') as incoming:
                    while data := incoming.read(1024*1024):
                        if cancel.is_set(): raise InterruptedError()
                        size += len(data)
                        if size > expected['size']: raise ValueError('同步文件大小与记录不符。')
                        digest.update(data); output.write(data)
            output.flush(); os.fsync(output.fileno())
        if size != expected['size'] or digest.hexdigest() != expected['sha256']:
            raise ValueError('同步文件校验失败。')
        os.replace(staged,target)
        return target
    finally:
        staged.unlink(missing_ok=True)
        for part in parts: Path(part).unlink(missing_ok=True)


def read_utf8(path,cancel):
    if cancel.is_set(): raise InterruptedError()
    return Path(path).read_text(encoding='utf8')


def unpack_directory(source, destination, cancel, limit=MAX_FILE_BYTES):
    destination = Path(destination)
    stage = destination.with_name('.'+destination.name+'-'+uuid4().hex)
    stage.mkdir(parents=True)
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(x.file_size for x in entries) > limit:
                raise ValueError('同步文件夹超过解压限制。')
            seen = set()
            for item in entries:
                name = item.filename; relative = PurePosixPath(name)
                if (not name or '\\' in name or relative.is_absolute() or any(p in ('..','') for p in relative.parts)
                        or any(safe_name(p) != p for p in relative.parts) or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError('同步压缩包包含不安全的路径。')
                key = name.rstrip('/').casefold()
                if key in seen: raise ValueError('同步压缩包包含重复路径。')
                seen.add(key)
                if cancel.is_set(): raise InterruptedError()
                target = stage.joinpath(*relative.parts)
                if item.is_dir(): target.mkdir(parents=True,exist_ok=True); continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(item) as incoming, target.open('xb') as output:
                    total = 0
                    while data := incoming.read(1024*1024):
                        if cancel.is_set(): raise InterruptedError()
                        total += len(data)
                        if total > item.file_size: raise ValueError('同步压缩包大小无效。')
                        output.write(data)
        if destination.exists(): raise ValueError('同步目标已存在。')
        os.replace(stage,destination)
        return str(destination)
    finally:
        if stage.exists(): shutil.rmtree(stage)


class _WorkSignals(QObject):
    done = Signal(object, object)


class DiskWork(QRunnable):
    def __init__(self, function, *args):
        super().__init__()
        self.signals = _WorkSignals(); self.cancel = threading.Event()
        self.function, self.args = function, args

    def run(self):
        try: result, error = self.function(*self.args,self.cancel), None
        except Exception as problem: result,error = None,problem
        self.signals.done.emit(result,error)
