"""A lost WebSocket commit hint must not strand another device on pending upload."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from shelf_sync import ShelfSync


APP = QApplication.instance() or QApplication([])
PAYLOAD = b'original bytes from another device'
IDENTITY = str(uuid4())
RECORD = {'id':IDENTITY, 'version':1, 'deleted':False, 'kind':'file',
          'name':'remote.bin', 'mime_type':'application/octet-stream',
          'size':len(PAYLOAD), 'sha256':hashlib.sha256(PAYLOAD).hexdigest()}


def spin(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate(): return True
        time.sleep(.002)
    return False


class Api(QObject):
    def __init__(self, device_id):
        super().__init__()
        self.device_id = device_id
        self.committed = False
        self.was_cancelled = False
        self.pulls = 0
        self.pending = set()

    def request(self, method, path, callback, **options):
        assert method == 'GET', (method, path)
        key = uuid4().hex
        self.pending.add(key)

        def deliver():
            if key not in self.pending: return
            self.pending.remove(key)
            if path.startswith('/v1/sync/changes?'):
                self.pulls += 1
                result = {'changes':[{'cursor':'1', 'record':copy.deepcopy(RECORD)}]
                          if self.committed else [],
                          'cursor':'1' if self.committed else '0',
                          'has_more':False,
                          'transfers':[] if self.committed or self.was_cancelled else [{
                              'upload_id':str(uuid4()), 'item_id':IDENTITY,
                              'name':RECORD['name'], 'kind':'file',
                              'size':len(PAYLOAD), 'received_bytes':0,
                              'state':'pending', 'device_id':self.device_id}]}
            elif path.endswith('/content'):
                result = {'record':copy.deepcopy(RECORD), 'chunk_size':65536,
                          'part_count':1}
            elif '/parts/0?' in path:
                assert options['expected']=={'size':len(PAYLOAD), 'with_digest':True}
                target=Path(options['destination'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(PAYLOAD)
                result={'path':target, 'sha256':RECORD['sha256']}
                if options.get('receive_progress'):
                    options['receive_progress'](len(PAYLOAD)//2,len(PAYLOAD))
                    QTimer.singleShot(40,lambda:callback(result,None))
                    return
            else: raise AssertionError(path)
            callback(result,None)

        QTimer.singleShot(0,deliver)
        return key

    def cancel(self,key):
        self.pending.discard(key)


class Service(QObject):
    account_changed = Signal(object)

    def __init__(self, remote_device):
        super().__init__()
        self.device_id = 'receiver'
        self.api = Api(remote_device)
        self.signed_in = True


class Adapter:
    def __init__(self): self.view={'permanent':[], 'texts':{}, 'metadata':{}}
    def snapshot(self): return copy.deepcopy(self.view)
    def restore(self,view): self.view=copy.deepcopy(view)
    def title(self,ref): return Path(ref).name


def test_lost_commit_hint_still_starts_download():
    with tempfile.TemporaryDirectory(prefix='flickdock-commit-wakeup-') as root:
        service=Service('sender')
        adapter=Adapter()
        engine=ShelfSync(root,service,adapter,poll_ms=600000,remote_poll_ms=80)
        seen=[]
        engine.transfers_changed.connect(lambda transfers:seen.extend(transfers))
        try:
            service.account_changed.emit({'id':'poll-account'})
            assert spin(lambda:service.api.pulls>=1 and engine.remote_poll.isActive())
            assert adapter.view['permanent']==[]
            # The source commits without a WebSocket delivery to this peer.
            service.api.committed=True
            assert spin(lambda:len(adapter.view['permanent'])==1 and not engine.running)
            assert service.api.pulls>=2
            assert any(item.get('phase')=='downloading' and 0<item.get('received',0)<len(PAYLOAD)
                       for item in seen), 'The progress view must move before the whole part finishes'
            assert any(item.get('phase')=='validating' for item in seen), (
                'The final original-byte check must have its own visible phase')
            assert Path(adapter.view['permanent'][0]).read_bytes()==PAYLOAD
            assert not engine.remote_poll.isActive()
            pulls=service.api.pulls
            assert not spin(lambda:service.api.pulls>pulls,seconds=.18), 'Idle peers must stop extra polling'
        finally: engine.close()


def test_own_upload_does_not_start_remote_poll():
    with tempfile.TemporaryDirectory(prefix='flickdock-own-commit-') as root:
        service=Service('receiver')
        engine=ShelfSync(root,service,Adapter(),poll_ms=600000,remote_poll_ms=80)
        try:
            service.account_changed.emit({'id':'poll-account'})
            assert spin(lambda:service.api.pulls>=1 and not engine.running)
            assert not engine.remote_poll.isActive()
            pulls=service.api.pulls
            assert not spin(lambda:service.api.pulls>pulls,seconds=.18)
        finally: engine.close()


def test_cancel_hint_and_lost_cancel_hint_clear_remote_placeholder():
    for delivered_hint in (False,True):
        with tempfile.TemporaryDirectory(prefix='flickdock-cancel-wakeup-') as root:
            service=Service('sender')
            engine=ShelfSync(root,service,Adapter(),poll_ms=600000,remote_poll_ms=80)
            try:
                service.account_changed.emit({'id':'poll-account'})
                assert spin(lambda:bool(engine.remote_transfers) and engine.remote_poll.isActive())
                service.api.was_cancelled=True
                if delivered_hint:engine.request_refresh()
                assert spin(lambda:not engine.remote_transfers and not engine.remote_poll.isActive())
                assert service.api.pulls>=2
            finally: engine.close()


def test_disconnected_socket_recovers_unseen_commit_without_idle_poll():
    with tempfile.TemporaryDirectory(prefix='flickdock-disconnected-wakeup-') as root:
        service=Service('sender')
        service.api.was_cancelled=True  # No pending reservation was ever seen.
        adapter=Adapter()
        engine=ShelfSync(root,service,adapter,poll_ms=600000,
                         remote_poll_ms=80,disconnected_poll_ms=80)
        try:
            engine.set_event_stream_online(False)
            service.account_changed.emit({'id':'poll-account'})
            assert spin(lambda:service.api.pulls>=1 and engine.remote_poll.isActive())
            service.api.committed=True
            assert spin(lambda:len(adapter.view['permanent'])==1)
            assert Path(adapter.view['permanent'][0]).read_bytes()==PAYLOAD
            assert engine.remote_poll.isActive(), 'Disconnected peers keep bounded reconciliation'
            engine.set_event_stream_online(True)
            assert not engine.remote_poll.isActive()
        finally: engine.close()


def test_all_bytes_sent_is_waiting_for_cloud_receipt():
    with tempfile.TemporaryDirectory(prefix='flickdock-upload-receipt-') as root:
        service=Service('sender')
        engine=ShelfSync(root,service,Adapter(),poll_ms=600000)
        seen=[]
        engine.transfers_changed.connect(lambda transfers:seen.append(transfers))
        engine.account_id='poll-account'
        engine.state={'entries':{IDENTITY:{'id':IDENTITY, 'ref':'local.bin',
                        'name':'local.bin', 'kind':'file', 'dirty':True, 'fingerprint':'local'}},
                      'resumable_uploads':{IDENTITY:{'upload_id':str(uuid4()),
                          'size':len(PAYLOAD), 'part_count':1, 'chunk_size':len(PAYLOAD),
                          'next_part':0, '_sent_bytes':len(PAYLOAD)}}}
        engine._notify_transfers()
        assert seen[-1][0]['phase']=='verifying'
        assert seen[-1][0]['received']==len(PAYLOAD)
        assert engine.sync_status_for_ref('local.bin')=='uploading'
        engine.state['entries'][IDENTITY].update(version=1,dirty=False)
        assert engine.sync_status_for_ref('local.bin')=='synced'
        engine.upload_enabled=False
        assert engine.sync_status_for_ref('local.bin')=='synced'
        engine.upload_enabled=True
        engine.state['entries'][IDENTITY]['dirty']=True
        engine.state['resumable_uploads'].clear()
        assert engine.sync_status_for_ref('local.bin')=='pending'
        engine.state['entries'][IDENTITY]['paused_upload']=True
        assert engine.sync_status_for_ref('local.bin')=='paused'
        engine.upload_enabled=False
        assert engine.sync_status_for_ref('local.bin')=='local'
        engine.close()


def test_pending_metadata_refreshes_while_another_transfer_is_busy():
    with tempfile.TemporaryDirectory(prefix='flickdock-busy-pending-') as root:
        service=Service('sender');adapter=Adapter()
        engine=ShelfSync(root,service,adapter,poll_ms=600000,remote_poll_ms=600000)
        seen=[];engine.transfers_changed.connect(seen.append)
        try:
            service.account_changed.emit({'id':'poll-account'})
            assert spin(lambda:service.api.pulls>=1 and not engine.running)
            engine.remote_transfers=[];seen.clear();engine.running=True
            cursor=engine.state['cursor'];pulls=service.api.pulls
            engine.request_refresh();engine.request_refresh()
            assert spin(lambda:any(row.get('phase')=='remote' for batch in seen for row in batch))
            assert engine.running,'The ongoing transfer must remain running'
            assert service.api.pulls==pulls+1,'Concurrent hints share one metadata request'
            assert engine.state['cursor']==cursor and adapter.view['permanent']==[]
            service.api.was_cancelled=True;engine.request_refresh()
            assert spin(lambda:not engine.remote_transfers),'Cancellation must clear the placeholder while busy'
        finally:engine.close()


if __name__=='__main__':
    test_lost_commit_hint_still_starts_download()
    test_own_upload_does_not_start_remote_poll()
    test_cancel_hint_and_lost_cancel_hint_clear_remote_placeholder()
    test_disconnected_socket_recovers_unseen_commit_without_idle_poll()
    test_all_bytes_sent_is_waiting_for_cloud_receipt()
    test_pending_metadata_refreshes_while_another_transfer_is_busy()
    print('PASS remote commit/cancel handoff with or without hint, disconnected fallback, idle/own no-poll, receipt phase')
