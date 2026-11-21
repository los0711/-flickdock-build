"""Persistent local clipboard history. All database and image work is off the GUI thread."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


PAGE = 12
MAX_ITEM_BYTES = 20*1024*1024


def encode_png(image):
    data=QByteArray()
    buffer=QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer,'PNG'):
        raise ValueError('无法保存剪贴板图片')
    return bytes(data)


class ClipboardHistoryStore:
    def __init__(self,root):
        self.root=Path(root)
        self.path=self.root/'clipboard-history.sqlite3'

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
            return kind,title[:160],text[:640],payload,b''
        if kind=='image':
            raw=data.get('raw')
            if raw and len(raw)>MAX_ITEM_BYTES:return None
            image=data.get('image') or QImage.fromData(raw or b'')
            if image.isNull():return None
            return ('image','剪贴板图片',f'{image.width()} × {image.height()}',
                    raw or encode_png(image),encode_png(image.scaled(300,196,Qt.KeepAspectRatio,Qt.SmoothTransformation)))
        if kind=='files':
            paths=[str(path) for path in data
                   if Path(path).is_absolute() and 'clipboard-cache' not in Path(path).parts]
            if not paths:return None
            title=Path(paths[0]).name if len(paths)==1 else f'{len(paths)} 个文件'
            return 'file',title[:160],'\n'.join(paths)[:640],json.dumps(paths).encode('utf-8'),b''
        return None

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
            if operation=='remember':
                kind,title,excerpt,payload,thumb=snapshot
                key=hashlib.sha256(kind.encode()+b'\0'+payload).hexdigest()
                db.execute('''INSERT INTO clips VALUES (?,?,?,?,?,?,?,0)
                    ON CONFLICT(id) DO UPDATE SET updated=excluded.updated''',
                    (key,kind,title,excerpt,payload,thumb,time.time()))
                return key
            if operation in ('list','page'):
                rows=[dict(row) for row in db.execute('''SELECT id,kind,title,excerpt,thumbnail,updated,pinned
                    FROM clips ORDER BY pinned DESC,updated DESC,id LIMIT ? OFFSET ?''',arguments)]
                return (rows,db.execute('SELECT COUNT(*) FROM clips').fetchone()[0]) if operation=='page' else rows
            if operation=='pin':
                db.execute('UPDATE clips SET pinned=NOT pinned WHERE id=?',arguments)
                return True
            if operation=='remove':
                db.execute('DELETE FROM clips WHERE id=?',arguments)
                return True
            if operation=='clear':
                db.execute('DELETE FROM clips')
                return True
            if operation=='payload':
                row=db.execute('SELECT kind,payload FROM clips WHERE id=?',arguments).fetchone()
                if row is None:return None
                kind,payload=row
                return kind, payload if kind=='image' else json.loads(payload)
            raise ValueError('未知的历史操作')
