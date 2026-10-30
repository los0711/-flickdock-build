"""QR presentation for the existing short-lived code; no transport or URL launch."""
import re
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage,QPainter,QColor

CODE=re.compile(r'[23456789A-HJ-NP-Z]{8}\Z')


def parse_code(text):
    if not isinstance(text,str) or len(text)>256:raise ValueError('二维码不是有效的 FlickDock 接收码。')
    value=text.strip().upper()
    if CODE.fullmatch(value):return value
    url=urlsplit(text)
    query=parse_qs(url.query,strict_parsing=True)
    code=query.get('code',[])
    if (url.scheme!='flickdock' or url.netloc!='pair' or url.path not in ('','/')
            or url.fragment or set(query)!={'code'} or len(code)!=1 or not CODE.fullmatch(code[0])):
        raise ValueError('二维码不是有效的 FlickDock 接收码。')
    return code[0]


def qr_image(code):
    code=parse_code(code)
    import qrcode
    qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=8,border=4)
    qr.add_data('flickdock://pair?code='+code);qr.make(fit=True)
    matrix=qr.get_matrix();side=len(matrix);scale=8
    image=QImage(side*scale,side*scale,QImage.Format_RGB32);image.fill(Qt.white)
    painter=QPainter(image);painter.setPen(Qt.NoPen);painter.setBrush(QColor('black'))
    for y,row in enumerate(matrix):
        for x,ink in enumerate(row):
            if ink:painter.drawRect(x*scale,y*scale,scale,scale)
    painter.end();return image


def decode_qr(source,cancel):
    import zxingcpp
    if cancel.is_set():return None
    if isinstance(source,QImage):image=source
    else:
        from PySide6.QtGui import QImageReader
        path=Path(source)
        if path.stat().st_size>32*1024*1024:raise ValueError('二维码图片过大，请使用较小的图片。')
        reader=QImageReader(str(path));size=reader.size()
        if size.width()*size.height()>24_000_000:raise ValueError('二维码图片分辨率过大。')
        image=reader.read()
    if image.isNull():raise ValueError('无法读取二维码图片。')
    if image.width()*image.height()>24_000_000:raise ValueError('二维码图片分辨率过大。')
    result=zxingcpp.read_barcode(image,formats=zxingcpp.BarcodeFormat.QRCode)
    if cancel.is_set():return None
    if result is None:raise ValueError('没有找到接收码二维码，请换一张清晰的图片。')
    return parse_code(result.text)
