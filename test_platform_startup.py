"""Only fake registry values and disposable LaunchAgent directories are used."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from pathlib import Path
import plistlib
import tempfile
import unittest
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from platform_startup import (APPROVED_KEY, RUN_KEY, CallbackBackend, LaunchAgentBackend,
                              StartupManager, WindowsRunBackend, startup_command,
                              startup_runtime_allowed)

APP = QApplication.instance() or QApplication([])


class Registry:
    HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_SET_VALUE = 1, 2, 3
    def __init__(self):
        self.values = {(RUN_KEY, 'Unrelated'): ('do-not-change', 1)}
        self.writes = []

    class Key:
        def __init__(self, path): self.path = path
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def OpenKey(self, root, path, reserved, access): return self.Key(path)
    CreateKeyEx = OpenKey
    def QueryValueEx(self, key, name):
        try: return self.values[(key.path, name)]
        except KeyError: raise FileNotFoundError() from None
    def SetValueEx(self, key, name, reserved, kind, value):
        self.writes.append((key.path, name))
        self.values[(key.path, name)] = (value, kind)
    def DeleteValue(self, key, name):
        self.writes.append((key.path, name))
        if (key.path, name) not in self.values: raise FileNotFoundError()
        self.values.pop((key.path, name))


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flickdock-startup-')
        self.base = Path(self.temp.name)
        self.exe = self.base / 'new install' / 'FlickDock.exe'
        self.exe.parent.mkdir()
        self.exe.write_bytes(b'fixture-only')
        self.registry = Registry()
        self.backend = WindowsRunBackend(self.registry)
        self.manager = StartupManager(self.exe, platform='win32', frozen=True,
                                      runtime_allowed=True, backend=self.backend)

    def tearDown(self): self.temp.cleanup()

    def test_missing_registration_repaired_independent_of_initialized_preference(self):
        self.assertFalse(self.manager.status().enabled)
        status = self.manager.reconcile(True)
        self.assertTrue(status.enabled)
        self.assertTrue(status.changed)
        self.assertEqual(self.backend.read(), (startup_command(self.exe, frozen=True), 1))
        self.assertFalse(self.manager.reconcile(True, explicit=True).changed)
        self.assertEqual(self.registry.values[(RUN_KEY, 'Unrelated')], ('do-not-change', 1))
        self.assertTrue(all(name == 'FlickDock' for path, name in self.registry.writes))

    def test_upgrade_updates_only_standalone_own_path(self):
        old = self.base / 'old' / 'FlickDock.exe'
        self.registry.values[(RUN_KEY, 'FlickDock')] = ('"' + str(old) + '"', 1)
        self.assertFalse(self.manager.status().enabled)
        self.assertTrue(self.manager.reconcile(True).enabled)
        self.assertEqual(self.manager.status().command, '"' + str(self.exe) + '"')
        foreign = ('"' + str(self.base / 'foreign.exe') + '"', 1)
        self.registry.values[(RUN_KEY, 'FlickDock')] = foreign
        self.assertFalse(self.manager.reconcile(True, explicit=True).enabled)
        self.assertEqual(self.backend.read(), foreign)
        self.assertFalse(self.manager.reconcile(False, explicit=True).changed)

    def test_windows_disabled_only_explicit_user_enable_clears_own_approval(self):
        self.manager.reconcile(True)
        self.registry.values[(APPROVED_KEY, 'FlickDock')] = (b'\x03' + bytes(11), 3)
        self.registry.values[(APPROVED_KEY, 'Unrelated')] = (b'\x03' + bytes(11), 3)
        self.assertTrue(self.manager.status().blocked)
        self.assertFalse(self.manager.reconcile(True).enabled)
        self.assertTrue(self.manager.reconcile(True, explicit=True).enabled)
        self.assertIn((APPROVED_KEY, 'Unrelated'), self.registry.values)
        self.assertNotIn((APPROVED_KEY, 'FlickDock'), self.registry.values)

    def test_explicit_off_removes_owned_registration_only(self):
        self.manager.reconcile(True)
        status = self.manager.reconcile(False, explicit=True)
        self.assertFalse(status.enabled)
        self.assertFalse(status.registered)
        self.assertIn((RUN_KEY, 'Unrelated'), self.registry.values)

    def test_write_failure_rollback_and_real_status(self):
        previous = ('"' + str(self.base / 'old' / 'FlickDock.exe') + '"', 1)
        data = [previous]
        failed = [False]
        def write(value):
            data[0] = value
            if not failed[0]:
                failed[0] = True
                raise OSError('fixture failure')
        manager = StartupManager(self.exe, platform='win32', runtime_allowed=True,
                                  backend=CallbackBackend(lambda: data[0], write))
        status = manager.reconcile(True, explicit=True)
        self.assertFalse(status.enabled)
        self.assertIn('fixture failure', status.reason)
        self.assertEqual(data[0], previous)

    def test_offscreen_source_never_accesses_real_or_injected_backend(self):
        def forbidden(*args): raise AssertionError('OS access forbidden')
        manager = StartupManager(self.exe, platform='win32', frozen=True, qt_platform='offscreen',
                                  backend=CallbackBackend(forbidden, forbidden))
        self.assertFalse(manager.status().supported)
        self.assertFalse(manager.reconcile(True, explicit=True).supported)
        self.assertFalse(startup_runtime_allowed(platform='darwin', frozen=True, qt_platform='offscreen'))
        self.assertFalse(startup_runtime_allowed(platform='win32', frozen=False, qt_platform='windows'))
        self.assertTrue(startup_runtime_allowed(platform='darwin', frozen=True, qt_platform='cocoa'))

    def test_mac_launchagent_round_trip_in_disposable_home(self):
        exe = self.base / 'Applications' / 'FlickDock.app' / 'Contents' / 'MacOS' / 'FlickDock'
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b'fixture')
        backend = LaunchAgentBackend(self.base / 'test-home')
        manager = StartupManager(exe, platform='darwin', frozen=True, runtime_allowed=True, backend=backend)
        self.assertTrue(manager.reconcile(True, explicit=True).enabled)
        data = plistlib.loads(backend.path.read_bytes())
        self.assertEqual(data['ProgramArguments'], [str(exe)])
        self.assertTrue(data['RunAtLoad'])
        self.assertFalse(data['KeepAlive'])
        self.assertEqual(data['Label'], 'com.flickdock.app')
        data['Disabled'], data['RunAtLoad'] = True, False
        backend.path.write_bytes(plistlib.dumps(data))
        self.assertFalse(manager.reconcile(True).enabled)
        self.assertTrue(manager.reconcile(True, explicit=True).enabled)
        self.assertFalse(manager.reconcile(False).registered)
        self.assertFalse(backend.path.exists())

    def test_mac_foreign_agent_never_overwritten(self):
        backend = LaunchAgentBackend(self.base / 'home')
        backend.path.parent.mkdir(parents=True)
        data = plistlib.dumps(dict(Label='foreign', ProgramArguments=['/bin/echo']))
        backend.path.write_bytes(data)
        manager = StartupManager(self.exe, platform='darwin', runtime_allowed=True, backend=backend)
        self.assertFalse(manager.reconcile(True, explicit=True).enabled)
        self.assertEqual(backend.path.read_bytes(), data)

    def test_mac_system_disabled_override_requires_explicit_own_enable(self):
        exe = self.base / 'FlickDock.app' / 'Contents' / 'MacOS' / 'FlickDock'
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b'fixture')
        disabled, calls = [True], []
        def launchctl(arguments, **kwargs):
            calls.append(arguments)
            if arguments[1] == 'print-disabled':
                return SimpleNamespace(stdout='disabled services = {\n "com.flickdock.app" => ' + ('true' if disabled[0] else 'false') + '\n}')
            disabled[0] = arguments[1] == 'disable'
            return SimpleNamespace(stdout='')
        backend = LaunchAgentBackend(self.base / 'home', runner=launchctl, system_actions=True, uid=501)
        manager = StartupManager(exe, platform='darwin', runtime_allowed=True, backend=backend)
        self.assertFalse(manager.reconcile(True).enabled)
        self.assertTrue(manager.status().blocked)
        self.assertTrue(manager.reconcile(True, explicit=True).enabled)
        mutation = [arguments for arguments in calls if arguments[1] != 'print-disabled']
        self.assertEqual(mutation, [['/bin/launchctl', 'enable', 'gui/501/com.flickdock.app']])
        disabled[0] = True
        backend.path.unlink()
        self.assertTrue(manager.reconcile(True, explicit=True).enabled)


if __name__ == '__main__': unittest.main(verbosity=2)
