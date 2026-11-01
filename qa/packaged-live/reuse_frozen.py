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

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS')!='Windows':raise RuntimeError('Ephemeral hosted Windows only')
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);args=parser.parse_args()
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
    root=Path.cwd();build=root/'build';build.mkdir(exist_ok=True)
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
        if not extracted or not (root/'dist/FlickDock/FlickDock.exe').is_file():raise ValueError('Normal dist absent')
    (build/'reused-frozen-receipt.json').write_text(json.dumps(dict(run=int(args.run),source=run['head_sha'],
        artifact=artifact['id'],artifact_sha256=checksum.hexdigest(),files=extracted,
        exe_sha256=hashlib.sha256((root/'dist/FlickDock/FlickDock.exe').read_bytes()).hexdigest(),
        scope='Immutable bytes reused; current source compiled-code verification and actual QA still required'),indent=2))
    print('Verified immutable dist bytes retrieved; current compiled source comparison follows')

if __name__=='__main__':main()
