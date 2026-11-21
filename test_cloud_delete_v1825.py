"""Actual shelf Remove action must publish deletion beside a held payload."""
import unittest
from pathlib import Path
import test_shelf_transfers_v181 as fixture


class DeleteBesidePayloadTests(unittest.TestCase):
    setUp=fixture.TransferTests.setUp
    tearDown=fixture.TransferTests.tearDown
    replica=fixture.TransferTests.replica
    project=fixture.TransferTests.project
    close_shelf=staticmethod(fixture.TransferTests.close_shelf)

    def test_completed_receive_does_not_reappear_from_an_older_upload_snapshot(self):
        _,uploaded,sender=self.replica('source',initial=True)
        _,received,receiver=self.replica('receiver')
        fixture.until(lambda:not sender.running and bool(self.server.account('account-A')['items']))
        receiver.request_refresh();fixture.until(lambda:not receiver.running and bool(received.view['permanent']))
        identity=next(iter(receiver.state['entries']))
        receiver.remote_transfers=[dict(item_id=identity,upload_id='old-upload',base_version=0,
            name='old.bin',kind='file',size=len(self.payload),received_bytes=len(self.payload),state='verifying')]
        states=[];receiver.transfers_changed.connect(states.append);receiver._notify_transfers()
        self.assertEqual(states[-1],[])

    def test_observed_tombstone_updates_an_open_overview_beside_a_held_download(self):
        import copy
        from PySide6.QtCore import QPoint
        from sync_adapter import ShelfAdapter
        source,uploaded,sender=self.replica('source',initial=True)
        other,received,receiver=self.replica('receiver')
        fixture.until(lambda:not sender.running and bool(self.server.account('account-A')['items']))
        receiver.request_refresh();fixture.until(lambda:not receiver.running and bool(received.view['permanent']))
        manager,shelf,_=self.project(received,receiver,native_shelf=True)
        manager.app=fixture.APP
        shelf.files['permanent']=list(received.view['permanent'])
        shelf.content.texts=copy.deepcopy(received.view['texts'])
        shelf.content.metadata=copy.deepcopy(received.view['metadata'])
        receiver.adapter=ShelfAdapter(manager)
        shelf.refresh();shelf._show_at(QPoint(60,60),'manual',QPoint())
        shelf.show_contents();shelf.geometry_morph.complete()
        popup=shelf.popup;popup.resume_content()
        fixture.until(lambda:bool(popup._item_widgets))
        ref=shelf.files['permanent'][0];popup.select_item(ref)
        self.assertTrue(shelf.isVisible());self.assertTrue(popup.isVisible())
        identity=next(e['id'] for e in sender.state['entries'].values() if e['ref']==str(self.source))
        extra=self.root/'other-payload.bin';extra.write_bytes(b'held payload'*(512*1024))
        uploaded.add(str(extra));sender.local_changed()
        fixture.until(lambda:not sender.running and len(self.server.account('account-A')['items'])==2)
        other.api.hold_download_part=0;receiver.request_refresh()
        fixture.until(lambda:bool(other.api.held))
        self.assertTrue(receiver.running)
        uploaded.view['permanent'].remove(str(self.source));sender.local_changed()
        fixture.until(lambda:self.server.account('account-A')['items'][identity]['record']['deleted'])
        # Seeing metadata is not applying it. A subsequent busy-cycle refresh
        # must still apply this same tombstone to the already displayed file.
        tombstone=self.server.account('account-A')['items'][identity]['record']
        receiver._observe_remote_changes([dict(record=tombstone)])
        receiver.request_refresh()
        fixture.until(lambda:ref not in shelf.display_refs(),timeout=2)
        self.assertTrue(shelf.isVisible());self.assertTrue(popup.isVisible())
        self.assertNotIn(ref,popup._item_widgets)
        self.assertIsNone(popup.preview.current_path)
        self.assertNotIn(ref,shelf.zones['permanent'].icon.refs)
        self.assertEqual(self.source.read_bytes(),self.payload)
        other.api.release();fixture.until(lambda:not receiver.running)

    def test_delete_all_reply_cannot_republish_cached_heads_before_the_change_feed(self):
        import copy
        service,adapter,sender=self.replica('source',initial=True)
        fixture.until(lambda:not sender.running and bool(self.server.account('account-A')['items']))
        identity=next(e['id'] for e in sender.state['entries'].values() if e['ref']==str(self.source))
        old=copy.deepcopy(self.server.account('account-A')['items'][identity]['record'])
        sender._observe_remote_changes([{'record':old}])
        manager,shelf,_=self.project(adapter,sender,native_shelf=True)
        shelf.files['permanent']=list(adapter.view['permanent'])
        def save():
            adapter.view['permanent']=list(shelf.files['permanent']);sender.local_changed()
        manager.save=save;snapshots=[]
        sender.transfers_changed.connect(lambda rows:snapshots.append([row['id'] for row in rows]))
        shelf.remove_all('permanent')
        fixture.until(lambda:identity not in sender.state['entries'],timeout=2)
        self.assertTrue(snapshots);self.assertTrue(all(identity not in rows for rows in snapshots))
        self.assertEqual(shelf.display_refs(),[])
        deleted=self.server.account('account-A')['items'][identity]['record']
        self.assertGreaterEqual(sender.state['ignored_versions'][identity],deleted['version'])
        sender._remote_heads.clear();sender._observe_remote_changes([{'record':old}])
        sender.remote_transfers=[dict(item_id=identity,upload_id='old-upload',device_id='another-device',name=old['name'],kind=old['kind'],size=old['size'])]
        sender._notify_transfers();self.assertNotIn(identity,snapshots[-1])
        completed=[];sender._receive(old,lambda:completed.append(True))
        self.assertTrue(completed);self.assertEqual(adapter.view['permanent'],[])
        self.assertEqual(self.source.read_bytes(),self.payload)

    def test_real_remove_action_sends_tombstone_without_cancelling_other_upload(self):
        service,adapter,sender=self.replica('source',initial=True)
        other,received,receiver=self.replica('receiver')
        fixture.until(lambda:not sender.running and bool(self.server.account('account-A')['items']))
        receiver.request_refresh()
        fixture.until(lambda:not receiver.running and bool(received.view['permanent']))
        identity=next(e['id'] for e in sender.state['entries'].values() if e['ref']==str(self.source))
        large=self.root/'another.bin';large.write_bytes(b'z'*(5*1024*1024))
        service.api.hold_part=0;adapter.add(str(large));sender.local_changed()
        fixture.until(lambda:bool(service.api.held))
        self.assertTrue(sender.running)
        held={key for key in service.api.pending}
        manager,shelf,_=self.project(adapter,sender,native_shelf=True)
        shelf.files['permanent']=list(adapter.view['permanent'])
        def save():
            adapter.view['permanent']=list(shelf.files['permanent'])
            sender.local_changed()
        manager.save=save
        shelf.remove_path('permanent',str(self.source),keep_local=True)
        fixture.until(lambda:self.server.account('account-A')['items'][identity]['record']['deleted'],timeout=2)
        self.assertTrue(held & set(service.api.pending),'Unrelated payload was cancelled to send deletion')
        receiver.request_refresh()
        fixture.until(lambda:not received.view['permanent'],timeout=2)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.source.read_bytes(),self.payload)
        self.assertTrue(large.exists())


if __name__=='__main__':unittest.main()
