"""Durability and ordering checks for the background shelf journal."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from account_store import atomic_json
from shelf_journal import ShelfJournal


class ShelfJournalTests(unittest.TestCase):
    def test_coalesces_only_unwritten_snapshots_and_keeps_accounts_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            a=Path(folder)/'a'/'state.json';b=Path(folder)/'b'/'state.json'
            entered=threading.Event();release=threading.Event();writes=[]
            def writer(path,value):
                if not entered.is_set():entered.set();release.wait(3)
                writes.append((path,value));atomic_json(path,value)
            journal=ShelfJournal(writer)
            try:
                first=journal.submit(a,{'version':1});self.assertTrue(entered.wait(1))
                mutable={'version':2};journal.submit(a,mutable);mutable['version']=999
                last=journal.submit(a,{'version':3});other=journal.submit(b,{'version':8})
                self.assertEqual(journal.latest(a),{'version':3})
                self.assertEqual(journal.written(a),0)
                release.set();self.assertTrue(journal.flush(3))
                self.assertEqual(json.loads(a.read_text(encoding='utf8')),{'version':3})
                self.assertEqual(json.loads(b.read_text(encoding='utf8')),{'version':8})
                self.assertEqual(journal.written(a),last);self.assertEqual(journal.written(b),other)
                self.assertLess(first,last);self.assertNotIn({'version':999},[row[1] for row in writes])
            finally:release.set();journal.flush(3)

    def test_failed_write_does_not_advance_commit_and_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'state.json';fail=threading.Event();fail.set()
            def writer(path,value):
                if fail.is_set():raise OSError('simulated unavailable disk')
                atomic_json(path,value)
            journal=ShelfJournal(writer)
            revision=journal.submit(target,{'paused':True})
            deadline=time.monotonic()+3
            while journal._thread is not None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(journal.written(target),0);self.assertFalse(target.exists())
            self.assertEqual(journal.latest(target),{'paused':True})
            fail.clear();journal.retry();self.assertTrue(journal.flush(3))
            self.assertEqual(journal.written(target),revision)
            self.assertEqual(json.loads(target.read_text(encoding='utf8')),{'paused':True})


if __name__=='__main__':unittest.main()
