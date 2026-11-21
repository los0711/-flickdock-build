"""Opt-in production protocol timing probe with a disposable account.

No shelf, keychain, native UI, user files, or installer is touched. The test
item and active sessions are removed even if an intermediate step fails.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import statistics
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication

from account_service import derive_auth_key
from cloud_api import CloudApi
from cloud_config import DEFAULT_CLOUD_URL


def run(endpoint=DEFAULT_CLOUD_URL, size=3_000_000):
    app = QApplication.instance() or QApplication([])
    clients = [CloudApi(endpoint), CloudApi(endpoint)]
    name = 'qa-latency-' + uuid4().hex[:20]
    password = secrets.token_urlsafe(30)
    upload_id = str(uuid4())
    item_id = str(uuid4())
    duplicate_id = str(uuid4())
    content = os.urandom(size)
    timings = {}
    version = None
    duplicate_version = None
    reserved = False

    def call(client, method, path, data=None, raw=None, expected=200, authenticated=True, headers=None,
             destination=None, expected_response=None):
        result = []
        start = time.perf_counter()
        client.request(method, path, lambda value, error: result.append((value, error)),
                       data=data, raw=raw, authenticated=authenticated,
                       headers=headers, destination=destination, expected=expected_response, timeout_ms=60000)
        until = time.monotonic() + 65
        while not result and time.monotonic() < until:
            app.processEvents()
            time.sleep(.002)
        if not result:
            raise RuntimeError('Cloud timing probe timed out')
        value, error = result[0]
        if error is not None or expected != 200 and expected != 201:
            if error is not None:
                raise RuntimeError(f'HTTP {error.status} {error.code} {error.details}')
        return value, round((time.perf_counter() - start) * 1000, 2)

    def login(client, action, device):
        challenge, _ = call(client, 'POST', '/v1/auth/challenge',
                            {'username': name, 'purpose': action}, authenticated=False)
        proof = derive_auth_key(bytearray(password.encode()), bytes.fromhex(challenge['salt']))
        try:
            session, elapsed = call(client, 'POST', '/v1/auth/' + action,
                {'username': name, 'challenge_id': challenge['challenge_id'],
                 'auth_key': proof.hex(), 'device_name': device, 'platform': 'windows'},
                authenticated=False, expected=201 if action == 'register' else 200)
            client.token = session['token']
            return elapsed
        finally:
            proof[:] = b'\0' * len(proof)

    result = {'ok': False, 'size': size, 'endpoint': endpoint, 'timings_ms': timings}
    try:
        timings['register'] = login(clients[0], 'register', 'Latency sender')
        timings['peer_login'] = login(clients[1], 'login', 'Latency receiver')
        spec = {'upload_id': upload_id, 'item_id': item_id, 'base_version': 0,
                'kind': 'file', 'name': 'latency-probe.bin',
                'mime_type': 'application/octet-stream', 'size': size,
                'sha256': hashlib.sha256(content).hexdigest()}
        start = time.perf_counter()
        allocation, timings['initialize'] = call(clients[0], 'POST', '/v1/uploads', spec, expected=201)
        reserved = True
        chunk = allocation['chunk_size']
        parts = []
        remote, timings['pending_visible_query'] = call(clients[1], 'GET', '/v1/sync/changes?cursor=0&limit=100')
        assert any(p['upload_id'] == upload_id for p in remote['transfers'])
        for index in range(allocation['part_count']):
            data = content[index * chunk:(index + 1) * chunk]
            _, elapsed = call(clients[0], 'PUT', f'/v1/uploads/{upload_id}/parts/{index}', raw=data,
                              headers={'X-Content-SHA256': hashlib.sha256(data).hexdigest()})
            parts.append(elapsed)
        committed, timings['commit'] = call(clients[0], 'POST', f'/v1/uploads/{upload_id}/commit', {})
        version = committed['record']['version']
        changes, timings['committed_visible_query'] = call(clients[1], 'GET', '/v1/sync/changes?cursor=0&limit=100')
        assert any(c['record']['id'] == item_id for c in changes['changes'])
        timings['whole_upload'] = round((time.perf_counter() - start) * 1000, 2)
        metadata, timings['download_metadata'] = call(clients[1], 'GET', f'/v1/items/{item_id}/content')
        with tempfile.TemporaryDirectory(prefix='flickdock-roundtrip-') as temporary:
            received=bytearray()
            for index in range(metadata['part_count']):
                expected_bytes=content[index*metadata['chunk_size']:(index+1)*metadata['chunk_size']]
                target=Path(temporary)/f'{index}.bin'
                _, elapsed=call(clients[1], 'GET',f'/v1/items/{item_id}/parts/{index}?version={version}',
                    destination=target, expected_response={'size':len(expected_bytes),
                        'sha256':hashlib.sha256(expected_bytes).hexdigest()})
                timings.setdefault('download_parts_ms',[]).append(elapsed)
                received.extend(target.read_bytes())
            assert received==content
        result['download_original_verified']=True
        instant_spec=dict(spec,upload_id=str(uuid4()),item_id=duplicate_id,name='same-original-fast.bin')
        instant,timings['instant_reuse']=call(clients[0],'POST','/v1/uploads',instant_spec,expected=201)
        assert instant.get('instant') is True and instant['record']['sha256']==spec['sha256']
        duplicate_version=instant['record']['version']
        call(clients[0],'DELETE','/v1/items/'+item_id,{'base_version':version})
        version=None
        status,_=call(clients[0],'GET','/v1/me')
        assert status['storage']['used_bytes']==size
        with tempfile.TemporaryDirectory(prefix='flickdock-instant-download-') as temporary:
            metadata,_=call(clients[1],'GET',f'/v1/items/{duplicate_id}/content')
            restored=bytearray()
            for index in range(metadata['part_count']):
                expected_bytes=content[index*metadata['chunk_size']:(index+1)*metadata['chunk_size']]
                target=Path(temporary)/f'{index}.bin'
                call(clients[1],'GET',f'/v1/items/{duplicate_id}/parts/{index}?version={duplicate_version}',
                    destination=target,expected_response={'size':len(expected_bytes),
                        'sha256':hashlib.sha256(expected_bytes).hexdigest()})
                restored.extend(target.read_bytes())
            assert restored==content
        result['instant_reuse_verified']=True
        result.update(ok=True, chunk_size=chunk, part_count=len(parts),
                      part_ms=dict(min=min(parts), median=statistics.median(parts),
                                   max=max(parts), sum=round(sum(parts), 2)),
                      remote_visible_after_commit=True)
    except Exception as error:
        result['error'] = str(error)[:200]
    finally:
        cleanup = {'item_deleted': version is None and duplicate_version is not None,
                   'duplicate_deleted': False, 'upload_cancelled': False, 'sessions_closed': False}
        try:
            if version is not None:
                call(clients[0], 'DELETE', '/v1/items/' + item_id, {'base_version': version})
                cleanup['item_deleted'] = True
            elif reserved:
                if duplicate_version is None:
                    call(clients[0], 'DELETE', '/v1/uploads/' + upload_id)
                    cleanup['upload_cancelled'] = True
            if duplicate_version is not None:
                call(clients[0],'DELETE','/v1/items/'+duplicate_id,{'base_version':duplicate_version})
                cleanup['duplicate_deleted']=True
        except Exception as error:
            cleanup['error'] = str(error)[:120]
        closed = True
        for client in clients:
            if client.token:
                try:
                    call(client, 'POST', '/v1/auth/logout', {})
                except Exception:
                    closed = False
            client.token = ''
            client.close()
        cleanup['sessions_closed'] = closed
        result['cleanup'] = cleanup
        result['ok'] = result['ok'] and (cleanup['item_deleted'] or cleanup['upload_cancelled']) and (
            duplicate_version is None or cleanup['duplicate_deleted']) and closed
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--size', type=int, default=3_000_000)
    args = parser.parse_args()
    value = run(size=args.size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(value, ensure_ascii=False))
    raise SystemExit(0 if value['ok'] else 1)
