"""Uninstrumented original deadline check in the isolated hosted Mac job."""
import argparse,contextlib,hashlib,json,os,sys,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FILES=('win_clipboard_native.py','win_clipboard_cache.py','win_clipboard_io.py','test_clipboard_local_original_v1825.py')
CASE='test_real_100m_plus_one_logical_text_keeps_full_utf16_under_default_deadline'

def hosted_guard():
    required={'GITHUB_ACTIONS':'true','CI':'true','RUNNER_ENVIRONMENT':'github-hosted','RUNNER_OS':'macOS',
              'GITHUB_REPOSITORY':'los0711/-flickdock-build','GITHUB_EVENT_NAME':'workflow_dispatch',
              'GITHUB_JOB':'text-profile','FD_QA_TEXT_PROFILE':'1'}
    if sys.platform!='darwin' or any(os.environ.get(name)!=value for name,value in required.items()):
        raise RuntimeError('HOSTED_MAC_TEXT_JOB_REQUIRED')

def main(argv=None):
    hosted_guard()
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args(argv)
    output=args.output.absolute()
    if '..' in output.parts or not output.resolve().is_relative_to((ROOT/'build').resolve()) or output.resolve()==(ROOT/'build').resolve():raise ValueError('OWNED_BUILD_OUTPUT_REQUIRED')
    output.mkdir(parents=True,exist_ok=False)
    def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FILES}
    before=hashes()
    with tempfile.TemporaryDirectory(prefix='fd-uninstrumented-text-',dir=output) as profile:
        os.environ.update(QT_QPA_PLATFORM='offscreen',TEMP=profile,TMP=profile,TMPDIR=profile,APPDATA=profile,LOCALAPPDATA=profile,XDG_CONFIG_HOME=profile)
        os.environ.pop('FD_QA_NATIVE',None);os.environ.pop('FD_TEXT_STAGE_LOG',None);tempfile.tempdir=profile
        sys.path.insert(0,str(ROOT))
        from qt_unittest import QtTestRunner
        import test_clipboard_local_original_v1825 as tests
        code=getattr(tests.LocalOriginalTests,CASE).__code__
        with (output/'check.log').open('x',encoding='utf8') as stream,contextlib.redirect_stdout(stream):
            result=QtTestRunner(stream=stream,verbosity=2).run(unittest.TestSuite([tests.LocalOriginalTests(CASE)]))
        code_unchanged=getattr(tests.LocalOriginalTests,CASE).__code__ is code
        after=hashes()
    receipt=dict(before=before,after=after,stable=before==after,successful=result.wasSuccessful(),tests=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),test_code_identity_unchanged=code_unchanged,
        instrumentation=False,original_default_deadline_ms=3000,deadline_or_assertions_changed=False,
        scope='Original synthetic provider and actual helper/cache/Qt model on hosted Mac, without profiling hooks. No OS clipboard, installer or physical-device latency.',
        publication=False,complete_product=False)
    (output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
    return 0 if result.wasSuccessful() and before==after and code_unchanged else 1

if __name__=='__main__':raise SystemExit(main())
