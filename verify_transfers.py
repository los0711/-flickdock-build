"""Explicit frozen QA: ephemeral TLS, original bytes and cancel on loopback.

Never starts discovery, native input, startup registration or a credential store.
The Windows and native Mac binaries execute exactly the same verification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
from uuid import uuid4


def run(argv, load_font):
    if os.environ.get('QT_QPA_PLATFORM', '').lower() != 'offscreen':
        raise RuntimeError('Transfer verification requires explicit offscreen')
    from PySide6.QtWidgets import QApplication
    from app_version import APP_VERSION
    from lan_transfer import (LanTransferService, _read_json, _send_json,
                              BLOCK_BYTES, PROTOCOL, _crypto_library)
    app = QApplication.instance() or QApplication([argv[0]])
    app.setQuitOnLastWindowClosed(False); load_font(app)
    output = Path(argv[argv.index('--verify-output') + 1]).resolve()
    report = dict(ok=False, version=APP_VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  platform=sys.platform, qt_platform=app.platformName(),
                  discovery_broadcast=False, external_network=False, os_credentials_written=False,
                  startup_mutation=False, installer_executed=False, native_hooks=False,
                  phase='initializing', tls_status=[])
    services = []; streams = []
    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate(): return
            if report['phase'] == 'starting_tls':
                for service in services:
                    if not service._starting and not service.port:
                        raise RuntimeError('LAN TLS startup failed: ' + '; '.join(report['tls_status']))
            time.sleep(.003)
        raise TimeoutError('Frozen LAN verification timed out')
    try:
        report['openssl_version'] = ssl.OPENSSL_VERSION
        # This is the same libcrypto lookup and P-256 key generation used at
        # runtime, with no prebuilt/shared private key hidden in the bundle.
        crypto_path = Path(str(_crypto_library()._name))
        report['libcrypto_name'] = crypto_path.name
        if getattr(sys, 'frozen', False):
            bundle_root = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)).resolve()
            if not crypto_path.is_absolute() or not crypto_path.resolve().is_relative_to(bundle_root):
                raise AssertionError('Frozen LAN runtime used external OpenSSL instead of its bundled library')
            report['libcrypto_from_bundle'] = True
        with tempfile.TemporaryDirectory(prefix='flickdock-frozen-lan-') as folder:
            root = Path(folder); destination = root / 'received'; destination.mkdir()
            source = root / 'original-image.png'
            payload = b'\x89PNG\r\n\x1a\noriginal metadata\0' + bytes(range(256)) * 32768 + b'preserve trailing bytes'
            source.write_bytes(payload); expected = hashlib.sha256(payload).hexdigest()
            a = LanTransferService(device_name='QA sender', bind_host='127.0.0.1', discovery=False)
            b = LanTransferService(device_name='QA receiver', bind_host='127.0.0.1', discovery=False)
            a.status_changed.connect(lambda message: report['tls_status'].append('sender: ' + str(message)))
            b.status_changed.connect(lambda message: report['tls_status'].append('receiver: ' + str(message)))
            services.extend((a, b)); offers = []; sent = []; received = []; a_states = []; b_states = []
            a.progress.connect(a_states.append); b.progress.connect(b_states.append)
            a.completed.connect(sent.append); b.completed.connect(received.append); b.offer_received.connect(offers.append)
            report['phase'] = 'starting_tls'
            a.start(); b.start(); wait(lambda: a.port and b.port)
            if not a.fingerprint or a.fingerprint == b.fingerprint:
                raise AssertionError('Ephemeral TLS certificates must be unique')
            report['ephemeral_tls_certificate_generated'] = True
            report['phase'] = 'original_transfer'
            a.remember_peer(b.own_peer()); identifier = a.send(b.device_id, [source])
            wait(lambda: offers and any(row['state'] == 'waiting' for row in a_states))
            if list(destination.iterdir()): raise AssertionError('Incoming bytes appeared before receiver consent')
            report['no_write_before_consent'] = True
            code = next(row['code'] for row in a_states if row['state'] == 'waiting')
            if offers[0]['code'] != code: raise AssertionError('TLS channel authentication codes differ')
            report['tls_channel_code_matched'] = True
            b.accept(identifier, destination); wait(lambda: received and sent, timeout=30)
            target = Path(received[0]['paths'][0])
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != expected or target.stat().st_size != len(payload): raise AssertionError('Original bytes changed')
            report.update(original_bytes=len(payload), original_sha256=expected, received_sha256=digest,
                          original_image_metadata_preserved=True, original_filename_preserved=target.name == source.name)
            source.unlink(); report['original_handle_released'] = True
            # Exercise the actual frozen TLS server with an interrupted prefix;
            # then cancel and ensure only the incomplete transfer is removed.
            report['phase'] = 'cancel_partial'
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
            raw = socket.create_connection(('127.0.0.1', b.port), timeout=5)
            try: stream = context.wrap_socket(raw, server_hostname='flickdock-lan')
            except BaseException: raw.close(); raise
            streams.append(stream); stream.settimeout(1)
            partial_id = uuid4().hex; token = secrets.token_hex(32)
            _send_json(stream, dict(protocol=PROTOCOL, id=partial_id, token=token, sender='QA partial sender',
                                   entries=[dict(path='incomplete.bin', kind='file', size=BLOCK_BYTES * 4)]))
            wait(lambda: any(row['id'] == partial_id for row in offers))
            b.accept(partial_id, destination)
            ready = _read_json(stream, threading.Event())
            if ready.get('state') != 'ready': raise AssertionError('Receiver did not accept partial transfer')
            stream.sendall(b'\x7f' * BLOCK_BYTES)
            wait(lambda: b._transfers[partial_id].offset == BLOCK_BYTES)
            b.cancel(partial_id); wait(lambda: not b._transfers[partial_id].active)
            if any(path.name.startswith('.flickdock-receiving-') for path in destination.iterdir()):
                raise AssertionError('Cancellation left incomplete files')
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise AssertionError('Cancelling a transfer changed another completed file')
            report.update(cancel_cleans_partial_file=True, completed_file_preserved=True,
                          progress_reported=any(row['state'] == 'transferring' for row in b_states), phase='complete', ok=True)
    except Exception as error:
        report['error'] = str(error)
    finally:
        report['startup_diagnostics'] = [service.startup_diagnostic for service in services if service.startup_diagnostic]
        for stream in streams:
            try: stream.close()
            except OSError: pass
        for service in services: service.close()
        for service in services:
            for thread in list(service._threads): thread.join(timeout=1.2)
        app.processEvents()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(run(sys.argv, lambda application: None))
