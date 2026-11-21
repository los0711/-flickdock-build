"""Synchronize the reviewed release source without overwriting newer work."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

import requests

from app_version import APP_VERSION
from release_manifest import collect_source_files, source_archive
from release_pipeline import ROOT, REPO, WINDOWS_INDICES, context, main_head, release_indices


GITEE_URL = 'https://gitee.com/rooogerluo/flick-dock.git'
GITHUB_API = 'https://api.github.com/repos/' + REPO
WINDOWS_PUBLICATION_FILES = {
    '.github/workflows/central-release.yml', 'release_pipeline.py', 'release_source_sync.py',
    'test_release_pipeline.py', 'test_release_source_sync.py', 'test_ui_feedback_v1815.py',
    'test_feedback_v1818.py', 'website/scripts/release_downloads.py',
    'website/test/test_release_downloads.py', 'website/download-catalog.json', 'website/public/index.html',
}


def _safe_relative(name):
    if not isinstance(name,str) or not name or '\\' in name:
        raise ValueError('Invalid source inventory path.')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        raise ValueError('Unsafe source inventory path.')
    return Path(*path.parts)


def _checked_path(root, name):
    root = Path(root).resolve()
    path = root / _safe_relative(name)
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, 'is_junction', lambda:False)():
            raise ValueError('A release source path is a link: ' + name)
    if not path.resolve().is_relative_to(root):
        raise ValueError('A release source path leaves the repository.')
    return path


def verify_baseline(root, manifest):
    if (not isinstance(manifest,dict) or manifest.get('schema_version') != 1
            or manifest.get('product') != 'FlickDock' or not isinstance(manifest.get('files'),list)
            or not manifest['files']):
        raise ValueError('The previous source inventory is invalid.')
    seen = set()
    for row in manifest['files']:
        name = row.get('path') if isinstance(row,dict) else None
        if name in seen:
            raise ValueError('The previous source inventory contains duplicates.')
        seen.add(name)
        path = _checked_path(root,name)
        if not path.is_file():
            raise ValueError('Private Gitee source differs from the previous release: ' + name)
        data = path.read_bytes()
        if len(data) != row.get('size') or hashlib.sha256(data).hexdigest() != row.get('sha256'):
            raise ValueError('Private Gitee source differs from the previous release: ' + name)
    return seen


def verify_new_source_paths(root, baseline_paths, selected):
    for name,data in selected:
        if name in baseline_paths:
            continue
        path = _checked_path(root,name)
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise ValueError('Private Gitee has a different newly added source: ' + name)


def _git_environment(token):
    if not token:
        raise ValueError('Gitee release authorization is missing.')
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('GIT_TRACE','GIT_CURL_')) or key in (
                'GITHUB_TOKEN','GH_TOKEN','GITEE_RELEASE_TOKEN','CLOUDFLARE_API_TOKEN',
                'FLICKDOCK_RELEASE_SIGNING_KEY'):
            env.pop(key,None)
    encoded = base64.b64encode(('rooogerluo:' + token).encode()).decode('ascii')
    env.update(GIT_TERMINAL_PROMPT='0',GIT_CONFIG_COUNT='1',
               GIT_CONFIG_KEY_0='http.https://gitee.com/.extraheader',
               GIT_CONFIG_VALUE_0='Authorization: Basic ' + encoded)
    return env


def _git(args, *, cwd, env, allow_absent=False):
    result = subprocess.run(['git','-c','credential.helper=','-c','core.autocrlf=false',*args],cwd=cwd,env=env,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    if result.returncode and not (allow_absent and result.returncode == 2):
        raise RuntimeError('Private source synchronization failed at git ' + args[0] + '.')
    return result


def _api(client, method, path, **kwargs):
    response = client.request(method,GITHUB_API + path,timeout=45,**kwargs)
    if response.status_code not in (200,201):
        raise RuntimeError('GitHub source update failed with HTTP ' + str(response.status_code) + '.')
    return response.json()


def _commit_github(token, expected, files):
    client = requests.Session()
    client.headers.update({'Authorization':'Bearer ' + token,'Accept':'application/vnd.github+json'})
    if main_head(token) != expected:
        raise RuntimeError('The GitHub main branch changed; source publication was stopped.')
    commit = _api(client,'GET','/git/commits/' + expected)
    entries = []
    for name,data in sorted(files.items()):
        _safe_relative(name)
        content = base64.b64encode(data).decode('ascii')
        blob = _api(client,'POST','/git/blobs',json={'content':content,'encoding':'base64'})
        git_sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if blob.get('sha') != git_sha:
            raise ValueError('GitHub returned the wrong source blob hash: ' + name)
        entries.append({'path':name,'mode':'100644','type':'blob','sha':git_sha})
    tree = _api(client,'POST','/git/trees',json={'base_tree':commit['tree']['sha'],'tree':entries})
    result = _api(client,'POST','/git/commits',json={'message':'Record FlickDock ' + APP_VERSION + ' release',
        'parents':[expected],'tree':tree['sha']})
    if main_head(token) != expected:
        raise RuntimeError('The GitHub main branch changed; source publication was stopped.')
    _api(client,'PATCH','/git/refs/heads/main',json={'sha':result['sha'],'force':False})
    return result['sha']


def _checkout_baseline(parent, selected, env):
    root = Path(parent)/'source'
    _git(['clone','--quiet','--depth','1','--single-branch','--branch','master',GITEE_URL,str(root)],cwd=parent,env=env)
    baseline = json.loads((ROOT/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    verify_new_source_paths(root,verify_baseline(root,baseline),selected)
    return root


def verify_tagged_windows_source(root, baseline, selected):
    if baseline.get('version') != APP_VERSION:
        raise ValueError('The reviewed source inventory does not match the tagged release version.')
    paths = verify_baseline(root,baseline)
    current = dict(selected)
    if (paths ^ current.keys()) - WINDOWS_PUBLICATION_FILES:
        raise ValueError('The Windows build inventory differs from the already released source tag.')
    for name in sorted(paths - WINDOWS_PUBLICATION_FILES):
        if _checked_path(root,name).read_bytes() != current[name]:
            raise ValueError('The Windows build changes a file in the already released source tag: ' + name)


def _verify_windows_source(parent, selected, env):
    root = Path(parent)/'source'
    tag = 'v' + APP_VERSION
    _git(['clone','--quiet','--depth','1','--branch',tag,GITEE_URL,str(root)],cwd=parent,env=env)
    _git(['show-ref','--verify','refs/tags/' + tag],cwd=root,env=env)
    baseline = json.loads((ROOT/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    verify_tagged_windows_source(root,baseline,selected)


def _record_windows_release(expected, github_token):
    env = _git_environment(os.environ.get('GITEE_RELEASE_TOKEN',''))
    with tempfile.TemporaryDirectory(prefix='flickdock-source-',dir=os.environ['RUNNER_TEMP']) as parent:
        _verify_windows_source(parent,collect_source_files(ROOT),env)
    website = ROOT/'website'/'public'
    head = _commit_github(github_token,expected,{
        'website/download-catalog.json':(ROOT/'website'/'download-catalog.json').read_bytes(),
        'website/public/index.html':(website/'index.html').read_bytes(),
        'updates/latest.json':(website/'updates'/'latest.json').read_bytes(),
    })
    print('Recorded the Windows release against the existing private source tag:', head,flush=True)


def synchronize():
    expected = context()
    github_token = os.environ.get('GITHUB_TOKEN','')
    if not github_token or main_head(github_token) != expected:
        raise RuntimeError('The GitHub main branch changed; start a fresh release.')
    if release_indices() == WINDOWS_INDICES:
        _record_windows_release(expected,github_token)
        return
    selected = collect_source_files(ROOT)
    _,manifest = source_archive(ROOT,APP_VERSION)
    env = _git_environment(os.environ.get('GITEE_RELEASE_TOKEN',''))
    with tempfile.TemporaryDirectory(prefix='flickdock-source-',dir=os.environ['RUNNER_TEMP']) as parent:
        root = _checkout_baseline(parent,selected,env)
        tag = 'v' + APP_VERSION
        existing = _git(['ls-remote','--exit-code','--tags','origin','refs/tags/' + tag],
                        cwd=root,env=env,allow_absent=True)
        if existing.returncode == 0:
            raise ValueError('The private release version tag already exists.')
        for name,data in selected:
            path = _checked_path(root,name)
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(data)
        names = [name for name,_ in selected]
        for offset in range(0,len(names),50):
            _git(['add','--force','--',*names[offset:offset+50]],cwd=root,env=env)
        _git(['-c','user.name=FlickDock Release','-c','user.email=release@flickdock.cc.cd',
              'commit','--quiet','-m','Release FlickDock ' + APP_VERSION],cwd=root,env=env)
        _git(['tag',tag],cwd=root,env=env)
        if main_head(github_token) != expected:
            raise RuntimeError('The GitHub main branch changed; source publication was stopped.')
        _git(['push','--quiet','origin','HEAD:refs/heads/master'],cwd=root,env=env)
        _git(['push','--quiet','origin','refs/tags/' + tag],cwd=root,env=env)
    website = ROOT/'website'/'public'
    files = {'SOURCE_MANIFEST.json':manifest,
             'website/download-catalog.json':(ROOT/'website/download-catalog.json').read_bytes(),
             'website/public/index.html':(website/'index.html').read_bytes()}
    for name in ('latest.json','macos-arm64.json','macos-arm64-auto.json','macos-arm64-auto-v2.json'):
        files['updates/' + name] = (website/'updates'/name).read_bytes()
    head = _commit_github(github_token,expected,files)
    print('Synchronized private source and recorded the reviewed public release metadata:', head,flush=True)


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-baseline',action='store_true')
    if parser.parse_args().verify_baseline:
        context()
        selected=collect_source_files(ROOT)
        env=_git_environment(os.environ.get('GITEE_RELEASE_TOKEN',''))
        with tempfile.TemporaryDirectory(prefix='flickdock-source-preflight-',dir=os.environ['RUNNER_TEMP']) as parent:
            if release_indices() == WINDOWS_INDICES:
                _verify_windows_source(parent,selected,env)
                print('Verified the existing private source tag for the Windows build.',flush=True)
            else:
                root=_checkout_baseline(parent,selected,env)
                tag='refs/tags/v'+APP_VERSION
                if _git(['ls-remote','--exit-code','--tags','origin',tag],cwd=root,env=env,allow_absent=True).returncode==0:
                    raise ValueError('The private release version tag already exists.')
                print('Verified the private source baseline and unused release tag.',flush=True)
    else:
        synchronize()
