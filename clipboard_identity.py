"""Content identity across native clipboard representations, without rewriting data."""
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import re
import threading
from PySide6.QtCore import QBuffer,QIODevice
from PySide6.QtGui import QImage,QImageReader

IMAGE_IDENTITY_CACHE_ENTRIES=96
FILE_IDENTITY_CACHE_ENTRIES=96
MAX_FILE_IDENTITY_BYTES=20*1024*1024
MAX_IDENTITY_IMAGE_PIXELS=100_000_000
_images=OrderedDict()
_files=OrderedDict()
_lock=threading.Lock()
_fragment=re.compile(r'<!--\s*StartFragment\s*-->(.*?)<!--\s*EndFragment\s*-->',re.I|re.S)


def _image_signature(data):
    if 'raw' in data:
        raw=data['raw'];key=('encoded',hashlib.sha256(raw).digest())
    else:key=('native',data['image'].cacheKey())
    with _lock:
        cached=_images.get(key)
        if cached is not None:_images.move_to_end(key);return cached
    if 'raw' in data:
        buffer=QBuffer();buffer.setData(raw);buffer.open(QIODevice.ReadOnly)
        reader=QImageReader(buffer);size=reader.size()
        if not size.isValid() or size.width()*size.height()>MAX_IDENTITY_IMAGE_PIXELS:
            return ('image-encoded',key[1])
        image=reader.read()
    else:image=data['image']
    if image.isNull():return ('image-invalid',key)
    # Windows may render the same bitmap as DIB, PNG or premultiplied pixels.
    # Compare visible pixels in one representation; transport keeps originals.
    canonical=image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    value=('image',canonical.width(),canonical.height(),hashlib.sha256(canonical.constBits()).digest())
    with _lock:
        _images[key]=value;_images.move_to_end(key)
        while len(_images)>IMAGE_IDENTITY_CACHE_ENTRIES:_images.popitem(last=False)
    return value


def _text(value):return value.replace('\r\n','\n').replace('\r','\n')


def content_keys(value,*,read_files=False,cancel=None):
    """Replay identity, separate from file names/paths used by the actual copy.

    Remote clipboard bridges can materialize one bitmap as differently named
    PNG files on every round trip. Hash files only in a worker; the UI reads
    this bounded cache. Never alter the original payload to normalize it.
    """
    if value is None:return frozenset()
    kind,data=value
    if kind=='text':return frozenset((('text',hashlib.sha256(_text(data['text']).encode('utf8')).digest()),))
    if kind=='image':
        keys={signature(value)}
        if data.get('raw'):keys.add(('bytes',len(data['raw']),hashlib.sha256(data['raw']).digest()))
        return frozenset(keys)
    if kind!='files' or len(data)!=1:return frozenset()
    path=Path(data[0])
    try:
        before=path.stat()
        if not path.is_file() or not 0<before.st_size<MAX_FILE_IDENTITY_BYTES:return frozenset()
        stamp=(os.path.realpath(path),before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
        with _lock:
            cached=_files.get(stamp)
            if cached is not None:_files.move_to_end(stamp);return cached
        if not read_files:return None
        raw=bytearray()
        with path.open('rb') as stream:
            while chunk:=stream.read(256*1024):
                if cancel is not None and cancel.is_set():raise InterruptedError()
                raw.extend(chunk)
                if len(raw)>=MAX_FILE_IDENTITY_BYTES:raise OSError('Clipboard file changed during identity read')
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise OSError('Clipboard file changed during identity read')
        keys={('bytes',len(raw),hashlib.sha256(raw).digest())}
        if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'):
            image_key=_image_signature({'raw':bytes(raw)})
            if image_key[0]=='image':keys.add(image_key)
        result=frozenset(keys)
        with _lock:
            _files[stamp]=result;_files.move_to_end(stamp)
            while len(_files)>FILE_IDENTITY_CACHE_ENTRIES:_files.popitem(last=False)
        return result
    except OSError:
        if read_files:raise
        return frozenset()


def signature(value):
    if value is None:return None
    kind,data=value
    if kind=='image':return _image_signature(data)
    if kind=='files':
        versions=[]
        for path in data:
            # Cocoa can return /private/var for a file written through /var,
            # or resolve another filesystem alias when exporting a file URL.
            canonical=os.path.normcase(os.path.realpath(path))
            try:info=Path(path).stat();versions.append((canonical,info.st_size,info.st_mtime_ns))
            except OSError:versions.append((canonical,None,None))
        return kind,tuple(versions)
    html=data.get('html','');fragment=_fragment.search(html)
    return kind,_text(data['text']),_text(fragment.group(1) if fragment else html)
