"""Build and helper guards only. No packaging, GUI launch or installation."""
import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import build_macos
import mac_entry
import mac_updates
import flickdock


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
        self.assertIn('LSUIElement=False', source)

    def test_installer_is_a_native_pkg_for_applications(self):
        source = Path(build_macos.__file__).read_text(encoding='utf-8')
        self.assertIn("'/usr/bin/pkgbuild'", source)
        self.assertIn("'--install-location', '/Applications'", source)
        self.assertIn("'/usr/sbin/pkgutil', '--expand'", source)

    def test_cocoa_smoke_is_opt_in_and_uses_disposable_profile(self):
        source = Path(build_macos.__file__).read_text(encoding='utf-8')
        self.assertIn("TemporaryDirectory(prefix='flickdock-cocoa-smoke-')", source)
        self.assertIn("QT_QPA_PLATFORM='cocoa'", source)
        self.assertIn("FLICKDOCK_CI_COCOA_SMOKE='1'", source)
        self.assertIn("'--verify-cocoa-startup'", source)

    def test_python_startup_exception_gets_private_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mac_entry.Path, 'home', return_value=Path(directory)), \
                 patch('platform_native.configure_media_backend', side_effect=RuntimeError('smoke startup failure')):
                self.assertEqual(mac_entry.main([]), 1)
            log = Path(directory) / 'Library/Logs/FlickDock/startup-last-error.log'
            self.assertIn('smoke startup failure', log.read_text(encoding='utf-8'))

    def test_previous_update_failure_opens_settings_with_error_once(self):
        manager = flickdock.ShelfManager.__new__(flickdock.ShelfManager)
        events = []
        manager._quitting = False
        manager.show_settings = lambda: events.append('settings')
        manager.updates = SimpleNamespace(open_error=lambda error: events.append(error))
        with patch.object(mac_updates, 'consume_update_failure', return_value='管理员授权已取消'):
            manager._show_previous_mac_update_failure()
        self.assertEqual(events, ['settings', '管理员授权已取消'])


if __name__ == '__main__': unittest.main(verbosity=2)
