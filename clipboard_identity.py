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
MAX_IDENTITY_IMAGE_PIXELS=100_000_000
_images=OrderedDict()
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
