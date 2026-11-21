"""Persistent local clipboard history. All database and image work is off the GUI thread."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


PAGE = 12
MAX_ITEM_BYTES = 20*1024*1024
MAX_IMAGE_PIXELS = 24*1024*1024


def encode_png(image):
    data=QByteArray()
    buffer=QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer,'PNG'):
        raise ValueError('无法保存剪贴板图片')
    return bytes(data)


def decode_history_image(payload):
    image=QImage.fromData(payload)
    if not image.isNull() or not payload or sys.platform!='darwin':
        return image
    # Old histories may contain native TIFF/HEIC from the pasteboard. The
    # installed Qt build need not ship the same image plugins as its writer.
    import objc
    from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
    from Foundation import NSData
    with objc.autorelease_pool():
        data=NSData.dataWithBytes_length_(payload,len(payload))
        bitmap=NSBitmapImageRep.imageRepWithData_(data)
        if bitmap is None or not 0 < bitmap.pixelsWide()*bitmap.pixelsHigh() <= MAX_IMAGE_PIXELS:
            return QImage()
        png=bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG,{})
        return QImage.fromData(bytes(png)) if png is not None else QImage()


class ClipboardHistoryStore:
    def __init__(self,root):
        self.root=Path(root)
        self.path=self.root/'clipboard-history.sqlite3'
        self.files=self.root/'clipboard-history-files'

    def _snapshot(self,value):
        if not value:return None
        kind,data=value
        if kind=='text':
            text=data['text']
            title=next((line.strip() for line in text[:2048].splitlines() if line.strip()),'')
            if not title:return None
            html=data.get('html','')
            if len(text)+len(html)>MAX_ITEM_BYTES:return None
            payload=json.dumps({'text':text,'html':html},ensure_ascii=False).encode('utf-8')
            try:
                parsed=urlsplit(title) if title==text.strip() and len(title)<8192 else None
            except ValueError:
                parsed=None
            if parsed is not None and parsed.scheme in ('https','http') and parsed.hostname:
                kind,title='link',parsed.hostname
            return kind,title[:160],text[:640],payload,b'',''
        if kind=='image':
            raw=data.get('raw')
            image=data.get('image') or decode_history_image(raw or b'')
            if image.isNull():return None
            # Keep a portable lossless copy when it fits. Older records and
            # large images already in native formats can still be read.
            payload=raw if raw and raw.startswith(b'\x89PNG\r\n\x1a\n') else encode_png(image)
            if len(payload)>MAX_ITEM_BYTES and raw and len(raw)<=MAX_ITEM_BYTES:payload=raw
            pixels=image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
            digest=hashlib.sha256(pixels.constBits()).hexdigest()
            fingerprint=f'{pixels.width()}x{pixels.height()}:{digest}'
            return ('image','剪贴板图片',f'{image.width()} × {image.height()}',
                    payload,encode_png(image.scaled(300,196,Qt.KeepAspectRatio,Qt.SmoothTransformation)),fingerprint)
        if kind=='files':
            paths=[str(path) for path in data
                   if Path(path).is_absolute() and 'clipboard-cache' not in Path(path).parts]
            if not paths:return None
            title=Path(paths[0]).name if len(paths)==1 else f'{len(paths)} 个文件'
            return 'file',title[:160],'\n'.join(paths)[:640],json.dumps(paths).encode('utf-8'),b'',''
        return None

    @staticmethod
    def _keep_file(source,target):
        try:
            return os.link(source,target)
        except OSError:
            return shutil.copy2(source,target)

    def _remember_cloud_files(self,db,identity,paths,directory):
        source_root=(Path(directory)/'files').resolve()
        if source_root.parent.parent!= (self.root/'clipboard-cache').resolve():return None
        paths=[Path(path) for path in paths]
        if not paths or any(path.is_symlink() or path.is_junction()
                            or path.resolve().parent!=source_root or not path.exists()
                            for path in paths):return None
        key=hashlib.sha256(b'cloud-files\0'+identity.encode('utf-8')).hexdigest()
        target_root=self.files/key
        retained=[str(target_root/path.name) for path in paths]
        title=paths[0].name if len(paths)==1 else f'{len(paths)} 个文件'
        payload=json.dumps(retained,ensure_ascii=False).encode('utf-8')
        if not target_root.is_dir():
            self.files.mkdir(mode=0o700,parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='receiving-',dir=self.files) as temporary:
                staging=Path(temporary)/'files'
                staging.mkdir()
                for source in paths:
                    target=staging/source.name
                    if source.is_dir():shutil.copytree(source,target,copy_function=self._keep_file)
                    else:self._keep_file(source,target)
                staging.rename(target_root)
        db.execute('''INSERT INTO clips VALUES (?,?,?,?,?,?,?,0)
            ON CONFLICT(id) DO UPDATE SET updated=excluded.updated''',
            (key,'file',title[:160],'\n'.join(path.name for path in paths)[:640],payload,b'',time.time()))
        return key

    def _remove_retained_files(self,keys):
        for key in keys:
            if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):continue
            directory=self.files/key
            if directory.is_dir():shutil.rmtree(directory)

    def _file_payload(self,key,paths):
        retained=self.files/key
        result=[]
        for original in paths:
            path=Path(original)
            relocated=retained/path.name
            if path.parts[-3:-1]==(self.files.name,key) and relocated.exists():
                result.append(str(relocated))
            else:
                result.append(original)
        return result

    def execute(self,operation,*arguments):
        snapshot=self._snapshot(arguments[0]) if operation=='remember' else None
        if operation=='remember' and (snapshot is None or len(snapshot[3])>MAX_ITEM_BYTES):return None
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA secure_delete=ON')
            db.execute('''CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, excerpt TEXT NOT NULL,
                payload BLOB NOT NULL, thumbnail BLOB NOT NULL, updated REAL NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS clips_order ON clips (pinned DESC, updated DESC)')
            db.execute('CREATE TABLE IF NOT EXISTS clip_images (fingerprint TEXT PRIMARY KEY, clip_id TEXT NOT NULL)')
            if operation=='remember_cloud_files':
                return self._remember_cloud_files(db,*arguments)
            if operation=='remember':
                kind,title,excerpt,payload,thumb,fingerprint=snapshot
                key=hashlib.sha256(kind.encode()+b'\0'+payload).hexdigest()
                if fingerprint:
                    existing=db.execute('SELECT clip_id FROM clip_images JOIN clips ON clips.id=clip_images.clip_id WHERE fingerprint=?',(fingerprint,)).fetchone()
                    if existing:key=existing[0]
                db.execute('''INSERT INTO clips VALUES (?,?,?,?,?,?,?,0)
                    ON CONFLICT(id) DO UPDATE SET updated=excluded.updated''',
                    (key,kind,title,excerpt,payload,thumb,time.time()))
                if fingerprint:db.execute('INSERT OR REPLACE INTO clip_images VALUES (?,?)',(fingerprint,key))
                return key
            if operation in ('list','page'):
                rows=[dict(row) for row in db.execute('''SELECT id,kind,title,excerpt,thumbnail,updated,pinned
                    FROM clips ORDER BY pinned DESC,updated DESC,id LIMIT ? OFFSET ?''',arguments)]
                return (rows,db.execute('SELECT COUNT(*) FROM clips').fetchone()[0]) if operation=='page' else rows
            if operation=='pin':
                db.execute('UPDATE clips SET pinned=NOT pinned WHERE id=?',arguments)
                return True
            if operation=='remove':
                db.execute('DELETE FROM clip_images WHERE clip_id=?',arguments)
                db.execute('DELETE FROM clips WHERE id=?',arguments)
                db.commit()
                self._remove_retained_files(arguments)
                return True
            if operation=='clear':
                files=[row[0] for row in db.execute("SELECT id FROM clips WHERE kind='file'")]
                db.execute('DELETE FROM clip_images')
                db.execute('DELETE FROM clips')
                db.commit()
                self._remove_retained_files(files)
                return True
            if operation=='payload':
                row=db.execute('SELECT kind,payload FROM clips WHERE id=?',arguments).fetchone()
                if row is None:return None
                kind,payload=row
                if kind=='image':return kind,payload
                value=json.loads(payload)
                return kind,self._file_payload(arguments[0],value) if kind=='file' else value
            raise ValueError('未知的历史操作')
