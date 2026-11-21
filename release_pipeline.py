"""Prepare, sign and verify a manually triggered GitHub release."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import requests

from app_version import APP_VERSION
from release_archives import file_metadata, publish_archives
from release_manifest import (ReleaseError, collect_source_files, create_release,
                              validate_verification, validate_versions, version_tuple,
                              verify_public_file)
from release_macos import create_mac_release, validate_reports
from release_signing import publisher_signer
from update_client import DEFAULT_ALLOWED_HOSTS, _json_object, validate_manifest
from update_signatures import verify_manifest_signature
from website.scripts.release_downloads import read as read_catalog, register, write as write_catalog


ROOT = Path(__file__).resolve().parent
HOST = 'flickdock-site.roger000416.workers.dev'
BASE = 'https://' + HOST
ORIGIN = 'https://download-origin.flickdock.cc.cd/'
REPO = 'los0711/-flickdock-build'
ACCOUNT = '59153da0809ff3d18297d943e1ef5b56'
INDEX_NAMES = ('latest.json','macos-arm64.json','macos-arm64-auto.json','macos-arm64-auto-v2.json',
               'macos-x86_64.json','macos-x86_64-auto.json','macos-x86_64-auto-v2.json')
MAC_ARM_INDICES = INDEX_NAMES[1:4]
WINDOWS_INDICES = INDEX_NAMES[:1]
RELEASE_HOSTS = set(DEFAULT_ALLOWED_HOSTS)


def release_indices():
    target = os.environ.get('FLICKDOCK_RELEASE_TARGET','both')
    if target == 'windows-x64':
        return WINDOWS_INDICES
    if target == 'mac-arm64':
        return MAC_ARM_INDICES
    if target == 'both':
        return ('latest.json',*MAC_ARM_INDICES)
    raise ValueError('Unknown release target.')


def context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise RuntimeError('Central publication is available only from a manual main-branch run.')
    sha = os.environ.get('GITHUB_SHA','')
    if not re.fullmatch('[a-f0-9]{40}',sha):
        raise ValueError('The source commit identity is invalid.')
    return sha


def work():
    path = Path(os.environ['RUNNER_TEMP']) / 'flickdock-release-state'
    path.mkdir(parents=True,exist_ok=True)
    return path


def _public_document(url):
    if not url.startswith(BASE + '/updates/') or url.removeprefix(BASE + '/updates/') not in INDEX_NAMES:
        raise ReleaseError('Unexpected update baseline URL.')
    response = requests.get(url,headers={'User-Agent':'FlickDock-Release/1','Cache-Control':'no-cache'},
                            timeout=(20,45),stream=True,allow_redirects=False)
    try:
        if response.status_code != 200:
            raise ReleaseError('Cannot read the published update baseline.')
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=16384):
            raw.extend(chunk)
            if len(raw) > 1024*1024:
                raise ReleaseError('The published update baseline is too large.')
    finally:
        response.close()
    raw = bytes(raw)
    value = json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_json_object)
    verify_manifest_signature(value,required=True)
    return value,raw


def baselines():
    documents = {name:_public_document(BASE + '/updates/' + name) for name in INDEX_NAMES}
    result = {name:document[0] for name,document in documents.items()}
    for name,value in result.items():
        platform = 'win32' if name == 'latest.json' else 'darwin'
        machine = 'x86_64' if 'x86_64' in name else 'arm64'
        validate_manifest(value,RELEASE_HOSTS,platform=platform,machine=machine)
    return result,{name:document[1] for name,document in documents.items()}


def main_head(token):
    if not token:
        raise ValueError('GitHub release authorization is missing.')
    response = requests.get('https://api.github.com/repos/' + REPO + '/git/ref/heads/main',
        headers={'Authorization':'Bearer ' + token,'Accept':'application/vnd.github+json'},timeout=30)
    if response.status_code != 200:
        raise RuntimeError('Cannot verify the GitHub source branch.')
    return response.json()['object']['sha']


def preflight(*, publish):
    sha = context()
    if main_head(os.environ.get('GITHUB_TOKEN')) != sha:
        raise RuntimeError('The main branch changed; start a fresh release.')
    version_tuple(APP_VERSION)
    old,raw = baselines()
    if release_indices() == WINDOWS_INDICES and any(old[name]['version'] != APP_VERSION for name in MAC_ARM_INDICES):
        raise ValueError('The Windows-only release must complete the existing Apple Silicon version.')
    for name in release_indices():
        current = old[name]['version']
        if version_tuple(APP_VERSION) < version_tuple(current) or (publish and version_tuple(APP_VERSION) <= version_tuple(current)):
            raise ValueError('The source version must be newer than every published update channel.')
    if publish:
        notes = os.environ.get('FLICKDOCK_RELEASE_NOTES','').strip()
        if not notes or len(notes) > 4000:
            raise ValueError('Provide concise, nonempty release notes.')
    for name in ('FlickDock.iss','ChineseSimplified.isl'):
        if not (ROOT/name).read_bytes().startswith(b'\xef\xbb\xbf'):
            raise ValueError(name + ' must retain its UTF-8 BOM.')
    collect_source_files(ROOT)
    print('Verified source identity, release baseline and source inventory:', APP_VERSION, flush=True)
    return old,raw


def credentials(*, signing_only=False, gitee_only=False):
    context()
    signer = publisher_signer()
    sample = b'FlickDock release authorization verification'
    signer.public_key().verify(signer.sign(sample),sample)
    if signing_only:
        print('Verified the central publisher key without publishing.',flush=True)
        return
    token = os.environ.get('GITEE_RELEASE_TOKEN','')
    cloud = os.environ.get('CLOUDFLARE_API_TOKEN','')
    if not token:
        raise ValueError('The Gitee release credential is required.')
    user = requests.get('https://gitee.com/api/v5/user',headers={'Authorization':'Bearer ' + token},timeout=30)
    source = requests.get('https://gitee.com/api/v5/repos/rooogerluo/flick-dock',
                          headers={'Authorization':'Bearer ' + token},timeout=30)
    if (user.status_code != 200 or user.json().get('login') != 'rooogerluo'
            or source.status_code != 200 or source.json().get('private') is not True):
        raise ValueError('Gitee authorization or private source repository is unavailable.')
    if gitee_only:
        print('Verified the publisher key and private Gitee source access.',flush=True)
        return
    if not cloud:
        raise ValueError('The Cloudflare release credential is required.')
    verify = requests.get('https://api.cloudflare.com/client/v4/user/tokens/verify',
                          headers={'Authorization':'Bearer ' + cloud},timeout=30)
    if verify.status_code != 200 or (verify.json().get('result') or {}).get('status') != 'active':
        raise ValueError('The Cloudflare release token is unavailable or inactive.')
    bucket = requests.get('https://api.cloudflare.com/client/v4/accounts/' + ACCOUNT + '/r2/buckets/flickdock-releases',
                          headers={'Authorization':'Bearer ' + cloud},timeout=30)
    if bucket.status_code != 200:
        raise ValueError('The Cloudflare release token cannot read the installer bucket.')
    print('Verified publisher key, Gitee account and Cloudflare release bucket.',flush=True)


def verified_windows_build_sha(run_id):
    """Re-use the tested artifact of a release whose publishing step failed."""
    from release_source_sync import WINDOWS_PUBLICATION_FILES

    current = context()
    if release_indices() != WINDOWS_INDICES or not re.fullmatch('[1-9][0-9]{0,19}',run_id):
        raise ValueError('An existing Windows build requires a Windows-only release and numeric run ID.')
    token = os.environ.get('GITHUB_TOKEN','')
    if not token:
        raise ValueError('GitHub artifact verification requires a token.')
    base = f'https://api.github.com/repos/{REPO}/actions/runs/{run_id}'
    headers = {'Authorization':'Bearer ' + token, 'Accept':'application/vnd.github+json'}
    response = requests.get(base,headers=headers,timeout=30)
    response.raise_for_status()
    run = response.json()
    sha = run.get('head_sha','')
    if (run.get('path') != '.github/workflows/central-release.yml' or run.get('event') != 'workflow_dispatch'
            or run.get('head_branch') != 'main' or run.get('status') != 'completed'
            or (run.get('head_repository') or {}).get('full_name') != REPO
            or not re.fullmatch('[a-f0-9]{40}',sha)):
        raise ValueError('The selected run is not a completed central release from main.')
    jobs = requests.get(base+'/jobs',headers=headers,timeout=30)
    jobs.raise_for_status()
    if not any(job['name']=='windows / build' and job['conclusion']=='success' for job in jobs.json()['jobs']):
        raise ValueError('The selected run did not pass the native Windows build and installer checks.')
    subprocess.run(['git','merge-base','--is-ancestor',sha,current],cwd=ROOT,check=True,timeout=30)
    files = subprocess.check_output(['git','diff','--name-only','-z',sha,current],cwd=ROOT,timeout=30)
    if set(files.decode('utf8').rstrip('\0').split('\0')) - WINDOWS_PUBLICATION_FILES - {''}:
        raise ValueError('The selected artifact was built from different application source.')
    return sha


def _verified_builds():
    windows = ROOT/'ci-windows-release'
    mac = ROOT/'ci-macos-release'
    setup = windows/('FlickDock-Setup-' + APP_VERSION + '.exe')
    win_report = windows/('windows-build-' + APP_VERSION + '.json')
    if 'latest.json' in release_indices():
        qa = json.loads((windows/'windows-installer-qa.json').read_text(encoding='utf-8'))
        if qa.get('ok') is not True:
            raise ValueError('Windows installer behavior verification did not pass.')
        win = file_metadata(setup)
        validate_verification(win_report,setup,APP_VERSION,win['sha256'],win['bytes'])
        validate_versions(ROOT,setup,APP_VERSION)
    else:
        setup=win_report=None
    if release_indices() == WINDOWS_INDICES:
        return setup,win_report,[],None,None
    build = mac/('mac-build-' + APP_VERSION + '-arm64.json')
    cloud = mac/'mac-cloud-arm64.json'
    report = json.loads(build.read_text(encoding='utf-8'))
    if report.get('pkg_payload_verified') is not True:
        raise ValueError('Mac installer payload verification did not pass.')
    mac_files = [mac/('FlickDock-' + APP_VERSION + '-macOS-arm64.' + ext) for ext in ('zip','dmg','pkg')]
    for path in mac_files:
        actual = file_metadata(path)
        rows = [row for row in report.get('artifacts',[]) if row.get('name') == path.name]
        if len(rows) != 1 or rows[0].get('bytes') != actual['bytes'] or rows[0].get('sha256') != actual['sha256']:
            raise ValueError('Mac artifact does not match its native build report: ' + path.name)
        if path.suffix in ('.zip','.dmg'):
            validate_reports(build,cloud,path,APP_VERSION,'arm64',actual['bytes'],actual['sha256'])
    return setup,win_report,mac_files,build,cloud


def _cloud_env():
    if not os.environ.get('CLOUDFLARE_API_TOKEN'):
        raise ValueError('Cloudflare release authorization is missing.')
    env = dict(os.environ,CLOUDFLARE_ACCOUNT_ID=ACCOUNT,WRANGLER_SEND_METRICS='false')
    for key in ('GITEE_RELEASE_TOKEN','GITHUB_TOKEN','GH_TOKEN','FLICKDOCK_RELEASE_SIGNING_KEY'):
        env.pop(key,None)
    return env


def _upload_origin(path, asset):
    url = ORIGIN + asset['originKey']
    probe = requests.head(url,headers={'User-Agent':'FlickDock-Release/1'},timeout=40,allow_redirects=False)
    if probe.status_code == 200:
        return
    if probe.status_code != 404:
        raise RuntimeError('Cannot inspect the immutable R2 object.')
    wrangler = ROOT/'website'/'node_modules'/'wrangler'/'bin'/'wrangler.js'
    if not wrangler.is_file():
        raise RuntimeError('The reviewed website Wrangler dependency is missing.')
    subprocess.run(['node',str(wrangler),'r2','object','put',
        'flickdock-releases/' + asset['originKey'],'--file',str(path),'--remote',
        '--content-type','application/octet-stream','--cache-control','public, max-age=31536000, immutable',
        '--content-disposition','attachment; filename="' + asset['name'] + '"'],
        cwd=ROOT,env=_cloud_env(),check=True,timeout=600)


def _next_html(previous):
    html = (ROOT/'website'/'public'/'index.html').read_text(encoding='utf-8')
    for index,label,filename in (
            ('latest.json','Windows x64','FlickDock-Setup-{version}.exe'),
            ('macos-arm64.json','Mac Apple 芯片','FlickDock-{version}-macOS-arm64.pkg')):
        old_version=previous[index]['version']
        new_version=APP_VERSION if index in release_indices() else old_version
        for template in ('v{version}/'+filename,label+' · {version}'):
            old=template.format(version=old_version)
            if html.count(old) != 1:
                raise ValueError('The existing website release link does not match the published version.')
            html=html.replace(old,template.format(version=new_version))
    return html


def stage():
    previous,raw = preflight(publish=True)
    setup,win_report,mac_files,build,cloud = _verified_builds()
    packages = [setup,*mac_files] if setup else mac_files
    notes = os.environ['FLICKDOCK_RELEASE_NOTES'].strip()
    build_sha = os.environ.get('FLICKDOCK_VERIFIED_WINDOWS_SHA') or context()
    assets = publish_archives(packages,APP_VERSION,build_sha,notes,
        github_token=os.environ.get('GITHUB_TOKEN',''))
    for path,asset in zip(packages,assets,strict=True):
        _upload_origin(path,asset)
    folder = work()
    target = ROOT/'website'/'public'/'updates'
    target.mkdir(parents=True,exist_ok=True)
    for name in previous:
        (folder/name).write_bytes(raw[name])
        (target/name).write_bytes(raw[name])
    catalog = ROOT/'website'/'download-catalog.json'
    write_catalog(catalog,register(read_catalog(catalog),[dict(assets=assets)],
                                   extend_windows=release_indices()==WINDOWS_INDICES))
    (folder/'next-index.html').write_text(_next_html(previous),encoding='utf-8')
    state = dict(version=APP_VERSION,source_sha=context(),notes=notes,assets=assets,indices=list(release_indices()))
    (folder/'state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Verified builds and archived assets; update indices remain unchanged.',flush=True)


def _state():
    value = json.loads((work()/'state.json').read_text(encoding='utf-8'))
    if (value.get('version') != APP_VERSION or value.get('source_sha') != context()
            or value.get('indices') != list(release_indices())):
        raise ValueError('Release state does not match this source commit.')
    return value


def sign():
    state = _state()
    setup,win_report,mac_files,build,cloud = _verified_builds()
    folder = work()
    (ROOT/'website'/'public'/'index.html').write_bytes((folder/'next-index.html').read_bytes())
    indices = folder/'indices'
    for sub in (indices,indices/'legacy',indices/'auto'):
        sub.mkdir(parents=True,exist_ok=True)
    signer = publisher_signer()
    previous = lambda name: json.loads((folder/name).read_text(encoding='utf-8'))
    if setup:
        create_release(root=ROOT,installer=setup,version=APP_VERSION,notes=state['notes'],
            download_url=BASE+'/downloads/'+setup.name,verification=win_report,output=indices/'latest.json',
            artifacts=folder.parent/'flickdock-private-release-artifacts',
            previous=previous('latest.json'),allowed_hosts=RELEASE_HOSTS,signer=signer)
    if mac_files:
        archive,dmg,pkg = mac_files
        pkg_info = file_metadata(pkg)
        verify_public_file(BASE+'/downloads/'+pkg.name,pkg_info['sha256'],pkg_info['bytes'],
                           RELEASE_HOSTS,pe=False)
        common = dict(build_report=build,cloud_report=cloud,version=APP_VERSION,architecture='arm64',
                      notes=state['notes'],allowed_hosts=RELEASE_HOSTS,signer=signer)
        create_mac_release(archive=dmg,download_url=BASE+'/downloads/'+dmg.name,
            output=indices/'legacy'/'macos-arm64.json',previous=previous('macos-arm64.json'),
            channel='legacy',**common)
        create_mac_release(archive=archive,download_url=BASE+'/downloads/'+archive.name,
            output=indices/'auto'/'macos-arm64-auto.json',previous=previous('macos-arm64-auto-v2.json'),
            channel='auto',**common)
    targets = {'latest.json':indices/'latest.json','macos-arm64.json':indices/'legacy'/'macos-arm64.json',
               'macos-arm64-auto.json':indices/'legacy'/'macos-arm64.json',
               'macos-arm64-auto-v2.json':indices/'auto'/'macos-arm64-auto.json'}
    destination = ROOT/'website'/'public'/'updates'
    for name,path in targets.items():
        if name not in release_indices():continue
        value = json.loads(path.read_text(encoding='utf-8'))
        verify_manifest_signature(value,required=True)
        shutil.copyfile(path,destination/name)
    print('Verified public installer bytes and signed update indices.',flush=True)


def verify():
    state = _state()
    versions={}
    for name in INDEX_NAMES:
        value,raw = _public_document(BASE + '/updates/' + name)
        expected=(ROOT/'website'/'public'/'updates'/name) if name in release_indices() else work()/name
        if raw != expected.read_bytes():
            raise ValueError('The published update index differs from the signed index: ' + name)
        platform = 'win32' if name == 'latest.json' else 'darwin'
        machine='x86_64' if 'x86_64' in name else 'arm64'
        validate_manifest(value,RELEASE_HOSTS,platform=platform,machine=machine)
        versions[name]=value['version']
        if name in release_indices() and value['version'] != state['version']:
            raise ValueError('The public update index is not the released version: ' + name)
    response = requests.get(BASE+'/',headers={'User-Agent':'FlickDock-Release/1','Cache-Control':'no-cache'},timeout=30)
    if response.status_code != 200:
        raise RuntimeError('The public website is unavailable.')
    for index,filename in (('latest.json','FlickDock-Setup-{version}.exe'),
                           ('macos-arm64.json','FlickDock-{version}-macOS-arm64.pkg')):
        version=versions[index];suffix=filename.format(version=version)
        if '/v' + version + '/' + suffix not in response.text:
            raise ValueError('The website does not show the new download: ' + suffix)
    print('Verified signed public indices and website release links.',flush=True)


def site_guidance(*, prepare):
    """Deploy download instructions with the exact seven live update indices."""
    context()
    public=ROOT/'website'/'public'
    versions,raw=baselines()
    if prepare:
        html=(public/'index.html').read_text(encoding='utf-8')
        if 'data-update-guide' not in html:
            raise ValueError('The local download instructions are missing.')
        for index,pattern in (('latest.json','FlickDock-Setup-{}.exe'),
                              ('macos-arm64.json','FlickDock-{}-macOS-arm64.pkg')):
            version=versions[index]['version']
            if '/v'+version+'/'+pattern.format(version) not in html:
                raise ValueError('The site would change an installer download link.')
        target=public/'updates'
        target.mkdir(exist_ok=True,parents=True)
        for name,value in raw.items():
            (target/name).write_bytes(value)
        return
    for name,value in raw.items():
        if (public/'updates'/name).read_bytes()!=value:
            raise ValueError('The published site changed the update index: ' + name)
    for base in (BASE,'https://flickdock.cc.cd'):
        response=requests.get(base+'/',headers={'User-Agent':'FlickDock-Release/1','Cache-Control':'no-cache'},timeout=25)
        response.raise_for_status()
        if 'data-update-guide' not in response.text:
            raise ValueError('The published download instructions are missing at ' + base)
    print('Verified the live download instructions and unchanged signed indices.',flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase',choices=('preflight','verify-signing','verify-gitee','verify-credentials',
                                        'windows-build-sha','stage','sign','verify','stage-site','verify-site'))
    args = parser.parse_args()
    if args.phase == 'preflight':preflight(publish=True)
    elif args.phase == 'verify-signing':credentials(signing_only=True)
    elif args.phase == 'verify-gitee':credentials(gitee_only=True)
    elif args.phase == 'verify-credentials':credentials()
    elif args.phase == 'windows-build-sha':print(verified_windows_build_sha(os.environ.get('FLICKDOCK_WINDOWS_BUILD_RUN','')))
    elif args.phase == 'stage':stage()
    elif args.phase == 'sign':sign()
    elif args.phase == 'stage-site':site_guidance(prepare=True)
    elif args.phase == 'verify-site':site_guidance(prepare=False)
    else:verify()


if __name__ == '__main__':
    main()
