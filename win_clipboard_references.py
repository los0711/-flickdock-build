"""Owned Windows explicit-Paste helper. Never started by a clipboard listener.

Copy CF_HDROP memory while locked, then release before parsing; no filesystem
metadata, file bytes, clipboard write, account access or application startup.
Unadvertised/virtual aliases retain Qt's original URL interpretation, but any
foreign renderer that blocks it is confined to this deadline-owned process.
"""
from __future__ import annotations
import struct
import sys
from clipboard_references import MAX_REFERENCE_BYTES,checked_paths,checked_request,encode_frame,receipt
from win_clipboard_native import NativeClipboard,TooLarge,write_stdout

CF_HDROP=15
DROPFILES_HEADER_BYTES=20


def parse_hdrop(raw,*,ansi_codec='mbcs'):
    if not isinstance(raw,bytes) or not DROPFILES_HEADER_BYTES<=len(raw)<=MAX_REFERENCE_BYTES:
        raise ValueError('Invalid DROPFILES size')
    offset,_,_,_,wide=struct.unpack_from('<IiiII',raw)
    if not DROPFILES_HEADER_BYTES<=offset<len(raw) or wide not in (0,1):
        raise ValueError('Invalid DROPFILES header')
    data=raw[offset:]
    if wide:
        if offset%2 or len(data)%2:raise ValueError('Invalid wide alignment')
        end=next((i for i in range(0,len(data)-3,2) if data[i:i+4]==b'\0\0\0\0'),None)
        if end is None:raise ValueError('Missing reference terminator')
        text=data[:end].decode('utf-16-le','strict')
    else:
        end=data.find(b'\0\0')
        if end<0:raise ValueError('Missing reference terminator')
        text=data[:end].decode(ansi_codec,'strict')
    return checked_paths(text.split('\0'))


def qt_local_references():
    from PySide6.QtGui import QGuiApplication
    app=QGuiApplication(['FlickDock-reference-reader'])
    mime=app.clipboard().mimeData()
    if mime is None or not mime.hasUrls():return ()
    urls=mime.urls()
    if not urls or any(not url.isLocalFile() for url in urls):return ()
    return checked_paths([url.toLocalFile() for url in urls if url.toLocalFile()])


def capture(request,sequence,*,provider=None,fallback=None,ansi_codec='mbcs'):
    checked_request(request,sequence)
    provider=NativeClipboard() if provider is None else provider
    fallback=qt_local_references if fallback is None else fallback
    def result(status='unavailable',**fields):return receipt(request,sequence,status,**fields)
    raw=None;phase='copy'
    try:
        if provider.sequence()!=sequence:return result(code='CHANGED')
        if not provider.open():return result(code='BUSY')
        try:
            if provider.sequence()!=sequence:return result(code='CHANGED')
            if provider.available(CF_HDROP):raw=provider.read(CF_HDROP,MAX_REFERENCE_BYTES)
        finally:
            phase='close';provider.close();phase='copy'
    except TooLarge:return result(code='REFERENCE_LIMIT')
    except OSError:return result(code='CLOSE_UNAVAILABLE' if phase=='close' else 'READ_UNAVAILABLE')
    try:
        if provider.sequence()!=sequence:return result(code='CHANGED')
        paths=parse_hdrop(raw,ansi_codec=ansi_codec) if raw is not None else fallback()
        if not isinstance(paths,(tuple,list)):raise TypeError('Reference provider must return a list')
        if paths:paths=checked_paths(paths)
        if provider.sequence()!=sequence:return result(code='CHANGED')
    except (ValueError,UnicodeError):return result(code='INVALID_REFERENCES')
    except OSError:return result(code='READ_UNAVAILABLE')
    return result('paths',paths=paths) if paths else result('not_files')


def parse_arguments(args):
    if len(args)!=4 or args[::2]!=['--request','--sequence']:
        raise ValueError('Invalid reference helper arguments')
    request,number=args[1::2]
    if not 1<=len(number)<=10 or not number.isascii() or not number.isdecimal() or str(int(number))!=number:
        raise ValueError('Invalid reference counter')
    sequence=int(number);checked_request(request,sequence)
    return request,sequence


def main(args=None):
    # Validate before imports that can touch native state; private flags never
    # fall through into the normal application entry or user profile.
    try:request,sequence=parse_arguments(list(sys.argv[1:] if args is None else args))
    except (ValueError,TypeError):return 2
    if sys.platform!='win32':return 2
    value=capture(request,sequence)
    try:write_stdout([encode_frame(value)])
    except (OSError,ValueError):return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
