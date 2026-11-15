"""Bound and normalize untrusted cover data in the isolated media process."""
import base64
from PySide6.QtCore import QByteArray,QBuffer,QIODevice,QSize,Qt
from PySide6.QtGui import QImageReader

MAX_COVER_BYTES=4*1024*1024
MAX_COVER_PIXELS=20_000_000
MAX_COVER_SIDE=1280
MAX_ENCODED_COVER_BYTES=512*1024
RASTER_FORMATS={b'jpeg',b'jpg',b'png',b'webp',b'gif',b'avif'}

def normalize_cover(data):
    if not data or len(data)>MAX_COVER_BYTES:raise ValueError('视频封面为空或超过 4 MB。')
    source=QBuffer();source.setData(QByteArray(data));source.open(QIODevice.ReadOnly)
    try:
        reader=QImageReader(source);reader.setAutoTransform(True)
        natural=reader.size()
        if bytes(reader.format()).lower() not in RASTER_FORMATS or not natural.isValid() or natural.width()*natural.height()>MAX_COVER_PIXELS:
            raise ValueError('视频封面的格式或尺寸不受支持。')
        if max(natural.width(),natural.height())>MAX_COVER_SIDE:
            reader.setScaledSize(natural.scaled(QSize(MAX_COVER_SIDE,MAX_COVER_SIDE),Qt.KeepAspectRatio))
        image=reader.read()
        if image.isNull():raise ValueError('视频封面解码失败。')
    finally:source.close()
    encoded=QByteArray();output=QBuffer(encoded);output.open(QIODevice.WriteOnly)
    try:
        if not image.save(output,'JPEG',82):raise ValueError('无法生成视频封面预览。')
    finally:output.close()
    if len(encoded)>MAX_ENCODED_COVER_BYTES:raise ValueError('视频封面预览超过大小限制。')
    return dict(image=base64.b64encode(bytes(encoded)).decode('ascii'),width=image.width(),height=image.height())
