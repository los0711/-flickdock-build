"""Canonical stage bytes, durable bindings and actual IO terminal ownership."""
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from test_shelf_payload_gate_v1825 import PayloadGateTests,ReuseApi,Service,Adapter,APP,spin
from shelf_sync import ShelfSync
import shelf_stage_store as resource


class CanonicalStageTests(unittest.TestCase):
    setUp=PayloadGateTests.setUp
    tearDown=PayloadGateTests.tearDown
    file=PayloadGateTests.file
    start=PayloadGateTests.start
    waiting=PayloadGateTests.waiting
    drain=PayloadGateTests.drain
    entry=PayloadGateTests.entry
    assert_originals=PayloadGateTests.assert_originals

    def settled_stages(self,count):
        self.assertTrue(spin(lambda:len(self.engine.state.get('resumable_uploads',{}))==count
                                  and not self.engine.jobs and bool(self.api.held)))

    def test_twelve_equal_originals_share_one_body_and_unrelated_payloads_keep_three_slots(self):
        raw=b'canonical original bytes'*11000
        raw=raw[:262144];self.assertEqual(len(raw),262144)
        paths=[self.file(f'original-{n:02}.bin',raw) for n in range(12)]
        self.start(paths);self.settled_stages(12)
        blobs=list(self.profile.rglob('*.blob'))
        self.assertEqual(len(blobs),1);self.assertEqual(blobs[0].stat().st_size,262144)
        self.assertEqual(len({row['path'] for row in self.engine.state['resumable_uploads'].values()}),1)
        self.assertEqual(len(self.engine.payload_gate.waiters),11);self.assertEqual(len(self.api.body_calls),1)
        more=[self.file(f'different-{n}.bin',bytes([n])*16384) for n in (1,2)]
        self.adapter.view['permanent']+=more;self.engine.local_changed();self.engine.small_lane.kick()
        self.assertTrue(spin(lambda:len(self.api.held)==3))
        names={row['spec']['name'] for row in self.api.uploads.values()}
        self.assertEqual(len(names),3);self.assertTrue({'different-1.bin','different-2.bin'}<=names)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),14)
        self.assertTrue(spin(lambda:not self.engine.jobs))
        self.assertFalse(list(self.profile.rglob('*.blob')))
        self.assert_originals(dict.fromkeys(paths,raw)|{more[0]:b'\1'*16384,more[1]:b'\2'*16384})

    def test_no_hardlinks_and_restart_after_originals_disappear_preserve_exact_staged_bytes(self):
        raw=b'owned immutable bytes'*1000;paths=[self.file(f'{n}.bin',raw) for n in range(4)]
        with patch.object(resource.os,'link',side_effect=OSError(errno.ENOTSUP,'unsupported')):
            self.start(paths);self.settled_stages(4)
        self.assertEqual(len(list(self.profile.rglob('*.blob'))),1)
        for path in paths:Path(path).unlink()
        self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
        self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000)
        self.engine.set_directions(True,False);self.service.account_changed.emit({'id':'account-A'})
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),4)
        self.assertEqual({row['data'] for row in self.api.account('account-A')['items'].values()},{raw})
        self.assertEqual(len(self.api.allocations),1);self.assertEqual(len(self.api.body_calls),1)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_each_binding_keeps_its_real_stage_age_without_borrowing_shared_inode_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox=Path(directory)/'account-A'/'outbox';outbox.mkdir(parents=True)
            raw=b'age-bound original';sha=hashlib.sha256(raw).hexdigest();snapshots=[];results=[]
            for index,stamp in enumerate((time.time()-4*86400,time.time()-3600)):
                path=outbox/(uuid4().hex+'.blob');path.write_bytes(raw);os.utime(path,(stamp,stamp))
                snapshot=dict(id=str(uuid4()),seq=1,fingerprint=f'fp{index}',size=len(raw),sha256=sha,
                              path=str(path),intake_id=str(uuid4()))
                result=resource.prepare_binding(outbox,'account-A',snapshot,threading.Event())
                self.assertAlmostEqual(result['original_mtime'],stamp,places=3)
                snapshots.append(snapshot);results.append(result)
            self.assertEqual(results[0]['path'],results[1]['path'])
            canonical=Path(results[0]['path']);os.utime(canonical,None)
            for original,result in zip(snapshots,results):
                restored=dict(original,path=result['path'])
                checked=resource.prepare_binding(outbox,'account-A',restored,threading.Event())
                self.assertEqual(checked['original_mtime'],result['original_mtime'])
            self.assertGreater(time.time()-results[0]['original_mtime'],3*86400)
            self.assertLess(time.time()-results[1]['original_mtime'],3*86400)

    def test_missing_or_corrupt_binding_pauses_only_that_item_and_preserves_body_and_journal(self):
        raw=b'cached original';path=self.file('source.bin',raw);self.start([path]);self.settled_stages(1)
        stage=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
        self.engine.set_directions(False,False);self.assertTrue(spin(lambda:not self.engine.jobs))
        record=next(stage.parent.glob('*.json'));saved=record.read_bytes();record.unlink()
        calls=len(self.api.body_calls);self.engine.set_directions(True,False)
        self.assertTrue(spin(lambda:bool(self.entry(path).get('blocked_fingerprint')) and not self.engine.jobs))
        self.assertEqual(len(self.api.body_calls),calls);self.assertEqual(stage.read_bytes(),raw)
        self.assertEqual(len(self.engine.state['resumable_uploads']),1)
        record.write_bytes(saved)
        value=json.loads(saved);value['original_mtime']=0;record.write_text(json.dumps(value))
        self.engine.retry_upload(path)
        self.assertTrue(spin(lambda:bool(self.entry(path).get('blocked_fingerprint')) and not self.engine.jobs))
        self.assertEqual(len(self.api.body_calls),calls);self.assertEqual(stage.read_bytes(),raw)
        record.write_bytes(saved);self.engine.retry_upload(path);self.drain()
        self.assertEqual(self.api.account('account-A')['items'][self.entry(path)['id']]['data'],raw)
        self.assertEqual(Path(path).read_bytes(),raw)

    def test_durable_binding_failure_keeps_old_stage_and_explicit_retry_recovers(self):
        path=self.file('persist.bin',b'original persistence proof')
        original=ShelfSync._persist;failed=[]
        def persist(owner):
            rows=(owner.state or {}).get('resumable_uploads',{}).values()
            if not failed and any(resource.canonical_path(row['path']) for row in rows):
                failed.append(True);raise OSError(errno.ENOSPC,'controlled full disk')
            return original(owner)
        with patch.object(ShelfSync,'_persist',persist):
            self.start([path])
            self.assertTrue(spin(lambda:bool(self.entry(path).get('blocked_fingerprint')) and not self.engine.jobs))
        row=next(iter(self.engine.state['resumable_uploads'].values()))
        self.assertFalse(resource.canonical_path(row['path']));self.assertEqual(Path(row['path']).read_bytes(),Path(path).read_bytes())
        durable=json.loads(self.engine._profile('account-A').read_text())
        self.assertEqual(durable['resumable_uploads'][row['id']]['path'],row['path'])
        self.assertFalse(self.api.body_calls)
        self.engine.retry_upload(path);self.drain();self.assertEqual(len(self.api.body_calls),1)

    def test_failed_duplicate_cleanup_applies_bounded_staging_backpressure_and_can_recover(self):
        raw=b'cleanup original'*16384;paths=[self.file(f'{n}.bin',raw) for n in range(12)]
        original=resource.remove_unreferenced
        def blocked(outbox,refs,path,cancel):
            if not resource.canonical_path(path):raise PermissionError(errno.EACCES,'controlled permission')
            return original(outbox,refs,path,cancel)
        with patch.object(resource,'remove_unreferenced',blocked):
            self.start(paths)
            self.assertTrue(spin(lambda:bool(self.engine.stage_store.cleanup_blocked) and not self.engine.jobs))
            start=time.monotonic()
            while time.monotonic()-start<.2:APP.processEvents();time.sleep(.002)
            self.assertLessEqual(len(list(self.profile.rglob('*.blob'))),4)
            self.assertLessEqual(len(self.engine.state['resumable_uploads']),3)
            self.assertFalse(self.api.body_calls)
        for path in paths:self.engine.retry_upload(path)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),12)
        self.assertEqual(len(self.api.allocations),1);self.assertEqual(len(self.api.body_calls),4)
        self.assertFalse(self.engine.stage_store.cleanup_blocked)
        self.assert_originals(dict.fromkeys(paths,raw))

    def test_last_reference_cleanup_waits_for_real_reader_terminal_and_close_does_not_drop_queued_job(self):
        raw=b'leased original';paths=[self.file(f'{n}.bin',raw) for n in range(2)]
        self.start(paths);self.settled_stages(2)
        stage=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
        release=self.engine.stage_store.acquire(stage)
        for path in paths:self.engine.cancel_upload(path)
        APP.processEvents();self.assertFalse(self.engine.state['resumable_uploads']);self.assertTrue(stage.exists())
        release();self.assertTrue(spin(lambda:not self.engine.jobs));self.assertFalse(stage.exists())
        self.assert_originals(dict.fromkeys(paths,raw))
        # A queued owned read is a real lifecycle, even before it starts. close
        # cancels its work but must retain the eventual terminal notification.
        blocker=threading.Event();entered=threading.Event();self.engine.pool.setMaxThreadCount(1)
        def hold(cancel):entered.set();blocker.wait(2)
        self.engine._work(hold,(),lambda *_:None)
        self.assertTrue(spin(entered.is_set))
        own=self.profile/'cloud-shelves'/'account-A'/'outbox'/(uuid4().hex+'.blob');own.write_bytes(raw)
        def read_part(path,cancel):return Path(path).read_bytes()
        self.engine._work(read_part,(str(own),),lambda *_:self.fail('closed callback escaped'))
        self.assertEqual(self.engine.stage_store.leases[str(own)],1)
        self.engine.close();self.assertEqual(self.engine.stage_store.leases[str(own)],1)
        blocker.set();self.assertTrue(spin(lambda:not self.engine.jobs))
        self.assertNotIn(str(own),self.engine.stage_store.leases);self.assertEqual(own.read_bytes(),raw)


    def test_stream_request_cancel_keeps_cache_until_its_actual_terminal_callback(self):
        class StreamApi(ReuseApi):
            supports_file_upload=True
            supports_file_slices=True
            def __init__(self):super().__init__();self.streams={}
            def request(self,method,path,callback,**options):
                stream=None
                if options.get('source'):
                    stream=Path(options.pop('source')).open('rb')
                    stream.seek(options.pop('source_offset',0))
                    options['raw']=stream.read(options.pop('source_size',-1))
                holder=[]
                def terminal(value,error):
                    if stream is not None:
                        stream.close()
                        self.streams.pop(holder[0],None)
                    callback(value,error)
                key=super().request(method,path,terminal,**options);holder.append(key)
                if stream is not None:self.streams[key]=stream
                return key
        self.api=StreamApi();self.service=Service(self.api)
        raw=b'actual streamed source'*8192;path=self.file('stream.bin',raw)
        self.start([path]);self.settled_stages(1)
        stage=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
        self.assertEqual(len(self.api.streams),1);key=next(iter(self.api.streams))
        terminal=self.api.callbacks[key]
        self.engine.cancel_upload(path)
        self.assertTrue(stage.exists());self.assertEqual(self.engine.stage_store.leases[str(stage)],1)
        self.assertFalse(self.api.streams[key].closed)
        # Request cancellation is not proof that this synthetic backend closed
        # its actual file. Only its explicit terminal callback establishes that.
        terminal(None,InterruptedError())
        self.assertTrue(spin(lambda:not self.engine.jobs));self.assertFalse(stage.exists())
        self.assertFalse(self.api.streams);self.assertFalse(self.engine.stage_store.leases)
        self.assertEqual(Path(path).read_bytes(),raw)

    def test_restart_collects_only_verified_orphan_intents_not_unknown_outbox_files(self):
        raw=b'durable orphan';path=self.file('source.bin',raw)
        self.start([path]);self.settled_stages(1)
        snapshot=dict(next(iter(self.engine.state['resumable_uploads'].values())))
        stage=Path(snapshot['path']);self.engine.set_directions(False,False)
        self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
        # Model a crash after durable removal but before resource retirement.
        profile=self.engine._profile('account-A');state=json.loads(profile.read_text())
        state['resumable_uploads'].clear()
        entry=state['entries'][snapshot['id']];entry.update(paused_upload=True)
        from account_store import atomic_json
        atomic_json(profile,state)
        unknown=stage.parent.parent/(uuid4().hex+'.blob');unknown.write_bytes(b'unknown in-progress stage')
        self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000)
        self.engine.set_directions(False,False);self.service.account_changed.emit({'id':'account-A'})
        self.assertTrue(spin(lambda:not self.engine.jobs))
        self.assertFalse(stage.exists());self.assertTrue(unknown.exists())
        self.assertEqual(unknown.read_bytes(),b'unknown in-progress stage')
        self.assertEqual(Path(path).read_bytes(),raw)


    def test_resource_intent_failure_never_creates_an_unowned_full_canonical_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox=Path(directory)/'account-A'/'outbox';outbox.mkdir(parents=True)
            source=outbox/(uuid4().hex+'.blob');raw=b'intent before data';source.write_bytes(raw)
            snapshot=dict(id=str(uuid4()),seq=1,fingerprint='verified-fingerprint',size=len(raw),
                          sha256=hashlib.sha256(raw).hexdigest(),path=str(source),intake_id=str(uuid4()))
            with patch.object(resource,'atomic_json',side_effect=OSError(errno.ENOSPC,'controlled full disk')):
                with self.assertRaises(OSError):resource.prepare_binding(outbox,'account-A',snapshot,threading.Event())
            self.assertEqual(source.read_bytes(),raw)
            self.assertFalse(list((outbox/'canonical-v1').glob('*.blob')))

    def test_retry_cancelled_owner_joins_live_canonical_with_its_new_reservation(self):
        raw=b'canonical remains in use'*1000;paths=[self.file(f'{n}.bin',raw) for n in range(2)]
        self.start(paths);self.settled_stages(2)
        owner=next(iter(self.engine.payload_gate.groups.values())).owner
        old_reservation=owner['upload_id'];self.engine.cancel_upload(owner['ref'])
        self.assertTrue(spin(lambda:len(self.api.allocations)==2 and bool(self.api.held)))
        self.engine.retry_upload(owner['ref'])
        self.assertTrue(spin(lambda:len(self.engine.payload_gate.waiters)==1 and not self.engine.jobs))
        current=self.engine.state['resumable_uploads'][owner['id']]
        self.assertNotEqual(current['intake_id'],old_reservation)
        self.assertEqual(len(list(self.profile.rglob('*.blob'))),1)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),2)
        self.assertEqual(len(self.api.body_calls),2);self.assertEqual(len(self.api.allocations),2)
        self.assert_originals(dict.fromkeys(paths,raw))


    def test_account_failure_during_resource_binding_cannot_restart_cancelled_upload(self):
        from cloud_api import CloudError
        entered=threading.Event();release=threading.Event();original=resource.prepare_binding
        def held(outbox,account,snapshot,cancel):
            entered.set()
            if not release.wait(3):raise AssertionError('fixture did not release its owned worker')
            return original(outbox,account,snapshot,cancel)
        path=self.file('account-bound.bin',b'account-fenced original')
        with patch.object(resource,'prepare_binding',held):
            self.start([path]);self.assertTrue(spin(entered.is_set))
            self.engine._error(CloudError('controlled unauthorized',401,'UNAUTHORIZED'))
            release.set();self.assertTrue(spin(lambda:not self.engine.jobs))
        self.assertFalse(any(method=='POST' and route=='/v1/uploads' for _,method,route,_ in self.api.calls))
        self.assertFalse(self.api.body_calls);self.assertTrue(self.engine.state['not_before']>time.time())
        self.assertEqual(Path(path).read_bytes(),b'account-fenced original')
        self.engine.state['not_before']=0;self.engine.tick();self.drain()
        self.assertEqual(len(self.api.allocations),1);self.assertEqual(len(self.api.body_calls),1)


    def test_restart_reclaims_only_temporary_files_named_by_durable_operation_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox=Path(directory)/'account-A'/'outbox';outbox.mkdir(parents=True)
            raw=b'owned crash-safe bytes';source=outbox/(uuid4().hex+'.blob');source.write_bytes(raw)
            snapshot=dict(id=str(uuid4()),seq=1,fingerprint='original stamp',size=len(raw),
                          sha256=hashlib.sha256(raw).hexdigest(),path=str(source),intake_id=str(uuid4()))
            first=resource.prepare_binding(outbox,'account-A',snapshot,threading.Event())
            target=Path(first['path']);record=json.loads(Path(first['record']).read_text())
            target.unlink()
            partial=target.parent/(target.stem+'-'+record['operation']+'.partial')
            partial.write_bytes(raw[:4])
            unknown=target.parent/(uuid4().hex+'.partial');unknown.write_bytes(b'unknown original')
            with patch.object(resource.os,'link',side_effect=OSError(errno.ENOTSUP,'unsupported')):
                recovered=resource.prepare_binding(outbox,'account-A',snapshot,threading.Event())
            self.assertEqual(Path(recovered['path']).read_bytes(),raw);self.assertFalse(partial.exists())
            self.assertEqual(unknown.read_bytes(),b'unknown original')
            # An owned repair left by a dead process is part of the same exact
            # cleanup intent. No current worker exists in this recovery fixture.
            repair=target.parent/(target.stem+'-'+record['operation']+'.repair');repair.write_bytes(raw[:7])
            self.assertTrue(resource.remove_unreferenced(outbox,frozenset(),target,threading.Event()))
            self.assertFalse(repair.exists());self.assertFalse(target.exists());self.assertFalse(source.exists())
            self.assertEqual(unknown.read_bytes(),b'unknown original')


    def test_retiring_path_waits_for_gc_terminal_before_new_binding_can_use_it(self):
        raw=b'retiring immutable content'*1000;first=self.file('before.bin',raw)
        self.start([first]);self.settled_stages(1)
        old=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
        entered=threading.Event();release=threading.Event();original=resource.remove_unreferenced
        def held(outbox,refs,path,cancel):
            if str(path)==str(old):
                entered.set()
                if not release.wait(3):raise AssertionError('fixture did not release owned GC')
            return original(outbox,refs,path,cancel)
        with patch.object(resource,'remove_unreferenced',held):
            self.engine.cancel_upload(first);self.assertTrue(spin(entered.is_set))
            second=self.file('after.bin',raw);self.adapter.view['permanent'].append(second)
            self.engine.local_changed();self.engine.tick()
            def waiting_on_retirement():
                rows=list(self.engine.state['resumable_uploads'].values())
                return bool(rows and rows[0]['ref']==second and not resource.canonical_path(rows[0]['path'])
                            and self.engine.stage_store.retired[str(old)][1])
            self.assertTrue(spin(waiting_on_retirement))
            ordinary=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
            self.assertTrue(ordinary.exists());self.assertEqual(ordinary.read_bytes(),raw)
            self.assertEqual(len(self.api.allocations),1)
            release.set();self.assertTrue(spin(lambda:len(self.api.allocations)==2 and bool(self.api.held)))
        self.drain();items=self.api.account('account-A')['items']
        self.assertEqual(len(items),1);self.assertEqual(next(iter(items.values()))['data'],raw)
        self.assert_originals({first:raw,second:raw})


    def test_cleanup_backpressure_is_account_scoped_and_old_account_intents_recover(self):
        raw=b'old account original'*1024;paths=[self.file(f'A-{n}.bin',raw) for n in range(2)]
        original=resource.remove_unreferenced
        def denied(outbox,refs,path,cancel):
            if not resource.canonical_path(path):raise PermissionError(errno.EACCES,'controlled owned cleanup failure')
            return original(outbox,refs,path,cancel)
        with patch.object(resource,'remove_unreferenced',denied):
            self.start(paths)
            self.assertTrue(spin(lambda:bool(self.engine.stage_store.cleanup_blocked) and not self.engine.jobs))
        blocked=set(self.engine.stage_store.cleanup_blocked)
        self.assertTrue(blocked);self.assertTrue(all(Path(path).exists() for path in blocked))
        old_profile=self.engine._profile('account-A')
        old_refs=json.loads(old_profile.read_text())['resumable_uploads']
        second=self.file('B-independent.bin',b'new account independent bytes')
        self.api.token='account-B';self.service.account_changed.emit({'id':'account-B'})
        self.adapter.view['permanent']=[second];self.engine.local_changed();self.engine.tick();self.engine.small_lane.kick()
        self.assertTrue(spin(lambda:any(token=='account-B' for token,uid,size in self.api.body_calls)))
        self.assertEqual(self.engine.stage_store.cleanup_blocked,blocked)
        self.assertEqual(json.loads(old_profile.read_text())['resumable_uploads'],old_refs)
        self.assertTrue(all(Path(path).exists() for path in blocked))
        self.drain();self.assertEqual(len(self.api.account('account-B')['items']),1)
        # Returning to A does not discard its backpressure. Its existing explicit
        # retry must really finish the old known cleanup before new admissions.
        self.api.token='account-A';self.service.account_changed.emit({'id':'account-A'})
        self.assertEqual(self.engine.stage_store.cleanup_blocked,blocked)
        for path in paths:self.engine.retry_upload(path)
        self.drain();self.assertEqual(len(self.api.account('account-A')['items']),2)
        self.assertFalse(self.engine.stage_store.cleanup_blocked)
        self.assertEqual(len(self.api.account('account-B')['items']),1)
        self.assert_originals(dict.fromkeys(paths,raw)|{second:b'new account independent bytes'})


    @staticmethod
    @contextmanager
    def owned_directory_link(target,link):
        if os.name=='nt':
            import _winapi
            _winapi.CreateJunction(str(target),str(link))
        else:link.symlink_to(target,target_is_directory=True)
        try:yield
        finally:
            if os.name=='nt':os.rmdir(link)
            else:link.unlink()
        assert target.is_dir() and not link.exists()

    def test_canonical_directory_junction_is_rejected_before_read_write_or_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);outbox=root/'account-A'/'outbox';outbox.mkdir(parents=True)
            foreign=root/'outside-owned-outbox';foreign.mkdir();junction=outbox/'canonical-v1'
            raw=b'preexisting outside bytes';source=outbox/(uuid4().hex+'.blob');source.write_bytes(raw)
            snapshot=dict(id=str(uuid4()),seq=1,fingerprint='owned proof',size=len(raw),
                          sha256=hashlib.sha256(raw).hexdigest(),path=str(source),intake_id=str(uuid4()))
            target=resource._canonical(outbox,'account-A',snapshot);existing=foreign/target.name;existing.write_bytes(raw)
            with self.owned_directory_link(foreign,junction):
                if os.name=='nt':self.assertTrue(junction.is_junction());self.assertFalse(junction.is_symlink())
                with patch.object(resource,'_check_bytes',side_effect=AssertionError('must not read external body')):
                    with self.assertRaises(resource.StageResourceError):resource.prepare_binding(outbox,'account-A',snapshot,threading.Event())
                self.assertFalse(resource.owned_stage_path(target,outbox))
                with self.assertRaises(resource.StageResourceError):resource.remove_unreferenced(outbox,frozenset(),target,threading.Event())
                with self.assertRaises(resource.StageResourceError):resource.scan_bindings(outbox,threading.Event())
                self.assertEqual(existing.read_bytes(),raw);self.assertEqual(source.read_bytes(),raw)
                self.assertFalse(list(foreign.glob('*.json')))

    def test_owned_ancestor_source_and_intent_reparse_points_are_rejected(self):
        for role in ('root','account','outbox','source','intent'):
            with self.subTest(role=role),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);cloud=root/'cloud-shelves';account=cloud/'account-A';outbox=account/'outbox'
                foreign=root/'foreign-owned';foreign.mkdir();raw=b'external fixture must survive'
                if role=='root':link=cloud;physical=foreign/'account-A'/'outbox'
                elif role=='account':cloud.mkdir();link=account;physical=foreign/'outbox'
                elif role=='outbox':account.mkdir(parents=True);link=outbox;physical=foreign
                else:
                    outbox.mkdir(parents=True);physical=outbox
                    link=outbox/(uuid4().hex+('.blob' if role=='source' else '.json'))
                physical.mkdir(parents=True,exist_ok=True)
                sentinel=foreign/'sentinel.bin';sentinel.write_bytes(raw)
                source=physical/(uuid4().hex+'.blob');source.write_bytes(raw)
                with self.owned_directory_link(foreign,link):
                    alias=link if role=='source' else outbox/source.name
                    snapshot=dict(id=str(uuid4()),seq=1,fingerprint='owned proof',size=len(raw),
                                  sha256=hashlib.sha256(raw).hexdigest(),path=str(alias),intake_id=str(uuid4()))
                    if role=='intent':
                        with self.assertRaises(resource.StageResourceError):resource._read_record(link)
                    else:
                        with self.assertRaises(resource.StageResourceError):resource.prepare_binding(outbox,'account-A',snapshot,threading.Event())
                        with self.assertRaises(resource.StageResourceError):resource.remove_unreferenced(outbox,frozenset(),alias,threading.Event())
                    self.assertEqual(sentinel.read_bytes(),raw);self.assertEqual(source.read_bytes(),raw)
        # A non-symlink Windows reparse attribute is also refused. This portable
        # shape supplements the real owned Windows junction cases above.
        from types import SimpleNamespace
        with patch.object(resource,'_is_link',return_value=False),patch.object(Path,'lstat',return_value=SimpleNamespace(st_file_attributes=0x400)):
            self.assertTrue(resource.linked_path(Path('owned-reparse')))


    def test_invalid_outbox_is_rejected_once_on_worker_before_copy_and_recovers(self):
        import shelf_sync
        import sync_files
        from PySide6.QtCore import QThread
        raw=b'owned source bytes'*16384
        paths=[self.file(f'admission-{n}.bin',raw+bytes([n])) for n in range(2)]
        self.profiles+=1;self.profile=self.root/f'profile-{self.profiles}'
        self.adapter.view['permanent']=paths
        self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000)
        self.engine.set_directions(True,False)
        account=self.engine.root/'account-A';account.mkdir();outbox=account/'outbox'
        foreign=self.root/'foreign-admission-owned';foreign.mkdir();sentinel=foreign/'sentinel';sentinel.write_bytes(b'foreign survivor')
        starts=[];reads=[];threads=[];work=self.engine._work;reader=sync_files._source_reader;guard=shelf_sync.validate_owned_outbox
        def tracked(function,args,callback):
            if function.__name__=='stage_item':starts.append(args[0])
            return work(function,args,callback)
        def tracked_reader(path):reads.append(str(path));return reader(path)
        def checked_directory(path):
            threads.append(QThread.currentThread()!=APP.thread())
            return guard(path)
        with self.owned_directory_link(foreign,outbox):
            with patch.object(self.engine,'_work',tracked),patch.object(sync_files,'_source_reader',tracked_reader),patch.object(shelf_sync,'validate_owned_outbox',checked_directory):
                self.service.account_changed.emit({'id':'account-A'})
                end=time.monotonic()+.4
                while time.monotonic()<end:APP.processEvents();time.sleep(.002)
                entries=list(self.engine.state['entries'].values())
                self.assertEqual(len(entries),2)
                self.assertTrue(all(entry.get('blocked_fingerprint')==entry['fingerprint'] for entry in entries))
                self.assertEqual(sorted(starts),sorted(paths));self.assertFalse(reads)
                self.assertEqual(threads,[True,True])
                self.assertFalse(self.engine.jobs);self.assertFalse(self.api.body_calls)
                self.assertEqual(list(foreign.iterdir()),[sentinel])
                self.assertEqual(sentinel.read_bytes(),b'foreign survivor')
                self.assertTrue(all(entry.get('last_error') for entry in entries))
        outbox.mkdir()
        for path in paths:self.engine.retry_upload(path)
        self.drain();items=self.api.account('account-A')['items']
        self.assertEqual(len(items),2)
        self.assertEqual({item['data'] for item in items.values()},{raw+b'\0',raw+b'\1'})
        self.assert_originals({paths[0]:raw+b'\0',paths[1]:raw+b'\1'})
        self.assertEqual(sentinel.read_bytes(),b'foreign survivor')


    def test_admission_keeps_original_stage_identity_for_cancel_and_epoch_late_cleanup(self):
        import shelf_sync
        from sync_files import stage_item
        for mode in ('owner-cancel','direction-epoch'):
            with self.subTest(mode=mode):
                if self.engine is not None:
                    self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
                self.profiles+=1;self.profile=self.root/f'profile-{self.profiles}'
                self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000)
                self.engine.account_id='account-A';self.engine.state={'entries':{},'resumable_uploads':{}}
                self.engine.transfer={'id':'held-owned-stage'}
                path=self.file(f'{mode}.bin',b'original late stage')
                ready=threading.Event();release=threading.Event();staged=[]
                admission=shelf_sync._stage_owned_admission
                def held(function,args,cancel):
                    result=admission(function,args,cancel);staged.append(result['path']);ready.set()
                    if not release.wait(3):raise AssertionError('fixture did not release completed owned stage')
                    return result
                with patch.object(shelf_sync,'_stage_owned_admission',held):
                    self.engine._work(stage_item,(path,None,Path(path).name,self.engine.root/'account-A'/'outbox'),
                                      lambda *_:self.fail('obsolete stage callback must not run'))
                    self.assertTrue(spin(ready.is_set));self.assertTrue(Path(staged[0]).exists())
                    if mode=='owner-cancel':self.engine._cancel_upload_owner('held-owned-stage')
                    else:self.engine.set_directions(False,False)
                    self.assertTrue(Path(staged[0]).exists(),'cancel requested is not actual worker terminal')
                    release.set();self.assertTrue(spin(lambda:not self.engine.jobs))
                self.assertFalse(Path(staged[0]).exists())
                self.assertEqual(Path(path).read_bytes(),b'original late stage');self.assertFalse(self.api.body_calls)


    def test_retired_binding_continuation_stops_at_original_close_direction_and_account_boundary(self):
        for boundary in ('close','directions','account'):
            with self.subTest(boundary=boundary):
                if self.engine is not None:
                    self.engine.close();self.assertTrue(spin(lambda:not self.engine.jobs))
                self.api=ReuseApi();self.service=Service(self.api);self.adapter=Adapter()
                raw=b'owned continuation original'*1000;first=self.file(f'{boundary}-first.bin',raw)
                self.start([first]);self.settled_stages(1)
                target=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
                entered=threading.Event();release=threading.Event();original=resource.remove_unreferenced
                after=[];prepare=resource.prepare_binding
                def held(outbox,refs,path,cancel):
                    if str(path)==str(target):
                        entered.set()
                        if not release.wait(3):raise AssertionError('fixture did not release owned GC')
                    return original(outbox,refs,path,cancel)
                def tracked(*args):after.append(True);return prepare(*args)
                try:
                    with patch.object(resource,'remove_unreferenced',held):
                        self.engine.cancel_upload(first);self.assertTrue(spin(entered.is_set))
                        second=self.file(f'{boundary}-second.bin',raw);self.adapter.view['permanent'].append(second)
                        self.engine.local_changed();self.engine.tick()
                        self.assertTrue(spin(lambda:bool(self.engine.stage_store.retired[str(target)][1])))
                        ordinary=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
                        self.assertFalse(resource.canonical_path(ordinary));self.assertTrue(ordinary.exists())
                        with patch.object(resource,'prepare_binding',tracked):
                            if boundary=='close':self.engine.close()
                            elif boundary=='directions':self.engine.set_directions(False,False)
                            else:
                                self.api.token='account-B';self.service.account_changed.emit({'id':'account-B'})
                            release.set();self.assertTrue(spin(lambda:not self.engine.jobs))
                        self.assertFalse(after,'an invalid old continuation must not start prepare')
                        self.assertEqual(ordinary.read_bytes(),raw)
                        old=json.loads(self.engine._profile('account-A').read_text())
                        self.assertIn(str(ordinary),{row['path'] for row in old['resumable_uploads'].values()})
                        self.assert_originals({first:raw,second:raw})
                finally:release.set()

    def test_cancel_retirement_waiter_then_close_finishes_authorized_gc_before_restart(self):
        raw=b'owned cancelled waiting bytes'*1000;first=self.file('cancel-wait-first.bin',raw)
        self.start([first]);self.settled_stages(1)
        target=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
        entered=threading.Event();release=threading.Event();original=resource.remove_unreferenced;completed=[]
        def held(outbox,refs,path,cancel):
            if str(path)==str(target):
                entered.set()
                if not release.wait(3):raise AssertionError('fixture did not release owned GC')
            result=original(outbox,refs,path,cancel);completed.append((str(path),cancel.is_set()));return result
        try:
            with patch.object(resource,'remove_unreferenced',held):
                self.engine.cancel_upload(first);self.assertTrue(spin(entered.is_set))
                second=self.file('cancel-wait-second.bin',raw);self.adapter.view['permanent'].append(second)
                self.engine.local_changed();self.engine.tick()
                self.assertTrue(spin(lambda:bool(self.engine.stage_store.retired[str(target)][1])))
                ordinary=Path(next(iter(self.engine.state['resumable_uploads'].values()))['path'])
                self.engine.cancel_upload(second);self.engine.close();release.set()
                self.assertTrue(spin(lambda:not self.engine.jobs))
            self.assertFalse(ordinary.exists());self.assertFalse(target.exists())
            self.assertTrue(any(path==str(ordinary) and not cancelled for path,cancelled in completed))
            self.engine=ShelfSync(self.profile,self.service,self.adapter,poll_ms=600000)
            self.engine.set_directions(False,False);self.service.account_changed.emit({'id':'account-A'})
            self.assertTrue(spin(lambda:not self.engine.jobs));self.assertFalse(list(self.profile.rglob('*.blob')))
            self.assertFalse(self.engine.state['resumable_uploads']);self.assert_originals({first:raw,second:raw})
        finally:release.set()

    def test_single_item_cancel_reaches_its_inflight_binding_hash_worker(self):
        raw=b'owned prepare cancellation bytes'*1000;first=self.file('hash-first.bin',raw)
        self.start([first]);self.settled_stages(1)
        second=self.file('hash-second.bin',raw)
        entered=threading.Event();release=threading.Event();seen=threading.Event();original=resource._check_bytes
        def held(path,snapshot,cancel):
            if snapshot.get('ref')==second:
                entered.set()
                while not release.is_set():
                    if cancel.wait(.01):seen.set();raise InterruptedError()
            return original(path,snapshot,cancel)
        try:
            with patch.object(resource,'_check_bytes',held):
                self.adapter.view['permanent'].append(second);self.engine.local_changed();self.engine.small_lane.kick()
                self.assertTrue(spin(entered.is_set))
                self.engine.cancel_upload(second)
                self.assertTrue(spin(seen.is_set,seconds=.2),'prepare must observe cancellation before the full read completes')
                release.set();self.assertTrue(spin(lambda:not self.engine.jobs))
            self.assertTrue(self.entry(second).get('paused_upload'));self.assertEqual(len(self.api.body_calls),1)
            self.drain();self.assertEqual(len(self.api.account('account-A')['items']),1)
            self.assert_originals({first:raw,second:raw})
        finally:release.set()


if __name__=='__main__':unittest.main()
