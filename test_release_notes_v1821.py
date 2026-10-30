"""Skipped releases are grouped numerically and shared by both update views."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import unittest
from PySide6.QtWidgets import QApplication
from release_history import update_notes,release_entries,manifest_notes
from update_client import validate_manifest
from update_dialog import UpdateDialog

APP=QApplication.instance() or QApplication([])
RELEASES=[dict(version='1.8.9',notes='already installed'),dict(version='1.8.10',notes='intermediate fixes'),
          dict(version='1.8.11',notes='latest fixes')]


class ReleaseNotesTests(unittest.TestCase):
    def manifest(self):
        return dict(schema_version=1,version='1.8.11',published_at='2026-10-30T00:00:00Z',notes='latest fixes',
            releases=RELEASES,installer=dict(url='https://gitee.com/rooogerluo/flick-dock-releases/installer.exe',
                                           size=123,sha256='a'*64))

    def test_skipped_versions_and_both_platforms(self):
        manifest=validate_manifest(self.manifest(),platform='win32')
        result=update_notes('1.8.9',manifest['version'],manifest['notes'],manifest['releases'])
        self.assertIn('v1.8.10\nintermediate fixes',result)
        self.assertIn('v1.8.11\nlatest fixes',result)
        self.assertNotIn('already installed',result)
        self.assertLess(result.index('1.8.11'),result.index('1.8.10'))
        dialog=UpdateDialog('1.8.9');dialog.set_state('available',manifest=manifest)
        self.assertEqual(dialog.release_notes.toPlainText(),result)
        mac=self.manifest();mac.update(platform='darwin',architecture='arm64')
        mac['installer']=dict(mac['installer'],url=mac['installer']['url'].replace('.exe','.zip'))
        self.assertEqual(validate_manifest(mac,platform='darwin',machine='arm64')['releases'],RELEASES)
        dialog.deleteLater();APP.processEvents()

    def test_one_release_unknown_legacy_and_invalid_history(self):
        self.assertEqual(update_notes('1.8.10','1.8.11','latest fixes',RELEASES),'latest fixes')
        self.assertEqual(update_notes('1.0.0','1.0.1','legacy notes'),'legacy notes')
        for rows in ([RELEASES[0],RELEASES[0]],[dict(version='9.9.9',notes='future')],
                     [dict(version='1.8.10',notes=123)]):
            value=self.manifest();value['releases']=rows
            with self.assertRaises(ValueError):validate_manifest(value,platform='win32')

    def test_published_archive_and_legacy_fallback(self):
        releases=release_entries('1.8.20','latest change')
        self.assertTrue({'1.7.0','1.8.0','1.8.2',*(f'1.8.{n}' for n in range(3,21))}<=
                        {row['version'] for row in releases})
        fallback=manifest_notes('1.8.20','latest change')
        self.assertIn('v1.8.10',fallback)
        result=update_notes('1.8.17','1.8.20',fallback,releases)
        for version in ('1.8.18','1.8.19','1.8.20'):self.assertEqual(result.count('v'+version+'\n'),1)
        self.assertNotIn('v1.8.17\n',result)
        self.assertNotIn('v1.8.10\n',result)
        self.assertEqual(update_notes('1.8.19','1.8.20',fallback,releases),'latest change')
        value=self.manifest();value.update(version='1.8.20',notes=fallback,releases=releases)
        validate_manifest(value,platform='win32')
        import json
        self.assertLess(len(json.dumps(value,ensure_ascii=False).encode()),128*1024)

    def test_next_release_is_included_without_manual_history_entry(self):
        releases=release_entries('1.8.99','next release')
        self.assertEqual(releases[0],dict(version='1.8.99',notes='next release'))
        self.assertIn('v1.8.20\n',manifest_notes('1.8.99','next release'))
        from app_version import APP_VERSION
        self.assertEqual(update_notes(APP_VERSION,'1.8.99',manifest_notes('1.8.99','next release'),releases),'next release')

    def test_settings_consumer_uses_installed_version_filter(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from flickdock import ShelfManager
        manifest=self.manifest()
        target=Mock()
        manager=SimpleNamespace(settings_dialog=target,updates=SimpleNamespace(manifest=manifest,app_version='1.8.10'))
        ShelfManager._render_settings_update(manager,'available',{})
        target.set_update_details.assert_called_once_with('1.8.11','latest fixes')


if __name__=='__main__':unittest.main()
