"""Controlled IO failure/blocked writer checks; no desktop/Qt/native APIs."""
import importlib.util,json,tempfile,threading,time,unittest
from pathlib import Path
from diagnostic_journal import DiagnosticJournal

class JournalTests(unittest.TestCase):
    def test_progress_trace_failure_cannot_interrupt_resource_cleanup_or_pass_gate(self):
        path=Path(__file__).with_name('ci_packaged_windows_live.py')
        spec=importlib.util.spec_from_file_location('progress_failure_qa',path);qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)
        with tempfile.TemporaryDirectory(prefix='fd-progress-failed-') as directory:
            progress=qa.Progress(Path(directory));progress.journal.close(1)
            progress.journal=DiagnosticJournal(write=lambda *args:(_ for _ in ()).throw(OSError('owned write failure')))
            progress.record('before','startup');progress.journal.thread.join(1)
            progress.record('exception');progress.record('before','cleanup')
            progress.uia_stage(dict(stage='request_begin',elapsed_s=.1,request_id=1))
            receipt=progress.close();self.assertFalse(receipt['complete']);self.assertEqual(receipt['error_type'],'OSError')
            self.assertEqual(progress.trace_failure_code,'WRITE_FAILED')
    def test_blocked_write_does_not_block_submit_or_fake_completion(self):
        entered=threading.Event();release=threading.Event();written=[]
        def write(path,row,prefix):entered.set();release.wait();written.append(row)
        journal=DiagnosticJournal(write=write)
        try:
            started=time.monotonic();journal.submit(Path('owned-not-opened'),{'stage':'request_begin'})
            self.assertLess(time.monotonic()-started,.05);self.assertTrue(entered.wait(1))
            receipt=journal.close(.01)
            self.assertFalse(receipt['complete']);self.assertTrue(receipt['writer_still_running']);self.assertEqual(receipt['pending'],1)
        finally:release.set();journal.thread.join(1)
        final=journal.close(.1);self.assertTrue(final['complete']);self.assertEqual(final['completed'],1)
        self.assertEqual(written,[{'stage':'request_begin'}]);self.assertFalse(receipt['complete'])
    def test_order_and_owned_copy_survive_later_caller_mutation(self):
        entered=threading.Event();release=threading.Event();written=[]
        def write(path,row,prefix):entered.set();release.wait();written.append(row)
        journal=DiagnosticJournal(write=write)
        try:
            row={'stage':'before','nested':[1]};journal.submit(Path('unused'),row)
            self.assertTrue(entered.wait(1));row['nested'].append(2);row['stage']='later'
            journal.submit(Path('unused'),{'stage':'after'});release.set()
            receipt=journal.close(1);self.assertTrue(receipt['complete'])
            self.assertEqual(written,[{'stage':'before','nested':[1]},{'stage':'after'}])
        finally:release.set();journal.close(1)
    def test_real_owned_file_contains_every_accepted_record_in_order(self):
        with tempfile.TemporaryDirectory(prefix='fd-journal-owned-') as directory:
            path=Path(directory)/'trace.jsonl';journal=DiagnosticJournal()
            for index in range(30):journal.submit(path,{'index':index})
            receipt=journal.close(1);self.assertTrue(receipt['complete']);self.assertEqual(receipt['completed'],30)
            self.assertEqual([json.loads(line)['index'] for line in path.read_text().splitlines()],list(range(30)))
    def test_write_failure_is_visible_without_exporting_exception_message(self):
        failed=threading.Event()
        def write(*args):failed.set();raise OSError('synthetic private content must not be exported')
        journal=DiagnosticJournal(write=write);journal.submit(Path('unused'),{'stage':'request_begin'})
        self.assertTrue(failed.wait(1));journal.thread.join(1)
        with self.assertRaisesRegex(RuntimeError,'WRITE_FAILED'):journal.submit(Path('unused'),{})
        receipt=journal.close(.1)
        self.assertFalse(receipt['complete']);self.assertEqual(receipt['error_type'],'OSError');self.assertEqual(receipt['pending'],1)
        self.assertNotIn('private',json.dumps(receipt))
    def test_full_queue_fails_explicitly_but_releases_after_drain(self):
        entered=threading.Event();release=threading.Event();written=[]
        def write(path,row,prefix):entered.set();release.wait();written.append(row)
        journal=DiagnosticJournal(capacity=1,write=write)
        try:
            journal.submit(Path('unused'),{'index':1});self.assertTrue(entered.wait(1))
            journal.submit(Path('unused'),{'index':2})
            with self.assertRaisesRegex(RuntimeError,'JOURNAL_FULL'):journal.submit(Path('unused'),{'index':3})
            release.set();receipt=journal.close(1)
            self.assertFalse(receipt['complete']);self.assertEqual(receipt['error_type'],'QUEUE_FULL')
            self.assertFalse(receipt['writer_still_running']);self.assertEqual(receipt['completed'],2);self.assertEqual(written,[{'index':1},{'index':2}])
        finally:release.set();journal.close(1)
    def test_closed_journal_rejects_new_records(self):
        journal=DiagnosticJournal();self.assertTrue(journal.close(1)['complete'])
        with self.assertRaisesRegex(RuntimeError,'JOURNAL_CLOSED'):journal.submit(Path('unused'),{})

if __name__=='__main__':unittest.main()
