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
