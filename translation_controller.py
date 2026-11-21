"""Clipboard translation, invitation, and stale-result fencing."""
import hashlib
import sys
from PySide6.QtCore import QMimeData, QObject, QThreadPool, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication
from translation_api import TranslationApi
from clipboard_sync import native_sequence
from sync_files import DiskWork
from translation_languages import (DEFAULT_TARGET, DETECTING_SOURCE, MAX_TEXT_CHARS, SOURCE_NAMES,
                                   TARGETS, UNKNOWN_SOURCE, copied_text, detect_source,
                                   language_code, quick_content_language)

COPY_ACK_TIMEOUT_MS = 2000
COPY_RETRY_MS = 50
COPY_RETRIES = 6


def _clipboard_chunks(text):
    """Prefer paragraph and word boundaries, and keep whitespace around each request."""
    start = 0
    while start < len(text):
        end = min(start + MAX_TEXT_CHARS, len(text))
        if end < len(text):
            search_from = start + MAX_TEXT_CHARS // 2
            boundary = text.rfind('\n', search_from, end)
            if boundary < 0:
                boundary = text.rfind(' ', search_from, end)
            if boundary >= 0:
                end = boundary + 1
        block = text[start:end]
        content = block.strip()
        if content:
            left = len(block) - len(block.lstrip())
            yield block[:left], content, block[left + len(content):]
        else:
            yield block, '', ''
        start = end


class TranslationController(QObject):
    changed = Signal()

    def __init__(self, manager, *, api=None, detector=detect_source, sequence=native_sequence):
        super().__init__(manager.app)
        self.manager = manager
        self.settings = manager.settings
        self._transport = None
        if api is None:
            from cloud_api import CloudApi
            # Account logout cancels its transport. Public translation owns
            # its requests independently while reusing the cloud HTTP client.
            self._transport = CloudApi(manager.account.api.origin, self)
        self.api = api or TranslationApi(self._transport, self)
        self.closed = False
        self.generation = 0
        self.text = self.result = self.error = ''
        self.phase = 'idle'
        self.notice = None
        self.automatic = False
        self._automatic_target = DEFAULT_TARGET
        self._pieces = None
        self._translated = []
        self._piece_spacing = '', ''
        self.detector = detector
        self.sequence = sequence
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._detection = None
        self.source = UNKNOWN_SOURCE
        self._observed_sequence = None
        self._override = None
        self._queued_manual = False
        self._copy_digest = None
        self._silent_copy_digest = None
        self.copy_timer = QTimer(self)
        self.copy_timer.setSingleShot(True)
        self.copy_timer.timeout.connect(self._copy_failed)
        self._preferences = self._read_preferences()
        self.settings.changed.connect(self._settings_changed)
        manager.clipboard_sync.copy_observed.connect(self.observe)
        self.api.completed.connect(self._completed)
        manager.clipboard_sync.set_local_observation(self._observing())

    @property
    def busy(self):
        return self.api.busy

    @property
    def target(self):
        source = language_code(self.source)
        default = self._automatic_target if self.automatic else self.settings.translation_target
        preferred = self.settings.translation_rules.get(source, default)
        if self._override:
            return self._override
        if preferred == source and not self.automatic:
            return 'en' if source == DEFAULT_TARGET else DEFAULT_TARGET
        return preferred

    def _read_preferences(self):
        return (self.settings.translation_enabled, self.settings.translation_target, self.settings.translation_rules)

    def _observing(self):
        return self.automatic or self.settings.translation_enabled

    def set_automatic(self, enabled, *, target=None):
        if self.closed:
            return
        enabled = bool(enabled)
        next_target = target if target in TARGETS else self._automatic_target
        if self.automatic == enabled and (not enabled or next_target == self._automatic_target):
            return
        was_observing = self._observing()
        self.automatic = enabled
        self._automatic_target = next_target
        self._reset()
        if was_observing != self._observing():
            self.manager.clipboard_sync.set_local_observation(self._observing())
        if self.notice:
            self.notice.dismiss()
        if enabled:
            notice = getattr(self.manager, 'clipboard_notice', None)
            if notice:
                notice.supersede()
        self.changed.emit()

    def select_target(self, code):
        if code in TARGETS:
            self.settings.update(translation_target=code)
            self._override = code
            self.changed.emit()

    def open_translator(self):
        if not self.text:
            return
        self.manager.show_translator_dialog(text=self.text, target=self.target)
        if self.notice:
            self.notice.dismiss()

    def _reset(self):
        self.generation += 1
        self.api.cancel()
        self._cancel_detection()
        self.copy_timer.stop()
        self._copy_digest = None
        self._pieces = None
        self._translated.clear()
        self._override = None
        self._queued_manual = False
        self.phase = 'idle'
        self.text = self.result = self.error = ''
        self.source = UNKNOWN_SOURCE

    def _settings_changed(self):
        if self.closed:
            return
        preferences = self._read_preferences()
        if preferences == self._preferences:
            return
        was_enabled, _, old_rules = self._preferences
        self._preferences = preferences
        if not self.automatic and was_enabled != preferences[0]:
            self.manager.clipboard_sync.set_local_observation(preferences[0])
        if self.automatic:
            # The active editor owns the fallback language. Only rule edits affect its requests.
            if old_rules != preferences[2]:
                self._reset()
                self.changed.emit()
            return
        text, source = self.text, self.source
        restart_detection = self._detection is not None and bool(text)
        self._reset()
        if preferences[0]:
            self.text, self.source = text, source
            if restart_detection:
                self._detect()
        elif self.notice:
            self.notice.dismiss()
        self.changed.emit()

    def _cancel_detection(self):
        if self._detection is not None:
            self._detection.cancel.set()
            self._detection = None
        self.pool.clear()

    def _detect(self):
        generation, text, detector = self.generation, self.text, self.detector
        use_quick=bool(self.automatic or self._preferences[2])
        def analyze(cancel):
            if cancel.is_set():return None
            if use_quick:
                direct=quick_content_language(text)
                if direct and direct!='en':return SOURCE_NAMES[direct]
            return detector(text)
        job = DiskWork(analyze)
        self._detection = job
        job.signals.done.connect(lambda value,error:self._detected(generation,value,error),Qt.QueuedConnection)
        self.pool.start(job)

    def _detected(self, generation, value, error):
        if self.closed or generation != self.generation:
            return
        self._detection = None
        if error:
            import logging
            logging.getLogger(__name__).warning('Clipboard language detection failed: %s', type(error).__name__)
        self.source = value or UNKNOWN_SOURCE
        if self.automatic:
            self._start_auto()
        elif self._queued_manual:
            self._queued_manual=False
            self.translate()
        else:
            self.changed.emit()

    def observe(self, value):
        if self.closed:
            return
        data = (value[1] if isinstance(value, (list, tuple)) and len(value) == 2
                and value[0] == 'text' and isinstance(value[1], dict) else {})
        body = data.get('text')
        digest = (hashlib.sha256(body.strip().encode('utf8')).digest()
                  if (self._copy_digest or self._silent_copy_digest) and isinstance(body, str) else None)
        if self._copy_digest is not None:
            self.copy_timer.stop()
            expected, self._copy_digest = self._copy_digest, None
            if digest == expected:
                self._silent_copy_digest = None
                self.phase = 'copied'
                self.changed.emit()
                return
        if self._silent_copy_digest is not None:
            expected, self._silent_copy_digest = self._silent_copy_digest, None
            if digest == expected:
                return
        if not self._observing():
            return
        if self.automatic:
            text = body if isinstance(body, str) and '\0' not in body and body.strip() else None
        else:
            text = copied_text(value)
        if data.get('_history_private') or text and self.automatic and self._private_clipboard():
            text = None
        sequence = data['_translation_sequence'] if '_translation_sequence' in data else self.sequence()
        self._accept(text, sequence)

    @staticmethod
    def _private_clipboard():
        from platform_native import native_input_available
        if not native_input_available():return False
        from clipboard_history_native import private_clipboard
        try:return private_clipboard()
        except (ImportError,OSError,RuntimeError) as error:
            import logging
            logging.getLogger(__name__).warning('Clipboard translation privacy check failed: %s',type(error).__name__)
            return True

    def _accept(self, text, sequence):
        self._reset()
        self._observed_sequence = sequence
        if not text:
            if self.notice:
                self.notice.dismiss()
            self.changed.emit()
            return
        self.text = text
        self.source = DETECTING_SOURCE
        if self.automatic:
            self.phase = 'detecting'
        else:
            if self.notice is None:
                from translation_notice import TranslationNotice
                self.notice = TranslationNotice(self.manager, self)
            self.notice.present()
        self._detect()
        self.changed.emit()

    def _start_auto(self):
        if self._observed_sequence != self.sequence() or language_code(self.source) == self.target:
            self.phase = 'idle'
            self.changed.emit()
            return
        self.phase = 'loading'
        self._pieces = _clipboard_chunks(self.text)
        self._next_auto_piece()
        self.changed.emit()

    def _next_auto_piece(self):
        if self._observed_sequence != self.sequence():
            self._reset()
            self.changed.emit()
            return
        for before, content, after in self._pieces:
            if content:
                self._piece_spacing = before, after
                self.api.translate(self.generation, content, self.target)
                return
            self._translated.append(before)
        self.result = ''.join(self._translated)
        self._pieces = None
        self._translated.clear()
        self.phase = 'ready'
        if self.result != self.text:
            self.copy_result(expected_sequence=self._observed_sequence, automatic=True)
        else:
            self.phase = 'idle'
        self.changed.emit()

    def translate(self):
        if self.closed or self.automatic or not self.settings.translation_enabled or not self.text or self.busy:
            return
        self.phase = 'loading'
        self.error = ''
        if self._detection is not None and self.settings.translation_rules and not self._override:
            self._queued_manual = True
        else:
            self.api.translate(self.generation, self.text, self.target)
        self.changed.emit()

    def _completed(self, generation, result, error):
        if self.closed or generation != self.generation:
            return
        if self.automatic and self._pieces is not None:
            if error or not result:
                self._pieces = None
                self._translated.clear()
                self.phase = 'error'
                self.error = error or '翻译未完成，已保留原剪贴板。'
                self.changed.emit()
                return
            before, after = self._piece_spacing
            self._translated.append(before + result + after)
            self._next_auto_piece()
            return
        self.result, self.error = result, error
        self.phase = 'error' if error else 'ready'
        self.changed.emit()

    def silence_output_copy(self,text):
        """Silence the next matching copy from either translator pane.

        There is no expiry timer: native clipboard observation can be delayed
        while another program owns it. Any different copy clears the marker.
        """
        if self.closed:return
        normalized=copied_text(('text',{'text':text}))
        self._silent_copy_digest=hashlib.sha256(normalized.encode('utf8')).digest() if normalized else None
        self._reset()
        if self.notice:self.notice.dismiss()
        self.changed.emit()

    def copy_result(self, *, expected_sequence=None, automatic=False, retries=COPY_RETRIES):
        if not self.result or self.closed or self.phase not in ('ready', 'copied'):
            return
        if automatic and (not self.automatic or expected_sequence != self.sequence()):
            self.phase = 'idle'
            self.changed.emit()
            return
        from native_clipboard_reader import foreign_clipboard_lock
        from platform_native import native_input_available
        if sys.platform == 'win32' and native_input_available() and foreign_clipboard_lock():
            if automatic and retries:
                generation = self.generation
                def retry():
                    if self.generation == generation:
                        self.copy_result(expected_sequence=expected_sequence, automatic=True, retries=retries-1)
                QTimer.singleShot(COPY_RETRY_MS, self, retry)
            else:
                self.error = ('剪贴板被其他应用占用，已保留原内容。' if automatic
                              else '剪贴板暂被其他应用占用，请再次点击复制。')
                self.changed.emit()
            return
        if automatic and expected_sequence != self.sequence():
            self.phase = 'idle'
            self.changed.emit()
            return
        self._copy_digest = hashlib.sha256(self.result.strip().encode('utf-8')).digest()
        self._silent_copy_digest = self._copy_digest
        self.copy_timer.start(COPY_ACK_TIMEOUT_MS)
        mime = QMimeData()
        mime.setText(self.result)
        QApplication.clipboard().setMimeData(mime)
        generation=self.generation
        QTimer.singleShot(0,self,lambda:self._confirm_owned_copy(generation))

    def _confirm_owned_copy(self,generation):
        if self.closed or generation!=self.generation or self._copy_digest is None:return
        clipboard=QApplication.clipboard()
        # Ownership confirms the synchronous Qt write without reading MIME data
        # that a remote-control application could replace between two GUI calls.
        # Retain the fingerprint until the observer consumes our write, so its
        # delayed notification cannot become another translation invitation.
        if clipboard.ownsClipboard():
            self.phase='copied'
            self.changed.emit()

    def _copy_failed(self):
        self._copy_digest = None
        if self.phase=='copied':return
        self.error = '尚未确认写入剪贴板，请再次点击复制。'
        self.changed.emit()

    def reconcile_pointer(self, point, buttons):
        if self.notice is not None:
            self.notice.reconcile_pointer(point, buttons)

    def close(self):
        self.closed = True
        self.automatic = False
        self._reset()
        if self._transport is not None:self._transport.close()
        self.copy_timer.stop()
        if self.notice:
            self.notice.supersede()
