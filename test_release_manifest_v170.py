"""Release producer tests; private temporary trees and fake anonymous HTTPS.

No upload, desktop input, installer execution, credentials or user data access.
"""
from __future__ import annotations

from contextlib import redirect_stdout,redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import release_manifest as release


class Response(io.BytesIO):
    def __init__(self,data,status=200,headers=None):
        super().__init__(data);self.status=status
        self.headers={'Content-Type':'application/octet-stream','Content-Length':str(len(data))}
        self.headers.update(headers or {})
    def getcode(self):return self.status


class PublicServer:
    def __init__(self,url,data):
        self.routes={url:(data,200,{})};self.requests=[];self.on_request=None
    def open(self,request,timeout):
        self.requests.append(request)
        if self.on_request:self.on_request()
        data,status,headers=self.routes[request.full_url]
        return Response(data,status,headers)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='flickdock-publisher-')
        self.addCleanup(self.directory.cleanup)
        self.base=Path(self.directory.name);self.root=self.base/'private-source';self.root.mkdir()
        self.version='1.7.0'
        for name in release.SOURCE_FILES+release.ICON_FILES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'# reviewed source\n' if path.suffix=='.py' else b'reviewed release asset\n')
        (self.root/'flickdock.py').write_text('from app_version import APP_VERSION\n',encoding='utf8')
        (self.root/'app_version.py').write_text('APP_VERSION = "1.7.0"\n',encoding='utf8')
        (self.root/'FlickDock.iss').write_text('#define AppVersion "1.7.0"\n',encoding='utf8')
        (self.root/'version_info.txt').write_text(
            "FixedFileInfo(filevers=(1, 7, 0, 0), prodvers=(1, 7, 0, 0))\n"
            "StringStruct('FileVersion', '1.7.0')\nStringStruct('ProductVersion', '1.7.0')\n",encoding='utf8')
        font=self.root/'fonts/HarmonyOS_Sans_SC_Regular.ttf'
        (self.root/'font_manifest.json').write_text(json.dumps({'fonts':[{
            'file':'fonts/HarmonyOS_Sans_SC_Regular.ttf','bytes':font.stat().st_size,
            'sha256':release.sha256_file(font)}]}),encoding='utf8')
        self.installer=self.base/'FlickDock-Setup-1.7.0.exe'
        self.data=b'MZ'+b'an already verified installer fixture\x00'*30
        self.installer.write_bytes(self.data)
        self.verification=self.base/'verification.json';self.write_verification()
        self.url='https://releases.example.org/FlickDock-Setup-1.7.0.exe'
        self.server=PublicServer(self.url,self.data)
        self.output=self.base/'public-index'/'updates'/'latest.json'
        self.artifacts=self.base/'private-artifacts'
        self.version_patch=patch.object(release,'installer_product_version',return_value='1.7.0')
        self.version_patch.start();self.addCleanup(self.version_patch.stop)

    def write_verification(self,**overrides):
        value=dict(version=self.version,checks=[dict(test='isolated regression',exit_code=0)],
                   frozen=dict(ok=True,frozen=True),setup=dict(name=self.installer.name,
                   bytes=self.installer.stat().st_size,sha256=release.sha256_file(self.installer).upper()))
        value.update(overrides);self.verification.write_text(json.dumps(value),encoding='utf8')

    def publish(self,**overrides):
        values=dict(root=self.root,installer=self.installer,version=self.version,notes='Checked release notes',
                    download_url=self.url,verification=self.verification,output=self.output,
                    artifacts=self.artifacts,initial_release=True,opener=self.server)
        values.update(overrides)
        return release.create_release(**values)

    def previous(self,version='1.6.5'):
        return dict(schema_version=1,version=version,published_at='2026-10-23T00:00:00Z',notes='Earlier release',
                    installer=dict(url='https://releases.example.org/old.exe',sha256='a'*64,size=55,
                                   filename='FlickDock-Setup-'+version+'.exe'))

    def blocked(self,**overrides):
        with self.assertRaises(release.ReleaseError):self.publish(**overrides)
        self.assertFalse(self.output.exists())

    def test_complete_private_source_public_manifest_workflow(self):
        # Unselected workspace material must never be swept into source ZIP.
        (self.root/'.env').write_text('PRIVATE_TOKEN=not-for-distribution',encoding='utf8')
        (self.root/'user-settings.json').write_text('{"private":true}',encoding='utf8')
        (self.root/'recording.mp4').write_bytes(b'user media fixture')
        manifest=self.publish()
        self.assertEqual(manifest,json.loads(self.output.read_text(encoding='utf8')))
        self.assertEqual(manifest['installer']['sha256'],hashlib.sha256(self.data).hexdigest())
        self.assertEqual(manifest['installer']['size'],len(self.data))
        self.assertNotIn('source',manifest)
        self.assertEqual(set(manifest),{'schema_version','version','published_at','notes','installer','releases'})
        self.assertEqual(manifest['releases'][0]['notes'],'Checked release notes')
        archive_path=self.artifacts/'FlickDock-1.7.0-source.zip'
        inventory=json.loads((self.artifacts/'FlickDock-1.7.0-source-manifest.json').read_text(encoding='utf8'))
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()),len(inventory['files'])+1)
            for entry in inventory['files']:
                data=archive.read('FlickDock-1.7.0/'+entry['path'])
                self.assertEqual(len(data),entry['size'])
                self.assertEqual(hashlib.sha256(data).hexdigest(),entry['sha256'])
            self.assertFalse(any(name.endswith(('.env','.mp4','user-settings.json')) for name in archive.namelist()))
        self.assertEqual(archive_path.read_bytes(),release.source_archive(self.root,self.version)[0])
        self.assertFalse(list(self.output.parent.glob('*source*')))
        self.assertTrue((self.artifacts/'FlickDock-1.7.0-SHA256SUMS.txt').is_file())
        self.assertTrue((self.artifacts/'FlickDock-1.7.0-release-record.json').is_file())
        request=self.server.requests[0]
        self.assertIsNone(request.get_header('Authorization'));self.assertIsNone(request.get_header('Cookie'))

    def test_oversized_history_never_publishes_an_unreadable_index(self):
        from unittest.mock import patch
        with patch('release_history.release_entries',return_value=[dict(version=self.version,notes='文'*23000)]),patch('release_history.manifest_notes',return_value='文'*23000):
            with self.assertRaises(release.ReleaseError):self.publish(notes='文'*23000)
        self.assertFalse(self.output.exists())

    def test_same_version_identical_bytes_are_idempotent(self):
        first=self.publish();second=self.publish(initial_release=False)
        self.assertEqual(first,second)
        self.assertEqual(len(self.server.requests),2)

    def test_same_version_replacement_is_blocked_and_old_manifest_survives(self):
        self.publish();old=self.output.read_bytes()
        self.installer.write_bytes(self.data+b'changed');self.write_verification()
        self.server.requests.clear()
        with self.assertRaisesRegex(release.ReleaseError,'repackaged'):self.publish()
        self.assertEqual(self.output.read_bytes(),old)
        self.assertFalse(self.server.requests)

    def test_downgrade_and_unknown_baseline_are_blocked_before_network(self):
        self.blocked(previous=self.previous('1.8.0'))
        self.blocked(initial_release=False)
        self.assertFalse(self.server.requests)

    def test_stale_explicit_baseline_cannot_bypass_newer_existing_index(self):
        self.output.parent.mkdir(parents=True)
        newer=self.previous('1.8.0');self.output.write_text(json.dumps(newer),encoding='utf8')
        old=self.output.read_bytes()
        with self.assertRaisesRegex(release.ReleaseError,'downgrade'):
            self.publish(previous=self.previous('1.6.5'))
        self.assertEqual(self.output.read_bytes(),old)
        self.assertFalse(self.server.requests)

    def test_explicit_older_baseline_cannot_bypass_same_version_immutability(self):
        self.publish();old=self.output.read_bytes();self.server.requests.clear()
        self.installer.write_bytes(self.data+b'changed');self.write_verification()
        with self.assertRaisesRegex(release.ReleaseError,'repackaged'):
            self.publish(previous=self.previous('1.6.5'))
        self.assertEqual(self.output.read_bytes(),old)
        self.assertFalse(self.server.requests)

    def test_forward_version_uses_numeric_order(self):
        self.assertGreater(release.version_tuple('1.10.0'),release.version_tuple('1.9.9'))
        for value in ('01.7.0','1.07.0','1.7','v1.7.0','1.7.0-beta','1.7.0.0',None):
            with self.subTest(value=value),self.assertRaises(release.ReleaseError):release.version_tuple(value)

    def test_verification_must_attest_exact_built_installer(self):
        for override in (dict(version='1.6.5'),dict(checks=[]),dict(checks=[{'exit_code':1}]),
                         dict(checks=[{'exit_code':False}]),dict(frozen={'ok':True,'frozen':False}),
                         dict(setup={'name':self.installer.name,'bytes':len(self.data),'sha256':'0'*64})):
            with self.subTest(override=override):
                self.write_verification(**override);self.blocked()
        self.verification.unlink();self.blocked()
        self.assertFalse(self.server.requests)

    def test_three_source_versions_and_built_pe_must_match(self):
        (self.root/'app_version.py').write_text('APP_VERSION="1.6.5"',encoding='utf8')
        self.blocked()
        (self.root/'app_version.py').write_text('APP_VERSION="1.7.0"',encoding='utf8')
        (self.root/'flickdock.py').write_text('from app_version import APP_VERSION\nAPP_VERSION="1.7.0"',encoding='utf8')
        self.blocked()
        (self.root/'flickdock.py').write_text('from app_version import APP_VERSION',encoding='utf8')
        with patch.object(release,'installer_product_version',return_value='1.6.5'):self.blocked()
        (self.root/'FlickDock.iss').write_text('#define AppVersion "1.6.5"',encoding='utf8');self.blocked()
        self.assertFalse(self.server.requests)

    def test_missing_source_and_unlisted_local_runtime_module_are_blocked(self):
        missing=self.root/'update_dialog.py';missing.unlink();self.blocked()
        missing.write_text('# source',encoding='utf8')
        (self.root/'new_runtime.py').write_text('# new implementation',encoding='utf8')
        (self.root/'update_client.py').write_text('import new_runtime',encoding='utf8')
        self.blocked()

    def test_changed_font_and_selected_credentials_are_blocked(self):
        font=self.root/'fonts/HarmonyOS_Sans_SC_Regular.ttf';old=font.read_bytes()
        font.write_bytes(b'changed font');self.blocked();font.write_bytes(old)
        (self.root/'update_client.py').write_text('SECRET="'+'ghp_'+'A'*30+'"',encoding='utf8')
        self.blocked();self.assertFalse(self.server.requests)

    def test_remote_html_truncation_and_wrong_hash_are_blocked(self):
        for data,headers in ((self.data,{'Content-Type':'text/html'}),(self.data[:-1],{}),
                             (self.data[:-1]+b'x',{}),(self.data,{'Content-Length':'not a number'})):
            with self.subTest(headers=headers,size=len(data)):
                self.server.routes[self.url]=(data,200,headers);self.blocked()

    def test_https_redirects_require_approved_hosts(self):
        cdn='https://cdn.example.org/package.exe'
        self.server.routes[self.url]=(b'',302,{'Location':cdn})
        self.server.routes[cdn]=(self.data,200,{})
        self.blocked()
        self.publish(allowed_hosts={'releases.example.org','cdn.example.org'})
        self.assertTrue(self.output.is_file())

    def test_http_redirect_is_blocked_without_requesting_http(self):
        self.server.routes[self.url]=(b'',302,{'Location':'http://releases.example.org/unsafe.exe'})
        self.blocked()
        self.assertEqual(len(self.server.requests),1)

    def test_signed_approved_cdn_redirect_is_transient_and_never_published(self):
        cdn='https://cdn.example.org/package.exe?metadata=public&signature=temporary'
        self.server.routes[self.url]=(b'',302,{'Location':cdn})
        self.server.routes[cdn]=(self.data,200,{})
        manifest=self.publish(allowed_hosts={'releases.example.org','cdn.example.org'})
        self.assertEqual(manifest['installer']['url'],self.url)
        self.assertNotIn('signature',self.output.read_text(encoding='utf8'))
        self.assertEqual(len(self.server.requests),2)
        self.assertRaises(release.ReleaseError,release.validate_url,cdn)

    def test_redirect_cannot_smuggle_access_credentials(self):
        cdn='https://cdn.example.org/package.exe?access_token=private'
        self.server.routes[self.url]=(b'',302,{'Location':cdn})
        self.blocked(allowed_hosts={'releases.example.org','cdn.example.org'})
        self.assertEqual(len(self.server.requests),1)

    def test_gitee_attachment_server_token_redirect_keeps_stable_manifest_url(self):
        cdn=('https://foruda.gitee.com/attach_file/123456/flickdock-setup-1.7.0.exe'
             '?token=anonymous-server-signature&ts=1792980480&attname=flickdock-setup-1.7.0.exe')
        self.server.routes[self.url]=(b'',302,{'Location':cdn})
        self.server.routes[cdn]=(self.data,200,{})
        manifest=self.publish(allowed_hosts={'releases.example.org','foruda.gitee.com'})
        self.assertEqual(manifest['installer']['url'],self.url)
        self.assertNotIn('token=',self.output.read_text(encoding='utf8'))
        self.assertEqual(len(self.server.requests),2)
        for request in self.server.requests:
            self.assertIsNone(request.get_header('Authorization'))
            self.assertIsNone(request.get_header('Cookie'))
        with self.assertRaises(release.ReleaseError):release.validate_url(cdn)
        with self.assertRaises(release.ReleaseError):
            release.validate_previous(dict(manifest,installer=dict(manifest['installer'],url=cdn)))

    def test_gitee_attachment_token_exception_has_exact_redirect_scope(self):
        prefix='https://foruda.gitee.com/attach_file/123456/setup.exe'
        good_query='?token=server-signature&ts=1792980480'
        bad=(
            prefix.replace('https:','http:')+good_query,
            prefix.replace('foruda.gitee.com','cdn.example.org')+good_query,
            prefix.replace('foruda.gitee.com','sub.foruda.gitee.com')+good_query,
            prefix.replace('foruda.gitee.com','foruda.gitee.com.example.org')+good_query,
            prefix.replace('/attach_file/','/attachments/')+good_query,
            'https://foruda.gitee.com/attach_file'+good_query,
            prefix+'?token=server-signature',
            prefix+'?token=server-signature&ts=',
            prefix+'?token=&ts=1792980480',
            prefix+'?token=%20&ts=1792980480',
            prefix+good_query+'&extra=1',
            prefix+good_query+'&access_token=private',
            prefix+good_query+'&signature=extra',
            prefix+good_query+'&token=other',
            prefix+'?TOKEN=server-signature&ts=1792980480',
        )
        hosts={'releases.example.org','foruda.gitee.com','cdn.example.org',
               'sub.foruda.gitee.com','foruda.gitee.com.example.org'}
        for cdn in bad:
            with self.subTest(url=cdn):
                self.server.requests.clear()
                self.server.routes[self.url]=(b'',302,{'Location':cdn})
                with self.assertRaises(release.ReleaseError):
                    release._public_response(self.url,hosts,self.server)
                self.assertEqual(len(self.server.requests),1)
        self.server.requests.clear()
        with self.assertRaises(release.ReleaseError):
            release._public_response(prefix+good_query,hosts,self.server)
        self.assertFalse(self.server.requests)

    def test_gitee_attachment_token_redirect_still_needs_host_approval(self):
        cdn=('https://foruda.gitee.com/attach_file/123456/setup.exe'
             '?token=server-signature&ts=1792980480')
        self.server.routes[self.url]=(b'',302,{'Location':cdn})
        with self.assertRaises(release.ReleaseError):
            release._public_response(self.url,{'releases.example.org'},self.server)
        self.assertEqual(len(self.server.requests),1)

    def test_credentials_private_addresses_and_expiring_links_are_rejected(self):
        bad=('http://releases.example.org/file.exe','https://localhost/file.exe','https://127.0.0.1/file.exe',
             'https://192.168.1.2/file.exe','https://[::1]/file.exe',
             'https://' + 'user:password' + '@releases.example.org/file.exe',
             self.url+'?access_token=temporary',self.url+'?signature=temporary',
             self.url+'#fragment',self.url.replace('.org/','.org:8443/'))
        for url in bad:
            with self.subTest(url=url),self.assertRaises(release.ReleaseError):release.validate_url(url)

    def test_source_and_installer_mutation_during_download_are_blocked(self):
        def change_source():
            self.server.on_request=None
            (self.root/'ui_theme.py').write_text('# changed during download',encoding='utf8')
        self.server.on_request=change_source;self.blocked()
        def change_installer():
            self.server.on_request=None;self.installer.write_bytes(self.data+b'late mutation')
        self.server.on_request=change_installer;self.blocked()

    def test_public_index_cannot_contain_private_source_artifacts(self):
        self.blocked(artifacts=self.output.parent/'source')

    def test_previous_manifest_schema_and_utc_are_checked(self):
        for override in (dict(schema_version=True),dict(published_at='2026-10-23T01:00:00+08:00'),
                         dict(published_at='2026-10-23'),dict(notes={}),dict(installer={'sha256':'0'*64,'size':False})):
            value=self.previous();value.update(override)
            with self.subTest(override=override),self.assertRaises(release.ReleaseError):release.validate_previous(value)

    def test_real_cli_writes_only_after_public_verification(self):
        notes=self.base/'notes.txt';notes.write_text('Checked release notes',encoding='utf8')
        argv=['--root',str(self.root),'--installer',str(self.installer),'--version',self.version,
              '--notes-file',str(notes),'--verification',str(self.verification),'--download-url',self.url,
              '--artifacts',str(self.artifacts),'--output',str(self.output),'--initial-release']
        stdout=io.StringIO()
        with patch.object(release,'build_opener',return_value=self.server),redirect_stdout(stdout):
            self.assertEqual(release.main(argv),0)
        self.assertTrue(json.loads(stdout.getvalue())['public_installer_verified'])
        self.assertTrue(self.output.is_file())

    def test_cli_rejection_preserves_previous_latest(self):
        self.publish();old=self.output.read_bytes()
        notes=self.base/'notes.txt';notes.write_text('Changed notes',encoding='utf8')
        stderr=io.StringIO()
        argv=['--root',str(self.root),'--installer',str(self.installer),'--version',self.version,
              '--notes-file',str(notes),'--verification',str(self.verification),'--download-url',self.url,
              '--artifacts',str(self.artifacts),'--output',str(self.output)]
        with redirect_stderr(stderr):self.assertEqual(release.main(argv),1)
        self.assertIn('RELEASE_BLOCKED',stderr.getvalue());self.assertEqual(self.output.read_bytes(),old)


class NativeVersionTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows PE version resource reader')
    def test_real_existing_installer_resource_without_execution(self):
        fixture=Path(__file__).resolve().parent.parent/'outputs/FlickDock-Setup-1.6.5.exe'
        if not fixture.is_file():self.skipTest('Optional existing local installer fixture is unavailable')
        self.assertEqual(release.installer_product_version(fixture),'1.6.5')


if __name__=='__main__':unittest.main(verbosity=2)
