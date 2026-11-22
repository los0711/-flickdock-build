"""Account-independent translation; provider credentials remain server-side."""
from uuid import uuid4
from PySide6.QtCore import QObject, Signal
from translation_languages import MAX_TEXT_CHARS, TARGETS

REQUEST_TIMEOUT_MS = 120000
MAX_TRANSLATION_CHARS = 24000


class TranslationApi(QObject):
    completed = Signal(int, str, str)
    units_ready = Signal(int, object, str)
    aligned = Signal(int, object, str)
    document_aligned = Signal(int, object, str)

    def __init__(self, cloud_api, parent=None):
        super().__init__(parent)
        self.api = cloud_api
        self.request = None
        self.epoch = 0

    @property
    def busy(self):
        return self.request is not None

    def translate(self, generation, text, target, *, source='auto', glossary=()):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS or target not in TARGETS:
            raise ValueError('文字或目标语言无效。')
        values = dict(text=text, target=target)
        if source != 'auto':
            if source not in TARGETS:raise ValueError('源语言无效。')
            values['source'] = source
        if glossary:values['glossary'] = self._glossary(glossary)
        self._submit(generation, values)

    @staticmethod
    def _glossary(terms):
        if not isinstance(terms, (tuple, list)) or len(terms) > 100 or any(
                not isinstance(term,str) or not term.strip() or len(term)>80 or '\x00' in term for term in terms):
            raise ValueError('术语保护列表无效。')
        return list(dict.fromkeys(term.strip() for term in terms))

    def polish(self, generation, text, target, instruction, *, original='', glossary=(),scope='target'):
        if (not isinstance(text,str) or not text.strip() or len(text)>MAX_TRANSLATION_CHARS or '\x00' in text
                or target not in TARGETS or not isinstance(instruction,str) or not instruction.strip()
                or len(instruction)>1000 or '\x00' in instruction
                or not isinstance(original,str) or len(original)>MAX_TEXT_CHARS or '\x00' in original or scope not in ('source','target')):
            raise ValueError('译文或润色指令无效。')
        values = dict(operation='polish',text=text,target=target,instruction=instruction.strip(),original=original)
        if scope=='source':values['polish_scope']='source'
        if glossary:values['glossary'] = self._glossary(glossary)
        self._submit(generation, values)

    def _submit(self, generation, values):
        self.cancel()
        epoch = self.epoch
        self.request = 'starting'
        operation = values.get('operation','translate')

        def finished(result, error):
            if epoch != self.epoch:
                return
            self.request = None
            if error is not None:
                messages = {'TRANSLATION_UNAVAILABLE': '翻译服务暂未开放，请稍后重试。',
                    'TRANSLATION_INCOMPLETE': '译文未完整生成，请缩短原文后重试。',
                    'TRANSLATION_TERMS': '生成结果未保留保护术语，请重试；当前译文已保留。',
                    'INVALID_REQUEST': '服务端尚不支持这项翻译功能，需更新翻译服务后重试。'}
                self.completed.emit(generation, '', messages.get(str(getattr(error, 'code', '')).upper(), '翻译未完成，请检查网络后重试。'))
                return
            content = result.get('translation') if isinstance(result, dict) else None
            if operation == 'polish' and isinstance(result,dict) and result.get('operation') != 'polish':
                self.completed.emit(generation, '', '服务端尚未启用 AI 润色，请更新翻译服务后重试。')
            elif values.get('polish_scope')=='source' and isinstance(result,dict) and result.get('polish_scope')!='source':
                self.completed.emit(generation,'','服务端尚不支持润色原文，原文已保留。')
            elif not isinstance(content, str) or not content.strip() or len(content) > MAX_TRANSLATION_CHARS or '\x00' in content:
                self.completed.emit(generation, '', '翻译服务返回了无效内容。')
            else:
                self.completed.emit(generation, content.strip(), '')

        request = self.api.request('POST', '/v1/translation', finished,
            data={**values, 'request_id':str(uuid4())}, authenticated=False, timeout_ms=REQUEST_TIMEOUT_MS)
        # CloudApi may report a validation error synchronously.
        if self.request is not None:
            self.request = request

    def translate_units(self,generation,units,target,*,source='auto',glossary=()):
        if not units or len(units)>128 or target not in TARGETS:raise ValueError('Invalid translation batch')
        self._advanced(generation,dict(operation='translate_units',units=units,target=target,source=source,
            glossary=self._glossary(glossary)),self.units_ready)

    def align(self,generation,source,target,selection,side,selection_start=0):
        self._advanced(generation,dict(operation='align',source_text=source,target_text=target,
            selection=selection,side=side,selection_start=selection_start),self.aligned)

    def align_document(self, generation, source_text, target_text, source_lang, target_lang):
        self.cancel();epoch=self.epoch;self.request='starting'
        def done(value,error):
            if epoch!=self.epoch:return
            self.request=None
            self.document_aligned.emit(generation,value,'' if error is None else '对齐服务暂不可用')
        request=self.api.request('POST','/v1/align',done,
            data=dict(source_text=source_text,target_text=target_text,source_lang=source_lang,target_lang=target_lang),
            authenticated=False,timeout_ms=9000)
        if self.request is not None:self.request=request

    def _advanced(self,generation,values,signal):
        self.cancel();epoch=self.epoch;self.request='starting'
        def done(value,error):
            if epoch!=self.epoch:return
            self.request=None
            if error is not None or not isinstance(value,dict) or value.get('operation')!=values['operation']:
                signal.emit(generation,None,'局部翻译或对齐未完成，已保留现有内容。');return
            if values['operation']=='translate_units':
                rows=value.get('units');expected={row['id'] for row in values['units']}
                if (not isinstance(rows,list) or len(rows)!=len(expected) or any(not isinstance(row,dict)
                    or row.get('id') not in expected or not isinstance(row.get('text'),str) or not row['text'].strip()
                    or '\0' in row['text'] for row in rows) or len({row['id'] for row in rows})!=len(expected)
                    or sum(len(row['text']) for row in rows)>24000):
                    signal.emit(generation,None,'局部译文不完整，已保留现有内容。');return
            signal.emit(generation,value,'')
        request=self.api.request('POST','/v1/translation',done,data={**values,'request_id':str(uuid4())},
            authenticated=False,timeout_ms=REQUEST_TIMEOUT_MS)
        if self.request is not None:self.request=request

    def cancel(self):
        self.epoch += 1
        request, self.request = self.request, None
        if request and request != 'starting':
            self.api.cancel(request)
