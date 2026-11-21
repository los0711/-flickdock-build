import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import release_archives as archives


def response(status, value):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = value
    return result


class ArchiveTests(unittest.TestCase):
    def test_existing_github_asset_is_verified_without_reupload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'FlickDock-Setup-1.8.38.exe'
            path.write_bytes(b'MZ fixture')
            meta = archives.file_metadata(path)
            client = MagicMock()
            client.get.side_effect = [
                response(200, {'tag_name':'v1.8.38','draft':False,
                               'assets':[{'name':path.name,'size':meta['bytes']}]}),
                response(200, {'object':{'sha':'a'*40}}),
            ]
            with patch.object(archives,'verify_archive') as verify:
                url = archives._github_asset(client,path,meta,'1.8.38','a'*40,'优化体验')
            client.post.assert_not_called()
            self.assertEqual(verify.call_args.args[0]['sha256'],meta['sha256'])
            self.assertTrue(url.endswith('/v1.8.38/' + path.name))

    def test_duplicate_existing_assets_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'FlickDock-Setup-1.8.38.exe'
            path.write_bytes(b'MZ fixture')
            meta = archives.file_metadata(path)
            client = MagicMock()
            client.get.side_effect = [
                response(200, {'tag_name':'v1.8.38','draft':False,
                               'assets':[{'name':path.name},{'name':path.name}]}),
                response(200, {'object':{'sha':'a'*40}}),
            ]
            with self.assertRaisesRegex(ValueError,'duplicate'):
                archives._github_asset(client,path,meta,'1.8.38','a'*40,'优化体验')
            client.post.assert_not_called()

    def test_new_small_windows_and_macos_assets_use_github_and_keep_immutable_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/name for name in ('FlickDock-Setup-1.8.39.exe',
                                                   'FlickDock-1.8.39-macOS-arm64.pkg')]
            for path in paths:
                path.write_bytes(b'public fixture')
            with patch.object(archives,'_session') as session, \
                 patch.object(archives,'_github_asset') as archive:
                archive.side_effect = lambda _, path, *args: (
                    'https://github.com/los0711/-flickdock-build/releases/download/v1.8.39/' + path.name)
                rows = archives.publish_archives(paths,'1.8.39','b'*40,'优化体验',github_token='fixture-token')
                session.assert_called_once_with('fixture-token')
                self.assertEqual(archive.call_count,2)
            for path,row in zip(paths,rows,strict=True):
                metadata = archives.file_metadata(path)
                self.assertEqual(row['sha256'],metadata['sha256'])
                self.assertEqual(row['originKey'],'v1.8.39/sha256-'+metadata['sha256']+'/'+path.name)
                self.assertTrue(row['url'].startswith('https://github.com/'))

    def test_existing_github_tag_must_match_source(self):
        client = MagicMock()
        client.get.side_effect = [
            response(200, {'tag_name':'v1.8.38','draft':False,'assets':[]}),
            response(200, {'object':{'sha':'other-source'}}),
        ]
        with self.assertRaisesRegex(ValueError,'different source'):
            archives._github_asset(client,Path('ignored'),{'name':'ignored','bytes':1},'1.8.38','expected','优化体验')
        client.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
