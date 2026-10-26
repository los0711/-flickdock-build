"""Mac release gates: real disposable ZIPs and fake anonymous HTTPS only."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
import zipfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import release_macos as release
from update_client import validate_manifest


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data); self.status = status; self.read_sizes = []
        self.headers = {'Content-Type': 'application/zip', 'Content-Length': str(len(data))}
        self.headers.update(headers or {})
    def getcode(self): return self.status
    def read(self, size=-1):
        if not 0 < size <= 1024 * 1024:
            raise AssertionError('Public ZIP verification must use bounded streaming reads')
        self.read_sizes.append(size)
        return super().read(size)


class Server:
    def __init__(self, url, data):
        self.routes = {url: (data, 200, {})}; self.requests = []; self.responses = []; self.on_request = None
    def open(self, request, timeout):
        self.requests.append(request)
        if self.on_request: self.on_request()
        result = Response(*self.routes[request.full_url]); self.responses.append(result)
        return result


class MacReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flickdock-mac-release-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.version = '1.8.0'; self.arch = 'arm64'
        self.archive = self.root / 'FlickDock-1.8.0-macOS-arm64.zip'
        self.build_report = self.root / 'mac-build.json'; self.cloud_report = self.root / 'mac-cloud.json'
        self.output = self.root / 'updates' / 'macos-arm64.json'
        self.url = 'https://releases.example.org/' + self.archive.name
        self.write_zip()
        self.write_reports()
        self.server = Server(self.url, self.archive.read_bytes())

    def write_zip(self, *, architecture=None, version=None):
        cpu = {'arm64': 0x0100000c, 'x86_64': 0x01000007}[architecture or self.arch]
        metadata = dict(CFBundleIdentifier='com.flickdock.app', CFBundleExecutable='FlickDock',
            CFBundlePackageType='APPL', CFBundleShortVersionString=version or self.version,
            CFBundleVersion=version or self.version, LSMinimumSystemVersion='14.4')
        with zipfile.ZipFile(self.archive, 'w', compression=zipfile.ZIP_STORED) as package:
            package.writestr('FlickDock.app/Contents/Info.plist', plistlib.dumps(metadata))
            package.writestr('FlickDock.app/Contents/MacOS/FlickDock', b'\xcf\xfa\xed\xfe' + struct.pack('<I', cpu) + b'\0' * 24 + b'native fixture')
            package.writestr('FlickDock.app/Contents/Resources/payload', bytes(range(256)) * 8193)

    def write_reports(self):
        self.build = dict(ok=True, host='macOS-15.7-fixture', version=self.version, architecture=self.arch,
            signed=False, notarized=False, frozen=dict(ok=True, version=self.version, platform='darwin',
                qt_platform='offscreen', bundle_id='com.flickdock.app'), artifacts=[dict(name=self.archive.name,
                bytes=self.archive.stat().st_size, sha256=release.sha256_file(self.archive))])
        self.cloud = dict(ok=True, version=self.version, frozen=True, mac_application_tested=True)
        self.save_reports()

    def save_reports(self):
        self.build_report.write_text(json.dumps(self.build), encoding='utf8')
        self.cloud_report.write_text(json.dumps(self.cloud), encoding='utf8')

    def publish(self, **overrides):
        args = dict(archive=self.archive, build_report=self.build_report, cloud_report=self.cloud_report,
            version=self.version, architecture=self.arch, notes='Verified native Mac release',
            download_url=self.url, output=self.output, initial_release=True, opener=self.server)
        args.update(overrides)
        return release.create_mac_release(**args)

    def blocked(self, **overrides):
        with self.assertRaises(release.ReleaseError): self.publish(**overrides)
        self.assertFalse(self.output.exists())

    def test_native_arm64_manifest_is_committed_after_whole_anonymous_stream_validation(self):
        self.server.on_request = lambda: self.assertFalse(self.output.exists())
        manifest = self.publish()
        self.assertEqual(json.loads(self.output.read_text()), manifest)
        self.assertEqual(manifest, validate_manifest(manifest, {'releases.example.org'}, platform='darwin', machine='arm64'))
        self.assertEqual(manifest['installer']['sha256'], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertGreater(len(self.server.responses[0].read_sizes), 2)
        request = self.server.requests[0]
        self.assertIsNone(request.get_header('Authorization')); self.assertIsNone(request.get_header('Cookie'))
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_native_intel_has_its_own_channel(self):
        self.arch = 'x86_64'; self.archive = self.root / 'FlickDock-1.8.0-macOS-x86_64.zip'
        self.output = self.root / 'updates' / 'macos-x86_64.json'; self.url = 'https://releases.example.org/' + self.archive.name
        self.write_zip(); self.write_reports(); self.server = Server(self.url, self.archive.read_bytes())
        self.assertEqual(self.publish()['architecture'], 'x86_64')
        self.assertFalse((self.output.parent / 'macos-arm64.json').exists())

    def test_reports_require_real_native_frozen_mac_and_exact_zip_bytes(self):
        original_build, original_cloud = copy.deepcopy(self.build), copy.deepcopy(self.cloud)
        changes = [('build', 'ok', False), ('build', 'version', '1.7.0'), ('build', 'architecture', 'x86_64'),
                   ('build', 'host', 'Windows-11'), ('cloud', 'ok', False), ('cloud', 'frozen', False),
                   ('cloud', 'mac_application_tested', False), ('cloud', 'version', '1.7.0')]
        for target, key, value in changes:
            with self.subTest(target=target, key=key):
                self.build, self.cloud = copy.deepcopy(original_build), copy.deepcopy(original_cloud)
                (self.build if target == 'build' else self.cloud)[key] = value
                self.save_reports(); self.blocked()
        for section, key, value in [('frozen', 'platform', 'win32'), ('frozen', 'ok', False), ('frozen', 'bundle_id', 'other.app')]:
            self.build, self.cloud = copy.deepcopy(original_build), copy.deepcopy(original_cloud)
            self.build[section][key] = value; self.save_reports(); self.blocked()
        for key, value in [('name', 'other.zip'), ('bytes', self.archive.stat().st_size + 1), ('sha256', '0' * 64)]:
            self.build, self.cloud = copy.deepcopy(original_build), copy.deepcopy(original_cloud)
            self.build['artifacts'][0][key] = value; self.save_reports(); self.blocked()
        self.assertFalse(self.server.requests)

    def test_embedded_bundle_version_and_native_architecture_must_match(self):
        for options in [dict(version='1.7.0'), dict(architecture='x86_64')]:
            with self.subTest(options=options):
                self.write_zip(**options); self.write_reports(); self.blocked()
        self.assertFalse(self.server.requests)

    def test_initial_release_requires_explicit_flag_and_wrong_output_is_rejected(self):
        self.blocked(initial_release=False)
        self.blocked(output=self.root / 'updates' / 'latest.json')
        self.assertFalse(self.server.requests)

    def test_same_release_is_idempotent_but_repackaging_and_note_changes_are_blocked(self):
        first = self.publish(); old = self.output.read_bytes()
        self.assertEqual(self.publish(initial_release=False), first)
        with self.assertRaises(release.ReleaseError): self.publish(notes='Different notes')
        self.assertEqual(self.output.read_bytes(), old)
        self.archive.write_bytes(self.archive.read_bytes() + b'changed')
        self.write_reports(); self.server.routes[self.url] = (self.archive.read_bytes(), 200, {})
        before = len(self.server.requests)
        with self.assertRaisesRegex(release.ReleaseError, 'repackaged'): self.publish()
        self.assertEqual(len(self.server.requests), before); self.assertEqual(self.output.read_bytes(), old)

    def test_existing_newer_index_cannot_be_bypassed_with_stale_previous_argument(self):
        manifest = self.publish(); self.output.unlink()
        newer = copy.deepcopy(manifest); newer['version'] = '1.9.0'
        self.output.write_text(json.dumps(newer), encoding='utf8'); old = self.output.read_bytes()
        with self.assertRaisesRegex(release.ReleaseError, 'downgrade'): self.publish(previous=manifest)
        self.assertEqual(self.output.read_bytes(), old)

    def test_public_corruption_login_and_unsafe_redirect_never_write_index(self):
        original = self.archive.read_bytes()
        for body, headers in [(original[:-1] + b'x', {}), (original[:-1], {}), (b'<html>login</html>', {'Content-Type': 'text/html'})]:
            with self.subTest(headers=headers):
                self.server.routes[self.url] = (body, 200, headers); self.blocked()
        self.server.routes[self.url] = (b'', 302, {'Location': 'http://releases.example.org/insecure.zip'})
        self.blocked()
        self.server.routes[self.url] = (b'', 302, {'Location': 'https://unapproved.example.org/file.zip'})
        self.blocked()

    def test_safe_redirect_is_streamed_and_nonzip_header_is_rejected_even_with_matching_hash(self):
        other = 'https://cdn.example.org/release.zip'
        self.server.routes[self.url] = (b'', 302, {'Location': other})
        self.server.routes[other] = (self.archive.read_bytes(), 200, {})
        self.publish(allowed_hosts={'releases.example.org', 'cdn.example.org'})
        bad = b'not a ZIP'; server = Server(self.url, bad)
        with self.assertRaisesRegex(release.ReleaseError, 'ZIP header'):
            release.verify_public_zip(self.url, hashlib.sha256(bad).hexdigest(), len(bad), {'releases.example.org'}, opener=server)

    def test_mutable_zip_report_or_index_changed_during_network_is_never_overwritten(self):
        original = self.archive.read_bytes()
        actions = [lambda: self.archive.write_bytes(original + b'changed'),
                   lambda: self.cloud_report.write_text('{}', encoding='utf8')]
        for action in actions:
            self.archive.write_bytes(original); self.write_reports(); self.server.on_request = action
            self.blocked()
        self.archive.write_bytes(original); self.write_reports()
        def concurrent_index():
            self.output.parent.mkdir(parents=True, exist_ok=True); self.output.write_bytes(b'concurrent operator index')
        self.server.on_request = concurrent_index
        with self.assertRaisesRegex(release.ReleaseError, 'index changed'): self.publish()
        self.assertEqual(self.output.read_bytes(), b'concurrent operator index')


if __name__ == '__main__': unittest.main(verbosity=2)
