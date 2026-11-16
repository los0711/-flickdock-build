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
    def test_existing_gitee_asset_is_verified_without_reupload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'FlickDock-Setup-1.8.38.exe'
            path.write_bytes(b'MZ fixture')
            meta = archives.file_metadata(path)
            client = MagicMock()
            client.get.side_effect = [
                response(200, {'private':False,'owner':{'login':'rooogerluo'},'default_branch':'master'}),
                response(200, {'id':42,'tag_name':'v1.8.38'}),
                response(200, [{'name':path.name,'size':meta['bytes']}]),
            ]
            with patch.object(archives,'verify_archive') as verify:
                url = archives._gitee_asset(client,path,meta,'windows','1.8.38','优化体验')
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
                response(200, {'private':False,'owner':{'login':'rooogerluo'}}),
                response(200, {'id':42,'tag_name':'v1.8.38'}),
                response(200, [{'name':path.name},{'name':path.name}]),
            ]
            with self.assertRaisesRegex(ValueError,'duplicate'):
                archives._gitee_asset(client,path,meta,'windows','1.8.38','优化体验')
            client.post.assert_not_called()

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
