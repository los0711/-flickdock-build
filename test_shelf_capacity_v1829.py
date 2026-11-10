"""Real SQLite quota triggers and native Qt presentation/lifecycle regressions."""
import json,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QPoint,Qt
from shelf_capacity import CLOUD_SHELF_BYTES,ShelfCapacityButton,storage_value,intake_bytes
import test_html_native_workspace as fixture
from test_html_native_workspace import APP,SyncView,flush
from shelf_sync import ShelfSync

ROOT=Path(__file__).parent
def storage(used=0):
    return dict(used_bytes=used,quota_bytes=CLOUD_SHELF_BYTES,server_time='2026-11-10T15:00:00Z',reset_at='2026-11-10T16:00:00Z')

class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'quota.sqlite'
        self.db=sqlite3.connect(self.path)
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.db.close)
        for migration in sorted((ROOT/'cloud/migrations').glob('*.sql')):self.db.executescript(migration.read_text('utf-8'))
        for name in ('a','b'):
            self.db.execute('INSERT INTO accounts(id,username,username_norm,username_key,auth_verifier,auth_salt,created_at,quota_bytes) VALUES(?,?,?,?,?,?,0,?)',(name,name,name,name,'hash','salt',8*CLOUD_SHELF_BYTES))
        self.db.commit()
    def tearDown(self):pass
    @staticmethod
    def reserve(db,size,*,owner='a',kind='file',state='pending',identity=None):
        uid=str(uuid4());identity=identity or str(uuid4())
        db.execute("INSERT INTO uploads(id,account_id,item_id,base_version,kind,name,mime_type,size,sha256,chunk_size,part_count,state,expires_at) VALUES(?,?,?,0,?,'file.bin','application/octet-stream',?, ?,65536,1,?,?)",(uid,owner,identity,kind,size,'0'*64,state,int(time.time()*1000)+60000))
        return uid,identity
    def used(self):return self.db.execute("SELECT COALESCE(SUM(size),0) FROM shelf_usage WHERE account_id='a'").fetchone()[0]
    def test_reservations_and_commits_share_one_budget_without_double_count(self):
        uid,_=self.reserve(self.db,CLOUD_SHELF_BYTES)
        self.assertEqual(self.used(),CLOUD_SHELF_BYTES)
        with self.assertRaisesRegex(sqlite3.IntegrityError,'SHELF_QUOTA'):self.reserve(self.db,1)
        self.db.execute("UPDATE uploads SET state='committed' WHERE id=?",(uid,))
        self.assertEqual(self.used(),CLOUD_SHELF_BYTES)
        updated,expires=self.db.execute('SELECT updated_at,expires_at FROM items').fetchone()
        self.assertEqual(expires,((updated+8*3600000)//86400000+1)*86400000-8*3600000)
    def test_simultaneous_devices_cannot_reserve_more_than_one_gib(self):
        outcomes=[];barrier=threading.Barrier(2)
        def run():
            db=sqlite3.connect(self.path,timeout=5);barrier.wait()
            try:
                with db:self.reserve(db,600*1024**2)
                outcomes.append('accepted')
            except sqlite3.IntegrityError as error:outcomes.append(str(error))
            finally:db.close()
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(8)
        self.assertEqual(sorted(outcomes),['SHELF_QUOTA','accepted'])
        self.assertEqual(self.used(),600*1024**2)
    def test_manual_transfer_and_clipboard_object_do_not_consume_shelf_budget(self):
        self.reserve(self.db,2*CLOUD_SHELF_BYTES,kind='transfer')
        self.reserve(self.db,1024,state='committed')
        self.assertEqual(self.used(),0)
        self.reserve(self.db,CLOUD_SHELF_BYTES);self.reserve(self.db,CLOUD_SHELF_BYTES,owner='b')
        self.assertEqual(self.used(),CLOUD_SHELF_BYTES)
    def test_expired_reservation_frees_capacity_and_cannot_commit_after_midnight(self):
        uid,_=self.reserve(self.db,CLOUD_SHELF_BYTES)
        self.db.execute('UPDATE uploads SET expires_at=1 WHERE id=?',(uid,))
        self.assertEqual(self.used(),0)
        with self.assertRaisesRegex(sqlite3.IntegrityError,'UPLOAD_EXPIRED'):
            self.db.execute("UPDATE uploads SET state='committed' WHERE id=?",(uid,))
    def test_expired_cloud_copy_does_not_reserve_next_day_capacity(self):
        uid,identity=self.reserve(self.db,CLOUD_SHELF_BYTES)
        self.db.execute("UPDATE uploads SET state='committed' WHERE id=?",(uid,))
        self.db.execute('UPDATE items SET expires_at=1 WHERE id=?',(identity,))
        self.assertEqual(self.used(),0);self.reserve(self.db,CLOUD_SHELF_BYTES)

class PresentationTests(unittest.TestCase):
    setUp=fixture.WorkspaceTests.setUp
    tearDown=fixture.WorkspaceTests.tearDown
    overview=fixture.WorkspaceTests.overview
    def test_small_preview_and_overview_keep_ring_left_of_clipboard(self):
        zone=self.shelf.zones['permanent']
        self.assertLess(zone.capacity_button.geometry().right(),zone.clipboard_button.x())
        popup=self.overview()
        self.assertLess(popup.capacity_button.geometry().right(),popup.clipboard_button.x())
        self.assertLessEqual(popup.capacity_button.width(),28)
        self.assertFalse(popup.capacity_button.text())
    def test_pending_to_local_handoff_keeps_the_same_display_slot(self):
        ids=['a','b'];refs=['flickdock:transfer:'+value for value in ids]
        self.shelf.content.pending.update({ref:dict(name=ref,kind='file') for ref in refs})
        before=self.shelf.display_refs();index=before.index(refs[0])
        received=self.base/'received.bin';received.write_bytes(b'received')
        self.shelf.files['permanent'].append(str(received));self.shelf.content.metadata[str(received)]={'_cloud_item_id':'a'}
        self.shelf.content.pending.pop(refs[0]);after=self.shelf.display_refs()
        self.assertEqual(after[index],str(received));self.assertEqual(after[-1],refs[1])
        self.assertEqual(len(before),len(after))
    def test_clipboard_window_is_part_of_the_hover_region(self):
        from PySide6.QtWidgets import QWidget
        dialog=QWidget();dialog.setGeometry(1300,100,200,150);dialog.show();flush()
        self.manager.clipboard_preview_dialog=dialog
        try:self.assertTrue(self.shelf.hover_region_contains(QPoint(1400,175)))
        finally:dialog.close();dialog.deleteLater()
    def test_conflict_forks_with_shared_origin_metadata_remain_two_visible_files(self):
        for ref in self.paths[:2]:self.shelf.content.metadata[ref]={'_cloud_item_id':'shared-origin'}
        self.assertEqual(set(self.shelf.display_refs()),set(self.paths))
    def test_native_stationary_pointer_reconciles_empty_drop_feedback(self):
        self.shelf.files['permanent'].clear();self.shelf.refresh();zone=self.shelf.zones['permanent']
        point=zone.icon.mapToGlobal(zone.icon.rect().center())
        with patch('flickdock.QCursor.pos',return_value=point),patch('flickdock.QApplication.widgetAt',return_value=zone):zone._reconcile_card_hover()
        self.assertTrue(zone.icon.border_motion.active)
        with patch('flickdock.QCursor.pos',return_value=QPoint(-1000,-1000)):zone._reconcile_card_hover()
        self.assertFalse(zone.icon.border_motion.active)
    def test_popover_countdown_and_used_bytes_share_validated_server_state(self):
        button=ShelfCapacityButton();button.set_storage(storage(640*1024**2))
        self.assertEqual(button.storage['remaining_bytes'],384*1024**2)
        self.assertRegex(button.countdown.text(),r'00:59:59|01:00:00')
        self.assertIn('北京时间每日 00:00',button.rule.text())
        self.assertFalse(button.clock.isActive());button.close();button.deleteLater()
        with self.assertRaises(ValueError):storage_value(dict(storage(),used_bytes=True))
        with self.assertRaises(ValueError):storage_value(dict(storage(),reset_at='invalid'))

class LocalCopySafetyTests(unittest.TestCase):
    def setUp(self):
        import test_shelf_transfers_v181 as sync_fixture
        self.fixture=sync_fixture.TransferTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service,self.adapter,self.engine=self.fixture.replica('capacity-local',initial=True)
        sync_fixture.until(lambda:not self.engine.running)
        self.entry=next(iter(self.engine.state['entries'].values()))
    def test_expiry_or_cloud_copy_deletion_keeps_local_shelf_and_bytes(self):
        record=dict(id=self.entry['id'],version=self.entry['version']+1,deleted=True,expires_at='2026-11-10T16:00:00Z')
        completed=[];self.engine._receive(record,lambda:completed.append(True))
        self.assertEqual(completed,[True]);self.assertIn(str(self.fixture.source),self.adapter.view['permanent'])
        self.assertTrue(self.entry['paused_upload']);self.assertTrue(self.entry['dirty'])
        self.assertEqual(self.fixture.source.read_bytes(),self.fixture.payload)
    def test_deleted_local_entry_is_not_recreated_by_stale_remote_upload_metadata(self):
        self.entry.update(deleted=True,dirty=True)
        self.engine.remote_transfers=[dict(item_id=self.entry['id'],upload_id='remote-pending',device_id='other',name='old',kind='file',size=10,received_bytes=1)]
        updates=[];self.engine.transfers_changed.connect(updates.append);self.engine._notify_transfers()
        self.assertEqual(updates[-1],[])
    def test_capacity_failure_pauses_only_that_item_and_preserves_unrelated_payload(self):
        unrelated=dict(id='another-item',seq=1);self.engine.transfer=unrelated;self.engine.running=True
        notices=[];self.engine.capacity_exceeded.connect(notices.append)
        self.engine._capacity_rejected(self.entry['id'])
        self.assertIs(self.engine.transfer,unrelated);self.assertTrue(self.engine.running)
        self.assertTrue(self.entry['paused_upload']);self.assertEqual(notices,[[str(self.fixture.source)]])
        self.assertIn(str(self.fixture.source),self.adapter.view['permanent'])

if __name__=='__main__':unittest.main()
