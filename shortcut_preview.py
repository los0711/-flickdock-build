"""Portable preview metadata; never execute or follow local shortcut targets."""
from pathlib import Path
import plistlib,re
from urllib.parse import urlsplit

SUFFIXES={'.url','.webloc','.lnk'}
def shortcut_details(path):
    path=Path(path)
    if path.suffix.lower() not in SUFFIXES:return None
    try:
        if path.stat().st_size>256*1024:return {'description':'快捷方式文件过大，保留原文件。'}
        data=path.read_bytes()
        value=''
        if path.suffix.lower()=='.webloc':
            value=plistlib.loads(data).get('URL','')
        elif path.suffix.lower()=='.url':
            text=data.decode('utf-16' if data[:2] in (b'\xff\xfe',b'\xfe\xff') else 'utf-8-sig',errors='replace')
            match=re.search(r'(?im)^URL\s*=\s*([^\r\n]+)',text)
            if match:value=match[1].strip()
        else:
            # MS Shell Link HeaderSize + LinkCLSID. Display readable target
            # information only; Windows local paths have no target on a Mac.
            if len(data)<76 or data[:4]!=b'L\0\0\0' or data[4:20]!=bytes.fromhex('0114020000000000c000000000000046'):return {'description':'Windows 快捷方式'}
            targets=[]
            for text in (data.decode('utf-16-le',errors='ignore'),data.decode('utf-8',errors='ignore')):
                targets+=re.findall(r'[A-Za-z]:\\[^\x00-\x1f]{1,1024}',text)
            return {'description':'Windows 快捷方式'+('\n目标：'+targets[0] if targets else '\n目标文件位于原设备。')}
        parsed=urlsplit(str(value))
        if parsed.scheme.lower() in ('http','https') and parsed.hostname and not parsed.username and not parsed.password and len(value)<=4096:
            return {'url':value,'description':value}
        return {'description':'网页快捷方式\n'+str(value)[:1024]}
    except (OSError,ValueError,TypeError,AttributeError,UnicodeError,plistlib.InvalidFileException):
        return {'description':'快捷方式\n无法解析目标，原文件仍可复制或拖出。'}
