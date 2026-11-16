"""Tests for the release gate; no live account or signing key is required."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import release_pipeline as pipeline


class ReleasePipelineTests(unittest.TestCase):
    def test_only_manual_main_runs_can_publish(self):
        env = {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':pipeline.REPO,
               'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
               'GITHUB_SHA':'a'*40}
        with patch.dict(os.environ,env,clear=True):
            self.assertEqual(pipeline.context(),'a'*40)
            for key,value in (('GITHUB_REF','refs/heads/other'),('GITHUB_EVENT_NAME','push'),
                              ('GITHUB_REPOSITORY','other/repo'),('GITHUB_SHA','bad')):
                with self.subTest(key=key), patch.dict(os.environ,{key:value}):
                    with self.assertRaises((RuntimeError,ValueError)):
                        pipeline.context()

    def test_current_version_cannot_be_republished(self):
        env = {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':pipeline.REPO,
               'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
               'GITHUB_SHA':'a'*40,'GITHUB_TOKEN':'fixture'}
        old = {key:{'version':pipeline.APP_VERSION} for key in pipeline.INDEX_NAMES}
        with patch.dict(os.environ,env,clear=True), patch.object(pipeline,'main_head',return_value='a'*40), \
             patch.object(pipeline,'baselines',return_value=(old,{})):
            with self.assertRaisesRegex(ValueError,'newer'):
                pipeline.preflight(publish=True)

    def test_changed_main_fails_before_reading_live_indices(self):
        env = {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':pipeline.REPO,
               'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
               'GITHUB_SHA':'a'*40,'GITHUB_TOKEN':'fixture'}
        with patch.dict(os.environ,env,clear=True), patch.object(pipeline,'main_head',return_value='b'*40), \
             patch.object(pipeline,'baselines') as baselines:
            with self.assertRaisesRegex(RuntimeError,'changed'):
                pipeline.preflight(publish=True)
            baselines.assert_not_called()

    def test_baseline_is_bounded_and_does_not_follow_redirects(self):
        response = MagicMock(status_code=200)
        response.iter_content.return_value = iter([b'x' * (1024*1024), b'x'])
        with patch.object(pipeline.requests,'get',return_value=response) as get:
            with self.assertRaisesRegex(ValueError,'too large'):
                pipeline._public_document(pipeline.BASE + '/updates/latest.json')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        response.close.assert_called_once()

    def test_duplicate_manifest_fields_are_rejected(self):
        response = MagicMock(status_code=200)
        response.iter_content.return_value = iter([b'{"version":"1.0.0","version":"1.0.1"}'])
        with patch.object(pipeline.requests,'get',return_value=response):
            with self.assertRaises(ValueError):
                pipeline._public_document(pipeline.BASE + '/updates/latest.json')
        response.close.assert_called_once()

    def test_arbitrary_baseline_url_is_rejected_before_network(self):
        with patch.object(pipeline.requests,'get') as get:
            with self.assertRaises(ValueError):
                pipeline._public_document('https://elsewhere.invalid/updates/latest.json')
        get.assert_not_called()

    def test_private_source_access_can_be_verified_without_cloud_credentials(self):
        user = MagicMock(status_code=200)
        user.json.return_value = {'login':'rooogerluo'}
        source = MagicMock(status_code=200)
        source.json.return_value = {'private':True}
        with patch.dict(os.environ,{'GITEE_RELEASE_TOKEN':'fixture'},clear=True), \
             patch.object(pipeline,'context'), patch.object(pipeline,'publisher_signer'), \
             patch.object(pipeline.requests,'get',side_effect=[user,source]) as get:
            pipeline.credentials(gitee_only=True)
        self.assertEqual(get.call_count,2)

    def test_staging_keeps_current_indices_until_signing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root/'website'/'public'
            public.mkdir(parents=True)
            (root/'website'/'download-catalog.json').write_text('{}',encoding='utf-8')
            folder = root/'state'
            folder.mkdir()
            previous = {name:{'version':'1.8.36'} for name in pipeline.INDEX_NAMES}
            raw = {name:('old-' + name).encode() for name in previous}
            files = [root/name for name in ('installer.exe','archive.zip','disk.dmg','installer.pkg')]
            assets = [{'name':path.name} for path in files]
            env = {'FLICKDOCK_RELEASE_NOTES':'优化体验'}
            with patch.dict(os.environ,env), patch.object(pipeline,'ROOT',root), \
                 patch.object(pipeline,'work',return_value=folder), \
                 patch.object(pipeline,'context',return_value='a'*40), \
                 patch.object(pipeline,'preflight',return_value=(previous,raw)), \
                 patch.object(pipeline,'_verified_builds',return_value=(files[0],root/'win.json',files[1:],root/'mac.json',root/'cloud.json')), \
                 patch.object(pipeline,'publish_archives',return_value=assets), \
                 patch.object(pipeline,'_upload_origin') as upload, \
                 patch.object(pipeline,'read_catalog',return_value={}), \
                 patch.object(pipeline,'register',return_value={}), \
                 patch.object(pipeline,'write_catalog'), \
                 patch.object(pipeline,'_next_html',return_value='<html>next</html>'):
                pipeline.stage()
            self.assertEqual(upload.call_count,4)
            for name,data in raw.items():
                self.assertEqual((public/'updates'/name).read_bytes(),data)
            self.assertEqual((folder/'next-index.html').read_text(encoding='utf-8'),'<html>next</html>')


if __name__ == '__main__':
    unittest.main()
