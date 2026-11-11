"""Local-copy invitation, explicit translation and stale-result fencing."""
import hashlib
import sys
from PySide6.QtCore import QMimeData, QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication
from sync_files import DiskWork
from translation_api import TranslationApi
from translation_languages import copied_text, detect_source

COPY_ACK_TIMEOUT_MS = 2000


class TranslationController(QObject):
    changed = Signal()

    def __init__(self, manager, *, api=None, detector=detect_source):
        super().__init__(manager.app)
        self.manager = manager
        self.settings = manager.settings
        self.api = api or TranslationApi(manager.account.api, self)
        self.detector = detector
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.jobs = set()
        self.closed = False
        self.generation = 0
        self.text = self.result = self.error = ''
        self.source = '自动识别'
        self.phase = 'idle'
        self.notice = None
        self._copy_digest = None
        self.copy_timer = QTimer(self)
        self.copy_timer.setSingleShot(True)
        self.copy_timer.timeout.connect(self._copy_failed)
        self._preferences = (self.settings.translation_enabled, self.settings.translation_target)
        self.settings.changed.connect(self._settings_changed)
        manager.clipboard_sync.copy_observed.connect(self.observe)
        self.api.completed.connect(self._completed)
        manager.account.account_changed.connect(self._account_changed)
        manager.clipboard_sync.set_local_observation(self.settings.translation_enabled)

    @property
    def busy(self):
        return self.api.busy

    def _account_changed(self):
        if self.closed:return
        self.generation += 1
        self.api.cancel()
        self.copy_timer.stop();self._copy_digest=None
        self.result = self.error = ''
        self.phase = 'idle'
        self.changed.emit()

    def _settings_changed(self):
        if self.closed:return
        preferences = (self.settings.translation_enabled, self.settings.translation_target)
        if preferences == self._preferences:
            return
        self._preferences = preferences
        self.manager.clipboard_sync.set_local_observation(preferences[0])
        self.generation += 1
        self.api.cancel()
        self.copy_timer.stop();self._copy_digest=None
        self.phase = 'idle'
        self.result = self.error = ''
        if not preferences[0]:
            self.text = ''
            if self.notice:
                self.notice.dismiss()
        self.changed.emit()

    def observe(self, value):
        if self.closed:
            return
        text = copied_text(value)
        if self._copy_digest is not None:
            digest = hashlib.sha256(text.encode('utf-8')).digest() if text else None
            if digest == self._copy_digest:
                self._copy_digest = None
                self.copy_timer.stop()
                self.phase = 'copied'
                self.changed.emit()
                return
            self._copy_digest = None
            self.copy_timer.stop()
        if not self.settings.translation_enabled:
            return
        self.generation += 1
        generation = self.generation
        self.api.cancel()
        self.text = text or ''
        self.result = self.error = ''
        self.phase = 'idle'
        self.source = '自动识别'
        if text is None:
            if self.notice:
                self.notice.dismiss()
            self.changed.emit()
            return
        if self.notice is None:
            from translation_notice import TranslationNotice
            self.notice = TranslationNotice(self.manager, self)
        self.notice.present()
        self.changed.emit()
        job = DiskWork(lambda cancel: self.detector(text))
        self.jobs.add(job)

        def ready(result, error):
            self.jobs.discard(job)
            if self.closed or generation != self.generation:
                return
            if error is not None:
                # Source detection is local decoration; translation itself can
                # auto-detect. Keep the failure visible in diagnostics, not a guess.
                import logging
                logging.getLogger(__name__).warning('Source-language detection failed: %s', type(error).__name__)
            else:
                self.source = result
                self.changed.emit()

        job.signals.done.connect(ready)
        for previous in self.jobs - {job}:
            previous.cancel.set()
        self.pool.start(job)

    def translate(self):
        if self.closed or not self.settings.translation_enabled or not self.text or self.busy:
            return
        if not self.manager.account.signed_in:
            self.manager.show_settings(category='账号与设备')
            return
        self.phase = 'loading'
        self.error = ''
        self.api.translate(self.generation, self.text, self.settings.translation_target)
        self.changed.emit()

    def _completed(self, generation, result, error):
        if self.closed or generation != self.generation:
            return
        self.result, self.error = result, error
        self.phase = 'error' if error else 'ready'
        self.changed.emit()

    def copy_result(self):
        if not self.result or self.closed or self.phase not in ('ready', 'copied'):
            return
        from native_clipboard_reader import foreign_clipboard_lock
        if sys.platform == 'win32' and foreign_clipboard_lock():
            self.error = '剪贴板暂被其他应用占用，请再次点击复制。'
            self.changed.emit()
            return
        self._copy_digest = hashlib.sha256(self.result.strip().encode('utf-8')).digest()
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
            self.phase='copied';self.changed.emit()

    def _copy_failed(self):
        self._copy_digest = None
        if self.phase=='copied':return
        self.error = '尚未确认写入剪贴板，请再次点击复制。'
        self.changed.emit()

    def close(self):
        self.closed = True
        self.generation += 1
        self.api.cancel()
        self.copy_timer.stop()
        for job in self.jobs:
            job.cancel.set()
        self.pool.clear()
        if self.notice:
            self.notice.supersede()
