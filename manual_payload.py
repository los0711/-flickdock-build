"""Stable manual-transfer snapshots and atomic delivery, independent of transport."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import stat
import zipfile

from selection_archive import _is_link,_source_reader
from transfer_priority import yield_bulk_payload

IO_BYTES=1024*1024
MAX_PAYLOAD_BYTES=(1<<63)-1
MAX_ENTRIES=100000
SHA256=re.compile('[a-f0-9]{64}')


def digest_file(path,cancel):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block:=stream.read(IO_BYTES):
            if cancel.is_set():raise InterruptedError('已取消传输。')
            digest.update(block)
    return digest.hexdigest()


def descriptor(value):
    if (not isinstance(value,dict) or set(value)!=set(('size','sha256'))
            or type(value.get('size')) is not int or not 0<value['size']<=MAX_PAYLOAD_BYTES
            or not isinstance(value.get('sha256'),str) or not SHA256.fullmatch(value['sha256'])):
        raise ValueError('文件内容描述无效。')
    return dict(value)


def prepare(paths,folder,cancel):
    from lan_transfer import _make_manifest
    entries,sources,total=_make_manifest(paths,cancel)
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False,mode=0o700)
    target=folder/'content.zip'
    try:
        with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for row in entries:
                if cancel.is_set():raise InterruptedError('已取消传输。')
                name=row['path']+('/' if row['kind']=='directory' else '')
                info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_STORED
                if row['kind']=='directory':archive.writestr(info,b'');continue
                source,size,modified=sources[row['path']]
                if _is_link(source):raise ValueError('原文件已变为符号链接，请重新选择。')
                copied=0
                with _source_reader(source) as incoming,archive.open(info,'w',force_zip64=True) as outgoing:
                    before=os.fstat(incoming.fileno())
                    if before.st_size!=size or before.st_mtime_ns!=modified:raise ValueError('原文件已改变，请重新发送。')
                    while block:=incoming.read(IO_BYTES):
                        yield_bulk_payload(cancel)
                        if cancel.is_set():raise InterruptedError('已取消传输。')
                        copied+=len(block)
                        if copied>size:raise ValueError('原文件已改变，请重新发送。')
                        outgoing.write(block)
                    after=os.fstat(incoming.fileno())
                if copied!=size or after.st_mtime_ns!=modified:raise ValueError('原文件已改变，请重新发送。')
        with target.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
        spec=descriptor(dict(size=target.stat().st_size,sha256=digest_file(target,cancel)))
        return dict(folder=folder,path=target,descriptor=spec,entries=entries,total=total,
                    names=[row['path'] for row in entries if '/' not in row['path']],
                    count=sum(row['kind']=='file' for row in entries))
    except BaseException:
        # Only the exact folder created above is owned by this call.
        if folder.is_dir() and not _is_link(folder):shutil.rmtree(folder)
        raise


def extract(payload,output,spec,*,total,count,names,cancel):
    from lan_transfer import _validate_manifest,_portable_path
    spec=descriptor(spec);payload=Path(payload);output=Path(output)
    if (_is_link(payload) or not payload.is_file() or payload.stat().st_size!=spec['size']
            or digest_file(payload,cancel)!=spec['sha256']):raise ValueError('接收文件完整性校验失败。')
    if output.exists():raise FileExistsError('接收解包目录已存在。')
    output.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(payload) as archive:
            infos=archive.infolist()
            if not 0<len(infos)<=MAX_ENTRIES:raise ValueError('接收文件数量无效。')
            entries=[]
            for info in infos:
                if (info.flag_bits&1 or info.compress_type!=zipfile.ZIP_STORED
                        or stat.S_ISLNK(info.external_attr>>16)):
                    raise ValueError('接收文件包含不支持的项目。')
                path=info.filename[:-1] if info.is_dir() else info.filename
                _portable_path(path)
                entries.append(dict(path=path,kind='directory' if info.is_dir() else 'file',size=info.file_size))
            actual=_validate_manifest(entries)
            roots=[row['path'] for row in entries if '/' not in row['path']]
            if actual!=total or sum(row['kind']=='file' for row in entries)!=count or roots[:12]!=names[:12]:
                raise ValueError('收到的文件与已确认的请求不一致。')
            for info,row in zip(infos,entries):
                if cancel.is_set():raise InterruptedError('已取消传输。')
                path=output.joinpath(*PurePosixPath(row['path']).parts)
                if row['kind']=='directory':path.mkdir();continue
                written=0
                with archive.open(info) as incoming,path.open('xb') as outgoing:
                    while block:=incoming.read(IO_BYTES):
                        if cancel.is_set():raise InterruptedError('已取消传输。')
                        written+=len(block)
                        if written>row['size']:raise ValueError('接收文件大小无效。')
                        outgoing.write(block)
                    outgoing.flush();os.fsync(outgoing.fileno())
                if written!=row['size']:raise ValueError('接收文件内容不完整。')
        return roots
    except BaseException:
        if output.is_dir() and not _is_link(output):shutil.rmtree(output)
        raise


def publish(output,destination,roots,cancel):
    from lan_transfer import _rename_noreplace,_portable_path
    output=Path(output);destination=Path(destination).resolve(strict=True)
    if _is_link(output) or _is_link(destination):raise ValueError('接收位置已改变。')
    reserved={path.name.casefold() for path in destination.iterdir()};moved=[]
    try:
        for name in roots:
            if cancel.is_set():raise InterruptedError('已取消传输。')
            if len(_portable_path(name).parts)!=1:raise ValueError('接收文件名无效。')
            source=output/name;stem,suffix=(name,'') if source.is_dir() else (source.stem,source.suffix)
            candidate=name;number=2
            while True:
                if candidate.casefold() in reserved:candidate=f'{stem} ({number}){suffix}';number+=1;continue
                try:_rename_noreplace(source,destination/candidate);break
                except FileExistsError:reserved.add(candidate.casefold())
            reserved.add(candidate.casefold());moved.append((source,destination/candidate))
        return [str(target) for _,target in moved]
    except BaseException:
        for source,target in reversed(moved):
            if target.exists() and not source.exists():_rename_noreplace(target,source)
        raise
