"""Mach-O byte fixtures in temporary folders; no app or installer executes."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import hashlib
from io import BytesIO
import json
from pathlib import Path
import plistlib
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
from urllib.request import Request

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
import mac_updates as update

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def spin(predicate, timeout=2):
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(5, loop.quit)
        loop.exec()
    return predicate()


def metadata(version):
    return dict(CFBundleIdentifier=update.BUNDLE_ID, CFBundlePackageType='APPL',
                CFBundleExecutable='FlickDock', CFBundleShortVersionString=version)


class MacUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flickdock-mac-update-fixture-')
        self.base = Path(self.temp.name).resolve()
        self.target = self.base / 'Applications' / 'FlickDock.app'
        (self.target / 'Contents' / 'MacOS').mkdir(parents=True)
        (self.target / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(metadata('1.7.0')))
        (self.target / 'Contents' / 'MacOS' / 'FlickDock').write_bytes(b'\xcf\xfa\xed\xfeOLD-FIXTURE')
        self.zip = self.base / 'setup.zip'
        self.prepared = []
        self.write_zip()

    def tearDown(self):
        for prepared in self.prepared: prepared.cleanup()
        self.temp.cleanup()

    def write_zip(self, *, extras=(), version='1.7.1', identity=None, executable=b'\xcf\xfa\xed\xfeNEW-FIXTURE'):
        data = metadata(version)
        if identity is not None: data['CFBundleIdentifier'] = identity
        with zipfile.ZipFile(self.zip, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content, mode in [('FlickDock.app/Contents/Info.plist', plistlib.dumps(data), stat.S_IFREG | 0o644),
                    ('FlickDock.app/Contents/MacOS/FlickDock', executable, stat.S_IFREG | 0o755), *extras]:
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = mode << 16
                archive.writestr(item, content)
        if any('\\' in name for name, content, mode in extras):
            raw = self.zip.read_bytes()
            for name, content, mode in extras:
                if '\\' in name:
                    raw = raw.replace(name.replace('\\', '/').encode(), name.encode())
            self.zip.write_bytes(raw)
        self.manifest = dict(version='1.7.1', installer=dict(size=self.zip.stat().st_size,
                    sha256=hashlib.sha256(self.zip.read_bytes()).hexdigest()))

    def prepare(self):
        result = update.prepare_update(self.zip, self.manifest, current_version='1.7.0', installed_app=self.target)
        self.prepared.append(result)
        return result

    def assert_old(self):
        self.assertEqual(update.bundle_metadata(self.target)['CFBundleShortVersionString'], '1.7.0')
        self.assertEqual((self.target / 'Contents' / 'MacOS' / 'FlickDock').read_bytes(), b'\xcf\xfa\xed\xfeOLD-FIXTURE')

    def test_prepare_does_not_replace_and_atomic_install_fixture_only(self):
        prepared = self.prepare()
        self.assert_old()
        self.assertEqual(prepared.stage.parent, self.target.parent)
        events = []
        result = update.install_prepared(prepared, relaunch=lambda path: events.append(path), test_only=True)
        self.assertEqual(result, self.target)
        self.assertEqual(events, [self.target])
        self.assertEqual(update.bundle_metadata(self.target)['CFBundleShortVersionString'], '1.7.1')
        self.assertFalse(prepared.stage.exists())
        self.assertFalse(list(self.target.parent.glob('.FlickDock.rollback-*')))

    def test_user_owned_staging_can_replace_system_application_after_authorization(self):
        prepared = update.prepare_update(self.zip, self.manifest, current_version='1.7.0',
                    installed_app=self.target, staging_parent=self.zip.parent)
        self.prepared.append(prepared)
        self.assertEqual(prepared.stage.parent, self.zip.parent)
        self.assert_old()
        steps = []
        update.install_prepared(prepared, test_only=True,
            replacement_ready=lambda: steps.append('replaced'),
            await_readiness=lambda: steps.append('ready'))
        self.assertEqual(steps, ['replaced', 'ready'])
        self.assertEqual(update.bundle_metadata(self.target)['CFBundleShortVersionString'], '1.7.1')
        self.assertFalse(prepared.stage.exists())
        self.assertFalse(list(self.target.parent.glob('.flickdock-update-*')))

    def test_failed_new_app_readiness_restores_original_after_cross_directory_swap(self):
        prepared = update.prepare_update(self.zip, self.manifest, current_version='1.7.0',
                    installed_app=self.target, staging_parent=self.zip.parent)
        self.prepared.append(prepared)
        events = []
        def fail_readiness():
            events.append('readiness checked')
            raise TimeoutError('injected startup failure')
        with self.assertRaisesRegex(TimeoutError, 'injected startup failure'):
            update.install_prepared(prepared, test_only=True,
                replacement_ready=lambda: events.append('replacement signalled'),
                await_readiness=fail_readiness)
        self.assertEqual(events, ['replacement signalled', 'readiness checked'])
        self.assert_old()
        self.assertTrue(prepared.app_path.exists())
        self.assertFalse(list(self.target.parent.glob('.FlickDock.rollback-*')))
        self.assertFalse(list(self.target.parent.glob('.flickdock-update-*')))

    def test_admin_command_quotes_untrusted_path_without_running_a_shell_here(self):
        prepared = update.prepare_update(self.zip, self.manifest, current_version='1.7.0',
                    installed_app=self.target, staging_parent=self.zip.parent)
        self.prepared.append(prepared)
        script = update._administrator_script(prepared,
                     self.target / 'Contents' / 'MacOS' / 'FlickDock')
        self.assertTrue(script.startswith('do shell script "'))
        self.assertIn('with administrator privileges', script)
        self.assertIn('--elevated-mac-update', script)
        self.assertIn('update-receipt.json', script)

    def test_privileged_helper_ignores_tampered_user_stage_and_installs_sealed_zip(self):
        prepared = update.prepare_update(self.zip, self.manifest, current_version='1.7.0',
                    installed_app=self.target, staging_parent=self.zip.parent)
        self.prepared.append(prepared)
        update.launch_update_helper(prepared,
                    executable=self.target / 'Contents/MacOS/FlickDock',
                    popen=lambda *args, **kwargs: object(), quit_callback=lambda: None)
        (prepared.app_path / 'Contents/MacOS/FlickDock').write_bytes(b'\xcf\xfa\xed\xfeTAMPERED')
        marker = prepared.stage / 'replacement-ready'
        def acknowledge():
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            (prepared.stage / 'launch-ready').write_text(prepared.ready_token, encoding='ascii')
        thread = threading.Thread(target=acknowledge)
        thread.start()
        try:
            update.run_elevated_update_helper(prepared.stage / 'update-receipt.json',
                    test_only=True, pid_alive=lambda pid: False,
                    trusted_manifest=self.manifest)
        finally:
            thread.join(timeout=3)
        self.assertEqual((self.target / 'Contents/MacOS/FlickDock').read_bytes(),
                         b'\xcf\xfa\xed\xfeNEW-FIXTURE')

    def test_privileged_helper_rejects_archive_changed_after_user_preparation(self):
        prepared = update.prepare_update(self.zip, self.manifest, current_version='1.7.0',
                    installed_app=self.target, staging_parent=self.zip.parent)
        self.prepared.append(prepared)
        update.launch_update_helper(prepared,
                    executable=self.target / 'Contents/MacOS/FlickDock',
                    popen=lambda *args, **kwargs: object(), quit_callback=lambda: None)
        self.zip.write_bytes(self.zip.read_bytes() + b'TAMPER')
        with self.assertRaisesRegex(ValueError, '官方索引'):
            update.run_elevated_update_helper(prepared.stage / 'update-receipt.json',
                    test_only=True, pid_alive=lambda pid: False,
                    trusted_manifest=self.manifest)
        self.assert_old()
        self.assertFalse(list(self.target.parent.glob('.flickdock-update-*')))

    def test_privileged_manifest_is_fetched_from_fixed_auto_channel(self):
        endpoint = update.manifest_endpoint('darwin', 'arm64')
        published = dict(schema_version=1, version='1.7.1', platform='darwin',
                         architecture='arm64', published_at='2026-10-27T00:00:00Z',
                         notes='fixture', installer=dict(url='https://gitee.com/release/update.zip',
                         size=self.manifest['installer']['size'],
                         sha256=self.manifest['installer']['sha256']))
        class Reply(BytesIO):
            def geturl(self): return endpoint
        requests = []
        def opener(request, timeout):
            requests.append((request.full_url, timeout))
            return Reply(json.dumps(published).encode('utf-8'))
        trusted = update._trusted_manifest_for('1.7.1', machine='arm64', opener=opener)
        self.assertEqual(trusted['installer']['sha256'], self.manifest['installer']['sha256'])
        self.assertEqual(requests, [(endpoint, 15)])
        published['version'] = '1.7.2'
        with self.assertRaisesRegex(ValueError, '索引已变更'):
            update._trusted_manifest_for('1.7.1', machine='arm64', opener=opener)
        redirect = update._TrustedManifestRedirect()
        with self.assertRaises(ValueError):
            redirect.redirect_request(Request(endpoint), None, 302, 'redirect', {},
                                      'http://not-trusted.example/update.json')

    @unittest.skipUnless(os.name == 'posix', 'Directory descriptor markers require POSIX')
    def test_privileged_replacement_marker_is_readable_by_owner(self):
        prepared = update.prepare_update(self.zip, self.manifest, current_version='1.7.0',
                    installed_app=self.target, staging_parent=self.zip.parent)
        self.prepared.append(prepared)
        update._write_privileged_marker(prepared, 'replacement-ready')
        marker = prepared.stage / 'replacement-ready'
        self.assertEqual(marker.read_text(encoding='ascii'), prepared.ready_token)
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o644)
        with self.assertRaises(FileExistsError):
            update._write_privileged_marker(prepared, 'replacement-ready')

    def test_failed_mac_handoff_is_reported_once_to_restarted_app(self):
        prepared = self.prepare()
        profile = self.base / 'FlickDock'
        prepared.source_archive = profile / 'updates' / prepared.version / 'update.zip'
        update._record_update_failure(prepared, '管理员授权已取消')
        self.assertEqual(update.consume_update_failure(profile), '管理员授权已取消')
        self.assertEqual(update.consume_update_failure(profile), '')

    def test_hash_size_version_identity_and_macho_fail_without_changes(self):
        for case in ('hash', 'size', 'version', 'identity', 'macho', 'downgrade'):
            with self.subTest(case=case):
                self.write_zip(version='1.7.2' if case == 'version' else '1.7.1',
                    identity='foreign.app' if case == 'identity' else None,
                    executable=b'MZWindowsFixture' if case == 'macho' else b'\xcf\xfa\xed\xfeNEW')
                if case == 'hash': self.manifest['installer']['sha256'] = '0' * 64
                if case == 'size': self.manifest['installer']['size'] += 1
                if case == 'downgrade': self.manifest['version'] = '1.6.9'
                with self.assertRaises((ValueError, OSError)):
                    self.prepare()
                self.assert_old()
                self.assertFalse(list(self.target.parent.glob('.flickdock-update-*')))

    def test_traversal_absolute_drive_backslash_duplicate_and_link_escape(self):
        attacks = [('FlickDock.app/../../escaped', b'x', stat.S_IFREG | 0o644),
                   ('/tmp/escaped', b'x', stat.S_IFREG | 0o644),
                   ('C:/escaped', b'x', stat.S_IFREG | 0o644),
                   ('FlickDock.app\\escaped', b'x', stat.S_IFREG | 0o644),
                   ('FlickDock.app/Contents/info.plist', b'collision', stat.S_IFREG | 0o644),
                   ('FlickDock.app/escape', b'../../escaped', stat.S_IFLNK | 0o777),
                   ('FlickDock.app/pipe', b'', stat.S_IFIFO | 0o644)]
        for attack in attacks:
            with self.subTest(path=attack[0]):
                self.write_zip(extras=[attack])
                with self.assertRaises(ValueError): self.prepare()
                self.assert_old()
                self.assertFalse(list(self.target.parent.glob('.flickdock-update-*')))
        self.assertFalse((self.base / 'escaped').exists())

    def test_replace_or_relaunch_failure_rolls_back_original(self):
        for case in ('replace', 'relaunch'):
            with self.subTest(case=case):
                prepared = self.prepare()
                calls = []
                def replace(source, target):
                    calls.append((source, target))
                    if case == 'replace' and len(calls) == 2:
                        raise OSError('injected replacement failure')
                    os.replace(source, target)
                def relaunch(path):
                    if case == 'relaunch': raise OSError('injected launch failure')
                with self.assertRaises(OSError):
                    update.install_prepared(prepared, replace=replace, relaunch=relaunch, test_only=True)
                self.assert_old()
                self.assertTrue(prepared.app_path.exists())
                self.assertFalse(list(self.target.parent.glob('.FlickDock.rollback-*')))

    def test_staging_tamper_detected_before_replacement(self):
        prepared = self.prepare()
        (prepared.app_path / 'Contents' / 'MacOS' / 'FlickDock').write_bytes(b'\xcf\xfa\xed\xfeTAMPER')
        with self.assertRaises(ValueError): update.install_prepared(prepared, test_only=True)
        self.assert_old()
        with self.assertRaises(ValueError): update.install_prepared(prepared)

    def test_internal_framework_symlinks_preserved(self):
        probe = self.base / 'symlink-probe'
        try:
            probe.symlink_to(self.zip)
            probe.unlink()
        except OSError:
            self.skipTest('Host does not permit creating temporary symlinks; run on macOS')
        self.write_zip(extras=[
            ('FlickDock.app/Contents/Frameworks/Example.framework/Versions/A/Example', b'framework-fixture', stat.S_IFREG | 0o755),
            ('FlickDock.app/Contents/Frameworks/Example.framework/Versions/Current', b'A', stat.S_IFLNK | 0o777),
            ('FlickDock.app/Contents/Frameworks/Example.framework/Example', b'Versions/Current/Example', stat.S_IFLNK | 0o777)])
        prepared = self.prepare()
        link = prepared.app_path / 'Contents/Frameworks/Example.framework/Example'
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), b'framework-fixture')
        update.install_prepared(prepared, test_only=True)
        installed = self.target / 'Contents/Frameworks/Example.framework/Example'
        self.assertTrue(installed.is_symlink())
        self.assertEqual(installed.read_bytes(), b'framework-fixture')

    def test_injected_helper_spawn_then_quit_wait_and_install_no_actual_execution(self):
        prepared = self.prepare()
        events = []
        executable = self.target / 'Contents' / 'MacOS' / 'FlickDock'
        def popen(arguments, **kwargs):
            events.append(('spawn', arguments, kwargs))
            return object()
        # Handoff must not synchronously hash all staged files on the GUI thread.
        with patch.object(update, '_tree_hash', side_effect=AssertionError('GUI tree hash')):
            update.launch_update_helper(prepared, executable=executable, popen=popen,
                                         quit_callback=lambda: events.append(('quit',)))
        self.assertEqual([item[0] for item in events], ['spawn', 'quit'])
        self.assertEqual(events[0][1][:2], [str(executable), '--apply-mac-update'])
        self.assert_old()
        receipt = Path(events[0][1][2])
        with self.assertRaises(TimeoutError):
            update.run_update_helper(receipt, timeout=0, test_only=True, pid_alive=lambda pid: True)
        self.assert_old()
        relaunched = []
        update.run_update_helper(receipt, test_only=True, pid_alive=lambda pid: False,
                                 relaunch=lambda path: relaunched.append(path))
        self.assertEqual(relaunched, [self.target])
        self.assertEqual(update.bundle_metadata(self.target)['CFBundleShortVersionString'], '1.7.1')

    def test_spawn_failure_does_not_quit_or_replace(self):
        prepared = self.prepare()
        quits = []
        def failed(*args, **kwargs): raise OSError('injected spawn failure')
        with self.assertRaises(OSError):
            update.launch_update_helper(prepared, popen=failed,
                executable=self.target / 'Contents/MacOS/FlickDock', quit_callback=lambda: quits.append(True))
        self.assertEqual(quits, [])
        self.assert_old()

    def test_async_preparation_preserves_heartbeat_and_cancel_cleans_job_only(self):
        entered, release = threading.Event(), threading.Event()
        real = update.prepare_update
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(2)
            return real(*args, **kwargs)
        outcomes, beats = [], []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: beats.append(time.perf_counter()))
        timer.start()
        with patch.object(update, 'prepare_update', delayed):
            job = update.PrepareMacUpdateJob(self.zip, self.manifest, current_version='1.7.0', installed_app=self.target)
            job.finished.connect(lambda value: outcomes.append('finished'))
            job.failed.connect(lambda error: outcomes.append(error))
            job.cancelled.connect(lambda: outcomes.append('cancelled'))
            self.assertTrue(job.start())
            self.assertTrue(spin(entered.is_set))
            self.assertTrue(spin(lambda: len(beats) >= 4))
            start = time.perf_counter()
            job.cancel()
            self.assertLess(time.perf_counter() - start, 0.02)
            release.set()
            self.assertTrue(spin(lambda: bool(outcomes)))
        timer.stop()
        self.assertEqual(outcomes, ['cancelled'])
        self.assertTrue(job.wait(1000))
        self.assertLess(max(b - a for a, b in zip(beats, beats[1:])), 0.1)
        self.assert_old()
        self.assertFalse(list(self.target.parent.glob('.flickdock-update-*')))


if __name__ == '__main__': unittest.main(verbosity=2)
