"""Opt-in cloud throughput probe using only disposable accounts and bytes.

Uploads 3 MB and 80 MiB original buffers, verifies the other device's
download, deletes both cloud items, closes both sessions, and writes a JSON
report. Never reads a user file, shelf profile or operating-system keychain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication

from account_service import derive_auth_key
from cloud_api import CloudApi
from cloud_config import DEFAULT_CLOUD_URL


def _sha_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def run(endpoint=DEFAULT_CLOUD_URL, sizes=(3_000_000, 80 * 1024 * 1024)):
    app = QApplication.instance() or QApplication([])
    sender, receiver = (CloudApi(endpoint), CloudApi(endpoint))
    clients = (sender, receiver)
    username = 'qa-throughput-' + uuid4().hex[:20]
    password = secrets.token_urlsafe(32)
    tracked = []
    report = {'ok': False, 'endpoint': endpoint, 'cases': [], 'cleanup': {}}

    def call(client, method, path, *, data=None, raw=None, headers=None, destination=None,
             expected_bytes=None, authenticated=True, timeout_ms=60000):
        completed = []
        started = time.perf_counter()
        client.request(method, path, lambda value, error: completed.append((value, error)),
                       data=data, raw=raw, headers=headers, destination=destination,
                       expected=expected_bytes, authenticated=authenticated,
                       timeout_ms=timeout_ms)
        # CloudApi scales a large PUT's total deadline with payload size.
        deadline = time.monotonic() + 15 * 60
        while not completed and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.002)
        if not completed:
            raise TimeoutError('云端测速请求未返回。')
        value, error = completed[0]
        if error is not None:
            raise RuntimeError(f'HTTP {error.status} {error.code} {str(error)[:120]}')
        return value, time.perf_counter() - started

    def sign_in(client, purpose, label):
        challenge, _ = call(client, 'POST', '/v1/auth/challenge', data={
            'username': username, 'purpose': purpose}, authenticated=False)
        key = derive_auth_key(bytearray(password.encode()), bytes.fromhex(challenge['salt']))
        try:
            result, _ = call(client, 'POST', '/v1/auth/' + purpose, data={
                'username': username, 'challenge_id': challenge['challenge_id'],
                'auth_key': key.hex(), 'device_name': label, 'platform': 'windows'},
                authenticated=False)
            client.token = result['token']
        finally:
            key[:] = b'\0' * len(key)

    def remote_item(identity):
        cursor = '0'
        latest = None
        for _ in range(100):
            page, _ = call(sender, 'GET', f'/v1/sync/changes?cursor={cursor}&limit=200')
            for change in page['changes']:
                record = change['record']
                if record['id'] == identity:
                    latest = record
            if not page['has_more']:
                break
            cursor = page['cursor']
        return latest

    def clean_item(entry):
        try:
            record = remote_item(entry['item_id'])
            if record and not record['deleted']:
                call(sender, 'DELETE', '/v1/items/' + entry['item_id'],
                     data={'base_version': record['version']})
                entry['cleaned'] = True
            elif record and record['deleted']:
                entry['cleaned'] = True
            else:
                call(sender, 'DELETE', '/v1/uploads/' + entry['upload_id'])
                entry['cleaned'] = True
        except Exception as error:
            entry['cleanup_error'] = str(error)[:160]

    try:
        sign_in(sender, 'register', 'Throughput sender')
        sign_in(receiver, 'login', 'Throughput receiver')
        for size in sizes:
            if not 0 < size <= 90 * 1024 * 1024:
                raise ValueError('Probe size must be within the single-PUT free-tier limit.')
            original = os.urandom(size)
            checksum = hashlib.sha256(original).hexdigest()
            entry = {'item_id': str(uuid4()), 'upload_id': str(uuid4()), 'cleaned': False}
            tracked.append(entry)
            case = {'bytes': size}
            report['cases'].append(case)
            started = time.perf_counter()
            spec = {'upload_id': entry['upload_id'], 'item_id': entry['item_id'],
                    'base_version': 0, 'kind': 'file', 'name': f'probe-{size}.bin',
                    'mime_type': 'application/octet-stream', 'size': size,
                    'sha256': checksum, 'fast_upload': True}
            reservation, init_seconds = call(sender, 'POST', '/v1/uploads', data=spec)
            assert reservation['upload_id'] == entry['upload_id']
            assert reservation['part_count'] == 1 and reservation['chunk_size'] >= size
            _, put_seconds = call(sender, 'PUT', f'/v1/uploads/{entry["upload_id"]}/parts/0',
                                  raw=original, headers={'X-Content-SHA256': checksum})
            committed, commit_seconds = call(sender, 'POST',
                f'/v1/uploads/{entry["upload_id"]}/commit', data={})
            assert committed['record']['sha256'] == checksum
            case.update(initialize_s=round(init_seconds, 3), put_s=round(put_seconds, 3),
                        commit_s=round(commit_seconds, 3),
                        whole_upload_s=round(time.perf_counter() - started, 3),
                        upload_mib_s=round(size / (1024 * 1024 * put_seconds), 2))
            # The second device must observe the committed item independently.
            visible_started = time.perf_counter()
            page, _ = call(receiver, 'GET', '/v1/sync/changes?cursor=0&limit=200')
            assert any(change['record']['id'] == entry['item_id']
                       and not change['record']['deleted'] for change in page['changes'])
            case['remote_visible_s'] = round(time.perf_counter() - visible_started, 3)
            received_started = time.perf_counter()
            metadata, metadata_seconds = call(receiver, 'GET',
                f'/v1/items/{entry["item_id"]}/content',
                headers={'X-FlickDock-Fast-Content': '1'})
            assert metadata['record']['sha256'] == checksum
            assert metadata['part_count'] == 1 and metadata['chunk_size'] >= size
            with tempfile.TemporaryDirectory(prefix='flickdock-cloud-speed-') as directory:
                destination = Path(directory) / 'original.bin'
                _, get_seconds = call(receiver, 'GET',
                    f'/v1/items/{entry["item_id"]}/parts/0?version={committed["record"]["version"]}',
                    headers={'X-FlickDock-Fast-Content': '1'}, destination=destination,
                    expected_bytes={'size': size, 'sha256': checksum}, timeout_ms=120000)
                actual_size, actual_sha = _sha_file(destination)
                assert actual_size == size and actual_sha == checksum
                case.update(download_metadata_s=round(metadata_seconds, 3),
                            get_s=round(get_seconds, 3),
                            whole_download_s=round(time.perf_counter() - received_started, 3),
                            download_mib_s=round(size / (1024 * 1024 * get_seconds), 2),
                            sha256_verified=True)
                if size >= 80 * 1024 * 1024:
                    legacy, _ = call(receiver, 'GET',
                        f'/v1/items/{entry["item_id"]}/content')
                    assert legacy['chunk_size'] == 1024 * 1024
                    assert legacy['part_count'] == (size + legacy['chunk_size'] - 1) // legacy['chunk_size']
                    for index in (0, legacy['part_count'] - 1):
                        offset = index * legacy['chunk_size']
                        expected_part = original[offset:offset + legacy['chunk_size']]
                        part_sha = hashlib.sha256(expected_part).hexdigest()
                        part_path = Path(directory) / f'legacy-{index}.bin'
                        call(receiver, 'GET',
                             f'/v1/items/{entry["item_id"]}/parts/{index}?version={committed["record"]["version"]}',
                             destination=part_path,
                             expected_bytes={'size': len(expected_part), 'sha256': part_sha})
                        part_size, actual_part_sha = _sha_file(part_path)
                        assert part_size == len(expected_part) and actual_part_sha == part_sha
                        assert part_path.read_bytes() == expected_part
                    case['legacy_compat_verified'] = True
            clean_item(entry)
            if not entry['cleaned']:
                raise RuntimeError('测速文件未成功清理，已停止后续测试。')
            del original
        report['ok'] = True
    except Exception as error:
        report['error'] = str(error)[:240]
    finally:
        if sender.token:
            for entry in tracked:
                if not entry['cleaned']:
                    clean_item(entry)
        report['cleanup']['items'] = [{key: entry[key] for key in ('item_id', 'cleaned')}
                                      for entry in tracked]
        if sender.token:
            try:
                for _ in range(40):
                    status, _ = call(sender, 'GET', '/v1/me')
                    if status['storage']['used_bytes'] == 0:
                        report['cleanup']['storage_zero'] = True
                        break
                    time.sleep(.25)
                else:
                    report['cleanup']['storage_zero'] = False
            except Exception as error:
                report['cleanup']['storage_error'] = str(error)[:160]
        sessions_closed = True
        for client in clients:
            if client.token:
                try:
                    call(client, 'POST', '/v1/auth/logout', data={})
                except Exception:
                    sessions_closed = False
            client.token = ''
            client.close()
        report['cleanup']['sessions_closed'] = sessions_closed
        report['ok'] = (report['ok'] and all(entry['cleaned'] for entry in tracked)
                        and report['cleanup'].get('storage_zero') is True and sessions_closed)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Disposable FlickDock remote cloud transfer benchmark')
    parser.add_argument('--endpoint', default=DEFAULT_CLOUD_URL)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-remote-usage', action='store_true',
                        help='Acknowledge two temporary remote file transfers (~83 MB total)')
    args = parser.parse_args()
    if not args.allow_remote_usage:
        parser.error('Pass --allow-remote-usage to run against the cloud service.')
    value = run(endpoint=args.endpoint)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(value, ensure_ascii=False))
    raise SystemExit(0 if value['ok'] else 1)
