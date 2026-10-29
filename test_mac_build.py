"""Build and helper guards only. No packaging, GUI launch or installation."""
import ast
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import build_macos
import mac_entry
import mac_updates
import flickdock
import platform_native


class MacBuildTests(unittest.TestCase):
    def test_non_mac_build_rejects_before_commands_or_artifacts(self):
        with patch.object(build_macos.sys, 'platform', 'win32'), patch.object(build_macos, 'run') as run:
            with self.assertRaises(SystemExit) as raised:
                build_macos.main([])
            self.assertEqual(raised.exception.code, 2)
            run.assert_not_called()

    def test_entry_dispatches_helper_without_application_main(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'update-receipt.json'
            with patch.object(mac_updates, 'run_update_helper') as helper:
                self.assertEqual(mac_entry.main(['--apply-mac-update', str(receipt)]), 0)
                helper.assert_called_once_with(receipt)
            self.assertEqual(mac_entry.main(['--apply-mac-update']), 2)

    def test_mac_spec_has_own_entry_and_no_windows_media_hook(self):
        path = Path(build_macos.__file__).with_name('FlickDock-mac.spec')
        source = path.read_text(encoding='utf-8')
        ast.parse(source)
        self.assertIn("root / 'mac_entry.py'", source)
        self.assertIn("root / 'runtime_macos.py'", source)
        self.assertNotIn("root / 'runtime_media.py'", source)
        self.assertIn("LSMinimumSystemVersion='14.4'", source)
        self.assertIn('LSUIElement=True', source)

    def test_installer_is_a_native_pkg_for_applications(self):
        source = Path(build_macos.__file__).read_text(encoding='utf-8')
        self.assertIn("'/usr/bin/pkgbuild'", source)
        self.assertIn("'--install-location', '/Applications'", source)
        self.assertIn("'/usr/sbin/pkgutil', '--expand-full'", source)

    def test_pkg_replaces_canonical_bundle_and_checks_running_instances(self):
        import plistlib
        recorded=[]
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);app=folder/'FlickDock.app';app.mkdir();(app/'payload').write_bytes(b'fixture')
            def inspect(command,**options):
                recorded.append([str(value) for value in command])
                if command[0]=='/usr/bin/pkgbuild':
                    component=Path(command[command.index('--component-plist')+1])
                    values=plistlib.loads(component.read_bytes())[0]
                    self.assertFalse(values['BundleIsRelocatable']);self.assertTrue(values['BundleHasStrictIdentifier'])
                    self.assertEqual(values['BundleOverwriteAction'],'upgrade')
                    script=Path(command[command.index('--scripts')+1])/'preinstall'
                    self.assertIn('pgrep -x FlickDock',script.read_text(encoding='utf8'))
                    self.assertNotIn('killall',script.read_text(encoding='utf8'))
            with patch.object(build_macos,'run',side_effect=inspect):build_macos._create_pkg(app,folder/'new.pkg','1.8.17')
        self.assertTrue(any(command[:2]==['/bin/sh','-n'] for command in recorded))

    def test_disk_image_launch_uses_installed_copy_and_only_ejects_an_image_mount(self):
        import plistlib
        for image in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=root/'mounted/FlickDock.app';target=root/'Applications/FlickDock.app'
                for app in (source,target):
                    (app/'Contents').mkdir(parents=True)
                    (app/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier='com.flickdock.app',CFBundleShortVersionString='1.8.17')))
                commands=[];launches=[]
                def run(command,**options):
                    commands.append(command)
                    return SimpleNamespace(returncode=0,stdout=plistlib.dumps(dict(images=[{'image-path':'/fixture.dmg','system-entities':[{'mount-point':str(source.parent)}]}] if image else [])))
                self.assertTrue(mac_entry._dmg_launch(source,target,runner=run,launcher=lambda *args,**options:launches.append(args)))
                self.assertEqual(commands[0],['/usr/bin/hdiutil','info','-plist'])
                self.assertEqual(len(launches),1)
                self.assertEqual(launches[0][0][-2],str(target))
                self.assertEqual(launches[0][0][-1],str(source.parent) if image else '')

    def test_disk_image_does_not_replace_a_running_older_installation(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'mounted/FlickDock.app';target=root/'Applications/FlickDock.app'
            (source/'Contents').mkdir(parents=True)
            (source/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier='com.flickdock.app',CFBundleShortVersionString='1.8.17')))
            calls=[]
            def run(command,**options):
                calls.append(command);return SimpleNamespace(returncode=0,stdout='999999' if command[0].endswith('pgrep') else b'')
            self.assertTrue(mac_entry._dmg_launch(source,target,runner=run))
            self.assertEqual(len(calls),2);self.assertIn('请先退出',calls[1][-1]);self.assertFalse(target.exists())

    def test_cocoa_smoke_is_opt_in_and_uses_disposable_profile(self):
        source = Path(build_macos.__file__).read_text(encoding='utf-8')
        self.assertIn("TemporaryDirectory(prefix='flickdock-cocoa-smoke-')", source)
        self.assertIn("QT_QPA_PLATFORM='cocoa'", source)
        self.assertIn("FLICKDOCK_CI_COCOA_SMOKE='1'", source)
        self.assertIn("'--verify-cocoa-startup'", source)

    def test_frozen_native_order_probe_is_not_an_ordinary_startup_path(self):
        with patch.object(mac_entry.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(RuntimeError, 'frozen Cocoa CI mode'):
                mac_entry.verify_cocoa_window_order(None, None)

    def test_frozen_native_order_probe_exercises_conflict_and_restores_windows(self):
        class Window:
            def __init__(self):
                self.behavior, self.native_level, self.orders = 4, 0, 0

            def level(self): return self.native_level
            def collectionBehavior(self): return self.behavior
            def setLevel_(self, value): self.native_level = value
            def setCollectionBehavior_(self, value):
                if value & 1 and value & 2:
                    raise RuntimeError('Spaces flags conflict')
                self.behavior = value
            def orderFrontRegardless(self): self.orders += 1

        windows = {1: Window(), 2: Window()}
        edge, shelf = (SimpleNamespace(winId=lambda: 1), SimpleNamespace(winId=lambda: 2))
        appkit = SimpleNamespace(NSFloatingWindowLevel=3,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1,
            NSWindowCollectionBehaviorMoveToActiveSpace=2,
            NSWindowCollectionBehaviorIgnoresCycle=4,
            NSWindowCollectionBehaviorFullScreenAuxiliary=1 << 8)
        objc = SimpleNamespace(objc_object=lambda c_void_p:
            SimpleNamespace(window=lambda: windows[c_void_p.value]))
        with patch.object(mac_entry.sys, 'platform', 'darwin'), \
             patch.object(mac_entry.sys, 'frozen', True, create=True), \
             patch.dict(os.environ, QT_QPA_PLATFORM='cocoa', FLICKDOCK_CI_COCOA_SMOKE='1'), \
             patch.dict(sys.modules, {'AppKit': appkit, 'objc': objc}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            report = mac_entry.verify_cocoa_window_order(edge, shelf)
        self.assertTrue(report['ok'])
        self.assertEqual({case['window'] for case in report['cases']}, {'edge', 'shelf'})
        self.assertEqual([window.orders for window in windows.values()], [2, 2])
        self.assertEqual([(window.level(), window.collectionBehavior()) for window in windows.values()], [(0, 4), (0, 4)])
        self.assertIsNone(shelf._flickdock_native_order)

    def test_python_startup_exception_gets_private_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mac_entry.Path, 'home', return_value=Path(directory)), \
                 patch('platform_native.configure_media_backend', side_effect=RuntimeError('smoke startup failure')):
                self.assertEqual(mac_entry.main([]), 1)
            log = Path(directory) / 'Library/Logs/FlickDock/startup-last-error.log'
            self.assertIn('smoke startup failure', log.read_text(encoding='utf-8'))

    def test_previous_update_failure_records_error_without_automatically_opening_settings(self):
        manager = flickdock.ShelfManager.__new__(flickdock.ShelfManager)
        events = []
        manager._quitting = False
        manager.show_settings = lambda: events.append('settings')
        manager.updates = SimpleNamespace(open_error=lambda error: events.append(error))
        with patch.object(mac_updates, 'consume_update_failure', return_value='管理员授权已取消'):
            manager._show_previous_mac_update_failure()
        self.assertEqual(events, ['管理员授权已取消'])
        self.assertEqual(manager._previous_update_error,'管理员授权已取消')


if __name__ == '__main__': unittest.main(verbosity=2)
