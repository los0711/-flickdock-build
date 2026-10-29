"""One bounded small-item upload beside the journal's existing bulk cycle.

The owner retains the protocol, journal and account epoch. Callbacks execute
serially on Qt's thread with their own transfer context, never on worker threads.
"""
from pathlib import Path
import time

from sync_files import stage_item
from PySide6.QtCore import QTimer,Qt

SMALL_BYTES = 4*1024*1024


class SmallUploadLane:
    def __init__(self, owner):
        self.owner = owner
        self.active = None
        self.retry_at = {}
        self.retry=QTimer(owner);self.retry.setSingleShot(True);self.retry.timeout.connect(self.kick)
        self.retry.setTimerType(Qt.TimerType.PreciseTimer)

    def reset(self):
        self.active = None
        self.retry_at.clear()
        self.retry.stop()

    def run(self, token, callback, *args):
        if token is not self.active:
            return
        owner = self.owner
        previous, context = owner.transfer, owner._lane_context
        owner.transfer, owner._lane_context = token, token
        try:
            return callback(*args)
        finally:
            owner.transfer, owner._lane_context = previous, context

    def finish(self, error=None):
        owner = self.owner
        token = self.active
        if token is None:
            return
        self.active = None
        if error:
            self.retry_at[token['id']] = time.monotonic()+30
            owner._refresh_again = True
        else:self.retry_at.pop(token['id'],None)
        future=[value for value in self.retry_at.values() if value>time.monotonic()]
        if future:self.retry.start(max(1,round((min(future)-time.monotonic())*1000)+1))
        owner._persist()
        owner._notify_transfers()
        # Do not re-enter the bulk cycle while its request is outstanding.
        owner.debounce.start(30001 if error else 1)
        QTimer.singleShot(0,owner,self._continue)

    def _continue(self):
        owner=self.owner
        if owner.closed or owner._clipboard_priority:return
        if owner.running and owner.transfer is None and self.active is None:owner._push_next()
        else:self.kick()

    def eligible(self, entry, texts):
        if (entry.get('version', 0) != 0 or entry.get('deleted')
                or not entry.get('dirty') or entry.get('paused_upload')
                or entry.get('blocked_fingerprint') == entry.get('fingerprint')
                or self.retry_at.get(entry['id'], 0) > time.monotonic()):
            return False
        text = texts.get(entry['ref'])
        try:
            return (len(text.encode('utf-8')) <= SMALL_BYTES if text is not None
                    else Path(entry['ref']).is_file()
                    and Path(entry['ref']).stat().st_size <= SMALL_BYTES)
        except (OSError, UnicodeError):
            return False

    def kick(self):
        owner = self.owner
        if (self.active or owner.closed or not owner.running or not owner.transfer
                or owner._lane_context or owner._clipboard_priority
                or not owner.upload_enabled or not owner.service.signed_in
                or owner._retry_paused() or owner._paused('quota_not_before')):
            return
        bulk = owner.transfer
        try:
            size = bulk.get('size')
            if size is None:
                source = Path(bulk['ref'])
                size = source.stat().st_size if source.is_file() else SMALL_BYTES+1
            if size <= SMALL_BYTES:
                return
        except (OSError, KeyError):
            return
        texts = owner.adapter.snapshot().get('texts', {})
        resumes = owner.state.setdefault('resumable_uploads', {})
        entry = next((item for item in owner.state['entries'].values()
                      if item['id'] != bulk['id']
                      and self.eligible(item, texts)), None)
        if entry is None:
            return
        saved=resumes.get(entry['id'])
        token = self.active = dict(saved or entry)
        if saved:
            from shelf_sync import _verify_staged
            def verified(result,error):
                if error:
                    owner._forget_resume(token['id']);self.finish(error);return
                resumes[token['id']]=token
                owner._resume_upload(token)
            self.run(token,owner._work,_verify_staged,(token,),verified)
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
                  owner.root/owner.account_id/'outbox'), staged)

