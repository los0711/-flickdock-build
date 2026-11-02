"""Bounded independent uploads beside the journal's existing main cycle.

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
        self.tasks = {}
        self.capacity = 2  # Plus the main cycle: at most three file uploads.
        self.retry_at = {}
        self.retry=QTimer(owner);self.retry.setSingleShot(True);self.retry.timeout.connect(self.kick)
        self.retry.setTimerType(Qt.TimerType.PreciseTimer)

    @property
    def active(self):
        return next(iter(self.tasks.values()),None)

    @active.setter
    def active(self,value):
        # Compatibility for journal/lifecycle fixtures; production cancels by ID.
        self.tasks.clear()
        if value is not None:self.tasks[value['id']]=value

    def owns(self,token):
        return token is not None and self.tasks.get(token['id']) is token

    def discard(self,identity):
        self.tasks.pop(identity,None)
        self.retry_at.pop(identity,None)

    def defer(self,identity,delay):
        self.retry_at[identity]=time.monotonic()+delay
        future=[value for value in self.retry_at.values() if value>time.monotonic()]
        if future:self.retry.start(max(1,round((min(future)-time.monotonic())*1000)+1))

    def reset(self):
        self.tasks.clear()
        self.retry_at.clear()
        self.retry.stop()

    def run(self, token, callback, *args):
        if not self.owns(token):
            return
        owner = self.owner
        previous, context = owner.transfer, owner._lane_context
        owner.transfer, owner._lane_context = token, token
        try:
            return callback(*args)
        finally:
            owner.transfer, owner._lane_context = previous, context

    def finish(self, error=None, *, token=None):
        owner = self.owner
        token = token or owner._lane_context
        if not self.owns(token):
            return
        self.tasks.pop(token['id'])
        if error:
            self.defer(token['id'],30)
            entry=(owner.state or {}).get('entries',{}).get(token['id'])
            if entry is not None:entry['last_error']=owner._upload_error_summary(error)
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
        self.kick()

    def eligible(self, entry, texts):
        bridge=getattr(self.owner,'clipboard_bridge',None)
        if bridge and bridge.waiting(entry):return False
        if (entry.get('version', 0) != 0 or entry.get('deleted')
                or not entry.get('dirty') or entry.get('paused_upload')
                or entry.get('blocked_fingerprint') == entry.get('fingerprint')
                or self.retry_at.get(entry['id'], 0) > time.monotonic()):
            return False
        text = texts.get(entry['ref'])
        try:
            # New objects have no concurrent-version reconciliation side
            # effects. Larger files/directories can use the same isolated
            # slots only when the transport streams both whole files and
            # ranges; never multiply large Python request buffers. Directory
            # preparation stays in the owner's bounded disk worker pool.
            api=self.owner.api
            streaming=(getattr(api,'supports_file_upload',False)
                       and getattr(api,'supports_file_slices',False))
            if text is None and streaming:
                # Admission is on Qt's thread. The bounded stage_item worker
                # owns validation; even stat can block on a disconnected share.
                return True
            return (len(text.encode('utf-8')) <= SMALL_BYTES if text is not None
                    else Path(entry['ref']).is_file()
                    and Path(entry['ref']).stat().st_size <= SMALL_BYTES)
        except (OSError, UnicodeError):
            return False

    def kick(self):
        owner = self.owner
        if (owner.closed or not owner.running
                or owner._lane_context or owner._clipboard_priority
                or not owner.upload_enabled or not owner.service.signed_in
                or owner._main_cycle_phase not in ('upload','upload_tail')
                or owner._retry_paused() or owner._paused('quota_not_before')):
            return
        if owner.transfer is None:
            # No transfer also describes cleanup/receive requests in flight.
            # Only the main cycle's explicit upload-tail boundary is idle:
            # use its free slot even when both auxiliary PUTs are still held.
            if owner._main_cycle_phase=='upload_tail':owner._push_next()
            return
        if len(self.tasks)>=self.capacity:return
        bulk = owner.transfer or {}
        # A slow small-file commit must not serialize all other small files.
        texts = owner.adapter.snapshot().get('texts', {})
        resumes = owner.state.setdefault('resumable_uploads', {})
        entry = next((item for item in owner.state['entries'].values()
                      if item['id'] != bulk.get('id') and item['id'] not in self.tasks
                      and self.eligible(item, texts)), None)
        if entry is None:
            return
        saved=resumes.get(entry['id'])
        token = dict(saved or entry)
        self.tasks[token['id']]=token
        QTimer.singleShot(0,owner,self.kick)
        if saved:
            from shelf_sync import _verify_staged
            def verified(result,error):
                if error:
                    owner._forget_resume(token['id']);self.finish(error,token=token);return
                resumes[token['id']]=token
                owner._resume_upload(token)
            self.run(token,owner._work,_verify_staged,(token,),verified)
            return

        def staged(result, error):
            if error:
                if isinstance(error,ValueError):
                    # A rejected archive/size is not a transient request.
                    # Match the main slot's unchanged-revision fence instead
                    # of repeatedly scanning the same invalid directory.
                    owner._end_intake(token)
                    current=owner.state['entries'].get(token['id'])
                    if current and current.get('seq')==token.get('seq'):
                        current.update(blocked_fingerprint=token['fingerprint'],
                                       last_error=owner._upload_error_summary(error))
                    owner.status_changed.emit(owner._upload_error_summary(error))
                    self.finish(token=token)
                    QTimer.singleShot(0,owner,owner._retire_abandoned_uploads)
                else:self.finish(error,token=token)
                return
            token.update(result)
            if not owner._resume_matches(token):
                Path(token['path']).unlink(missing_ok=True)
                self.finish(token=token)
                return
            resumes[token['id']] = token
            owner._persist()
            owner._notify_transfers()
            owner._initialize_upload(token)

        self.run(token, owner._work, stage_item,
                 (entry['ref'], texts.get(entry['ref']), entry['name'],
                  owner.root/owner.account_id/'outbox'), staged)

