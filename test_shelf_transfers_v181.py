"""Real Qt sync journals and two replicas against the existing protocol fixture.

The v180 fixture is a script with top-level scenarios. Load only its reusable
class declarations so these tests do not execute that independent suite twice.
No cloud deployment, desktop, native hooks or real credential store is used.
"""
from __future__ import annotations

import ast
import copy
from contextlib import contextmanager
import hashlib
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
from cloud_api import CloudError
from shelf_sync import ShelfSync
from shelf_content import ContentRepository

APP = QApplication.instance() or QApplication([])
_fixture_path = Path(__file__).with_name('test_shelf_sync_v180.py')
_fixture_tree = ast.parse(_fixture_path.read_text(encoding='utf8'))
_fixture_scope = {'copy': copy, 'hashlib': hashlib, 'Path': Path, 'CloudError': CloudError, 'uuid4': uuid4}
exec(compile(ast.Module(body=[node for node in _fixture_tree.body if isinstance(node, ast.ClassDef)
                             and node.name in ('Server', 'Adapter')], type_ignores=[]),
             str(_fixture_path), 'exec'), _fixture_scope)
BaseServer = _fixture_scope['Server']; BaseAdapter = _fixture_scope['Adapter']


def until(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition(): return
        time.sleep(.002)
    raise AssertionError('Transfer state did not converge')


class Server(BaseServer):
    def __init__(self):
        super().__init__(); self.calls = []; self.actor = ''; self.receipts = set()

    def dispatch(self, token, method, path, data, raw):
        self.calls.append((token, method, path)); parts = path.split('?')[0].split('/')[2:]
        uid = data.get('upload_id') if method == 'POST' and parts == ['uploads'] else parts[1] if parts[0] == 'uploads' and len(parts) > 1 else None
        if uid and method == 'DELETE':
            upload = self.uploads.get(uid)
            if upload and upload['token'] != token: raise CloudError('Missing', 404, 'NOT_FOUND')
            if not upload or upload['state'] != 'committed':
                self.receipts.add((token, uid)); self.uploads.pop(uid, None); return {'cancelled': True}
        if uid and (token, uid) in self.receipts:
            raise CloudError('Upload was cancelled', 410, 'UPLOAD_CANCELLED')
        if parts[:2] == ['sync', 'changes']:
            result = super().dispatch(token, method, path, data, raw)
            result['transfers'] = self.transfers(token)
            return result
        if parts == ['transfers'] and method == 'GET': return {'transfers': self.transfers(token)}
        result = super().dispatch(token, method, path, data, raw)
        if method == 'POST' and parts == ['uploads']:
            self.uploads[result['upload_id']].setdefault('device_id', self.actor)
        if parts[0] == 'items' and method == 'DELETE':
            for identifier, upload in list(self.uploads.items()):
                if upload['token'] == token and upload['spec']['item_id'] == parts[1]: self.uploads.pop(identifier)
        return result

    def transfers(self, token):
        rows = []
        for uid, upload in self.uploads.items():
            if upload['token'] != token or upload['state'] not in ('pending', 'verifying'): continue
            spec = upload['spec']
            rows.append(dict(upload_id=uid, item_id=spec['item_id'], base_version=spec['base_version'],
                             kind=spec['kind'], name=spec['name'], size=spec['size'],
                             received_bytes=sum(map(len, upload['chunks'])), device_id=upload['device_id'],
                             device_name='另一台电脑'))
        return rows


class Api(QObject):
    unauthorized = Signal()
    def __init__(self, server, device):
        super().__init__(); self.server = server; self.device = device; self.token = ''
        self.pending = {}; self.held = []; self.hold_part = None; self.hold_init = False
        self.hold_delete = False
        self.hold_download_part = None
        self.hold_commit_response = False; self.lost_commit_responses = []

    def request(self, method, path, callback, **kwargs):
        key = uuid4().hex; token = self.token
        def run():
            if key not in self.pending: return
            self.pending.pop(key)
            try:
                self.server.actor = self.device
                result = self.server.dispatch(token, method, path, kwargs.get('data'), kwargs.get('raw'))
                if self.hold_commit_response and method == 'POST' and path.endswith('/commit'):
                    # The server's bytes and commit are real, but the origin
                    # has not received the response when the user cancels.
                    self.lost_commit_responses.append((callback, copy.deepcopy(result)))
                    return
                if kwargs.get('destination'):
                    if len(result) != kwargs['expected']['size']: raise AssertionError('Unexpected received part size')
                    target = Path(kwargs['destination']); target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(result); result = target
                callback(result, None)
            except Exception as error: callback(None, error)
        self.pending[key] = run
        blocked = self.hold_init and method == 'POST' and path == '/v1/uploads'
        blocked = blocked or (self.hold_part is not None and method == 'PUT' and int(path.rsplit('/', 1)[1]) == self.hold_part)
        blocked = blocked or (self.hold_download_part is not None and method == 'GET' and '/parts/' in path
                              and int(path.rsplit('/', 1)[1].split('?')[0]) == self.hold_download_part)
        blocked = blocked or (self.hold_delete and method == 'DELETE' and path.startswith('/v1/uploads/'))
        if blocked: self.held.append((run, method, path, copy.deepcopy(kwargs)))
        else: QTimer.singleShot(0, run)
        return key

    def cancel(self, key): self.pending.pop(key, None)
    def release(self):
        held, self.held = self.held, []
        self.hold_part = None; self.hold_init = False; self.hold_download_part = None; self.hold_delete = False
        for run, _, _, _ in held: QTimer.singleShot(0, run)


class Service(QObject):
    account_changed = Signal(object)
    def __init__(self, server, device):
        super().__init__(); self.device_id = device; self.api = Api(server, device)
    @property
    def signed_in(self): return bool(self.api.token)
    def signin(self, account):
        self.api.token = account or ''; self.account_changed.emit({'id': account} if account else None)


class Adapter(BaseAdapter):
    def __init__(self):
        super().__init__(); self.cancelled = 0; self.removed = []; self.observer = None
    def cancel_pending(self): self.cancelled += 1
    def remove_owned(self, ref): self.removed.append(ref)
    def restore(self, view):
        super().restore(view)
        if self.observer: self.observer.files['permanent'] = list(view['permanent'])


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='flickdock-transfer-qa-'); self.root = Path(self.folder.name)
        self.server = Server(); self.engines = []
        self.source = self.root / '原图.png'; self.payload = b'\x89PNG\r\nmetadata\0' + bytes(range(251)) * 2000
        self.source.write_bytes(self.payload)

    def tearDown(self):
        for engine in self.engines: engine.close()
        APP.processEvents(); self.folder.cleanup()

    def replica(self, device, *, initial=False, hold_part=None, hold_init=False, hold_download_part=None, base=None):
        service = Service(self.server, device); adapter = Adapter()
        if initial: adapter.add(str(self.source))
        service.api.hold_part = hold_part; service.api.hold_init = hold_init
        service.api.hold_download_part = hold_download_part
        engine = ShelfSync(base or self.root / device, service, adapter, poll_ms=600000)
        self.engines.append(engine); service.signin('account-A')
        return service, adapter, engine

    def pending(self):
        service, adapter, engine = self.replica('device-a', initial=True, hold_part=1)
        until(lambda: service.api.held and bool(self.server.uploads))
        transfer = next(iter(engine.state['resumable_uploads'].values()))
        self.assertEqual(transfer['next_part'], 1)
        return service, adapter, engine, transfer

    def project(self, adapter, engine, *, native_shelf=False):
        """Use the actual manager projection, without a native manager or hooks."""
        from flickdock import ShelfManager, ShelfWindow
        manager = SimpleNamespace(_sync_transfer_refs=set())
        if native_shelf:
            from app_settings import SettingsStore
            manager.settings=SettingsStore(self.root/'offscreen-settings')
            manager.motion=False;manager.save=lambda:None;manager.update_handles=lambda:None
            manager.activate_shelf=lambda _:None;manager._quitting=False;manager.outgoing_drag=False
            shelf=ShelfWindow(manager,1);manager.sync=engine
            self.addCleanup(self.close_shelf,shelf)
        else:
            shelf=SimpleNamespace(content=ContentRepository(),files={'permanent':[]},transfers_updated=lambda:None)
        manager.shelf=shelf;adapter.observer=shelf
        snapshots=[]
        def changed(rows):
            ShelfManager._transfers_changed(manager,rows)
            snapshots.append(dict(rows=copy.deepcopy(rows),pending=copy.deepcopy(shelf.content.pending),
                                  permanent=list(shelf.files['permanent'])))
        engine.transfers_changed.connect(changed);engine._notify_transfers()
        return manager,shelf,snapshots

    @staticmethod
    def close_shelf(shelf):
        shelf._transfer_timer.stop();shelf._image_timer.stop();shelf._intake_timer.stop()
        shelf.resolver.close();shelf.image_cache.close()
        shelf.presentation.stop();shelf.geometry_morph.stop();shelf.hide();shelf.deleteLater()
        APP.processEvents()

    def test_cancel_active_cleans_cloud_and_keeps_paused_ref_across_restart(self):
        service, adapter, engine, transfer = self.pending()
        identifier, uid, stage = transfer['id'], transfer['upload_id'], Path(transfer['path'])
        self.assertTrue(engine.cancel_upload(str(self.source)))
        until(lambda: not engine.running and uid not in self.server.uploads)
        self.assertEqual(adapter.view['permanent'], [str(self.source)])
        self.assertTrue(engine.state['entries'][identifier]['paused_upload']); self.assertFalse(stage.exists())
        self.assertTrue(self.source.exists()); self.assertIn(('account-A', uid), self.server.receipts)
        self.assertFalse(self.server.account('account-A')['items'])
        engine.close(); calls = len(self.server.calls)
        restarted = ShelfSync(self.root / 'device-a', service, adapter, poll_ms=600000)
        self.engines.append(restarted); service.signin('account-A')
        until(lambda: not restarted.running)
        self.assertTrue(restarted.state['entries'][identifier]['paused_upload'])
        self.assertFalse(any(m == 'POST' and p == '/v1/uploads' for _, m, p in self.server.calls[calls:]))

    def test_explicit_retry_uses_fresh_upload_and_clears_progress(self):
        service, adapter, engine, transfer = self.pending(); uid = transfer['upload_id']; states = []
        engine.transfers_changed.connect(states.append)
        engine.cancel_upload(str(self.source)); until(lambda: not engine.running and uid not in self.server.uploads)
        service.api.release(); engine.retry_upload(str(self.source))
        until(lambda: not engine.running and bool(self.server.account('account-A')['items']))
        record = self.server.account('account-A')['items'][transfer['id']]
        self.assertEqual(record['data'], self.payload); self.assertFalse(engine.state['entries'][transfer['id']].get('paused_upload'))
        self.assertEqual(states[-1], []); self.assertFalse(engine.state['resumable_uploads'])
        self.assertEqual(len([row for row in self.server.uploads if row != uid]), 1)

    def test_cancelled_large_upload_cleanup_does_not_block_new_file(self):
        service, adapter, engine, transfer = self.pending()
        old_upload = transfer['upload_id']
        service.api.hold_delete = True
        self.assertTrue(engine.cancel_upload(str(self.source)))
        fresh = self.root / 'fresh.txt'; fresh.write_text('new upload must start while old cleanup waits', encoding='utf8')
        adapter.add(str(fresh)); engine.local_changed()
        until(lambda: any(method == 'DELETE' and path == '/v1/uploads/' + old_upload
                          for _, method, path, _ in service.api.held))
        until(lambda: any(method == 'POST' and path == '/v1/uploads' and
                          row['spec']['name'] == 'fresh.txt' for _, method, path in self.server.calls
                          for row in self.server.uploads.values() if row['spec']['name'] == 'fresh.txt'))
        self.assertIn(old_upload, engine.state['pending_uploads'])
        service.api.release()
        until(lambda: not engine.running and old_upload not in engine.state['pending_uploads'])
        self.assertIn(('account-A', old_upload), self.server.receipts)

    def test_delete_active_is_prioritized_before_transfer_backoff(self):
        service, adapter, engine, transfer = self.pending(); uid = transfer['upload_id']
        engine.state['not_before'] = time.time() + 86400
        adapter.view['permanent'].clear(); engine.local_changed()
        until(lambda: not engine.running and uid not in self.server.uploads)
        self.assertEqual(adapter.view['permanent'], []); self.assertTrue(self.source.exists())
        self.assertFalse(self.server.account('account-A')['items']); self.assertFalse(Path(transfer['path']).exists())

    def test_remote_pending_is_dim_progress_metadata_then_commit_replaces_placeholder(self):
        service, adapter, engine, transfer = self.pending()
        other_service, other_adapter, other = self.replica('device-b'); states = []
        from flickdock import ShelfManager
        content = ContentRepository()
        shelf = SimpleNamespace(content=content, files={'permanent': []}, updates=0)
        def changed(): shelf.updates += 1
        shelf.transfers_updated = changed; other_adapter.observer = shelf
        manager = SimpleNamespace(shelf=shelf, _sync_transfer_refs=set())
        other.transfers_changed.connect(lambda rows: (states.append(rows), ShelfManager._transfers_changed(manager, rows)))
        other.tick(); until(lambda: not other.running and bool(states) and bool(states[-1]))
        remote = states[-1][0]
        self.assertTrue(remote['remote']); self.assertEqual(remote['phase'], 'remote')
        self.assertEqual(remote['received'], 65536); self.assertEqual(remote['total'], len(self.payload))
        self.assertEqual(other_adapter.view['permanent'], []); self.assertEqual(len(content.pending), 1)
        self.assertFalse(content.mime_for(list(content.pending)).hasUrls())
        service.api.release(); until(lambda: not engine.running and bool(self.server.account('account-A')['items']))
        other.tick(); until(lambda: not other.running and bool(other_adapter.view['permanent']))
        self.assertEqual(content.pending, {}); self.assertEqual(content.transfers, {})
        self.assertEqual(Path(other_adapter.view['permanent'][0]).read_bytes(), self.payload)

    def test_remote_cancel_removes_placeholder_and_origin_does_not_resurrect(self):
        service, adapter, engine, transfer = self.pending(); uid = transfer['upload_id']
        other_service, other_adapter, other = self.replica('device-b'); rows = []
        other.transfers_changed.connect(rows.append); other.tick()
        until(lambda: not other.running and bool(other.remote_transfers))
        self.assertTrue(other.cancel_upload(transfer['id']))
        until(lambda: not other.running and uid not in self.server.uploads)
        self.assertEqual(rows[-1], []); self.assertFalse(other_adapter.view['permanent'])
        service.api.release()
        until(lambda: not engine.running)
        engine.state['not_before'] = 0; engine.tick(); until(lambda: not engine.running)
        self.assertTrue(engine.state['entries'][transfer['id']].get('paused_upload'))
        self.assertEqual(adapter.view['permanent'], [str(self.source)])
        self.assertFalse(self.server.uploads); self.assertFalse(self.server.account('account-A')['items'])

    def test_offline_origin_after_cancel_receipt_expiry_pauses_until_explicit_fresh_retry(self):
        service,adapter,engine,transfer=self.pending()
        identifier,uid,stage=transfer['id'],transfer['upload_id'],Path(transfer['path'])
        engine.close();self.assertTrue(stage.exists())
        # Another device cancels while the origin is offline. Seven days later
        # even the terminal receipt is gone, so the authoritative reply is404.
        self.server.dispatch('account-A','DELETE','/v1/uploads/'+uid,None,None)
        self.server.receipts.discard(('account-A',uid))
        stale=time.time()-8*86400;os.utime(stage,(stale,stale))
        before=len(self.server.calls)
        restarted=ShelfSync(self.root/'device-a',service,adapter,poll_ms=600000)
        self.engines.append(restarted);service.signin('account-A')
        until(lambda:not restarted.running and restarted.state['entries'][identifier].get('paused_upload'))
        calls=self.server.calls[before:]
        self.assertTrue(any(method=='GET' and path=='/v1/uploads/'+uid for _,method,path in calls))
        self.assertFalse(any(method=='POST' and path=='/v1/uploads' for _,method,path in calls),
                         'A missing old upload must never be silently recreated after a cancellation receipt expires')
        self.assertFalse(self.server.uploads);self.assertFalse(self.server.account('account-A')['items'])
        self.assertFalse(stage.exists());self.assertEqual(adapter.view['permanent'],[str(self.source)])
        self.assertEqual(self.source.read_bytes(),self.payload)
        service.api.release();restarted.retry_upload(str(self.source))
        until(lambda:not restarted.running and identifier in self.server.account('account-A')['items'])
        self.assertEqual(self.server.account('account-A')['items'][identifier]['data'],self.payload)
        self.assertNotIn(uid,self.server.uploads)
        self.assertEqual(len(self.server.uploads),1);self.assertFalse(restarted.state['entries'][identifier].get('paused_upload'))

    def test_lost_init_older_than_pending_lifetime_requires_manual_fresh_retry(self):
        service,adapter,engine=self.replica('device-a',initial=True,hold_init=True)
        until(lambda:bool(service.api.held))
        transfer=next(iter(engine.state['resumable_uploads'].values()))
        identifier,uid,stage=transfer['id'],transfer['upload_id'],Path(transfer['path'])
        self.assertNotIn('chunk_size',transfer);engine.close()
        # No initialization reply was journaled, and any cancel receipt could
        # have expired while this origin stayed offline. Its old UUID is unsafe.
        stale=time.time()-8*86400;os.utime(stage,(stale,stale));service.api.release()
        before=len(self.server.calls)
        restarted=ShelfSync(self.root/'device-a',service,adapter,poll_ms=600000)
        self.engines.append(restarted);service.signin('account-A')
        until(lambda:not restarted.running and restarted.state['entries'][identifier].get('paused_upload'))
        self.assertFalse(any(method=='POST' and path=='/v1/uploads' for _,method,path in self.server.calls[before:]))
        self.assertFalse(self.server.uploads);self.assertFalse(self.server.account('account-A')['items'])
        self.assertFalse(stage.exists());self.assertEqual(self.source.read_bytes(),self.payload)
        restarted.retry_upload(str(self.source))
        until(lambda:not restarted.running and identifier in self.server.account('account-A')['items'])
        self.assertEqual(self.server.account('account-A')['items'][identifier]['data'],self.payload)
        self.assertNotIn(uid,self.server.uploads);self.assertEqual(len(self.server.uploads),1)

    def test_cancel_before_init_response_rejects_late_creation(self):
        service, adapter, engine = self.replica('device-a', initial=True, hold_init=True)
        until(lambda: bool(service.api.held))
        init = service.api.held[0][3]['data']; uid = init['upload_id']
        self.assertTrue(engine.cancel_upload(str(self.source)))
        until(lambda: not engine.running and ('account-A', uid) in self.server.receipts)
        with self.assertRaises(CloudError) as result:
            self.server.dispatch('account-A', 'POST', '/v1/uploads', init, None)
        self.assertEqual(result.exception.code, 'UPLOAD_CANCELLED'); self.assertFalse(self.server.uploads)
        self.assertTrue(self.source.exists()); self.assertEqual(adapter.view['permanent'], [str(self.source)])

    def _cancel_after_lost_commit(self, *, restart=False):
        service, adapter, engine = self.replica('device-a', initial=True)
        service.api.hold_commit_response = True
        until(lambda: bool(service.api.lost_commit_responses))
        transfer = copy.deepcopy(next(iter(engine.state['resumable_uploads'].values())))
        identifier, uid = transfer['id'], transfer['upload_id']
        record = self.server.account('account-A')['items'][identifier]['record']
        self.assertEqual(record['version'], 1)
        self.assertEqual(self.server.uploads[uid]['state'], 'committed')
        self.assertEqual(engine.state['entries'][identifier]['version'], 0)
        self.assertEqual(self.server.account('account-A')['items'][identifier]['data'], self.payload)
        before = len(self.server.calls)
        service.api.hold_commit_response = False
        self.assertTrue(engine.cancel_upload(str(self.source), remove=False))
        if restart:
            # Cancellation receipts must survive closing before its cleanup
            # request is dispatched, just like the durable upload journal.
            engine.close()
            engine = ShelfSync(self.root / 'device-a', service, adapter, poll_ms=600000)
            self.engines.append(engine); service.signin('account-A')
        until(lambda: not engine.running and not engine.state.get('pending_uploads'))
        self.assertEqual(adapter.view['permanent'], [str(self.source)],
                         'A cancellation arriving after commit must not fork or redownload its identical local revision')
        self.assertEqual(set(engine.state['entries']), {identifier})
        entry = engine.state['entries'][identifier]
        self.assertEqual(entry['version'], 1); self.assertEqual(entry.get('sha256'), hashlib.sha256(self.payload).hexdigest())
        self.assertFalse(entry.get('dirty')); self.assertFalse(entry.get('deleted'))
        self.assertEqual(set(self.server.account('account-A')['items']), {identifier})
        self.assertFalse(any(method == 'POST' and path == '/v1/uploads'
                             for _, method, path in self.server.calls[before:]))
        self.assertFalse(Path(transfer['path']).exists()); self.assertTrue(self.source.is_file())
        # A late original HTTP response cannot undo the reconciled result.
        for callback, result in service.api.lost_commit_responses:
            callback(result, None)
        APP.processEvents()
        self.assertEqual(adapter.view['permanent'], [str(self.source)])
        self.assertEqual(set(engine.state['entries']), {identifier})

    def test_cancel_after_lost_commit_adopts_matching_revision_without_duplicate(self):
        self._cancel_after_lost_commit()

    def test_cancel_after_lost_commit_receipt_survives_immediate_restart(self):
        self._cancel_after_lost_commit(restart=True)

    def test_lost_commit_cancel_does_not_mark_a_newer_local_revision_clean(self):
        service, adapter, engine = self.replica('device-a', initial=True)
        service.api.hold_commit_response = True
        until(lambda: bool(service.api.lost_commit_responses))
        identifier = next(iter(engine.state['resumable_uploads']))
        newer = b'new local bytes after server committed old revision\0' + self.payload
        self.source.write_bytes(newer); engine.local_changed()
        service.api.hold_commit_response = False
        self.assertTrue(engine.cancel_upload(str(self.source), remove=False))
        until(lambda: not engine.running and not engine.state.get('pending_uploads'))
        self.assertEqual(self.source.read_bytes(), newer)
        self.assertEqual(self.server.account('account-A')['items'][identifier]['data'], self.payload)
        self.assertTrue(any(entry.get('ref') == str(self.source) and entry.get('dirty')
                            for entry in engine.state['entries'].values()),
                        'A receipt for old committed bytes cannot mark changed local bytes as synchronized')

    def test_pending_commit_download_keeps_placeholder_until_complete_verified_bytes(self):
        service, adapter, engine, transfer = self.pending()
        other_service, other_adapter, other = self.replica('device-b',hold_download_part=1)
        manager,shelf,snapshots=self.project(other_adapter,other)
        until(lambda:not other.running and bool(shelf.content.pending))
        identity=transfer['id'];placeholder='flickdock:transfer:'+identity
        self.assertEqual(set(shelf.content.pending),{placeholder})
        self.assertEqual(shelf.content.transfers[placeholder]['phase'],'remote')
        start=len(snapshots)
        service.api.release()
        until(lambda:not engine.running and bool(self.server.account('account-A')['items']))
        other.tick()
        until(lambda:bool(other_service.api.held) and identity in other.download_transfers)
        self.assertEqual(set(shelf.content.pending),{placeholder},'commit is not enough to expose an incomplete local file')
        self.assertEqual(shelf.content.transfers[placeholder]['phase'],'downloading')
        self.assertEqual(other_adapter.view['permanent'],[])
        self.assertTrue(all(placeholder in snapshot['pending'] for snapshot in snapshots[start:]),
                        'the preview must not disappear between upload commit and verified local download')
        other_service.api.release()
        until(lambda:not other.running and bool(other_adapter.view['permanent']))
        path=Path(other_adapter.view['permanent'][0])
        self.assertEqual(path.read_bytes(),self.payload)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),transfer['sha256'])
        self.assertEqual(shelf.content.pending,{});self.assertEqual(shelf.content.transfers,{})
        self.assertFalse(other.download_transfers);self.assertFalse(other.state.get('downloads'))

    def test_cancel_download_keeps_cloud_ignores_version_and_cleans_reader_race(self):
        service,adapter,engine=self.replica('device-a',initial=True)
        until(lambda:not engine.running and bool(self.server.account('account-A')['items']))
        identity=next(iter(self.server.account('account-A')['items']))
        import shelf_sync as sync_module
        original_reader=sync_module._source_reader;original_cleanup=sync_module._remove_download_parts
        entered,release,cleanup_started=threading.Event(),threading.Event(),threading.Event()
        cleanup_finished=threading.Event();deleted_while_reading=[]
        self.addCleanup(release.set)
        class HeldReader:
            def __init__(self,stream):self.stream=stream;self.paused=False
            def __enter__(self):self.stream.__enter__();return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def read(self,count):
                data=self.stream.read(count)
                if data and not self.paused:
                    self.paused=True;entered.set();assert release.wait(5)
                return data
        @contextmanager
        def held_reader(path):
            with original_reader(path) as stream:
                if Path(path).name.endswith('.chunk-0') and 'device-b' in Path(path).parts:
                    yield HeldReader(stream)
                else:yield stream
        def cleanup(*args):
            cleanup_started.set()
            try:
                result=original_cleanup(*args)
                target=Path(args[0]);part=target.with_name('.'+target.name+'.chunk-0')
                deleted_while_reading.append(not part.exists() and not release.is_set())
                return result
            finally:cleanup_finished.set()
        with patch.object(sync_module,'_source_reader',held_reader),patch.object(sync_module,'_remove_download_parts',cleanup):
            other_service,other_adapter,other=self.replica('device-b',hold_download_part=4)
            manager,shelf,snapshots=self.project(other_adapter,other)
            until(entered.is_set)
            self.assertIn(identity,other.download_transfers)
            # Hold the actual hash reader's Windows sharing handle through the
            # first cleanup attempt, then release it without blocking Qt.
            def unblock_reader():
                cleanup_finished.wait(3);release.set()
            unblock=threading.Thread(target=unblock_reader,daemon=True);unblock.start()
            before=len(self.server.calls)
            self.assertTrue(other.cancel_download(identity))
            until(lambda:not other.running and not other.jobs)
            unblock.join(1);self.assertTrue(cleanup_started.is_set())
            self.assertEqual(deleted_while_reading,[True],
                             'the owned chunk must be deletable while its shared hash reader remains open')
            self.assertEqual(other.state.get('ignored_versions',{}).get(identity),1)
            self.assertFalse(other.state.get('downloads'));self.assertFalse(other.download_transfers)
            self.assertEqual(other_adapter.view['permanent'],[])
            self.assertEqual(shelf.content.pending,{});self.assertEqual(shelf.content.transfers,{})
            directory=other.root/'account-A'/'files'/identity
            self.assertEqual(list(directory.rglob('*.chunk-*')),[],
                             'cancelling during a live chunk hash read must not leak owned cache files')
            other_service.api.release();other.tick();until(lambda:not other.running)
        self.assertFalse(any(token=='account-A' and method=='GET' and '/parts/' in path
                             for token,method,path in self.server.calls[before:]))
        self.assertFalse(self.server.account('account-A')['items'][identity]['record']['deleted'])
        self.assertEqual(self.server.account('account-A')['items'][identity]['data'],self.payload)
        other.close();calls=len(self.server.calls)
        restarted=ShelfSync(self.root/'device-b',other_service,other_adapter,poll_ms=600000)
        self.engines.append(restarted);other_service.signin('account-A');until(lambda:not restarted.running)
        self.assertEqual(restarted.state.get('ignored_versions',{}).get(identity),1)
        self.assertEqual(other_adapter.view['permanent'],[])
        self.assertFalse(any(method=='GET' and '/parts/' in path for _,method,path in self.server.calls[calls:]))
        self.assertTrue(self.source.is_file())

    def test_clear_pending_download_deletes_committed_cloud_and_partial_cache(self):
        service,adapter,engine=self.replica('device-a',initial=True)
        until(lambda:not engine.running and bool(self.server.account('account-A')['items']))
        identity=next(iter(self.server.account('account-A')['items']))
        other_service,other_adapter,other=self.replica('device-b',hold_download_part=1)
        manager,shelf,snapshots=self.project(other_adapter,other,native_shelf=True)
        until(lambda:bool(other_service.api.held) and bool(other.state.get('downloads')))
        self.assertEqual(shelf.files['permanent'],[]);self.assertEqual(len(shelf.display_refs()),1)
        with patch.object(other,'cancel_download',wraps=other.cancel_download) as cancel:
            shelf.remove_all('permanent')
            cancel.assert_called_once_with(identity,remove=True)
        until(lambda:not other.running and not other.jobs and self.server.account('account-A')['items'][identity]['record']['deleted'])
        self.assertEqual(shelf.display_refs(),[]);self.assertFalse(other.download_transfers)
        self.assertFalse(other.state.get('downloads'));self.assertFalse(self.server.uploads)
        directory=other.root/'account-A'/'files'/identity
        self.assertEqual(list(directory.rglob('*.chunk-*')),[])
        self.assertFalse(any(path.is_file() for path in directory.rglob('*')))
        # A native cancellation can arrive before a scheduled callback. Releasing
        # it now and polling both peers must never restore the cleared content.
        other_service.api.release();other.tick();engine.tick()
        until(lambda:not other.running and not engine.running)
        self.assertEqual(other_adapter.view['permanent'],[]);self.assertEqual(shelf.display_refs(),[])
        self.assertTrue(self.server.account('account-A')['items'][identity]['record']['deleted'])
        self.assertFalse(self.server.uploads);self.assertTrue(self.source.is_file())

    def test_account_switch_discards_late_progress_and_never_cancels_old_owner_as_new_owner(self):
        service, adapter, engine, transfer = self.pending(); uid = transfer['upload_id']; states = []
        engine.transfers_changed.connect(states.append); service.signin('account-B')
        until(lambda: not engine.running)
        self.assertEqual(adapter.view['permanent'], []); self.assertEqual(states[-1], [])
        service.api.release(); APP.processEvents()
        self.assertEqual(adapter.view['permanent'], []); self.assertEqual(states[-1], [])
        self.assertFalse(any(token == 'account-B' and uid in path for token, _, path in self.server.calls))
        self.assertTrue(Path(transfer['path']).exists()); self.assertGreaterEqual(adapter.cancelled, 2)
        # Returning to the owner can finish its preserved, owner-scoped stage.
        service.signin('account-A'); until(lambda: not engine.running and bool(self.server.account('account-A')['items']))
        self.assertEqual(self.server.account('account-A')['items'][transfer['id']]['data'], self.payload)
        self.assertFalse(self.server.account('account-B')['items'])


if __name__ == '__main__': unittest.main(verbosity=2)
