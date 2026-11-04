"""Receive admission is bounded; clipboard pauses launches and cursor commits wait."""
import tempfile
import unittest
from uuid import uuid4
from shelf_sync import ShelfSync
import test_sync_receive_priority as fixture


class ParallelReceiveTests(unittest.TestCase):
    def test_three_independent_items_start_together_versions_keep_order_and_clipboard_resumes(self):
        identities=[uuid4().hex for _ in range(5)]
        records=[dict(cursor=str(index+1),record=fixture.file_record(identity,f'{index}.bin',b'small')) for index,identity in enumerate(identities)]
        records.append(dict(cursor='6',record=dict(records[0]['record'],version=2)))
        with tempfile.TemporaryDirectory() as directory:
            service=fixture.Service(records,{})
            engine=ShelfSync(directory,service,fixture.Adapter(),poll_ms=600000)
            engine.set_directions(False,True);started=[];held={}
            def receive(record,callback,*,remote_order=None):
                key=(record['id'],record['version']);self.assertNotIn(record['id'],{identity for identity,_ in held})
                started.append(key);held[key]=callback
            engine._receive=receive
            try:
                service.account_changed.emit({'id':'parallel-receive'})
                self.assertTrue(fixture.spin(lambda:len(held)==3));self.assertEqual(len(started),3)
                self.assertEqual(engine.state['cursor'],'0')
                engine.set_clipboard_priority(True)
                held.pop((identities[1],1))();fixture.APP.processEvents();fixture.APP.processEvents()
                self.assertEqual(len(held),2);self.assertEqual(len(started),3)
                engine.set_clipboard_priority(False)
                self.assertTrue(fixture.spin(lambda:len(held)==3));self.assertEqual(len(started),4)
                self.assertNotIn((identities[0],2),started)
                held.pop((identities[0],1))()
                self.assertTrue(fixture.spin(lambda:len(held)==3))
                held.pop((identities[2],1))()
                self.assertTrue(fixture.spin(lambda:(identities[0],2) in started))
                for _ in range(5):
                    for key,callback in list(held.items()):held.pop(key);callback()
                    self.assertTrue(fixture.spin(lambda:bool(held) or not engine.running))
                    self.assertLessEqual(len(held),3)
                    if not engine.running:break
                self.assertEqual(len(started),6);self.assertEqual(engine.state['cursor'],'6')
                self.assertFalse(engine.running)
            finally:engine.close()


if __name__=='__main__':unittest.main()
