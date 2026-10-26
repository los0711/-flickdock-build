"""Disposable loopback QA only: no discovery broadcasts, desktop or firewall changes."""
from __future__ import annotations

import hashlib
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import secrets
import socket
import ssl
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtCore import QPoint, QPointF
from PySide6.QtWidgets import QApplication

import lan_transfer as lan
from lan_panel import LanPanel

APP = QApplication.instance() or QApplication([])


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('Timed out waiting for loopback transfer')


class ManifestTests(unittest.TestCase):
    def test_mac_loader_never_probes_apple_system_crypto_stub(self):
        with patch.object(lan.sys, 'platform', 'darwin'), patch.object(Path, 'glob', return_value=[]), \
             patch.object(lan.ctypes, 'CDLL', side_effect=OSError('No test library')), \
             patch.object(lan.ctypes.util, 'find_library') as generic_lookup:
            with self.assertRaises(RuntimeError): lan._crypto_library()
            generic_lookup.assert_not_called()

    def test_no_traversal_absolute_duplicate_or_file_parent(self):
        cases = [
            [{'path': '../evil', 'kind': 'file', 'size': 1}],
            [{'path': '/evil', 'kind': 'file', 'size': 1}],
            [{'path': 'a\\evil', 'kind': 'file', 'size': 1}],
            [{'path': 'CON.png', 'kind': 'file', 'size': 1}],
            [{'path': 'a', 'kind': 'file', 'size': 1}, {'path': 'A', 'kind': 'file', 'size': 1}],
            [{'path': 'a', 'kind': 'file', 'size': 1}, {'path': 'a/child', 'kind': 'file', 'size': 1}],
            [{'path': 'a', 'kind': 'directory', 'size': 1}],
            [{'path': 'a', 'kind': 'file', 'size': True}],
            [{'path': 'a', 'kind': 'file', 'size': -1}],
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError): lan._validate_manifest(case)

    def test_public_or_untrusted_discovery_is_rejected(self):
        peer = {'id': uuid4().hex, 'name': 'hello', 'host': '8.8.8.8', 'port': 1234,
                'fingerprint': 'a' * 64, 'protocol': lan.PROTOCOL}
        with self.assertRaises(ValueError): lan._validate_peer(peer)
        peer['host'] = '127.0.0.1'; self.assertEqual(lan._validate_peer(peer)['host'], '127.0.0.1')
        peer['fingerprint'] = 'bad'
        with self.assertRaises(ValueError): lan._validate_peer(peer)

    def test_no_compression_or_pre_copy_needed_for_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, 'a.png'); source.write_bytes(b'raw original image metadata\x00')
            entries, sources, total = lan._make_manifest([source], lan.threading.Event())
            self.assertEqual(entries, [{'path': 'a.png', 'kind': 'file', 'size': source.stat().st_size}])
            self.assertEqual(sources['a.png'][0], source.resolve())
            self.assertEqual(list(Path(folder).iterdir()), [source]); self.assertEqual(total, source.stat().st_size)

    def test_cleanup_cannot_remove_a_matching_named_folder_outside_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); destination = root / 'chosen'; destination.mkdir()
            identifier = uuid4().hex; outside = root / ('.flickdock-receiving-' + identifier)
            outside.mkdir(); (outside / 'keep').write_bytes(b'original')
            transfer = lan._Transfer(identifier, 'receive', 'peer', stage=outside, destination=destination)
            self.assertFalse(lan.LanTransferService._cleanup(transfer))
            self.assertEqual((outside / 'keep').read_bytes(), b'original')


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='flickdock-lan-qa-')
        self.root = Path(self.folder.name); self.destination = self.root / 'received'; self.destination.mkdir()
        self.a = lan.LanTransferService(device_name='发送电脑', bind_host='127.0.0.1', discovery=False)
        self.b = lan.LanTransferService(device_name='接收电脑', bind_host='127.0.0.1', discovery=False)
        self.offers = []; self.sent = []; self.received = []; self.a_states = []; self.b_states = []
        self.b.offer_received.connect(self.offers.append)
        self.a.completed.connect(self.sent.append); self.b.completed.connect(self.received.append)
        self.a.progress.connect(self.a_states.append); self.b.progress.connect(self.b_states.append)
        self.a.start(); self.b.start(); wait_for(lambda: self.a.port and self.b.port)
        self.a.remember_peer(self.b.own_peer()); self.b.remember_peer(self.a.own_peer())

    def tearDown(self):
        self.a.close(); self.b.close()
        for service in (self.a, self.b):
            for thread in list(service._threads): thread.join(timeout=1.2)
        APP.processEvents(); self.folder.cleanup()

    def _manual_offer(self, data, name='original.bin', *, identifier=None, token=None):
        identifier = identifier or uuid4().hex; token = token or secrets.token_hex(32)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        stream = context.wrap_socket(socket.create_connection(('127.0.0.1', self.b.port)), server_hostname='flickdock-lan')
        stream.settimeout(1)
        offer = {'protocol': lan.PROTOCOL, 'id': identifier, 'token': token,
                 'sender': 'Loopback', 'entries': [{'path': name, 'kind': 'file', 'size': len(data)}]}
        lan._send_json(stream, offer)
        return stream, offer

    def test_original_files_directories_empty_files_and_consent(self):
        source = self.root / 'Original'; source.mkdir(); (source / 'empty folder').mkdir()
        (source / 'zero.mp4').write_bytes(b'')
        payload = b'\x89PNG\r\nmetadata and trailing bytes\0' + bytes(range(256)) * (1024 * 72)
        (source / 'photo.png').write_bytes(payload)
        identifier = self.a.send(self.b.device_id, [source])
        wait_for(lambda: self.offers)
        self.assertEqual(list(self.destination.iterdir()), [], 'No writes are allowed before consent')
        wait_for(lambda: any(s['state'] == 'waiting' for s in self.a_states))
        sent_code = next(s['code'] for s in self.a_states if s['state'] == 'waiting')
        self.assertEqual(self.offers[0]['code'], sent_code)
        self.b.accept(identifier, self.destination)
        wait_for(lambda: self.received and self.sent, timeout=20)
        received_root = Path(self.received[0]['paths'][0])
        self.assertEqual((received_root / 'photo.png').read_bytes(), payload)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), hashlib.sha256((received_root / 'photo.png').read_bytes()).hexdigest())
        self.assertTrue((received_root / 'empty folder').is_dir())
        self.assertEqual((received_root / 'zero.mp4').read_bytes(), b'')
        self.assertFalse(any(p.name.startswith('.flickdock-receiving-') for p in self.destination.iterdir()))
        (source / 'photo.png').unlink()  # No retained original file handle.

    def test_reject_has_no_filesystem_effect(self):
        source = self.root / 'sample'; source.write_bytes(b'private')
        identifier = self.a.send(self.b.device_id, [source]); wait_for(lambda: self.offers)
        self.b.reject(identifier)
        wait_for(lambda: any(s['state'] in ('error', 'cancelled') for s in self.a_states))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_cancel_accepted_transfer_cleans_partial_bytes(self):
        data = b'x' * (lan.BLOCK_BYTES * 4)
        stream, offer = self._manual_offer(data)
        try:
            wait_for(lambda: self.offers); self.b.accept(offer['id'], self.destination)
            ready = lan._read_json(stream, lan.threading.Event()); self.assertEqual(ready['state'], 'ready')
            stream.sendall(data[:lan.BLOCK_BYTES])
            wait_for(lambda: self.b._transfers[offer['id']].offset == lan.BLOCK_BYTES)
            self.b.cancel(offer['id'])
            wait_for(lambda: not self.b._transfers[offer['id']].active)
            self.assertEqual(list(self.destination.iterdir()), [])
        finally: stream.close()

    def test_terminal_cancel_event_is_emitted_only_after_cleanup_finishes(self):
        data = b'x' * (lan.BLOCK_BYTES * 2)
        stream, offer = self._manual_offer(data)
        release_cleanup = lan.threading.Event(); cleanup_entered = lan.threading.Event()
        try:
            wait_for(lambda: self.offers); self.b.accept(offer['id'], self.destination)
            lan._read_json(stream, lan.threading.Event()); stream.sendall(data[:lan.BLOCK_BYTES])
            transfer = self.b._transfers[offer['id']]
            wait_for(lambda: transfer.offset == lan.BLOCK_BYTES)
            cleanup = self.b._cleanup
            def blocked_cleanup(current):
                cleanup_entered.set()
                if not release_cleanup.wait(5): raise AssertionError('QA cleanup release timeout')
                return cleanup(current)
            with patch.object(self.b, '_cleanup', blocked_cleanup):
                self.b.cancel(offer['id']); wait_for(cleanup_entered.is_set)
                # Deterministically hold cleanup open, rather than trusting a
                # timing delay that happens to pass on an idle CPU.
                self.assertTrue(transfer.active)
                self.assertTrue(transfer.stage.exists())
                self.assertFalse(any(s['id'] == offer['id'] and s['state'] == 'cancelled' for s in self.b_states))
                release_cleanup.set()
                wait_for(lambda: any(s['id'] == offer['id'] and s['state'] == 'cancelled' for s in self.b_states))
                self.assertFalse(transfer.active); self.assertIsNone(transfer.stage)
                self.assertEqual(list(self.destination.iterdir()), [])
        finally: release_cleanup.set(); stream.close()

    def test_wrong_tls_pin_fails_before_offer_or_bytes(self):
        peer = self.b.own_peer(); peer['fingerprint'] = '0' * 64; self.a.remember_peer(peer)
        source = self.root / 'sample'; source.write_bytes(b'secret')
        self.a.send(self.b.device_id, [source])
        wait_for(lambda: any(s['state'] == 'error' for s in self.a_states))
        self.assertFalse(self.offers); self.assertEqual(list(self.destination.iterdir()), [])

    def test_checksum_mismatch_discards_received_file(self):
        data = b'original\0image'
        stream, offer = self._manual_offer(data)
        try:
            wait_for(lambda: self.offers); self.b.accept(offer['id'], self.destination)
            lan._read_json(stream, lan.threading.Event()); stream.sendall(data)
            lan._send_json(stream, {'sha256': '0' * 64})
            wait_for(lambda: any(s['state'] == 'error' for s in self.b_states))
            wait_for(lambda: not self.b._transfers[offer['id']].active)
            self.assertEqual(list(self.destination.iterdir()), []); self.assertFalse(self.received)
        finally: stream.close()

    def test_sender_cancel_removes_paused_remote_bytes_and_needs_token(self):
        data = b'z' * (lan.BLOCK_BYTES * 3)
        stream, offer = self._manual_offer(data)
        wait_for(lambda: self.offers); self.b.accept(offer['id'], self.destination)
        lan._read_json(stream, lan.threading.Event()); stream.sendall(data[:lan.BLOCK_BYTES]); stream.close()
        transfer = self.b._transfers[offer['id']]
        wait_for(lambda: not transfer.active)
        self.assertTrue(transfer.stage.exists())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(socket.create_connection(('127.0.0.1', self.b.port)), server_hostname='flickdock-lan') as attacker:
            attacker.settimeout(1)
            lan._send_json(attacker, {'protocol': lan.PROTOCOL, 'id': offer['id'], 'token': '0' * 64, 'action': 'cancel'})
            self.assertEqual(lan._read_json(attacker, lan.threading.Event())['state'], 'error')
        self.assertTrue(transfer.stage.exists(), 'Another LAN user must not cancel a private transfer')
        outgoing = lan._Transfer(offer['id'], 'send', 'receiver', token=offer['token'], remote_peer=self.b.own_peer())
        self.a._transfers[offer['id']] = outgoing; self.a.cancel(offer['id'])
        wait_for(lambda: transfer.cancel.is_set() and transfer.stage is None)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_lost_final_ack_keeps_received_completion_and_can_be_replayed(self):
        data = b'no lost content'
        stream, offer = self._manual_offer(data)
        wait_for(lambda: self.offers); self.b.accept(offer['id'], self.destination)
        lan._read_json(stream, lan.threading.Event())
        original_send = lan._send_json
        def drop_completion(connection, message):
            if message.get('state') == 'complete': raise ConnectionResetError('Simulated lost final acknowledgement')
            return original_send(connection, message)
        lan._send_json = drop_completion
        try:
            stream.sendall(data); original_send(stream, {'sha256': hashlib.sha256(data).hexdigest()})
            self.assertEqual(lan._read_json(stream, lan.threading.Event())['state'], 'verified')
            wait_for(lambda: self.received and not self.b._transfers[offer['id']].active)
            self.assertEqual(Path(self.received[0]['paths'][0]).read_bytes(), data)
            self.assertFalse(any(s['state'] == 'interrupted' for s in self.b_states))
        finally: lan._send_json = original_send; stream.close()
        replay, _ = self._manual_offer(data, identifier=offer['id'], token=offer['token'])
        try:
            self.assertEqual(lan._read_json(replay, lan.threading.Event())['next_file'], 1)
            self.assertEqual(lan._read_json(replay, lan.threading.Event())['state'], 'complete')
        finally: replay.close()
        self.assertEqual(len(self.received), 1)

    def test_disconnect_resumes_prefix_and_lost_completion_is_idempotent(self):
        data = bytes(range(251)) * 10000
        stream, offer = self._manual_offer(data)
        wait_for(lambda: self.offers); self.b.accept(offer['id'], self.destination)
        lan._read_json(stream, lan.threading.Event())
        stream.sendall(data[:lan.BLOCK_BYTES + 99]); stream.close()
        transfer = self.b._transfers[offer['id']]
        wait_for(lambda: not transfer.active)
        self.assertEqual(transfer.offset, lan.BLOCK_BYTES)
        resumed, _ = self._manual_offer(data, identifier=offer['id'], token=offer['token'])
        try:
            ready = lan._read_json(resumed, lan.threading.Event())
            self.assertEqual(ready['offset'], lan.BLOCK_BYTES)
            resumed.sendall(data[ready['offset']:])
            lan._send_json(resumed, {'sha256': hashlib.sha256(data).hexdigest()})
            self.assertEqual(lan._read_json(resumed, lan.threading.Event())['state'], 'verified')
            self.assertEqual(lan._read_json(resumed, lan.threading.Event())['state'], 'complete')
        finally: resumed.close()
        wait_for(lambda: self.received and not transfer.active)
        self.assertEqual(Path(self.received[0]['paths'][0]).read_bytes(), data)
        replay, _ = self._manual_offer(data, identifier=offer['id'], token=offer['token'])
        try:
            self.assertEqual(lan._read_json(replay, lan.threading.Event())['next_file'], 1)
            self.assertEqual(lan._read_json(replay, lan.threading.Event())['state'], 'complete')
        finally: replay.close()
        self.assertEqual(len(self.received), 1); self.assertEqual(len(self.offers), 1)

    def test_ui_updates_inline_and_wheel_does_not_select_devices(self):
        panel = LanPanel(self.a, theme='dark'); panel.show()
        self.a.remember_peer(dict(self.b.own_peer(), id=uuid4().hex, name='另一台电脑'))
        APP.processEvents()
        selected = panel.devices.currentIndex()
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        APP.sendEvent(panel.devices, event)
        self.assertEqual(panel.devices.currentIndex(), selected)
        panel._progress({'id': uuid4().hex, 'direction': 'send', 'peer': '电脑', 'state': 'waiting', 'code': '123456'})
        self.assertIn('确认码 123456', list(panel._rows.values())[0]['detail'].text())
        panel.close(); panel.deleteLater(); APP.processEvents()


if __name__ == '__main__': unittest.main(verbosity=2)
