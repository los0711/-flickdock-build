"""Account-scoped, journaled shelf replication using the private cloud API.

Only file bytes and display names cross devices; local paths never do. Disk
work runs outside the GUI thread. Every callback belongs to an account epoch.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, QThreadPool, QTimer, Qt, Signal
from account_store import atomic_json
from selection_archive import _source_reader
from sync_files import (DiskWork, DIRECTORY_MIME, assemble_parts, fingerprint, read_part, read_utf8, safe_name,
                        stage_item, unpack_directory,MAX_FILE_BYTES)


def _verify_staged(transfer, cancel):
    """Recheck a durable snapshot after a restart before trusting its journal."""
    digest = hashlib.sha256(); size = 0
    with Path(transfer['path']).open('rb') as stream:
        while chunk := stream.read(1024*1024):
            if cancel.is_set(): raise InterruptedError()
            size += len(chunk)
            if size > transfer['size']: raise ValueError('待同步文件已改变。')
            digest.update(chunk)
    if size != transfer['size'] or digest.hexdigest() != transfer['sha256']:
        raise ValueError('待同步文件校验失败，将重新读取原文件。')
    return transfer


def _checked_download_part(path, size, expected, cancel):
    """Hash at most one negotiated chunk outside the GUI thread."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size: return None
        digest = hashlib.sha256(); total = 0
        with _source_reader(path) as stream:
            while chunk := stream.read(min(1024*1024,size+1)):
                if cancel.is_set(): raise InterruptedError()
                total += len(chunk)
                if total > size: return None
                digest.update(chunk)
        actual = digest.hexdigest()
        return actual if total == size and (expected is None or actual == expected) else None
    except OSError: return None


def _remove_download_parts(target, count, cancel):
    target = Path(target)
    for index in range(count):
        if cancel.is_set(): return
        part = target.with_name('.'+target.name+f'.chunk-{index}')
        if not part.is_symlink():
            for attempt in range(12):
                try: part.unlink(missing_ok=True);break
                except PermissionError:
                    if attempt==11:raise
                    if cancel.wait(.05):return


class ShelfSync(QObject):
    status_changed = Signal(str)
    transfers_changed = Signal(list)

    def __init__(self, base, service, adapter, parent=None, *, poll_ms=300000,
                 remote_poll_ms=4000, disconnected_poll_ms=25000):
        super().__init__(parent)
        self.root = Path(base)/'cloud-shelves'; self.root.mkdir(parents=True,exist_ok=True)
        self.service, self.api, self.adapter = service, service.api, adapter
        self.meta_path = self.root/'profiles.json'
        self.meta = self._read(self.meta_path,{'owner':None,'imported_guest':False})
        self.owner = self.meta.get('owner'); self.account_id = None
        self.state = None; self.epoch = 0; self.running = False; self.closed = False
        self.applying = False; self.requests = set(); self.jobs = set()
        self.pool = QThreadPool(self); self.pool.setMaxThreadCount(2)
        self.retry_ms = 15000; self.transfer = None
        self.cleanup_attempts = {}
        self._cleanup_inflight = set()
        self.remote_transfers=[]
        self.download_transfers={}
        self._interactive=False
        self._base_poll_ms=poll_ms
        self._remote_poll_ms=remote_poll_ms
        self._disconnected_poll_ms=disconnected_poll_ms
        self._event_stream_online=True
        self._progress_persisted=0.0
        self._part_cache={}
        self._refresh_again=False
        self._cycle_direction='receive'
        self.upload_enabled=True
        self.receive_enabled=True
        self.timer = QTimer(self); self.timer.setInterval(poll_ms); self.timer.timeout.connect(self.tick)
        self.debounce = QTimer(self); self.debounce.setSingleShot(True); self.debounce.timeout.connect(self.tick)
        # Backoff deadlines are wall-clock boundaries. Qt's default coarse
        # timer may fire early and repeatedly pull before the deadline.
        self.debounce.setTimerType(Qt.TimerType.PreciseTimer)
        # The socket is the fast path. If its commit hint is lost after a
        # pending upload was observed, bounded polling keeps the other device
        # from waiting for the five-minute idle reconciliation. No active
        # remote upload means no extra requests.
        self.remote_poll = QTimer(self); self.remote_poll.setSingleShot(True)
        self.remote_poll.timeout.connect(self.request_refresh)
        service.account_changed.connect(self.select_account)

    def set_directions(self, upload_enabled, receive_enabled):
        """Switch cloud directions without losing local edits or transfer journals.

        Settings persistence belongs to the caller. In-flight replies from the
        old mode are discarded by the account epoch; an interrupted upload is
        resumed from the server's verified boundary when enabled again.
        """
        if self.closed:return False
        upload_enabled=bool(upload_enabled)
        receive_enabled=bool(receive_enabled)
        if (self.upload_enabled,self.receive_enabled)==(upload_enabled,receive_enabled):return False
        self._interrupt_cycle()
        self._refresh_again=False
        self.upload_enabled,self.receive_enabled=upload_enabled,receive_enabled
        if receive_enabled and self.state is not None:
            self.state.pop('awaiting_receive',None)
        if not receive_enabled:
            self.remote_transfers=[]
            self.download_transfers.clear()
            self.remote_poll.stop()
        self._persist()
        self._notify_transfers()
        if self.account_id and self.service.signed_in and (upload_enabled or receive_enabled):
            self.debounce.start(1)
        else:self.debounce.stop()
        return True

    @staticmethod
    def _read(path, fallback):
        try:
            if path.stat().st_size > 32*1024*1024: return copy.deepcopy(fallback)
            data = json.loads(path.read_text(encoding='utf8'))
            return data if isinstance(data,dict) else copy.deepcopy(fallback)
        except (OSError,ValueError): return copy.deepcopy(fallback)

    def _profile(self, owner):
        if owner is None: return self.root/'guest.json'
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(owner)): raise ValueError('账号标识无效。')
        return self.root/owner/'state.json'

    def _persist(self):
        if self.state is None: return
        self.state['view'] = self.adapter.snapshot()
        atomic_json(self._profile(self.account_id),self.state)

    def _persist_progress(self):
        # Resume checks the server boundary; a short journal lag is safe and
        # avoids blocking the GUI with an fsync on every 128KiB network packet.
        now=time.monotonic()
        if now-self._progress_persisted>=1:
            self._progress_persisted=now;self._persist()

    def _snapshot_owner(self):
        if self.state is not None and self.owner == self.account_id:
            self._persist()
        else:
            path = self._profile(self.owner)
            previous = self._read(path,{'entries':{},'cursor':'0'})
            previous['view'] = self.adapter.snapshot(); atomic_json(path,previous)

    def select_account(self, account):
        if self.closed: return
        target = account.get('id') if isinstance(account,dict) else None
        if target == self.account_id and self.state is not None:
            if self.service.signed_in and not self.timer.isActive(): self.timer.start()
            return
        self.epoch += 1; self.timer.stop(); self.debounce.stop(); self.remote_poll.stop()
        self.running = False; self.transfer = None
        self._refresh_again=False
        self.remote_transfers=[]; self.transfers_changed.emit([])
        self.download_transfers.clear()
        self._part_cache.clear();self._progress_persisted=0.0
        self._cleanup_inflight.clear()
        for job in tuple(self.jobs): job.cancel.set()
        for key in tuple(self.requests): self.api.cancel(key)
        self.requests.clear()
        self._snapshot_owner()
        self.account_id = target
        path = self._profile(target)
        self.state = self._read(path,{'entries':{},'cursor':'0','view':None})
        for identity,deferred in self.state.get('deferred_downloads',{}).items():
            record=deferred.get('record',{}) if isinstance(deferred,dict) else {}
            if record.get('id')==identity and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity):
                self.download_transfers[identity]=dict(record)
        if self.receive_enabled:self.state.pop('awaiting_receive',None)
        if self.state.get('view') is None:
            if target and self.owner is None and not self.meta.get('imported_guest'):
                self.state['view'] = self.adapter.snapshot(); self.meta['imported_guest'] = True
            else: self.state['view'] = {'permanent':[],'texts':{},'metadata':{}}
        self.owner = target; self.meta['owner'] = target
        atomic_json(self.meta_path,self.meta)
        self.applying = True
        try:
            if hasattr(self.adapter,'cancel_pending'):self.adapter.cancel_pending()
            self.adapter.restore(self.state['view'])
        finally: self.applying = False
        self._persist()
        if target:
            self.local_changed(); self.timer.start(); self.debounce.start(1)
            self._schedule_remote_poll()
        else: self.status_changed.emit('未登录；内容仅保留在这台设备。')

    def local_changed(self):
        if self.closed or self.applying or self.state is None: return
        if not self.account_id:
            self._persist(); return
        view = self.adapter.snapshot(); refs = view.get('permanent',[])
        entries = self.state.setdefault('entries',{})
        by_ref = {e['ref']:e for e in entries.values() if not e.get('deleted')}
        for ref in refs:
            text = view.get('texts',{}).get(ref)
            name = self.adapter.title(ref)
            fp = fingerprint(ref,text,name)
            entry = by_ref.get(ref)
            if entry is None:
                identity = str(uuid4())
                entry = {'id':identity,'ref':ref,'version':0,'seq':0}
                entries[identity] = entry
            if entry.get('fingerprint') != fp or entry.get('deleted'):
                entry.pop('blocked_fingerprint',None); entry.pop('last_error',None)
                entry.pop('paused_upload',None)
                entry.update(fingerprint=fp,name=name,dirty=True,deleted=False,
                             kind='url' if text is not None and re.fullmatch(r'https?://\S+',text.strip()) else 'text' if text is not None else 'file',
                             seq=entry.get('seq',0)+1)
        removed = False
        for entry in entries.values():
            if entry.get('ref') not in refs and not entry.get('deleted'):
                entry.update(deleted=True,dirty=True,seq=entry.get('seq',0)+1)
                removed = True
        self._persist()
        self._notify_transfers()
        if removed and self.running:
            self._interrupt_cycle()
        self.debounce.start(1 if removed else 400)

    def set_interactive(self,value):
        value=bool(value)
        if value==self._interactive:return
        self._interactive=value
        # WebSocket hints wake the journal immediately. Opening an interactive
        # window needs one reconciliation, not another GET every ten seconds.
        self.timer.setInterval(self._base_poll_ms)
        if value and self.service.signed_in:self.debounce.start(1)

    def set_event_stream_online(self,online):
        """Poll sparingly while live change hints cannot be delivered."""
        online=bool(online)
        if online==self._event_stream_online:return
        self._event_stream_online=online
        self._schedule_remote_poll()
        if online and self.account_id and self.receive_enabled:
            # One reconciliation catches commits that happened while the
            # socket was down, even if their pending reservation was unseen.
            self.request_refresh()

    def _schedule_remote_poll(self):
        """Recheck only pending uploads from another device if a hint goes missing."""
        device_id=getattr(self.service,'device_id',None)
        remote=[item for item in self.remote_transfers
                if not device_id or item.get('device_id') != device_id]
        if not self.receive_enabled or not self.account_id or not self.service.signed_in:
            self.remote_poll.stop()
            return
        # When all R2 parts are accepted, the last server commit should be
        # imminent. Otherwise a few seconds between checks is sufficient and
        # does not fan out one request per progress packet.
        if remote:
            ready=any(item.get('state') == 'verifying' or
                      item.get('size',0) == item.get('received_bytes',-1)
                      for item in remote)
            self.remote_poll.start(min(self._remote_poll_ms,1500) if ready
                                   else self._remote_poll_ms)
        elif not self._event_stream_online:
            self.remote_poll.start(self._disconnected_poll_ms)
        else:self.remote_poll.stop()

    def request_refresh(self):
        if self.closed or not self.account_id or not self.service.signed_in or not self.receive_enabled:return
        if self.running:self._refresh_again=True
        else:self.debounce.start(1)

    def _notify_transfers(self):
        if self.state is None:return
        transfers=[];known=set()
        for identity,entry in (self.state.get('entries',{}).items() if self.upload_enabled else ()):
            if entry.get('deleted') or not entry.get('dirty') or entry.get('paused_upload'):continue
            if entry.get('blocked_fingerprint')==entry.get('fingerprint'):continue
            resume=self.state.get('resumable_uploads',{}).get(identity,{})
            size=resume.get('size',0);received=min(size,resume.get('next_part',0)*resume.get('chunk_size',0)+resume.get('_sent_bytes',0))
            sent_all=(resume.get('upload_id') and isinstance(resume.get('part_count'),int)
                      and (resume.get('next_part',0)>=resume['part_count']
                           or size>0 and received>=size))
            phase=('verifying' if sent_all else 'uploading') if resume.get('upload_id') else (
                'preparing' if self.transfer and self.transfer.get('id')==identity else 'queued')
            transfers.append(dict(id=identity,ref=entry['ref'],name=entry.get('name',''),kind=entry.get('kind','file'),
                upload_id=resume.get('upload_id'),received=received,total=size,phase=phase,remote=False))
            known.add(identity)
        entries=self.state.get('entries',{})
        for identity,record in (self.download_transfers.items() if self.receive_enabled else ()):
            if identity in known:continue
            cached=self.state.get('downloads',{}).get(identity,{})
            hashes=cached.get('hashes',{})
            size=record.get('size',0);chunk=cached.get('chunk_size',0)
            received=sum(min(chunk,max(0,size-int(index)*chunk)) for index in hashes if str(index).isdecimal())
            in_flight=record.get('_inflight_bytes',{})
            received+=sum(min(max(0,size-int(index)*chunk),max(0,int(value)))
                          for index,value in in_flight.items()
                          if str(index).isdecimal() and str(index) not in hashes)
            received=min(size,received)
            phase=('retrying' if self.state.get('deferred_downloads',{}).get(identity,{}).get('retry_at',0)>time.time() else
                   'validating' if record.get('_assembling') else
                   'retrying' if not self.running and self._paused('not_before') else 'downloading')
            transfers.append(dict(id=identity,ref=entries.get(identity,{}).get('ref'),name=record.get('name',''),
                kind=record.get('kind','file'),received=received,total=size,
                phase=phase,remote=True))
            known.add(identity)
        for remote in (self.remote_transfers if self.receive_enabled else ()):
            identity=remote.get('item_id')
            if identity in known:continue
            # A cancelled local fingerprint is deliberately local-only.
            local=entries.get(identity,{})
            if remote.get('device_id')==getattr(self.service,'device_id','') and local.get('paused_upload'):continue
            if remote.get('device_id')==getattr(self.service,'device_id',''):continue
            transfers.append(dict(id=identity,ref=local.get('ref'),name=remote.get('name',''),kind=remote.get('kind','file'),
                upload_id=remote.get('upload_id'),received=remote.get('received_bytes',0),total=remote.get('size',0),
                phase='remote',remote=True,device_name=remote.get('device_name','另一台设备')))
        self.transfers_changed.emit(transfers)

    def _interrupt_cycle(self):
        self.epoch+=1
        self.running=False;self.transfer=None
        self._part_cache.clear()
        self._cleanup_inflight.clear()
        for key in tuple(self.requests):self.api.cancel(key)
        self.requests.clear()
        for job in tuple(self.jobs):job.cancel.set()

    def cancel_upload(self,value,*,remove=False):
        if self.closed or not self.state or not self.account_id:return False
        entry=next((e for e in self.state.get('entries',{}).values() if value in (e.get('id'),e.get('ref'))),None)
        if entry:
            self._interrupt_cycle()
            if remove:entry.update(deleted=True,dirty=True,seq=entry.get('seq',0)+1)
            else:
                entry['paused_upload']=True
                resume=self.state.get('resumable_uploads',{}).get(entry['id'],{})
                if resume.get('sha256'):
                    entry.update(cancelled_hash=resume['sha256'],cancelled_seq=resume.get('seq'),
                        cancelled_fingerprint=resume.get('fingerprint'),cancelled_upload_id=resume.get('upload_id'))
            self._forget_resume(entry['id'])
        else:
            remote=next((e for e in self.remote_transfers if value in (e.get('item_id'),e.get('upload_id'))),None)
            if not remote:return False
            self._interrupt_cycle()
            pending=self.state.setdefault('pending_uploads',[])
            if remote['upload_id'] not in pending:pending.append(remote['upload_id'])
            self.remote_transfers.remove(remote)
        self._persist();self._notify_transfers();self.debounce.start(1)
        return True

    def retry_upload(self,ref):
        if not self.state:return
        for entry in self.state.get('entries',{}).values():
            if entry.get('ref')==ref:
                entry.pop('paused_upload',None);entry.pop('blocked_fingerprint',None);entry['dirty']=True
                ignored=self.state.get('ignored_versions',{}).get(entry['id'],0)
                if isinstance(ignored,int):entry['version']=max(entry.get('version',0),ignored)
        self._persist();self._notify_transfers();self.debounce.start(1)

    def is_upload_paused(self,ref):
        return bool(self.state and self.service.signed_in and any(e.get('ref')==ref and
            (e.get('paused_upload') or e.get('blocked_fingerprint')) and not e.get('deleted')
            for e in self.state.get('entries',{}).values()))

    def sync_status_for_ref(self,ref):
        """Return the journal's truth for a local shelf ref, without network or disk IO."""
        if not self.state or not self.account_id or not self.service.signed_in:
            return 'local'
        entry=next((item for item in self.state.get('entries',{}).values()
                    if item.get('ref')==ref and not item.get('deleted')),None)
        return self._sync_status_for_entry(entry)

    def sync_statuses_for_refs(self,refs):
        """Read many badge states in O(entries + refs) for a large overview."""
        refs=tuple(refs)
        if not self.state or not self.account_id or not self.service.signed_in:
            return {ref:'local' for ref in refs}
        entries={item.get('ref'):item for item in self.state.get('entries',{}).values()
                 if not item.get('deleted')}
        return {ref:self._sync_status_for_entry(entries.get(ref)) for ref in refs}

    def _sync_status_for_entry(self,entry):
        if entry and not entry.get('dirty') and entry.get('version',0)>0:return 'synced'
        if not self.upload_enabled:return 'local'
        if entry is None:return 'pending'
        if entry.get('blocked_fingerprint') and entry.get('blocked_fingerprint')==entry.get('fingerprint'):
            return 'error'
        if entry.get('paused_upload') or self._paused('quota_not_before'):
            return 'paused'
        if self._retry_paused() and self.state.get('retry_direction')=='upload':
            return 'paused'
        resume=self.state.get('resumable_uploads',{}).get(entry['id'],{})
        if resume.get('upload_id') or self.transfer and self.transfer.get('id')==entry['id']:
            return 'uploading'
        return 'pending'

    def cancel_download(self,identity,*,remove=False):
        record=self.download_transfers.get(identity)
        if not record or not self.state:return False
        self._interrupt_cycle();self.download_transfers.pop(identity,None)
        self.state.get('deferred_downloads',{}).pop(identity,None)
        # This was the sole record being received when the retry deadline was
        # set. Once the user skips/deletes it, that deadline must not hold up
        # unrelated files later in the same remote change page.
        if self.state.get('retry_direction')=='receive':
            self.state.pop('not_before',None)
            self.state.pop('retry_direction',None)
        if remove:
            entry=self.state.setdefault('entries',{}).setdefault(identity,dict(id=identity,ref='flickdock:transfer:'+identity,seq=0))
            entry.update(version=record['version'],deleted=True,dirty=True,seq=entry.get('seq',0)+1)
        else:self.state.setdefault('ignored_versions',{})[identity]=record['version']
        self._forget_download(identity)
        self._persist();self._notify_transfers();self.debounce.start(1)
        return True

    def _request(self, method, path, callback, **kwargs):
        epoch = self.epoch; holder = []
        def done(result,error):
            if holder: self.requests.discard(holder[0])
            if self.closed or epoch != self.epoch: return
            callback(result,error)
        key = self.api.request(method,path,done,**kwargs)
        holder.append(key); self.requests.add(key)
        return key

    def _work(self, function, args, callback):
        epoch = self.epoch; job = DiskWork(function,*args); self.jobs.add(job)
        def done(result,error):
            self.jobs.discard(job)
            if self.closed or epoch != self.epoch:
                if function is stage_item and isinstance(result,dict) and result.get('path'):
                    Path(result['path']).unlink(missing_ok=True)
                return
            callback(result,error)
        job.signals.done.connect(done); self.pool.start(job)

    def _error(self,error):
        if getattr(error,'code','') in ('UPLOAD_CANCELLED','UPLOAD_EXPIRED'):
            transfer=self.transfer or {}
            entry=self.state.get('entries',{}).get(transfer.get('id'))
            if entry and entry.get('seq')==transfer.get('seq'):entry['paused_upload']=True
            if transfer.get('id'):self._forget_resume(transfer['id'],completed=getattr(error,'code','')=='UPLOAD_CANCELLED')
            self.running=False;self.transfer=None
            self._persist();self._notify_transfers();self.debounce.start(1)
            self.status_changed.emit('上传已取消，原文件仍保留在本机。')
            return
        self.running = False; self.transfer = None
        self._notify_transfers()
        if getattr(error,'code','') == 'cancelled': return
        message = {'ACCOUNT_QUOTA':'云空间已满，请清理内容后重试。',
                   'GLOBAL_QUOTA':'共享云空间已满，内容会先保留在本机。',
                   'RATE_LIMITED':'同步请求暂时达到限额，稍后会自动重试。',
                   'STORAGE_REQUEST_BUDGET':'本月云存储请求已达限额，内容会先保留在本机。',
                   'PLATFORM_QUOTA':'今日云同步免费额度已用完，内容会先保留在本机。',
                   'UPLOAD_LIMIT':'正在清理中断的上传，稍后会自动重试。',
                    'SERVICE_UNAVAILABLE':'云服务暂时不可用，稍后会自动重试。',
                    'DOWNLOAD_SAVE_FAILED':'无法保存云端文件，请检查保存位置或文件占用。',
                    'CLIENT_PROCESSING_ERROR':'云同步处理失败，稍后会自动重试。'}.get(getattr(error,'code',''))
        self.status_changed.emit(message or (str(error) if not getattr(error,'code','') else '云同步暂时中断，稍后会自动重试。'))
        delay = self._retry_delay(error)
        self.state['not_before'] = time.time()+delay
        self.state['retry_direction'] = self._cycle_direction
        if self._quota_error(error): self.state['quota_not_before'] = self.state['not_before']
        self._persist()
        self._notify_transfers()
        self.debounce.start(min(2147483647,int(delay*1000)+1))

    @staticmethod
    def _quota_error(error):
        return getattr(error,'code','') in ('STORAGE_REQUEST_BUDGET','PLATFORM_QUOTA') or getattr(error,'status',0)==1027

    def _needs_remote_version(self):
        """Stop retrying a conflicting upload while inbound sync is off."""
        self.state['awaiting_receive']=True
        self.running=False;self.transfer=None
        self._persist();self._notify_transfers()
        self.status_changed.emit('云端版本有变化；开启接收后才能继续上传该项。')

    @staticmethod
    def _retry_delay(error):
        delay = 30
        details = getattr(error,'details',None)
        if isinstance(details,dict):
            problem = details.get('error',details)
            retry = problem.get('retry_after') if isinstance(problem,dict) else None
            if isinstance(retry,(int,float)) and 0 < retry <= 31*86400:
                delay = max(delay,retry)
        if getattr(error,'code','') == 'PLATFORM_QUOTA' and delay == 30:
            delay = max(30, 86400-time.time()%86400+5)
        return delay

    def _paused(self, key):
        value = self.state.get(key,0)
        return isinstance(value,(int,float)) and time.time() < value

    def _retry_paused(self):
        direction=self.state.get('retry_direction')
        if direction=='upload' and not self.upload_enabled:return False
        if direction=='receive' and not self.receive_enabled:return False
        return self._paused('not_before')

    def _schedule_after_pause(self, *keys):
        """Keep the absolute retry deadline after an early change hint.

        A hint and the retry share ``debounce``. Starting it for the hint
        replaces the pending retry, so every paused exit must rearm it.
        """
        now = time.time()
        deadlines = [self.state.get(key,0) for key in keys]
        remaining = max((value-now for value in deadlines
                         if isinstance(value,(int,float)) and value>now),default=0)
        if remaining > 0:
            wait = min(2147483647,max(1,int(remaining*1000)+1))
            # Cleanup and content uploads have independent cooldowns. Keep a
            # pending earlier wake-up when both need another attempt.
            if self.debounce.isActive():
                wait = min(wait,max(1,self.debounce.remainingTime()))
            self.debounce.start(wait)
            return True
        return False

    def tick(self):
        if self.closed or self.running or not self.account_id or not self.service.signed_in: return
        if not self.upload_enabled and not self.receive_enabled:
            self.status_changed.emit('云同步已暂停；内容保留在本机。')
            return
        if self._paused('quota_not_before'):
            self._schedule_after_pause('quota_not_before')
            self.status_changed.emit('云同步额度用完，内容已保留在本机，额度恢复后继续。')
            return
        self.state.pop('quota_not_before',None)
        self.cleanup_attempts = {}
        self.running = True; self.status_changed.emit('正在同步…')
        self._retire_uploads()

    def _after_cleanup(self):
        if self._retry_paused() or self._paused('quota_not_before'):
            self.running = False; self.transfer = None
            self._schedule_after_pause(*(('not_before',) if self._retry_paused() else ()),
                                       'quota_not_before')
            self.status_changed.emit('云同步额度暂时用完，稍后自动续传。' if self._paused('quota_not_before')
                                     else '云同步暂时中断，稍后自动续传。')
            return
        self.state.pop('not_before',None)
        self._pull()

    def _cleanup_error(self, error):
        if getattr(error,'code','') == 'cancelled': self.running=False; return
        delay = self._retry_delay(error)
        self.state['cleanup_not_before'] = time.time()+delay
        if self._quota_error(error): self.state['quota_not_before'] = self.state['cleanup_not_before']
        self._persist()
        self.debounce.start(min(2147483647,int(delay*1000)+1))
        self.status_changed.emit('云端清理暂时中断，删除操作已保留并会自动重试。')
        self._after_cleanup()

    def _pending_removal(self):
        if self._paused('cleanup_not_before'): return None
        return next((e for e in self.state['entries'].values() if e.get('dirty') and e.get('deleted')
                     and e.get('version',0)>0 and e.get('cleanup_conflict_version') != e.get('version')),None)

    def _owned_stage(self, transfer):
        try:
            path = Path(transfer['path'])
            directory = self.root/self.account_id/'outbox'
            return (not directory.is_symlink() and not directory.parent.is_symlink()
                    and not path.is_symlink() and re.fullmatch(r'[0-9a-f]{32}\.blob',path.name) is not None
                    and path.resolve().parent == directory.resolve())
        except (KeyError,TypeError,ValueError,OSError): return False

    def _resume_matches(self, transfer):
        entry = self.state['entries'].get(transfer.get('id'))
        if not entry or entry.get('deleted') or not entry.get('dirty') or entry.get('paused_upload'): return False
        if any(entry.get(key) != transfer.get(key) for key in ('id','ref','seq','fingerprint','name','kind','version')):
            return False
        if (transfer.get('upload_id') and (entry.get('pending_hash') != transfer.get('sha256')
                or entry.get('pending_seq') != transfer.get('seq'))): return False
        try:
            return (self._owned_stage(transfer) and Path(transfer['path']).is_file()
                    and Path(transfer['path']).stat().st_size == transfer['size']
                    and isinstance(transfer['size'],int) and 0 <= transfer['size'] <= MAX_FILE_BYTES
                    and re.fullmatch(r'[0-9a-f]{64}',str(transfer['sha256'])) is not None)
        except (KeyError,TypeError,OSError): return False

    def _forget_resume(self, identity, *, completed=False):
        transfer = self.state.setdefault('resumable_uploads',{}).pop(identity,None)
        if not isinstance(transfer,dict): return
        if self._owned_stage(transfer):
            try: Path(transfer['path']).unlink(missing_ok=True)
            except OSError: pass
        upload_id = transfer.get('upload_id')
        pending = self.state.setdefault('pending_uploads',[])
        if completed and upload_id in pending: pending.remove(upload_id)
        entry = self.state['entries'].get(identity)
        if entry and entry.get('pending_seq') == transfer.get('seq'):
            entry.pop('pending_hash',None); entry.pop('pending_seq',None)

    def _retire_uploads(self):
        if not self.upload_enabled:
            self._after_cleanup()
            return
        self._cycle_direction='upload'
        resumes = self.state.setdefault('resumable_uploads',{})
        for identity,transfer in tuple(resumes.items()):
            if not isinstance(transfer,dict) or not self._resume_matches(transfer):
                self._forget_resume(identity)
        self._persist()
        if self._paused('cleanup_not_before'):
            self._after_cleanup()
            self._schedule_after_pause('cleanup_not_before')
            return
        self.state.pop('cleanup_not_before',None)
        removal = self._pending_removal()
        if removal:
            identity, version = removal['id'],removal['version']
            self.cleanup_attempts[identity] = self.cleanup_attempts.get(identity,0)+1
            def removed(result,error):
                current = self.state['entries'].get(identity)
                if error and getattr(error,'status',0) not in (404,409):
                    self._cleanup_error(error); return
                if error and getattr(error,'status',0)==409:
                    if current and current.get('version') == version:
                        details = getattr(error,'details',{})
                        problem = details.get('error',details) if isinstance(details,dict) else {}
                        record = problem.get('current_record') if isinstance(problem,dict) else None
                        if (isinstance(record,dict) and record.get('id') == identity
                                and isinstance(record.get('version'),int) and version <= record['version'] <= 9007199254740991):
                            if record.get('deleted'): self.state['entries'].pop(identity,None)
                            else:
                                current['version'] = record['version']; current.pop('cleanup_conflict_version',None)
                                if self.cleanup_attempts[identity]>=2:
                                    # Another device is actively changing this
                                    # item. Keep its latest version and retry
                                    # cleanup independently after a short pause.
                                    self.state['cleanup_not_before'] = time.time()+30
                                    self._persist(); self.debounce.start(30001); self._after_cleanup(); return
                        else: current['cleanup_conflict_version'] = version
                elif current and current.get('deleted'):
                    self.state['entries'].pop(identity,None); self._forget_download(identity)
                self._persist(); self._retire_uploads()
            self._request('DELETE','/v1/items/'+quote(identity,safe=''),removed,data={'base_version':version}); return
        abandoned = self.state.setdefault('pending_uploads',[])
        active = {x.get('upload_id') for x in resumes.values()}
        identity = next((x for x in abandoned if x not in active and x not in self._cleanup_inflight),None)
        if identity is not None:
            # A slow R2 purge must not keep a new file queued. The cancellation
            # receipt is independent of the active content-sync cycle.
            self._cleanup_inflight.add(identity)
            def done(result,error):
                self._cleanup_inflight.discard(identity)
                if error and getattr(error,'status',0) not in (404,409):
                    delay = self._retry_delay(error)
                    self.state['cleanup_not_before'] = time.time()+delay
                    if self._quota_error(error): self.state['quota_not_before']=self.state['cleanup_not_before']
                    self._persist();self.debounce.start(min(2147483647,int(delay*1000)+1))
                    self.status_changed.emit('云端清理暂时中断，删除操作已保留并会自动重试。')
                    return
                if error and getattr(error,'code','') == 'ALREADY_COMMITTED':
                    details = getattr(error,'details',{})
                    problem = details.get('error',details) if isinstance(details,dict) else {}
                    record = problem.get('current_record') if isinstance(problem,dict) else None
                    if isinstance(record,dict):
                        current = self.state['entries'].get(record.get('id'))
                        if (current and current.get('deleted') and current.get('dirty')
                                and isinstance(record.get('version'),int) and record['version']>0):
                            current['version'] = record['version']
                            if record.get('deleted'): self.state['entries'].pop(current['id'],None)
                if identity in abandoned: abandoned.remove(identity)
                self._persist()
                if not self.running: self.debounce.start(1)
            self._request('DELETE','/v1/uploads/'+quote(identity,safe=''),done)
        self._persist();self._after_cleanup()

    def _pull(self):
        if not self.receive_enabled:
            self.remote_transfers=[]
            self._push_next()
            return
        self._cycle_direction='receive'
        cursor = str(self.state.get('cursor','0'))
        if not cursor.isdecimal(): cursor = '0'
        def done(result,error):
            if error: self._error(error); return
            changes = result.get('changes',[])
            remote=result.get('transfers',[])
            if isinstance(remote,list) and len(remote)<=100:
                self.remote_transfers=[r for r in remote if isinstance(r,dict) and isinstance(r.get('size'),int)
                    and r.get('size',-1)>=0 and isinstance(r.get('item_id'),str) and isinstance(r.get('upload_id'),str)]
                self._schedule_remote_poll()
            if not isinstance(changes,list) or len(changes)>100: self._error(ValueError('云同步记录无效。')); return
            pending = list(enumerate(changes))
            incoming={change.get('record',{}).get('id') for change in changes if isinstance(change,dict)}
            for identity,deferred in list(self.state.get('deferred_downloads',{}).items()):
                if identity not in incoming and deferred.get('retry_at',0)<=time.time():
                    pending.append((len(pending),{'cursor':str(deferred.get('remote_order') or ''),'record':deferred['record']}))
            def next_record():
                if not pending:
                    self.state['cursor'] = str(result['cursor'])
                    self.state.pop('awaiting_receive',None)
                    self._persist()
                    self._notify_transfers()
                    if result.get('has_more'): self._pull()
                    else: self._push_next()
                    return
                # A 90 MiB item must not keep a later independent text or
                # small file out of the shelf until its download finishes.
                # Only the first queued revision of each item may move ahead;
                # updates and tombstones for that item retain server order.
                seen = set()
                chosen = 0
                for position, (_, change) in enumerate(pending):
                    record = change.get('record',{}) if isinstance(change,dict) else {}
                    identity = str(record.get('id')) if isinstance(record,dict) else ''
                    if identity in seen: continue
                    seen.add(identity)
                    size = record.get('size') if isinstance(record,dict) else None
                    if record.get('deleted') or isinstance(size,int) and size <= 4*1024*1024:
                        chosen = position
                        break
                _, change = pending.pop(chosen)
                cursor = change.get('cursor')
                order = int(cursor) if isinstance(cursor,str) and cursor.isdecimal() and 0 < int(cursor) <= 9007199254740991 else None
                self._receive(change['record'],next_record,remote_order=order)
            next_record()
        self._request('GET','/v1/sync/changes?cursor='+cursor+'&limit=100',done)

    def _fork(self,entry):
        fork = dict(entry,id=str(uuid4()),version=0,dirty=True,seq=entry.get('seq',0)+1)
        old_ref = entry['ref']
        if old_ref.startswith('flickdock:text:'):
            fork['ref'] = 'flickdock:text:'+fork['id']
            def change(view):
                view['permanent'] = [fork['ref'] if ref==old_ref else ref for ref in view['permanent']]
                view.setdefault('texts',{})[fork['ref']] = view['texts'].pop(old_ref)
                view.setdefault('metadata',{})[fork['ref']] = view['metadata'].pop(old_ref,{})
            self._change_view(change)
        self.state['entries'][fork['id']] = fork
        return fork

    def _receive(self, record, callback, *, remote_order=None):
        identity = str(record.get('id',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity): self._error(ValueError('同步项目标识无效。')); return
        if not isinstance(record.get('version'),int) or not 1 <= record['version'] <= 9007199254740991:
            self._error(ValueError('同步项目版本无效。')); return
        deferred=self.state.setdefault('deferred_downloads',{}).get(identity)
        if deferred:
            if record['version']==deferred['record'].get('version') and deferred.get('retry_at',0)>time.time():
                callback();return
            if record['version']>deferred['record'].get('version',0):self.state['deferred_downloads'].pop(identity,None)
        entries = self.state['entries']; entry = entries.get(identity)
        ignored=self.state.get('ignored_versions',{}).get(identity,0)
        if not record.get('deleted') and isinstance(ignored,int) and record['version']<=ignored:
            callback();return
        if entry and record['version'] <= entry.get('version',0): callback(); return
        # A pending removal stays queued when another device updates that item.
        if entry and entry.get('deleted') and entry.get('dirty'):
            entry['version'] = record['version']
            if record.get('deleted'): entries.pop(identity,None)
            self._persist(); callback(); return
        if record.get('deleted'):
            self.state.get('deferred_downloads',{}).pop(identity,None)
            self.download_transfers.pop(identity,None)
            self._forget_download(identity)
            if entry:
                if entry.get('dirty'): self._fork(entry)
                else: self._remove_ref(entry['ref'])
                entries.pop(identity,None)
            self._persist(); callback(); return
        if not isinstance(record.get('size'),int) or not 0 <= record['size'] <= MAX_FILE_BYTES or not re.fullmatch(r'[0-9a-f]{64}',str(record.get('sha256',''))):
            self._error(ValueError('同步文件信息无效。')); return
        if entry and entry.get('dirty'):
            view=self.adapter.snapshot()
            current_fp=fingerprint(entry['ref'],view.get('texts',{}).get(entry['ref']),self.adapter.title(entry['ref']))
            if current_fp!=entry.get('fingerprint'):self.local_changed()
        # A connection can close after the server commits but before its reply
        # arrives. Recognize our exact staged revision instead of duplicating it.
        if (entry and entry.get('dirty') and entry.get('name') == record['name'] and (
                (entry.get('pending_hash') == record['sha256'] and entry.get('pending_seq') == entry.get('seq')) or
                (entry.get('cancelled_hash') == record['sha256'] and entry.get('cancelled_seq') == entry.get('seq') and
                 entry.get('cancelled_fingerprint')==entry.get('fingerprint') and entry.get('cancelled_upload_id')))):
            entry.update(version=record['version'],sha256=record['sha256'],dirty=False)
            self._forget_resume(identity,completed=True)
            entry.pop('pending_hash',None); entry.pop('pending_seq',None)
            entry.pop('cancelled_hash',None);entry.pop('cancelled_seq',None);entry.pop('paused_upload',None)
            entry.pop('cancelled_fingerprint',None);entry.pop('cancelled_upload_id',None)
            self._persist(); callback(); return
        if entry and not entry.get('dirty') and entry.get('sha256') == record['sha256']:
            entry['version'] = record['version']; entry['name'] = record['name']
            self._set_title(entry['ref'],record['name']); self._persist(); callback(); return
        if entry and entry.get('dirty'):
            self._fork(entry)
            entries.pop(identity,None)
            entry = None
            self.status_changed.emit('另一台设备也修改了此项，已保留两个版本。')
        target = self.root/self.account_id/'files'/identity/str(record['version'])/safe_name(record['name'])
        self.download_transfers[identity]=dict(record);self._notify_transfers()
        def receive_failed(error):
            # Authentication/quota errors affect the account. One damaged file,
            # local save failure or interrupted body affects only this item.
            if getattr(error,'status',0) in (401,403,429) or self._quota_error(error):
                self._error(error);return
            pending=self.state.setdefault('deferred_downloads',{})
            attempts=pending.get(identity,{}).get('attempts',0)+1
            message=re.sub(r'https?://\S+','[连接]',str(error))[:180]
            pending[identity]=dict(record=dict(record),remote_order=remote_order,attempts=attempts,
                retry_at=time.time()+min(60,2**min(attempts,6)),last_error=message)
            self.download_transfers[identity].pop('_assembling',None)
            self._persist();self._notify_transfers()
            self.status_changed.emit('“'+str(record['name'])[:60]+'”同步未完成：'+message+'。其他文件继续同步。')
            callback()
        def downloaded(path,error):
            if error: receive_failed(error); return
            # Edits made while downloading must be preserved too.
            current = entries.get(identity)
            if current and current.get('dirty'):
                self._fork(current); entries.pop(identity,None); current = None
            def apply_ref(ref,text=None):
                if current: self._remove_ref(current['ref'])
                if text is not None: ref = 'flickdock:text:'+identity
                self._add_ref(ref,text,record['name'],remote_order=remote_order)
                entries[identity] = {'id':identity,'ref':ref,'version':record['version'],
                    'sha256':record['sha256'],'name':record['name'],'kind':record['kind'],
                    'fingerprint':fingerprint(ref,text,record['name']),'dirty':False,'deleted':False,'seq':0,
                    **({'remote_order':remote_order} if remote_order is not None else {})}
                self.download_transfers.pop(identity,None)
                self.state.get('deferred_downloads',{}).pop(identity,None)
                self._persist(); self._notify_transfers();callback()
            if record['kind'] in ('text','url'):
                def decoded(text,error):
                    if error: receive_failed(error)
                    else: apply_ref('',text)
                self._work(read_utf8,(path,),decoded)
            elif record.get('mime_type') == DIRECTORY_MIME:
                destination = target.with_name(target.name+'-folder')
                def unpacked(ref,error):
                    if error: receive_failed(error)
                    else: apply_ref(ref)
                self._work(unpack_directory,(path,destination),unpacked)
            else: apply_ref(str(path))
        self._download_record(record,target,downloaded,
                               changed=lambda current:self._receive(current,callback,remote_order=remote_order) if current else callback())

    def _download_target(self, record):
        identity, version = record.get('id'),record.get('version')
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(identity))
                or not isinstance(version,int) or not 1 <= version <= 9007199254740991): return None
        target = self.root/self.account_id/'files'/identity/str(version)/safe_name(record.get('name',''))
        # A journal stores only metadata, never an arbitrary deletion/read path.
        if target.is_symlink() or any(p.is_symlink() for p in tuple(target.parents)[:4]): return None
        return target

    def _forget_download(self, identity, finished=None):
        cached = self.state.setdefault('downloads',{}).pop(identity,None)
        target = self._download_target(dict(cached,id=identity)) if isinstance(cached,dict) else None
        count = cached.get('part_count') if isinstance(cached,dict) else None
        if target is not None and isinstance(count,int) and 0 <= count <= MAX_FILE_BYTES//65536:
            self._work(_remove_download_parts,(target,count),lambda result,error:finished() if finished else None)
        elif finished:
            epoch = self.epoch
            QTimer.singleShot(0,lambda:finished() if not self.closed and epoch == self.epoch else None)

    def _download_record(self, record, target, callback, *, changed=None):
        identity = quote(record['id'],safe='')
        previous=self.state.get('downloads',{}).get(record['id'],{})
        # Large multipart objects use the service's bounded range view. Some
        # routes cleanly terminate 64 MiB streams early, without an HTTP error.
        small_parts=[record['size']>90*1024*1024 or
            previous.get('version')==record['version'] and 0<previous.get('chunk_size',0)<=4*1024*1024 and record['size']>4*1024*1024]
        def content_headers():return {} if small_parts[0] else {'X-FlickDock-Fast-Content':'1'}
        expected_target = self._download_target(record)
        if expected_target is None or Path(target).absolute() != expected_target.absolute():
            callback(None,ValueError('同步文件缓存位置无效。')); return
        def metadata(result,error):
            if error:
                if getattr(error,'status',0)==404 and changed:
                    self._forget_download(record['id'])
                    self.download_transfers.pop(record['id'],None)
                    self._persist(); self._notify_transfers(); changed(None)
                else: callback(None,error)
                return
            current = result.get('record',{})
            if current.get('version') != record['version'] or current.get('sha256') != record['sha256']:
                if (changed and current.get('id') == record['id'] and isinstance(current.get('version'),int)
                        and current['version'] > record['version']):
                    cached = self.state.setdefault('downloads',{}).get(record['id'],{})
                    if cached.get('version') != current['version']: self._forget_download(record['id'])
                    self._persist(); changed(current)
                else: callback(None,ValueError('云端文件版本已改变，请稍后重试。'))
                return
            chunk_size, count = result.get('chunk_size'), result.get('part_count')
            large_parts=(isinstance(chunk_size,int) and chunk_size>4*1024*1024 and isinstance(count,int) and count>1)
            if (not isinstance(chunk_size,int) or not 65536<=chunk_size<=90*1024*1024 or not isinstance(count,int)
                    or count != (record['size']+chunk_size-1)//chunk_size
                    or (chunk_size>4*1024*1024 and not (count==1 and chunk_size==record['size'])
                        and not (large_parts and record['size']>90*1024*1024 and chunk_size%(1024*1024)==0))):
                callback(None,ValueError('同步分块信息无效。')); return
            target.parent.mkdir(parents=True,exist_ok=True)
            downloads = self.state.setdefault('downloads',{})
            spec = dict(version=record['version'],name=record['name'],sha256=record['sha256'],
                        size=record['size'],chunk_size=chunk_size,part_count=count)
            cached = downloads.get(record['id'])
            waiting_cleanup = False
            if not isinstance(cached,dict) or any(cached.get(k) != v for k,v in spec.items()):
                waiting_cleanup = cached is not None
                self._forget_download(record['id'],(lambda:part()) if waiting_cleanup else None)
                cached = dict(spec,hashes={}); downloads[record['id']] = cached
            if not isinstance(cached.get('hashes'),dict): cached['hashes'] = {}
            self._persist()
            parts = [None]*count
            queue = iter(range(count)); active=set();completed=set();halted=False;assembling=False
            request_keys=set()
            failure=[None]
            def assembled(path,error):
                self.download_transfers.get(record['id'],{}).pop('_assembling',None)
                if error is None or isinstance(error,ValueError): downloads.pop(record['id'],None)
                self._persist(); callback(path,error)
            def failed(error,index):
                if halted:return
                active.discard(index)
                self.download_transfers.get(record['id'],{}).get('_inflight_bytes',{}).pop(str(index),None)
                self._notify_transfers()
                if failure[0] is None:failure[0]=error
                part()
            def part():
                nonlocal halted,assembling
                if halted:return
                if failure[0] is not None:
                    if not active:
                        halted=True;self._persist();callback(None,failure[0])
                    return
                if self._pending_removal() is not None:
                    halted=True;self._interrupt_cycle();self.tick();return
                if len(completed)==count and not active:
                    if not assembling:
                        assembling=True;self._persist()
                        self.download_transfers.get(record['id'],{})['_assembling']=True
                        self._notify_transfers()
                        self._work(assemble_parts,(parts,target,record),assembled)
                    return
                while len(active)<4:
                    index=next(queue,None)
                    if index is None:break
                    active.add(index);start_part(index)
            def start_part(index):
                path = target.with_name('.'+target.name+f'.chunk-{index}')
                size = min(chunk_size,record['size']-index*chunk_size)
                def request_part():
                    if halted:return
                    if failure[0] is not None:failed(failure[0],index);return
                    cached['hashes'].pop(str(index),None)
                    self.download_transfers.get(record['id'],{}).get('_inflight_bytes',{}).pop(str(index),None)
                    try:
                        if self._download_target(record) is None or path.is_symlink():
                            raise ValueError('同步文件缓存位置无效。')
                        path.unlink(missing_ok=True)
                    except (OSError,ValueError) as error: failed(error,index); return
                    def receiving(downloaded,total):
                        current=self.download_transfers.get(record['id'])
                        if halted or current is None or current.get('version')!=record['version']:return
                        previous=current.setdefault('_inflight_bytes',{}).get(str(index),0)
                        if downloaded>previous:
                            current['_inflight_bytes'][str(index)]=min(size,downloaded)
                            self._notify_transfers()
                    request_keys.add(self._request('GET',f'/v1/items/{identity}/parts/{index}?version={record["version"]}',received,
                        destination=path,expected={'size':size,'with_digest':True},timeout_ms=120000,
                        headers=content_headers(),receive_progress=receiving))
                def recorded(digest,error):
                    if halted:return
                    if error or digest is None:
                        failed(error or ValueError('同步分块校验失败。'),index); return
                    self.download_transfers.get(record['id'],{}).get('_inflight_bytes',{}).pop(str(index),None)
                    cached['hashes'][str(index)] = digest; self._persist_progress()
                    self._notify_transfers()
                    parts[index]=path;active.discard(index);completed.add(index);part()
                def received(result,error):
                    nonlocal halted
                    if halted:return
                    if error:
                        if (chunk_size>4*1024*1024 and not small_parts[0]
                                and getattr(error,'status',0) not in (401,403,429)
                                and not self._quota_error(error)):
                            # A cleanly closed large response may still be
                            # truncated by a proxy. Request the server's small
                            # verified range view once, preserving whole SHA.
                            halted=True;small_parts[0]=True
                            for key in tuple(request_keys):self.api.cancel(key)
                            self._request('GET','/v1/items/'+identity+'/content',metadata,headers=content_headers())
                        elif getattr(error,'status',0) in (404,409) and changed:
                            halted=True
                            for key in tuple(request_keys):self.api.cancel(key)
                            self._request('GET','/v1/items/'+identity+'/content',metadata,
                                          headers=content_headers())
                        elif count==1:
                            # A complete body with a lost HTTP completion must
                            # not become an endless 100% retry. Only accept it
                            # after matching the whole committed SHA-256.
                            def recovered(digest,problem):
                                if digest and not problem:recorded(digest,None)
                                else:failed(error,index)
                            self._work(_checked_download_part,(path,size,record['sha256']),recovered)
                        else: failed(error,index)
                        return
                    # CloudApi already checked the server's SHA-256 as it
                    # streamed the bytes to a fsynced file. A second read
                    # here doubles disk traffic before assembly. Retain the
                    # disk check for injected/older transports that return a
                    # bare Path and for resumed cached parts below.
                    if (isinstance(result,dict) and result.get('path')==path
                            and re.fullmatch(r'[0-9a-f]{64}',str(result.get('sha256','')))):
                        recorded(result['sha256'],None)
                    else:self._work(_checked_download_part,(path,size,None),recorded)
                expected = cached['hashes'].get(str(index))
                if re.fullmatch(r'[0-9a-f]{64}',str(expected)):
                    def checked(digest,error):
                        if halted:return
                        if error: failed(error,index)
                        elif digest is None: request_part()
                        else:recorded(digest,None)
                    self._work(_checked_download_part,(path,size,expected),checked)
                else: request_part()
            if not waiting_cleanup: part()
        def request_metadata():
            self._request('GET','/v1/items/'+identity+'/content',metadata,
                          headers=content_headers())
        if Path(target).is_file() and not Path(target).is_symlink():
            def existing(digest,error):
                if digest and not error:callback(Path(target),None)
                else:request_metadata()
            self._work(_checked_download_part,(target,record['size'],record['sha256']),existing)
        else:request_metadata()

    def _change_view(self, operation):
        self.applying = True
        try:
            view = self.adapter.snapshot(); operation(view); self.adapter.restore(view)
        finally: self.applying = False

    def _remove_ref(self,ref):
        # A conflict fork may share this ref: it owns the local bytes now.
        if any(e.get('ref')==ref and e.get('dirty') and not e.get('deleted') for e in self.state['entries'].values()): return
        if hasattr(self.adapter,'remove_owned'):self.adapter.remove_owned(ref)
        def change(view):
            view['permanent'] = [x for x in view.get('permanent',[]) if x!=ref]
            view.setdefault('texts',{}).pop(ref,None); view.setdefault('metadata',{}).pop(ref,None)
        self._change_view(change)

    def _set_title(self,ref,name):
        self._change_view(lambda view:view.setdefault('metadata',{}).setdefault(ref,{}).update(title=name))

    def _add_ref(self,ref,text,name,*,remote_order=None):
        def change(view):
            if ref not in view['permanent']:
                position=len(view['permanent'])
                if remote_order is not None:
                    orders={entry.get('ref'):entry.get('remote_order') for entry in self.state['entries'].values()
                            if not entry.get('deleted')}
                    position=next((index for index,existing in enumerate(view['permanent'])
                                   if isinstance(orders.get(existing),int) and orders[existing]>remote_order),position)
                view['permanent'].insert(position,ref)
            if text is not None: view.setdefault('texts',{})[ref] = text
            view.setdefault('metadata',{}).setdefault(ref,{})['title'] = name
        self._change_view(change)

    def _push_next(self):
        if not self.upload_enabled or (not self.receive_enabled and self.state.get('awaiting_receive')):
            self.running=False;self.transfer=None
            self._notify_transfers()
            if self.state.get('awaiting_receive') and not self.receive_enabled:
                self.status_changed.emit('云端版本有变化；开启接收后才能继续上传该项。')
            elif not self.upload_enabled:self.status_changed.emit('云端内容已接收；本机更改等待上传。')
            else:self.status_changed.emit('搁架已同步。')
            if self._refresh_again and self.receive_enabled:
                self._refresh_again=False;self.debounce.start(1)
            return
        self._cycle_direction='upload'
        resumes = self.state.setdefault('resumable_uploads',{})
        for identity,transfer in tuple(resumes.items()):
            if not isinstance(transfer,dict) or not self._resume_matches(transfer):
                self._forget_resume(identity)
        active = {x.get('upload_id') for x in resumes.values()}
        if (self._pending_removal() is not None or (not self._paused('cleanup_not_before') and
                any(x not in active and x not in self._cleanup_inflight
                    for x in self.state.setdefault('pending_uploads',[])))):
            self._retire_uploads(); return
        pending = next((e for e in self.state['entries'].values() if e.get('dirty') and not e.get('paused_upload') and
                        ((e.get('deleted') and e.get('version',0)==0 and not self.state.get('pending_uploads')) or (not e.get('deleted') and
                        e.get('blocked_fingerprint') != e.get('fingerprint')))),None)
        if pending is None:
            self.running = False; self.transfer = None
            self._notify_transfers()
            blocked = sum(bool(e.get('dirty')) for e in self.state['entries'].values())
            retrying=len(self.state.get('deferred_downloads',{}))
            self.status_changed.emit(f'{retrying} 项等待重试，其他内容已同步。' if retrying else f'可同步内容已同步；{blocked} 项暂留本机，请检查文件大小或位置。' if blocked else '搁架已同步。')
            if self._refresh_again:
                self._refresh_again=False;self.debounce.start(1)
            elif self.state.get('deferred_downloads'):
                due=min(item.get('retry_at',time.time()) for item in self.state['deferred_downloads'].values())
                self.debounce.start(max(1,min(60000,round((due-time.time())*1000))))
            return
        identity = pending['id']
        if pending.get('deleted'):
            self.state['entries'].pop(identity,None); self._persist(); self._push_next(); return
        text = self.adapter.snapshot().get('texts',{}).get(pending['ref'])
        resume = resumes.get(identity)
        if resume:
            self.transfer = resume
            def verified(result,error):
                if error:
                    self._forget_resume(identity); self._persist(); self._retire_uploads(); return
                self._resume_upload(result)
            self._work(_verify_staged,(resume,),verified); return
        snapshot = dict(pending); self.transfer = snapshot
        self._notify_transfers()
        def staged(result,error):
            if error:
                entry = self.state['entries'].get(identity)
                if entry and entry.get('seq')==snapshot['seq']:
                    entry.update(blocked_fingerprint=snapshot['fingerprint'],last_error=str(error))
                self._persist(); self.status_changed.emit(str(error)); self._push_next(); return
            snapshot.update(result)
            if not self._resume_matches(snapshot):
                Path(snapshot['path']).unlink(missing_ok=True); self._push_next(); return
            self.state.setdefault('resumable_uploads',{})[identity] = snapshot
            self._persist(); self._notify_transfers(); self._initialize_upload(snapshot)
        self._work(stage_item,(pending['ref'],text,pending['name'],self.root/self.account_id/'outbox'),staged)

    def _initialize_upload(self, snapshot):
        identity = snapshot['id']
        if not self._resume_matches(snapshot):
            self._forget_resume(identity); self._persist(); self._retire_uploads(); return
        if snapshot.get('upload_id'):
            # A device can return after the server's cancellation receipt has
            # expired. Never silently recreate an old, unacknowledged UUID.
            try:old=time.time()-Path(snapshot['path']).stat().st_mtime>3*86400
            except OSError:old=True
            if old:
                from cloud_api import CloudError
                self._error(CloudError('上传断点已失效。',410,'UPLOAD_CANCELLED'));return
        if not snapshot.get('upload_id'):
            snapshot['upload_id'] = str(uuid4())
        pending = self.state.setdefault('pending_uploads',[])
        if snapshot['upload_id'] not in pending: pending.append(snapshot['upload_id'])
        current = self.state['entries'].get(identity)
        if current: current.update(pending_hash=snapshot['sha256'],pending_seq=snapshot['seq'])
        # Write the id before making the request. A timeout after server-side
        # creation must reuse the same reservation, including across restarts.
        self._persist()
        def initialized(result,error):
            if error:
                if getattr(error,'code','')=='UPLOAD_EXPIRED':self._error(error)
                elif getattr(error,'status',0)==409:
                    self._forget_resume(identity); self._persist()
                    if self.receive_enabled:self.running=False; self.tick()
                    else:self._needs_remote_version()
                else: self._error(error)
                return
            if result.get('instant') is True:
                self._upload_committed(snapshot,result.get('record'),instant=True)
                return
            chunk, count, upload_id = result.get('chunk_size'),result.get('part_count'),result.get('upload_id')
            large_parts=(isinstance(chunk,int) and chunk>4*1024*1024 and isinstance(count,int) and count>1)
            if (not isinstance(chunk,int) or not 65536 <= chunk <= 90*1024*1024
                    or count != (snapshot['size']+chunk-1)//chunk
                    or (chunk>4*1024*1024 and not (count==1 and chunk==snapshot['size'])
                        and not (large_parts and snapshot['size']>90*1024*1024 and chunk%(1024*1024)==0))
                    or upload_id != snapshot['upload_id']):
                self._error(ValueError('云同步上传信息无效。')); return
            snapshot.update(upload_id=upload_id,chunk_size=chunk,part_count=count,next_part=0)
            self._persist(); self._notify_transfers(); self._send_part(snapshot,0)
        self._request('POST','/v1/uploads',initialized,data={
            'upload_id':snapshot['upload_id'],
            'item_id':identity,'base_version':snapshot.get('version',0),'kind':snapshot['kind'],
            'name':snapshot['name'],'mime_type':snapshot['mime_type'],'size':snapshot['size'],'sha256':snapshot['sha256'],
            'fast_upload':True,'fast_multipart':True})

    def _resume_upload(self, transfer):
        if not self._resume_matches(transfer):
            self._forget_resume(transfer['id']); self._persist(); self._retire_uploads(); return
        if not transfer.get('upload_id') or 'chunk_size' not in transfer:
            self._initialize_upload(transfer); return
        def status(result,error):
            if error:
                if getattr(error,'code','')=='UPLOAD_EXPIRED':self._error(error)
                elif getattr(error,'status',0)==404:
                    from cloud_api import CloudError
                    self._error(CloudError('上传已中止，右键可重新上传。',410,'UPLOAD_CANCELLED'))
                elif getattr(error,'status',0)==409:
                    self._forget_resume(transfer['id']); self._persist()
                    if self.receive_enabled:self._retire_uploads()
                    else:self._needs_remote_version()
                else: self._error(error)
                return
            if any(result.get(remote) != transfer.get(local) for remote,local in (
                    ('upload_id','upload_id'),('item_id','id'),('base_version','version'),
                    ('size','size'),('sha256','sha256'),('chunk_size','chunk_size'),('part_count','part_count'))):
                self._error(ValueError('云同步断点与文件不符。')); return
            if result.get('state') == 'committed':
                # Repeating commit returns its existing record. Receive can
                # be disabled, so a changefeed pull cannot be the sole way to
                # settle a commit reply lost just before switching modes.
                if self.receive_enabled:self._pull()
                else:self._send_part(transfer,transfer['part_count'])
                return
            index = result.get('next_part')
            if (result.get('state') not in ('pending','verifying') or not isinstance(index,int)
                    or not 0 <= index <= transfer['part_count']
                    or (result.get('state') == 'verifying' and index != transfer['part_count'])):
                self._error(ValueError('云同步断点信息无效。')); return
            transfer['next_part'] = index; self._persist(); self._send_part(transfer,index)
        self._request('GET','/v1/uploads/'+quote(transfer['upload_id'],safe=''),status)

    def _upload_committed(self,transfer,record,*,instant=False):
        if (not isinstance(record,dict) or record.get('id')!=transfer['id']
                or not isinstance(record.get('version'),int)
                or record['version']!=transfer.get('version',0)+1
                or record.get('sha256')!=transfer['sha256']):
            self._error(ValueError('云同步提交信息无效。')); return
        entry = self.state['entries'].get(transfer['id'])
        pending_uploads = self.state.setdefault('pending_uploads',[])
        if transfer['upload_id'] in pending_uploads: pending_uploads.remove(transfer['upload_id'])
        if entry:
            entry.update(version=record['version'],sha256=record['sha256'])
            if entry.get('seq')==transfer['seq']:
                entry.update(dirty=False,fingerprint=transfer['fingerprint'])
            entry.pop('pending_hash',None); entry.pop('pending_seq',None)
        self._forget_resume(transfer['id'],completed=True)
        self._persist(); self._notify_transfers()
        if instant:self.status_changed.emit('云端已有相同内容，已秒传。')
        self._push_next()

    def _send_part(self,transfer,index):
        self._part_cache={key:value for key,value in self._part_cache.items() if key[0]==transfer.get('upload_id') and key[1]>=index}
        if not self._resume_matches(transfer):
            self._forget_resume(transfer['id']); self._persist(); self._retire_uploads(); return
        if index >= transfer['part_count']:
            def committed(result,error):
                if error:
                    if getattr(error,'code','')=='UPLOAD_EXPIRED':self._error(error)
                    elif getattr(error,'status',0)==409:
                        self._forget_resume(transfer['id']); self._persist()
                        if self.receive_enabled:self.running=False; self.tick()
                        else:self._needs_remote_version()
                    else: self._error(error)
                    return
                self._upload_committed(transfer,result.get('record'))
            self._request('POST','/v1/uploads/'+transfer['upload_id']+'/commit',committed,data={}); return
        transfer['_sent_bytes']=0
        def progress(sent,total):
            if not self._resume_matches(transfer):return
            transfer['_sent_bytes']=sent;self._notify_transfers()
        def sent(result,error):
            if error:
                # The server may have accepted this part before the reply
                # was lost. Resume with its next_part, never guess/replay.
                self._error(error); return
            transfer['next_part'] = index+1;transfer.pop('_sent_bytes',None); self._persist_progress()
            self._notify_transfers()
            self.status_changed.emit(f'正在上传 {transfer["name"]} · {index+1}/{transfer["part_count"]}')
            self._send_part(transfer,index+1)
        if (index==0 and transfer['part_count']==1 and transfer['size']>4*1024*1024
                and getattr(self.api,'supports_file_upload',False)):
            # For a single large part the staged original SHA is already the
            # required R2 checksum. Let Qt stream its QFile directly: avoid
            # another Python read plus an 80 MiB QByteArray copy before the
            # first byte can leave the machine.
            self._request('PUT',f'/v1/uploads/{transfer["upload_id"]}/parts/0',sent,
                          source=transfer['path'],headers={'X-Content-SHA256':transfer['sha256']},progress=progress)
            return
        def read(result,error):
            if error: self._error(error); return
            data,digest = result
            self._request('PUT',f'/v1/uploads/{transfer["upload_id"]}/parts/{index}',sent,
                          raw=data,headers={'X-Content-SHA256':digest},progress=progress)
            next_key=(transfer['upload_id'],index+1)
            if index+1<transfer['part_count']:
                self._work(read_part,(transfer['path'],index+1,transfer['chunk_size']),
                    lambda result,error:self._part_cache.__setitem__(next_key,result) if not error and
                        transfer.get('next_part',0)<=index+1 and self._resume_matches(transfer) else None)
        key=(transfer['upload_id'],index)
        prepared=self._part_cache.pop(key,None)
        if prepared is not None:read(prepared,None)
        else:self._work(read_part,(transfer['path'],index,transfer['chunk_size']),read)

    def close(self):
        if self.closed: return
        self.local_changed(); self._persist()
        self.closed = True; self.epoch += 1
        self.service.account_changed.disconnect(self.select_account)
        self.timer.stop(); self.debounce.stop(); self.remote_poll.stop()
        for key in tuple(self.requests): self.api.cancel(key)
        for job in tuple(self.jobs): job.cancel.set()
        self.pool.waitForDone(3000)
