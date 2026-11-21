"""Qt's Windows virtual-file formats, materialized outside the GUI process."""
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
import struct
import subprocess
import sys
import time

DESCRIPTOR_MIME='application/x-qt-windows-mime;value="FileGroupDescriptorW"'
CONTENT_MIME='application/x-qt-windows-mime;value="FileContents";index=0'
DESCRIPTOR_BYTES=592
FILE_SIZE_FLAG=0x40
MAX_FILE_BYTES=20*1024*1024
WORKER_TIMEOUT_SECONDS=45
MAX_REPLY_BYTES=8192
REMOTE_PLACEHOLDER_NAME=re.compile(r'\.uuremote_[A-Za-z0-9_-]{8,128}\Z',re.IGNORECASE)


def remote_placeholder(name):
    """UU uses these reserved names as transfer tokens, never file contents."""
    return REMOTE_PLACEHOLDER_NAME.fullmatch(str(name).replace('\\','/').rsplit('/',1)[-1]) is not None


def only_remote_placeholders(mime):
    urls=mime.urls() if mime.hasUrls() else []
    return bool(urls) and all(url.isLocalFile() and remote_placeholder(url.toLocalFile()) for url in urls)


class VirtualClipboardError(ValueError):
    def __init__(self,code):super().__init__(code);self.code=code


@dataclass(frozen=True)
class VirtualFile:
    name:str
    size:int
    sha256:str


def describe(mime):
    if not mime.hasFormat(DESCRIPTOR_MIME):return None
    raw=bytes(mime.data(DESCRIPTOR_MIME))
    if len(raw)<4:raise VirtualClipboardError('VIRTUAL_FILE_NOT_READY')
    count=struct.unpack_from('<I',raw)[0]
    if count!=1:raise VirtualClipboardError('VIRTUAL_FILE_SINGLE_ONLY')
    if len(raw)!=4+DESCRIPTOR_BYTES:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_DESCRIPTOR')
    flags=struct.unpack_from('<I',raw,4)[0];attributes=struct.unpack_from('<I',raw,40)[0]
    if not flags&FILE_SIZE_FLAG or flags&4 and attributes&16:raise VirtualClipboardError('VIRTUAL_FILE_SIZE_UNKNOWN')
    high,low=struct.unpack_from('<II',raw,68);size=(high<<32)|low
    if size>=MAX_FILE_BYTES:raise VirtualClipboardError('VIRTUAL_FILE_SIZE_LIMIT')
    try:name=raw[76:596].decode('utf-16-le').split('\0',1)[0]
    except UnicodeError as error:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_NAME') from error
    if remote_placeholder(name):raise VirtualClipboardError('REMOTE_FILE_PLACEHOLDER')
    from sync_files import safe_name
    try:
        if safe_name(name)!=name:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_NAME')
    except ValueError as error:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_NAME') from error
    return VirtualFile(name,size,hashlib.sha256(raw).hexdigest())


def materialize(mime,directory,expected,sequence):
    descriptor=describe(mime)
    if descriptor is None or descriptor.sha256!=expected['descriptor'] or sequence()!=expected['sequence']:
        raise VirtualClipboardError('VIRTUAL_FILE_REPLACED')
    data=bytes(mime.data(CONTENT_MIME))
    if len(data)!=descriptor.size or len(data)>=MAX_FILE_BYTES:raise VirtualClipboardError('VIRTUAL_FILE_INCOMPLETE')
    if sequence()!=expected['sequence']:raise VirtualClipboardError('VIRTUAL_FILE_REPLACED')
    target=Path(directory)/descriptor.name
    with target.open('xb') as stream:stream.write(data)
    return dict(ok=True,name=descriptor.name,size=len(data),sha256=hashlib.sha256(data).hexdigest())


def capture(directory,descriptor,sequence,cancel):
    directory=Path(directory);job=directory/'.virtual-job.json'
    job.write_text(json.dumps(dict(sequence=sequence,descriptor=descriptor.sha256)),encoding='utf8')
    command=[sys.executable,'--virtual-clipboard-worker',str(job)] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__)),str(job)]
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    deadline=time.monotonic()+WORKER_TIMEOUT_SECONDS;published=False
    try:
        while True:
            if cancel.is_set():raise InterruptedError('Virtual clipboard capture cancelled')
            if time.monotonic()>=deadline:raise VirtualClipboardError('VIRTUAL_FILE_TIMEOUT')
            try:output,error=process.communicate(timeout=.05);break
            except subprocess.TimeoutExpired:continue
        if len(output)>MAX_REPLY_BYTES:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_REPLY')
        result=json.loads(output)
        if not isinstance(result,dict):raise VirtualClipboardError('VIRTUAL_FILE_INVALID_REPLY')
        if not result.get('ok'):raise VirtualClipboardError(result.get('code','VIRTUAL_FILE_READ_FAILED'))
        if process.returncode or result.get('name')!=descriptor.name or result.get('size')!=descriptor.size:
            raise VirtualClipboardError('VIRTUAL_FILE_INVALID_REPLY')
        target=directory/descriptor.name
        if target.is_symlink() or target.stat().st_size!=descriptor.size:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_REPLY')
        with target.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=result.get('sha256'):raise VirtualClipboardError('VIRTUAL_FILE_INVALID_REPLY')
        published=True;return dict(ok=True,path=str(target),sha256=digest)
    except (OSError,ValueError) as error:
        return dict(ok=False,code=error.code if isinstance(error,VirtualClipboardError) else type(error).__name__.upper())
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=3)
        target=directory/descriptor.name
        if not published and target.is_file() and not target.is_symlink() and target.resolve().parent==directory.resolve():target.unlink()
        job.unlink(missing_ok=True)


def run(path):
    try:
        job=Path(path).resolve(strict=True);directory=job.parent
        if job.name!='.virtual-job.json' or directory.parent.name!='clipboard-cache' or not directory.name.startswith('clip-'):
            raise VirtualClipboardError('VIRTUAL_FILE_INVALID_JOB')
        if any(part.is_symlink() or part.is_junction() for part in (Path(path),Path(path).parent,directory.parent)):
            raise VirtualClipboardError('VIRTUAL_FILE_INVALID_JOB')
        if json.loads((directory/'.lease.json').read_bytes()).get('flickdock_clipboard')!=1:
            raise VirtualClipboardError('VIRTUAL_FILE_INVALID_JOB')
        if job.stat().st_size>MAX_REPLY_BYTES:raise VirtualClipboardError('VIRTUAL_FILE_INVALID_JOB')
        expected=json.loads(job.read_bytes())
        if set(expected)!={'sequence','descriptor'} or type(expected['sequence']) is not int or not 0<=expected['sequence']<=0xffffffff:
            raise VirtualClipboardError('VIRTUAL_FILE_INVALID_JOB')
        import ctypes
        from PySide6.QtGui import QGuiApplication
        app=QGuiApplication.instance() or QGuiApplication([])
        clipboard=app.clipboard()
        result=materialize(clipboard.mimeData(),directory,expected,lambda:int(ctypes.windll.user32.GetClipboardSequenceNumber()))
        print(json.dumps(result),flush=True);return 0
    except (OSError,ValueError,RuntimeError,AttributeError) as error:
        print(json.dumps(dict(ok=False,code=error.code if isinstance(error,VirtualClipboardError) else type(error).__name__.upper())),flush=True)
        return 1


if __name__=='__main__':raise SystemExit(run(sys.argv[1]))
