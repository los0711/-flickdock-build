"""Opt-in diagnostics: disposable local fixtures; never use desktop devices."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from transfer_diagnostics import create
import test_lan_transfer_v181 as fixture
wait_for=fixture.wait_for


class DiagnosticsTests(unittest.TestCase):
    def test_disabled_by_default_and_finish_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{},clear=True):
            self.assertIsNone(create(folder,'lan','send'))
            self.assertEqual(list(Path(folder).iterdir()),[])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'FLICKDOCK_TRANSFER_DIAG':'1'}):
            diag=create(folder,'wan','send')
            diag.manifest(1,300);diag.route_changed('relay','udp')
            diag.sample({'sent':500,'rttMs':22,'buffered':100,'unacked':80},1)
            diag.sample({'sent':600,'rttMs':23,'buffered':30,'unacked':90},1)
            diag.sample({'sent':200,'rttMs':-1,'buffered':40,'unacked':1,'bufferedMax':180,'unackedMax':120},2)
            diag.finish('complete');diag.finish('error')
            lines=next((Path(folder)/'diagnostics').glob('*.jsonl')).read_text(encoding='utf8').splitlines()
            self.assertEqual(len(lines),1)
            row=json.loads(lines[0]);self.assertEqual(row['route'],'relay');self.assertEqual(row['relay_transport'],'udp')
            self.assertEqual(row['rtt_ms'],23);self.assertEqual(row['buffered_amount_max'],180);self.assertEqual(row['unacked_max'],120)
            self.assertEqual(row['stats_samples'],3);self.assertIsNone(row['fsync_count'])
            self.assertGreater(row['wire_bytes_per_second'],0)

    def test_payload_rate_excludes_time_waiting_for_user_acceptance(self):
        clock=SimpleNamespace(now=0.0)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'FLICKDOCK_TRANSFER_DIAG':'1'}), \
                patch('transfer_diagnostics.time',SimpleNamespace(perf_counter=lambda:clock.now)):
            diag=create(folder,'lan','send');diag.manifest(1,1000)
            clock.now=50;diag.begin_payload();diag.begin_file(0,1000)
            clock.now=55;diag.add('payload_bytes',1000)
            clock.now=56;diag.file_finished(0)
            clock.now=60;diag.finish('complete')
            row=json.loads(next((Path(folder)/'diagnostics').glob('*.jsonl')).read_text(encoding='utf8'))
            self.assertEqual(row['seconds'],60)
            self.assertEqual(row['payload_transfer_seconds'],5)
            self.assertEqual(row['payload_transfer_bytes_per_second'],200)
            self.assertEqual(row['slowest_files'],[{'ordinal':0,'size_bytes':1000,'ms':6000}])
            self.assertIsNone(row['wire_bytes_per_second'])

    def test_rejected_remote_offer_is_recorded_without_waiting_for_a_stream(self):
        from wan_transfer import WanTransferService
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'FLICKDOCK_TRANSFER_DIAG':'1'}):
            service=WanTransferService(folder,enabled=False);identifier=uuid4().hex
            try:
                service._message(json.dumps(dict(schema_version=1,type='request',id=identifier,
                    peer=dict(id='a'*64,name='private device'),token='b'*64,digest='c'*64,
                    total='123',count=1,names=['private-name.txt'])))
                service.cancel(identifier)
                root=Path(folder)/'diagnostics';wait_for(lambda:list(root.glob('*.jsonl')))
                data=next(root.glob('*.jsonl')).read_text(encoding='utf8');row=json.loads(data)
                self.assertEqual((row['protocol'],row['direction'],row['state']),('wan','receive','cancelled'))
                self.assertIsNone(row['route']);self.assertEqual((row['files'],row['total_bytes']),(1,123))
                self.assertNotIn('private',data)
            finally:
                service.close()
                for thread in list(service._threads):thread.join(timeout=2)


class LoopbackDiagnosticsTests(unittest.TestCase):
    setUp=fixture.TransferTests.setUp
    tearDown=fixture.TransferTests.tearDown
    def test_real_loopback_reports_both_directions_without_names_or_paths(self):
        self.a.diagnostics_base=self.root/'send-diag';self.b.diagnostics_base=self.root/'recv-diag'
        source=self.root/'secret-user-filename.bin';source.write_bytes(b'example-payload'*8192)
        with patch.dict(os.environ,{'FLICKDOCK_TRANSFER_DIAG':'1'}):
            identifier=self.a.send(self.b.device_id,[source])
            wait_for(lambda:self.offers);self.assertTrue(self.b.accept(identifier,self.destination,confirmation=self.offers[-1]['code']))
            wait_for(lambda:self.received and self.sent,timeout=20)
            send_dir=self.a.diagnostics_base/'diagnostics';receive_dir=self.b.diagnostics_base/'diagnostics'
            wait_for(lambda:list(send_dir.glob('*.jsonl')) and list(receive_dir.glob('*.jsonl')))
        rows=[]
        for root in (send_dir,receive_dir):
            lines=next(root.glob('*.jsonl')).read_text(encoding='utf8').splitlines()
            self.assertEqual(len(lines),1);rows.append(json.loads(lines[0]))
        self.assertEqual([row['state'] for row in rows],['complete','complete'])
        self.assertEqual([row['protocol'] for row in rows],['lan','lan'])
        self.assertEqual([row['route'] for row in rows],['direct','direct'])
        self.assertEqual([row['total_bytes'] for row in rows],[source.stat().st_size]*2)
        self.assertEqual(rows[1]['fsync_count'],1);self.assertIsNone(rows[0]['fsync_count'])
        self.assertIsNone(rows[0]['wire_bytes_per_second']);self.assertIsNone(rows[1]['rtt_ms'])
        self.assertEqual(rows[1]['files'],1);self.assertEqual(rows[1]['slowest_files'][0]['ordinal'],0)
        self.assertEqual(rows[1]['slowest_files'][0]['size_bytes'],source.stat().st_size)
        self.assertGreater(rows[1]['payload_transfer_seconds'],0)
        self.assertGreater(rows[1]['payload_transfer_bytes_per_second'],0)
        encoded=json.dumps(rows,ensure_ascii=False)
        self.assertNotIn(source.name,encoded);self.assertNotIn(str(self.root),encoded)


if __name__=='__main__':unittest.main()
