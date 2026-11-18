"""Bounded, local translator history. All disk reads/writes run off the GUI."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from uuid import uuid4

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, Qt

from account_store import atomic_json
from shelf_journal import ShelfJournal
from sync_files import DiskWork
from translation_languages import TARGETS

MAX_HISTORY = 200
MAX_HISTORY_CHARS = 1_000_000
MAX_STORE_BYTES = 8 * 1024 * 1024
LEGACY_PRESETS = (
    ('expand', '扩写', '适当扩写当前译文，补充自然的衔接，保留原意，不编造事实。'),
    ('shorten', '精简', '精简当前译文，删去冗余修饰，保留核心事实。'),
    ('business', '商务', '使用专业、得体、礼貌的商务沟通语气。'),
    ('friendly', '亲切', '使用亲切友好、自然平易的语气。'),
    ('natural', '地道', '使用目标语言母语者自然地道的表达，保持原意。'),
    ('formal', '正式', '使用适合报告、正式信函的书面表达。'),
)
DEFAULT_PRESETS = (
    ('expand', '扩写', '用当前文本的语言适当扩写，补充自然的衔接，保留原意和事实。'),
    ('shorten', '精简', '用当前文本的语言精简表达，删去冗余修饰，保留核心事实。'),
    ('business', '商务', '保留当前文本的语言，改为专业、得体、礼貌的商务表达。'),
    ('friendly', '亲切', '保留当前文本的语言，改为亲切友好、平实自然的表达。'),
    ('natural', '地道', '保留当前文本的语言，用该语言自然、地道的用词和句式改写，保持原意。'),
    ('formal', '正式', '保留当前文本的语言，改为适合报告或正式信函的书面表达。'),
)
PRESET_UPDATES={(key,label,prompt):updated for (key,label,prompt),(_,_,updated) in zip(LEGACY_PRESETS,DEFAULT_PRESETS)}


def defaults():
    return dict(schema_version=1, source='auto', target='en', debounce_ms=400,
                live=True, pinned=False, tts_rate=1., tts_pitch=1., tts_voices={'source':{},'target':{}}, tts_auto=False,
                favorites=['zh', 'en', 'ja'], source_favorites=['zh', 'en', 'ja'], recent=['en', 'zh', 'ja', 'ko'],
                presets=[dict(id=key, label=label, instruction=prompt) for key, label, prompt in DEFAULT_PRESETS],
                glossary=['FlickDock'], history=[])


def _text(value, maximum):
    return value if isinstance(value, str) and len(value) <= maximum and '\0' not in value else ''


def normalize(document):
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise ValueError('Unsupported translator history format')
    result = defaults()
    for key in ('source', 'target'):
        value = document.get(key)
        if isinstance(value,str) and (value in TARGETS or key == 'source' and value == 'auto'):result[key] = value
    for key in ('live', 'pinned', 'tts_auto'):
        if isinstance(document.get(key), bool):result[key] = document[key]
    for key,low,high in (('tts_rate',.5,2.),('tts_pitch',.8,1.2)):
        value=document.get(key)
        if isinstance(value,(int,float)) and not isinstance(value,bool) and low<=value<=high:result[key]=float(value)
    saved_voices = document.get('tts_voices')
    if isinstance(saved_voices, dict):
        for side, preferences in result['tts_voices'].items():
            saved_side = saved_voices.get(side)
            if not isinstance(saved_side, dict):
                continue
            for code in TARGETS:
                row = saved_side.get(code)
                if isinstance(row, dict) and _voice_id(row.get('id')):
                    preferences[code] = dict(id=row['id'], name=_text(row.get('name'), 120))
    elif _voice_id(document.get('tts_voice')):
        # A legacy shared choice belongs to the saved target only. The
        # catalog validates it before it can be used.
        result['tts_voices']['target'][result['target']] = dict(id=document['tts_voice'], name='')
    delay = document.get('debounce_ms')
    if isinstance(delay, int) and not isinstance(delay, bool) and 100 <= delay <= 5000:result['debounce_ms'] = delay
    for key in ('favorites', 'source_favorites', 'recent'):
        values = document.get(key)
        if isinstance(values, list):
            result[key] = list(dict.fromkeys(v for v in values if isinstance(v, str) and v in TARGETS))
            if key == 'recent':result[key] = result[key][:30]
    if 'source_favorites' not in document:
        # Older profiles had one shared list. Seed both sides with its choices.
        result['source_favorites'] = result['favorites'].copy()
    values = document.get('glossary')
    if isinstance(values, list):result['glossary'] = list(dict.fromkeys(v for v in values if _text(v, 80).strip()))[:100]
    for key, limit, fields in (('presets', 40, {'id':80, 'label':24, 'instruction':1000}),):
        values = document.get(key)
        if isinstance(values, list):
            result[key] = [];seen = set()
            for row in values[:limit]:
                if not isinstance(row, dict):continue
                item = {field:_text(row.get(field), maximum).strip() for field, maximum in fields.items()}
                if all(item.values()) and item['id'] not in seen:
                    item['instruction']=PRESET_UPDATES.get((item['id'],item['label'],item['instruction']),item['instruction'])
                    result[key].append(item);seen.add(item['id'])
    result['history'] = [];characters = 0;seen = set()
    values = document.get('history')
    for row in values[:MAX_HISTORY] if isinstance(values, list) else ():
        if not isinstance(row, dict):continue
        item = {key:_text(row.get(key), maximum) for key, maximum in (
            ('id',80), ('source_text',16000), ('text',24000), ('original',24000), ('created_at',40))}
        if not all(item[key] for key in ('id', 'source_text', 'text', 'created_at')) or item['id'] in seen:continue
        source,target=row.get('source'),row.get('target')
        if not isinstance(source,str) or not isinstance(target,str) or source not in TARGETS and source!='auto' or target not in TARGETS:continue
        amount = sum(len(item[key]) for key in ('source_text', 'text', 'original'))
        if characters + amount > MAX_HISTORY_CHARS:continue
        characters += amount;seen.add(item['id'])
        item.update(source=row['source'], target=row['target'], favorite=row.get('favorite') is True)
        result['history'].append(item)
    return result


def _voice_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value))


def _read(path, cancel):
    if not path.exists():return defaults()
    with path.open('rb') as stream:raw = stream.read(MAX_STORE_BYTES + 1)
    if cancel.is_set():raise InterruptedError()
    if len(raw) > MAX_STORE_BYTES:raise ValueError('Translator history exceeds bounded size')
    return normalize(json.loads(raw.decode('utf-8')))


class TranslatorStore(QObject):
    loaded = Signal()
    changed = Signal()
    error = Signal(str)

    def __init__(self, base, parent=None, *, autoload=True):
        super().__init__(parent)
        self.path = Path(base) / 'translator.json'
        self.data = defaults();self.ready = not autoload;self.writable = True;self.closed = False
        self.pool = QThreadPool(self);self.pool.setMaxThreadCount(1);self.job = None
        self.journal = ShelfJournal(atomic_json);self.journal.setParent(self)
        self.journal.failed.connect(self._write_failed)
        self.timer = QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(120);self.timer.timeout.connect(self._commit)
        if autoload:QTimer.singleShot(0, self, self._load)

    def _load(self):
        if self.closed:return
        path = self.path
        self.job = DiskWork(lambda cancel:_read(path, cancel))
        self.job.signals.done.connect(self._loaded,Qt.QueuedConnection)
        self.pool.start(self.job)

    def _loaded(self, value, error):
        self.job = None
        if self.closed:return
        self.ready = True
        if error is not None:
            self.writable = False
            self.error.emit('翻译记录读取失败，原文件已保留。本次会话暂不保存，请检查磁盘或恢复备份。')
        else:self.data = value
        self.loaded.emit();self.changed.emit()

    def save(self):
        if self.closed or not self.ready:return
        self.changed.emit()
        if self.writable:self.timer.start()

    def _commit(self):
        if self.writable and self.ready:self.journal.submit(self.path, self.data)

    def _write_failed(self, path, error_type):
        self.error.emit('翻译记录未能保存，请检查磁盘空间和目录权限；现有记录未被覆盖。')

    def update_preferences(self, **values):
        for key, value in values.items():
            if key not in ('source','target','debounce_ms','live','pinned','favorites','source_favorites','recent','tts_rate','tts_pitch','tts_auto'):raise ValueError('Unknown translator preference')
            self.data[key] = value
        self.save()

    def favorites_for(self, side):
        return self.data['source_favorites' if side=='source' else 'favorites']

    def toggle_language_favorite(self, side, code):
        if code not in TARGETS:return
        favorites=self.favorites_for(side)
        if code in favorites:favorites.remove(code)
        else:favorites.append(code)
        self.save()

    def voice_for(self, side, language):
        return self.data['tts_voices'][side].get(language, {})

    def set_voice(self, side, language, voice_id, name=''):
        if language not in TARGETS or (voice_id and not _voice_id(voice_id)):
            raise ValueError('Invalid speech preference')
        choices = self.data['tts_voices'][side]
        if voice_id:
            choices[language] = dict(id=voice_id, name=_text(name, 120))
        else:
            choices.pop(language, None)
        self.save()

    def remember(self, record):
        rows = self.data['history']
        previous = next((row for row in rows if row['source_text'] == record['source_text'] and row['target'] == record['target']), None)
        entry = deepcopy(record);entry['id'] = previous['id'] if previous else str(uuid4())
        entry['favorite'] = entry.get('favorite', previous['favorite'] if previous else False)
        rows[:] = [entry] + [row for row in rows if row is not previous]
        # Bound by content as well as count; one very long translation cannot
        # turn every subsequent history update into an unbounded snapshot.
        self.data['history'] = normalize(self.data)['history']
        self.save();return entry['id']

    def close(self):
        if self.closed:return
        self.timer.stop();self._commit();self.closed = True
        if self.job:self.job.cancel.set()
        self.pool.clear();self.pool.waitForDone(1500)
        if not self.journal.flush(2.):
            import logging
            logging.error('Translator history disk lane exceeded shutdown deadline')
