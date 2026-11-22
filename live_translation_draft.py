"""Incremental, in-memory translation for a single active editor."""
from PySide6.QtCore import QObject, QTimer, Signal

from translation_api import MAX_TRANSLATION_CHARS
from translation_document import TranslationDocument
from translation_languages import MAX_TEXT_CHARS, TARGETS, quick_content_language


class LiveDraft(QObject):
    changed = Signal()

    # A half-typed word is neither a useful request nor a useful preview.
    # Short pauses send an updated draft; further edits overwrite the queued draft.
    PAUSE_MS = 180
    CONTEXT_CHARS = 65

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.document = TranslationDocument()
        self.text = self.result = self.error = ''
        self.target = 'en'
        self.generation = 0
        self.pending = None
        self.queued = False
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self._ready)
        api.units_ready.connect(self._completed)

    @property
    def loading(self):
        return self.pending is not None or self.timer.isActive()

    @property
    def ready(self):
        return bool(self.result and not self.error and self.document.matches(self.text, self.result))

    def reset(self):
        self.generation += 1
        self.api.cancel()
        self.pending = None
        self.queued = False
        self.timer.stop()
        self.document = TranslationDocument()
        self.text = self.result = self.error = ''
        self.changed.emit()

    def set_target(self, code):
        if code not in TARGETS or code == self.target:
            return
        text = self.text
        self.target = code
        self.reset()
        if text:
            self.edit(text, immediate=True)

    def edit(self, text, *, immediate=False):
        if text == self.text:
            return
        if not text.strip():
            self.reset()
            return
        self.text, self.error = text, ''
        if '\0' in text or len(text) > MAX_TEXT_CHARS:
            self.error = '输入过长或含有不支持的字符。'
            self.changed.emit()
            return
        self.queued = False
        if immediate:
            self.timer.stop()
            self._ready()
        else:
            self.timer.start(self.PAUSE_MS)
        self.changed.emit()

    def _ready(self):
        if self.pending is not None:
            self.queued = True
            return
        self.queued = False
        text = self.text
        if not text or self.error:
            return
        if quick_content_language(text, preferred=self.target) == self.target:
            self.document.pair(text, text)
            self.result = text
            self.changed.emit()
            return
        options = ('auto', self.target, ())
        units = self.document.plan(text, options)
        self.pending = text, options, units
        self._send()

    def _send(self):
        text, options, units = self.pending
        missing = [i for i, unit in enumerate(units) if unit.translation is None]
        if not missing:
            if sum(len(unit.translation) + len(unit.gap) + 1 for unit in units) > MAX_TRANSLATION_CHARS:
                self._completed(self.generation, None, '译文过长，请缩短原文。')
                return
            completed = self.document.commit(text, units, options)
            self.pending = None
            if self.text == text:
                self.result = completed
            self.changed.emit()
            if self.queued:
                self._ready()
            return
        if self.text != text:
            self.pending = None
            if self.queued:
                self._ready()
            self.changed.emit()
            return
        rows, size = [], 0
        for i in missing[:128]:
            unit = units[i]
            if rows and size + len(unit.text) > 12000:
                break
            rows.append({'id': str(i), 'text': unit.text, 'before': '', 'after': ''})
            size += len(unit.text)
        request_indexes = {int(row['id']) for row in rows}
        for row in rows:
            i = int(row['id'])
            # Appending/prepending a new sentence does not need previously
            # translated neighbors. Interior edits receive short context.
            if 0 < i < len(units) - 1:
                if i - 1 not in request_indexes:
                    row['before'] = units[i - 1].text[-self.CONTEXT_CHARS:]
                if i + 1 not in request_indexes:
                    row['after'] = units[i + 1].text[:self.CONTEXT_CHARS]
        self.generation += 1
        self.api.translate_units(self.generation, rows, options[1])
        self.changed.emit()

    def _completed(self, generation, value, error):
        if generation != self.generation or self.pending is None:
            return
        if error:
            text, _, _ = self.pending
            self.pending = None
            if self.text == text:
                self.error = error
            self.changed.emit()
            if self.queued:
                self._ready()
            return
        _, _, units = self.pending
        for row in value['units']:
            units[int(row['id'])].translation = row['text']
        self._send()
