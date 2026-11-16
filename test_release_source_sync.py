"""Ensure private source updates fail safely on concurrent changes."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import release_source_sync as sync


def inventory(data=b'original'):
    return {'schema_version':1,'product':'FlickDock','files':[
        {'path':'folder/source.py','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}


class SourceSyncTests(unittest.TestCase):
    def test_verified_private_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'folder/source.py'
            path.parent.mkdir()
            path.write_bytes(b'original')
            self.assertEqual(sync.verify_baseline(tmp,inventory()),{'folder/source.py'})
            path.write_bytes(b'newer user work')
            with self.assertRaisesRegex(ValueError,'differs'):
                sync.verify_baseline(tmp,inventory())
            self.assertEqual(path.read_bytes(),b'newer user work')

    def test_rejects_inventory_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('../elsewhere','/absolute','folder\\elsewhere'):
                value = inventory()
                value['files'][0]['path'] = name
                with self.subTest(name=name), self.assertRaises(ValueError):
                    sync.verify_baseline(tmp,value)

    def test_duplicate_inventory_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'folder/source.py'
            path.parent.mkdir()
            path.write_bytes(b'original')
            value = inventory()
            value['files'].append(dict(value['files'][0]))
            with self.assertRaisesRegex(ValueError,'duplicates'):
                sync.verify_baseline(tmp,value)

    def test_git_receives_only_the_dedicated_scoped_header(self):
        with patch.dict(os.environ,{'GITHUB_TOKEN':'do-not-forward','GIT_TRACE':'1',
                                    'FLICKDOCK_RELEASE_SIGNING_KEY':'do-not-forward'},clear=True):
            env = sync._git_environment('test-gitee-token')
        self.assertNotIn('GITHUB_TOKEN',env)
        self.assertNotIn('FLICKDOCK_RELEASE_SIGNING_KEY',env)
        self.assertNotIn('GIT_TRACE',env)
        self.assertEqual(env['GIT_CONFIG_KEY_0'],'http.https://gitee.com/.extraheader')
        self.assertEqual(env['GIT_TERMINAL_PROMPT'],'0')

    def test_github_conflict_stops_before_writing(self):
        with patch.object(sync,'main_head',return_value='b'*40), patch.object(sync,'_api') as api:
            with self.assertRaisesRegex(RuntimeError,'changed'):
                sync._commit_github('fixture','a'*40,{'file.txt':b'test'})
        api.assert_not_called()

    def test_local_git_release_updates_only_verified_source_and_tags_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            remote = parent/'private.git'
            initial = parent/'initial'
            subprocess.run(['git','init','--bare','--quiet',str(remote)],check=True,capture_output=True)
            subprocess.run(['git','init','--quiet','-b','master',str(initial)],check=True,capture_output=True)
            (initial/'folder').mkdir()
            (initial/'folder/source.py').write_bytes(b'original')
            (initial/'unrelated.txt').write_bytes(b'keep')
            subprocess.run(['git','add','.'],cwd=initial,check=True,capture_output=True)
            subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.invalid',
                            'commit','--quiet','-m','baseline'],cwd=initial,check=True,capture_output=True)
            subprocess.run(['git','remote','add','origin',str(remote)],cwd=initial,check=True,capture_output=True)
            subprocess.run(['git','push','--quiet','origin','master'],cwd=initial,check=True,capture_output=True)
            root = parent/'release'
            public = root/'website/public'
            (public/'updates').mkdir(parents=True)
            (root/'SOURCE_MANIFEST.json').write_text(__import__('json').dumps(inventory()),encoding='utf-8')
            (root/'website/download-catalog.json').write_text('{}',encoding='utf-8')
            (public/'index.html').write_text('site',encoding='utf-8')
            for name in ('latest.json','macos-arm64.json','macos-arm64-auto.json','macos-arm64-auto-v2.json'):
                (public/'updates'/name).write_text('{}',encoding='utf-8')
            env = {'GITHUB_TOKEN':'fixture','GITEE_RELEASE_TOKEN':'fixture','RUNNER_TEMP':tmp}
            with patch.dict(os.environ,env), patch.object(sync,'ROOT',root), \
                 patch.object(sync,'GITEE_URL',str(remote)), patch.object(sync,'context',return_value='a'*40), \
                 patch.object(sync,'main_head',return_value='a'*40), \
                 patch.object(sync,'collect_source_files',return_value=[('folder/source.py',b'released')]), \
                 patch.object(sync,'source_archive',return_value=(b'zip',b'new inventory')), \
                 patch.object(sync,'_commit_github',return_value='b'*40):
                sync.synchronize()
            show = lambda ref:subprocess.run(['git','--git-dir',str(remote),'show',ref],check=True,
                                              capture_output=True).stdout
            self.assertEqual(show('master:folder/source.py'),b'released')
            self.assertEqual(show('master:unrelated.txt'),b'keep')
            self.assertEqual(show('v'+sync.APP_VERSION+':folder/source.py'),b'released')


if __name__ == '__main__':
    unittest.main()
