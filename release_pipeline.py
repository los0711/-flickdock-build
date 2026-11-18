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
RELEASE_HOSTS = set(DEFAULT_ALLOWED_HOSTS)


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
    for name in ('latest.json','macos-arm64.json','macos-arm64-auto.json','macos-arm64-auto-v2.json'):
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


def _verified_builds():
    windows = ROOT/'ci-windows-release'
    mac = ROOT/'ci-macos-release'
    setup = windows/('FlickDock-Setup-' + APP_VERSION + '.exe')
    win_report = windows/('windows-build-' + APP_VERSION + '.json')
    qa = json.loads((windows/'windows-installer-qa.json').read_text(encoding='utf-8'))
    if qa.get('ok') is not True:
        raise ValueError('Windows installer behavior verification did not pass.')
    win = file_metadata(setup)
    validate_verification(win_report,setup,APP_VERSION,win['sha256'],win['bytes'])
    validate_versions(ROOT,setup,APP_VERSION)
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


def _next_html(old_version):
    html = (ROOT/'website'/'public'/'index.html').read_text(encoding='utf-8')
    replacements = [
        ('v' + old_version + '/FlickDock-Setup-' + old_version + '.exe',
         'v' + APP_VERSION + '/FlickDock-Setup-' + APP_VERSION + '.exe'),
        ('v' + old_version + '/FlickDock-' + old_version + '-macOS-arm64.pkg',
         'v' + APP_VERSION + '/FlickDock-' + APP_VERSION + '-macOS-arm64.pkg'),
        ('Windows x64 · ' + old_version, 'Windows x64 · ' + APP_VERSION),
        ('Mac Apple 芯片 · ' + old_version, 'Mac Apple 芯片 · ' + APP_VERSION),
    ]
    for old,new in replacements:
        if html.count(old) != 1:
            raise ValueError('The existing website release link does not match the published version.')
        html = html.replace(old,new)
    return html


def stage():
    previous,raw = preflight(publish=True)
    if len({previous[name]['version'] for name in ('latest.json','macos-arm64.json','macos-arm64-auto-v2.json')}) != 1:
        raise ValueError('Windows and Mac published versions differ; resolve the baseline before release.')
    setup,win_report,mac_files,build,cloud = _verified_builds()
    notes = os.environ['FLICKDOCK_RELEASE_NOTES'].strip()
    assets = publish_archives([setup,*mac_files],APP_VERSION,context(),notes,
        gitee_token=os.environ.get('GITEE_RELEASE_TOKEN',''),github_token=os.environ.get('GITHUB_TOKEN',''))
    for path,asset in zip([setup,*mac_files],assets,strict=True):
        _upload_origin(path,asset)
    folder = work()
    target = ROOT/'website'/'public'/'updates'
    target.mkdir(parents=True,exist_ok=True)
    for name in previous:
        (folder/name).write_bytes(raw[name])
        (target/name).write_bytes(raw[name])
    catalog = ROOT/'website'/'download-catalog.json'
    write_catalog(catalog,register(read_catalog(catalog),[dict(assets=assets)]))
    (folder/'next-index.html').write_text(_next_html(previous['latest.json']['version']),encoding='utf-8')
    state = dict(version=APP_VERSION,source_sha=context(),notes=notes,assets=assets)
    (folder/'state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Verified builds and archived assets; update indices remain unchanged.',flush=True)


def _state():
    value = json.loads((work()/'state.json').read_text(encoding='utf-8'))
    if value.get('version') != APP_VERSION or value.get('source_sha') != context():
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
    create_release(root=ROOT,installer=setup,version=APP_VERSION,notes=state['notes'],
        download_url=BASE+'/downloads/'+setup.name,verification=win_report,output=indices/'latest.json',
        artifacts=folder.parent/'flickdock-private-release-artifacts',
        previous=previous('latest.json'),allowed_hosts=RELEASE_HOSTS,signer=signer)
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
        value = json.loads(path.read_text(encoding='utf-8'))
        verify_manifest_signature(value,required=True)
        shutil.copyfile(path,destination/name)
    print('Verified public installer bytes and signed update indices.',flush=True)


def verify():
    state = _state()
    for name in ('latest.json','macos-arm64.json','macos-arm64-auto.json','macos-arm64-auto-v2.json'):
        value,raw = _public_document(BASE + '/updates/' + name)
        if raw != (ROOT/'website'/'public'/'updates'/name).read_bytes():
            raise ValueError('The published update index differs from the signed index: ' + name)
        platform = 'win32' if name == 'latest.json' else 'darwin'
        validate_manifest(value,RELEASE_HOSTS,platform=platform,machine='arm64')
        if value.get('version') != state['version']:
            raise ValueError('The public update index is not the released version: ' + name)
    response = requests.get(BASE+'/',headers={'User-Agent':'FlickDock-Release/1','Cache-Control':'no-cache'},timeout=30)
    if response.status_code != 200:
        raise RuntimeError('The public website is unavailable.')
    for suffix in ('FlickDock-Setup-' + APP_VERSION + '.exe',
                   'FlickDock-' + APP_VERSION + '-macOS-arm64.pkg'):
        if '/v' + APP_VERSION + '/' + suffix not in response.text:
            raise ValueError('The website does not show the new download: ' + suffix)
    print('Verified signed public indices and website release links.',flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase',choices=('preflight','verify-signing','verify-gitee','verify-credentials','stage','sign','verify'))
    args = parser.parse_args()
    if args.phase == 'preflight':preflight(publish=True)
    elif args.phase == 'verify-signing':credentials(signing_only=True)
    elif args.phase == 'verify-gitee':credentials(gitee_only=True)
    elif args.phase == 'verify-credentials':credentials()
    elif args.phase == 'stage':stage()
    elif args.phase == 'sign':sign()
    else:verify()


if __name__ == '__main__':
    main()
