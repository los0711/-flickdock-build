"""Real loopback HTTPS for Mac ZIP updates; no app launches or installation."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import test_update_client_v170 as https_fixture
import update_client as subject
app = https_fixture.app

def wait_until(predicate):
    # Native TLS/backend startup on hosted Intel Macs can exceed one second.
    # These cases verify validation/cancellation; dedicated timeout tests keep
    # their short deadlines in test_update_client_v170.
    return https_fixture.wait_until(predicate, timeout=8000)


class MacHTTPS(unittest.TestCase):
    @classmethod
    def setUpClass(cls): https_fixture.UpdateTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): https_fixture.UpdateTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='mac-https-', dir=self.root)
        self.addCleanup(self.folder.cleanup)
        self.clients = []
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('FlickDock.app/Contents/MacOS/FlickDock', b'\xcf\xfa\xed\xfe' + bytes(range(256)) * 4096)
        self.payload = data.getvalue()
        self.manifest = dict(schema_version=1, version='1.10.0', published_at='2026-10-26T00:00:00Z',
            notes='Mac fixture', platform='darwin', architecture='arm64',
            installer=dict(url=self.origin + '/mac.zip', size=len(self.payload),
                sha256=hashlib.sha256(self.payload).hexdigest(), filename='FlickDock-1.10.0-macOS-arm64.zip'))
        self.routes['/mac-manifest'] = dict(type='application/json', body=json.dumps(self.manifest).encode())
        self.routes['/mac.zip'] = dict(body=self.payload, chunk_delay=.001)

    def tearDown(self):
        for client in self.clients: client.close()
        app.processEvents()

    def client(self):
        network = self.Network()
        client = subject.UpdateClient('1.9.0', Path(self.folder.name) / 'updates', endpoint=self.origin + '/mac-manifest',
                    allowed_hosts={'127.0.0.1'}, network=network, platform='darwin', machine='arm64', timeout_ms=5000)
        client._test_network = network
        self.clients.append(client)
        state = {key: [] for key in ('update_available', 'failed', 'progress', 'verified_file', 'cancelled')}
        for key in state: getattr(client, key).connect(lambda *args, key=key: state[key].append(args))
        return client, state

    def test_architecture_endpoints_and_cross_platform_manifest_guards(self):
        self.assertEqual(subject.manifest_endpoint('win32', 'AMD64'), subject.DEFAULT_MANIFEST_URL)
        self.assertTrue(subject.manifest_endpoint('darwin', 'aarch64').endswith('/macos-arm64-auto-v2.json'))
        self.assertTrue(subject.manifest_endpoint('darwin', 'x86_64').endswith('/macos-x86_64-auto-v2.json'))
        self.assertEqual(subject.validate_manifest(self.manifest, {'127.0.0.1'}, platform='darwin', machine='arm64')['architecture'], 'arm64')
        with self.assertRaises(ValueError): subject.validate_manifest(self.manifest, {'127.0.0.1'}, platform='darwin', machine='x86_64')
        with self.assertRaises(ValueError): subject.validate_manifest(self.manifest, {'127.0.0.1'}, platform='win32')
        foreign = copy.deepcopy(self.manifest)
        foreign['installer']['url'] = self.origin + '/setup.exe'
        foreign['installer']['filename'] = 'setup.exe'
        with self.assertRaises(ValueError): subject.validate_manifest(foreign, {'127.0.0.1'}, platform='darwin', machine='arm64')
        universal = copy.deepcopy(self.manifest)
        universal['architecture'] = 'universal2'
        self.assertEqual(subject.validate_manifest(universal, {'127.0.0.1'}, platform='darwin', machine='x86_64')['architecture'], 'universal2')

    def test_real_https_mac_download_publishes_only_update_zip_after_hash(self):
        client, state = self.client()
        client.check(True)
        wait_until(lambda: state['update_available'] or state['failed'])
        self.assertFalse(state['failed'])
        client.download(state['update_available'][0][0])
        wait_until(lambda: state['verified_file'] or state['failed'])
        self.assertFalse(state['failed'])
        path = Path(state['verified_file'][0][0])
        self.assertEqual(path.name, 'update.zip')
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertTrue(path.read_bytes().startswith(b'PK\x03\x04'))
        self.assertGreaterEqual(len(state['progress']), 2)
        self.assertEqual(state['progress'][-1],(len(self.payload),len(self.payload)))
        self.assertFalse(list(path.parent.glob('*.part')))
        self.assertFalse((path.parent / 'setup.exe').exists())

    def test_mac_bad_header_hash_and_architecture_never_publish(self):
        for mode in ('header', 'hash', 'arch'):
            with self.subTest(mode=mode):
                manifest = copy.deepcopy(self.manifest)
                body = self.payload
                if mode == 'header':
                    body = b'MZ' + body[2:]
                    manifest['installer']['sha256'] = hashlib.sha256(body).hexdigest()
                elif mode == 'hash': manifest['installer']['sha256'] = '0' * 64
                else: manifest['architecture'] = 'x86_64'
                self.routes['/mac.zip'] = dict(body=body)
                self.routes['/mac-manifest'] = dict(type='application/json', body=json.dumps(manifest).encode())
                client, state = self.client()
                client.check(True)
                wait_until(lambda: state['update_available'] or state['failed'])
                if mode != 'arch':
                    self.assertFalse(state['failed'],state['failed'])
                    self.assertTrue(state['update_available'],'Valid manifest must precede the corrupt payload')
                    client.download(state['update_available'][0][0])
                    wait_until(lambda: state['failed'])
                self.assertTrue(state['failed'])
                self.assertFalse(state['verified_file'])
                self.assertFalse(list(client.updates_dir.rglob('*.part')))
                self.assertFalse(list(client.updates_dir.rglob('update.zip')))

    def test_mac_download_cancel_and_insecure_redirect_no_late_file(self):
        client, state = self.client()
        client.check()
        wait_until(lambda: state['update_available'])
        client.download(self.manifest)
        wait_until(lambda: any(received > 0 for received, total in state['progress']))
        client.cancel()
        self.assertTrue(state['cancelled'])
        self.assertFalse(state['verified_file'])
        wait_until(lambda:not list(client.updates_dir.rglob('*.part')))
        self.assertFalse(list(client.updates_dir.rglob('*.part')))
        self.routes['/mac.zip'] = dict(status=302, location='http://127.0.0.1/insecure.zip')
        client.download(self.manifest)
        wait_until(lambda: state['failed'])
        self.assertFalse(state['verified_file'])
        self.assertFalse(list(client.updates_dir.rglob('update.zip')))


if __name__ == '__main__': unittest.main(verbosity=2)
