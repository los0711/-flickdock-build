"""Portable ZIP/API fixtures only. No requests, app execution or native input."""
import copy,hashlib,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import reuse_frozen as qa

ROOT=Path(__file__).resolve().parents[2]
LABELS=qa.required_normal_labels(ROOT)
EXE=b'synthetic immutable executable, never executed'
SHA=hashlib.sha256(EXE).hexdigest()


def fixtures():
    report=dict(ok=True,normal_entry=True,verify_switches=False,cocoa_smoke=False,
        production_entry_imported=False,frozen_executable_patched=False,installer_executed=False,
        exe_sha256=SHA,checks=['synthetic core '+str(i) for i in range(15)],
        screen_segments=[dict(label=label,distinct_pixels=1) for label in sorted(LABELS)])
    source=dict(before=True,after=True,exe_sha256=SHA,source_manifest_sha256='b'*64,reviewed_files=524,
        compiled={'normal-flickdock-entry':True,**{'synthetic-module-'+str(i):True for i in range(98)}})
    return report,source


class PassedNormalTests(unittest.TestCase):
    def validate(self,report,source):
        return qa.validate_passed_normal(json.dumps(report).encode(),json.dumps(source).encode(),SHA,LABELS)
    def test_full_contract_has_exact_58_flow_and_77_total_labels(self):
        self.assertEqual(len(LABELS),77)
        self.assertEqual(len([x for x in LABELS if x.startswith('flow-')]),58)
        report,source=fixtures();proof=self.validate(report,source)
        self.assertEqual(proof['flow_steps'],58);self.assertEqual(proof['screen_steps'],77)
    def test_failed_cleanup_and_observation_reports_cannot_qualify(self):
        for mutation in ('failed','cleanup','observer','writer','installer-marker','verified','patched'):
            report,source=fixtures()
            if mutation=='failed':report['ok']=False
            elif mutation=='cleanup':report['profile_cleanup_error']='PermissionError'
            elif mutation=='observer':report['observation_only']=True
            elif mutation=='writer':report['writer_observation']={}
            elif mutation=='installer-marker':report['installer_acceptance']=False
            elif mutation=='verified':report['verify_switches']=True
            else:report['frozen_executable_patched']=True
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.validate(report,source)
    def test_missing_wrong_duplicate_or_nonpixel_steps_are_rejected(self):
        for mutation in ('missing','arbitrary','duplicate','no-pixel','no-core'):
            report,source=fixtures()
            if mutation=='missing':report['screen_segments'].pop()
            elif mutation=='arbitrary':report['screen_segments'][0]['label']='flow-00-made-up'
            elif mutation=='duplicate':report['screen_segments'][0]=report['screen_segments'][1]
            elif mutation=='no-pixel':report['screen_segments'][0]['distinct_pixels']=0
            else:report['checks']=report['checks'][:14]
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.validate(report,source)
    def test_wrong_sha_or_incomplete_source_proof_is_rejected(self):
        for mutation in ('report-sha','source-sha','before','after','false-compiled','partial-compiled'):
            report,source=fixtures()
            if mutation=='report-sha':report['exe_sha256']='c'*64
            elif mutation=='source-sha':source['exe_sha256']='c'*64
            elif mutation in ('before','after'):source[mutation]=False
            elif mutation=='false-compiled':source['compiled']['normal-flickdock-entry']=False
            else:source['compiled']={'normal-flickdock-entry':True}
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.validate(report,source)
    def run_main(self,root,*,require=False,reports=True,failed=False):
        report,source=fixtures()
        if failed:report['ok']=False
        raw=json.dumps(report,indent=3).encode();verification=json.dumps(source,indent=1).encode()
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('dist/FlickDock/FlickDock.exe',EXE)
            if reports:
                archive.writestr('build/normal-native-qa/normal-frozen-native-report.json',raw)
                archive.writestr('build/normal-source-verification.json',verification)
        archive=buffer.getvalue();urls=[]
        class Opener:
            def open(self,request,timeout):
                url=request.full_url;urls.append(url)
                if url.endswith('/actions/runs/123'):
                    data=json.dumps(dict(repository={'full_name':'los0711/-flickdock-build'},head_sha='a'*40)).encode()
                elif '/artifacts?per_page=' in url:
                    data=json.dumps(dict(artifacts=[dict(id=9,name='normal-frozen-windows-'+'a'*40,expired=False,
                        digest='sha256:'+hashlib.sha256(archive).hexdigest(),size_in_bytes=len(archive))])).encode()
                else:data=archive
                return io.BytesIO(data)
        args=['reuse_frozen.py','--run','123']+(['--require-passed-normal'] if require else [])
        before=Path.cwd()
        try:
            os.chdir(root)
            with patch.dict(os.environ,{'GITHUB_ACTIONS':'true','RUNNER_OS':'Windows','GITHUB_TOKEN':'synthetic-token'}), \
                    patch.object(sys,'argv',args),patch.object(qa,'build_opener',return_value=Opener()), \
                    patch.object(qa,'required_normal_labels',return_value=LABELS):qa.main()
        finally:os.chdir(before)
        return raw,verification,urls
    def test_default_old_mode_still_accepts_dist_without_normal_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.run_main(root,reports=False)
            self.assertEqual((root/'dist/FlickDock/FlickDock.exe').read_bytes(),EXE)
            self.assertFalse((root/'build/passed-normal-report.json').exists())
            self.assertNotIn('passed_normal',json.loads((root/'build/reused-frozen-receipt.json').read_text()))
    def test_optional_mode_uses_one_zip_and_saves_exact_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw,verification,urls=self.run_main(root,require=True)
            self.assertEqual(sum(x.endswith('/actions/artifacts/9/zip') for x in urls),1)
            self.assertEqual((root/'build/passed-normal-report.json').read_bytes(),raw)
            self.assertEqual((root/'build/passed-normal-source-verification.json').read_bytes(),verification)
            self.assertFalse((root/'build/normal-source-verification.json').exists())
            receipt=json.loads((root/'build/reused-frozen-receipt.json').read_text())
            self.assertEqual(receipt['passed_normal']['report_sha256'],hashlib.sha256(raw).hexdigest())
    def test_failed_optional_mode_never_writes_a_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ValueError):self.run_main(root,require=True,failed=True)
            self.assertFalse((root/'build/passed-normal-report.json').exists())
            self.assertFalse((root/'build/reused-frozen-receipt.json').exists())


if __name__=='__main__':unittest.main()
