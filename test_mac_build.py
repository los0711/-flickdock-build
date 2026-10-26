"""Build and helper guards only. No packaging, GUI launch or installation."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_macos
import mac_entry
import mac_updates


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


if __name__ == '__main__': unittest.main(verbosity=2)
