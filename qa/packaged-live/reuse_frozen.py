"""Reuse immutable dist bytes for QA-only changes, verifying archive and code.

No publishing or app execution. GitHub's token is scoped to this read step and
is stripped before artifact redirects. Only dist/FlickDock is extracted.
"""
import argparse,hashlib,json,os,re,tempfile,zipfile
from pathlib import Path,PurePosixPath
from urllib.request import Request,HTTPRedirectHandler,build_opener
from urllib.parse import urlsplit

class Redirect(HTTPRedirectHandler):
    def redirect_request(self,request,response,code,message,headers,url):
        parts=urlsplit(url)
        if parts.scheme!='https' or parts.username or parts.password:raise ValueError('Unsafe artifact redirect')
        return Request(url,headers={'User-Agent':'FlickDock-exact-dist-QA'})

def required_normal_labels(root):
    from ci_packaged_windows_live import compose_actions
    folder=Path(root)/'qa/packaged-live'
    values=[json.loads((folder/name).read_text(encoding='utf8')) for name in
        ('ui-actions.json','ui-extra-actions.json','ui-batch2-actions.json','ui-supplement-actions.json')]
    actions=compose_actions(*values)
    if len(actions)!=58:raise ValueError('The passed-normal flow contract requires review')
    labels={f'flow-{index:02}-{step["id"]}' for index,step in enumerate(actions)}
    labels.update(('01-normal-overview-open','02-native-text-copy','02c-native-peer-receive-and-no-echo',
        '03-settings-open','04a-enable-shelf-receive','04b-disable-shelf-receive','04-disable-cloud-clipboard',
        '05-native-file-copy-stalled-upload','05b-reenable-keeps-native-original','06-native-original-image-copy',
        '06a-enable-shelf-upload','06b-disable-shelf-upload','06c-resume-shelf-upload','06d-restore-clipboard-sync',
        '07-close-settings','08-reopen-overview','08a-open-overview-live-peer-add',
        '08b-open-overview-live-peer-remove','final-real-distinct-notice-stack'))
    return labels


def validate_passed_normal(report_raw,source_raw,exe_sha,required_labels):
    if len(report_raw)>2*1024**2 or len(source_raw)>1024**2:raise ValueError('Oversized prior normal proof')
    report=json.loads(report_raw);verification=json.loads(source_raw)
    if not isinstance(report,dict) or not isinstance(verification,dict):raise ValueError('Invalid prior normal proof')
    for name,value in (('ok',True),('normal_entry',True),('verify_switches',False),('cocoa_smoke',False),
                       ('production_entry_imported',False),('frozen_executable_patched',False),('installer_executed',False)):
        if report.get(name) is not value:raise ValueError('Prior normal proof flag mismatch: '+name)
    if (report.get('observation_only') not in (None,False) or 'writer_observation' in report
            or report.get('installer_acceptance') is False):raise ValueError('Observation report cannot qualify an installer')
    if any(report.get(name) for name in ('error','profile_cleanup_error','cleanup_error','automation_cleanup_error',
            'fixture_editor_cleanup_error','animation_preference_restore_error')):
        raise ValueError('Prior normal proof contains a failure')
    if 'profile_failure' in report.get('cleanup_diagnostics',{}):raise ValueError('Prior profile cleanup failed')
    if not re.fullmatch(r'[0-9a-f]{64}',exe_sha) or report.get('exe_sha256')!=exe_sha or verification.get('exe_sha256')!=exe_sha:
        raise ValueError('Prior normal executable SHA mismatch')
    compiled=verification.get('compiled')
    if (verification.get('before') is not True or verification.get('after') is not True
            or not isinstance(compiled,dict) or len(compiled)<99 or compiled.get('normal-flickdock-entry') is not True
            or any(value is not True for value in compiled.values())
            or not re.fullmatch(r'[0-9a-f]{64}',str(verification.get('source_manifest_sha256','')))
            or type(verification.get('reviewed_files')) is not int or verification['reviewed_files']<=0):
        raise ValueError('Prior frozen source proof is incomplete')
    checks=report.get('checks');segments=report.get('screen_segments')
    if (not isinstance(checks,list) or len(checks)<15 or any(not isinstance(x,str) or not x for x in checks)
            or len(set(checks))!=len(checks)):raise ValueError('Prior normal core checks are incomplete')
    if not isinstance(segments,list) or len(segments)!=len(required_labels):raise ValueError('Prior normal screen steps are incomplete')
    labels=[]
    for segment in segments:
        if (not isinstance(segment,dict) or not isinstance(segment.get('label'),str)
                or type(segment.get('distinct_pixels')) is not int or segment['distinct_pixels']<=0):
            raise ValueError('Prior normal screen evidence is invalid')
        labels.append(segment['label'])
    if len(set(labels))!=len(labels) or set(labels)!=required_labels:raise ValueError('Prior normal full flow labels do not match')
    return dict(report_sha256=hashlib.sha256(report_raw).hexdigest(),source_verification_sha256=hashlib.sha256(source_raw).hexdigest(),
        exe_sha256=exe_sha,core_checks=len(checks),flow_steps=58,screen_steps=len(labels))


def read_passed_normal(package,root,exe_sha):
    names=('build/normal-native-qa/normal-frozen-native-report.json','build/normal-source-verification.json')
    raw=[]
    for name,limit in zip(names,(2*1024**2,1024**2)):
        matches=[info for info in package.infolist() if info.filename==name]
        if len(matches)!=1 or matches[0].file_size>limit:raise ValueError('One bounded prior normal proof is required')
        raw.append(package.read(matches[0]))
    proof=validate_passed_normal(*raw,exe_sha,required_normal_labels(root))
    return raw,proof


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS')!='Windows':raise RuntimeError('Ephemeral hosted Windows only')
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);parser.add_argument('--require-passed-normal',action='store_true');args=parser.parse_args()
    if not re.fullmatch(r'[1-9][0-9]{0,19}',args.run):raise ValueError('Invalid prior run ID')
    token=os.environ['GITHUB_TOKEN'];base='https://api.github.com/repos/los0711/-flickdock-build'
    opener=build_opener(Redirect())
    def get(path):
        request=Request(base+path,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'FlickDock-exact-dist-QA'})
        return opener.open(request,timeout=90)
    with get('/actions/runs/'+args.run) as response:run=json.load(response)
    if run['repository']['full_name']!='los0711/-flickdock-build':raise ValueError('Wrong artifact repository')
    with get('/actions/runs/'+args.run+'/artifacts?per_page=100') as response:rows=json.load(response)['artifacts']
    matches=[row for row in rows if row['name']=='normal-frozen-windows-'+run['head_sha'] and not row['expired']]
    if len(matches)!=1:raise ValueError('One exact normal-executable artifact is required')
    artifact=matches[0];expected=artifact.get('digest','')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',expected):raise ValueError('Artifact SHA256 attestation absent')
    root=Path.cwd();build=root/'build';build.mkdir(exist_ok=True);passed=None
    if args.require_passed_normal and any((build/name).exists() for name in ('passed-normal-report.json','passed-normal-source-verification.json')):
        raise ValueError('Refusing to overwrite an existing passed-normal proof')
    with tempfile.TemporaryDirectory(prefix='exact-frozen-qa-') as folder:
        archive=Path(folder)/'dist.zip';size=0;checksum=hashlib.sha256()
        with get('/actions/artifacts/'+str(artifact['id'])+'/zip') as response,archive.open('wb') as output:
            while body:=response.read(1024*1024):
                size+=len(body)
                if size>700*1024**2:raise ValueError('Unexpected artifact size')
                output.write(body);checksum.update(body)
        if expected!='sha256:'+checksum.hexdigest() or size!=artifact['size_in_bytes']:raise ValueError('Artifact integrity mismatch')
        with zipfile.ZipFile(archive) as package:
            if sum(info.file_size for info in package.infolist())>2*1024**3:raise ValueError('Oversized archive')
            extracted=0
            for info in package.infolist():
                path=PurePosixPath(info.filename)
                if path.is_absolute() or '..' in path.parts or ':' in info.filename or '\\' in info.filename:raise ValueError('Unsafe archive path')
                if path.parts[:2]!=('dist','FlickDock'):continue
                target=root.joinpath(*path.parts).resolve();target.relative_to((root/'dist/FlickDock').resolve())
                if info.is_dir():target.mkdir(parents=True,exist_ok=True);continue
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(package.read(info));extracted+=1
            if args.require_passed_normal:
                exe=root/'dist/FlickDock/FlickDock.exe'
                if not exe.is_file():raise ValueError('Normal dist absent')
                with exe.open('rb') as stream:exe_sha=hashlib.file_digest(stream,'sha256').hexdigest()
                raw,passed=read_passed_normal(package,root,exe_sha)
        if not extracted or not (root/'dist/FlickDock/FlickDock.exe').is_file():raise ValueError('Normal dist absent')
    receipt=dict(run=int(args.run),source=run['head_sha'],
        artifact=artifact['id'],artifact_sha256=checksum.hexdigest(),files=extracted,
        exe_sha256=hashlib.sha256((root/'dist/FlickDock/FlickDock.exe').read_bytes()).hexdigest(),
        scope='Immutable bytes reused; current source compiled-code verification and actual QA still required')
    if passed is not None:
        # Current-source comparison still runs in verify_frozen_source after
        # reuse. Preserve the attested originals byte for byte, never re-label.
        (build/'passed-normal-report.json').write_bytes(raw[0])
        (build/'passed-normal-source-verification.json').write_bytes(raw[1])
        receipt['passed_normal']=passed
    (build/'reused-frozen-receipt.json').write_text(json.dumps(receipt,indent=2))
    print('Verified immutable dist bytes retrieved; current compiled source comparison follows')

if __name__=='__main__':main()
