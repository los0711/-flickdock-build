"""Account-independent translation; provider credentials remain server-side."""
from uuid import uuid4
from PySide6.QtCore import QObject, Signal
from translation_languages import MAX_TEXT_CHARS, TARGETS

REQUEST_TIMEOUT_MS = 120000
MAX_TRANSLATION_CHARS = 24000


class TranslationApi(QObject):
    completed = Signal(int, str, str)

    def __init__(self, cloud_api, parent=None):
        super().__init__(parent)
        self.api = cloud_api
        self.request = None
        self.epoch = 0

    @property
    def busy(self):
        return self.request is not None

    def translate(self, generation, text, target):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS or target not in TARGETS:
            raise ValueError('文字或目标语言无效。')
        self.cancel()
        epoch = self.epoch
        self.request = 'starting'

        def finished(result, error):
            if epoch != self.epoch:
                return
            self.request = None
            if error is not None:
                messages = {'TRANSLATION_UNAVAILABLE': '翻译服务暂未开放，请稍后重试。',
                    'TRANSLATION_INCOMPLETE': '译文未完整生成，请缩短原文后重试。'}
                self.completed.emit(generation, '', messages.get(str(getattr(error, 'code', '')).upper(), '翻译未完成，请检查网络后重试。'))
                return
            content = result.get('translation') if isinstance(result, dict) else None
            if not isinstance(content, str) or not content.strip() or len(content) > MAX_TRANSLATION_CHARS or '\x00' in content:
                self.completed.emit(generation, '', '翻译服务返回了无效内容。')
            else:
                self.completed.emit(generation, content.strip(), '')

        request = self.api.request('POST', '/v1/translation', finished,
            data=dict(text=text, target=target, request_id=str(uuid4())), authenticated=False, timeout_ms=REQUEST_TIMEOUT_MS)
        # CloudApi may report a validation error synchronously.
        if self.request is not None:
            self.request = request

    def cancel(self):
        self.epoch += 1
        request, self.request = self.request, None
        if request and request != 'starting':
            self.api.cancel(request)
