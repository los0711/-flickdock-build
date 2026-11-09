"""Bounded parallel uploads sharing the journal's protocol and account epoch."""
from pathlib import Path
import time

from PySide6.QtCore import QTimer, Qt
from sync_files import stage_item

MAX_CONCURRENT_UPLOADS = 3
SMALL_BYTES = 4 * 1024 * 1024
RETRY_SECONDS = 30


class UploadLanes:
    def __init__(self, owner):
        self.owner = owner
        self.active = {}
        self.waiting = {}
        self._main_transfer = None
        self.retry_at = {}
        self.retry = QTimer(owner)
        self.retry.setSingleShot(True)
        self.retry.setTimerType(Qt.TimerType.PreciseTimer)
        self.retry.timeout.connect(self.kick)

    def reset(self):
        self.active.clear()
        self.waiting.clear()
        self.retry_at.clear()
        self.retry.stop()

    def owns(self, token):
        return self.active.get(token['id']) is token

    def run(self, token, callback, *args):
        if not self.owns(token):
            return
        owner = self.owner
        previous, context = owner.transfer, owner._lane_context
        if context is None:self._main_transfer=previous
        owner.transfer, owner._lane_context = token, token
        try:
            return callback(*args)
        finally:
            owner.transfer, owner._lane_context = previous, context
            if context is None:self._main_transfer=None

    def busy_ids(self):
        main=self._main_transfer if self.owner._lane_context is not None else self.owner.transfer
        return set(self.active)|({main['id']} if main else set())

    def blocked(self,identity):
        blocker=self.waiting.get(identity)
        if blocker in self.busy_ids():return True
        self.waiting.pop(identity,None)
        return False

    def finish(self, error=None):
        owner = self.owner
        token = owner._lane_context
        if token is None or not self.owns(token):
            return
        self.active.pop(token['id'])
        if error:
            self.retry_at[token['id']] = time.monotonic() + RETRY_SECONDS
            owner._refresh_again = True
        else:
            self.retry_at.pop(token['id'], None)
        future = [value for value in self.retry_at.values() if value > time.monotonic()]
        if future:
            self.retry.start(max(1, round((min(future) - time.monotonic()) * 1000) + 1))
        owner._persist()
        owner._notify_transfers()
        owner.debounce.start(RETRY_SECONDS * 1000 + 1 if error else 1)
        QTimer.singleShot(0, owner, self._continue)

    def _continue(self):
        owner = self.owner
        if owner.closed or owner._clipboard_priority or not owner.scheduler.connected:
            return
        if owner.running and owner.transfer is None and owner._cycle_direction=='upload':
            owner._push_next()
        self.kick()

    def _size(self, entry, texts):
        text = texts.get(entry['ref'])
        if text is not None:
            return len(text.encode('utf-8'))
        path = Path(entry['ref'])
        return path.stat().st_size if path.is_file() else SMALL_BYTES + 1

    def eligible(self, entry):
        owner = self.owner
        bridge = owner.clipboard_bridge
        return (entry['id'] not in self.active
                and not self.blocked(entry['id'])
                and entry['id'] != (owner.transfer or {}).get('id')
                and not (bridge and bridge.waiting(entry))
                and not entry.get('deleted') and entry.get('dirty')
                and not entry.get('paused_upload')
                and entry.get('blocked_fingerprint') != entry.get('fingerprint')
                and self.retry_at.get(entry['id'], 0) <= time.monotonic())

    def kick(self):
        owner = self.owner
        if (owner.closed or not owner.scheduler.connected or not owner.running or owner._lane_context
                or owner._clipboard_priority or not owner.upload_enabled
                or not owner.service.signed_in or owner._retry_paused()
                or owner._paused('quota_not_before')):
            return
        # One slot stays with the existing receive/journal cycle. The other
        # slots refill independently, so a slow part cannot hold small files.
        slots = MAX_CONCURRENT_UPLOADS - 1 - len(self.active)
        if slots <= 0:
            return
        texts = owner.adapter.snapshot().get('texts', {})
        candidates = []
        for order, entry in enumerate(owner.state['entries'].values()):
            if not self.eligible(entry):
                continue
            try:
                size = self._size(entry, texts)
            except (OSError, UnicodeError):
                continue
            candidates.append((size > SMALL_BYTES, order, entry))
        for _, _, entry in sorted(candidates, key=lambda row: row[:2])[:slots]:
            self._start(entry, texts)

    def _start(self, entry, texts):
        owner = self.owner
        resumes = owner.state.setdefault('resumable_uploads', {})
        saved = resumes.get(entry['id'])
        token = dict(saved or entry)
        self.active[token['id']] = token
        if saved:
            from shelf_sync import _verify_staged
            def verified(result, error):
                if error:
                    owner._forget_resume(token['id'])
                    self.finish(error)
                    return
                resumes[token['id']] = token
                owner._resume_upload(token)
            self.run(token, owner._work, _verify_staged, (token,), verified)
            return

        def staged(result, error):
            if error:
                self.finish(error)
                return
            token.update(result)
            if not owner._resume_matches(token):
                Path(token['path']).unlink(missing_ok=True)
                self.finish()
                return
            resumes[token['id']] = token
            owner._persist()
            owner._notify_transfers()
            owner._initialize_upload(token)

        self.run(token, owner._work, stage_item,
                 (entry['ref'], texts.get(entry['ref']), entry['name'],
                  owner.root / owner.account_id / 'outbox'), staged)
