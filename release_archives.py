"""Publish immutable, anonymously verified release archives."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import requests

from website.scripts.release_downloads import verify_archive


GITEE = 'https://gitee.com/api/v5'
GITHUB = 'https://api.github.com/repos/los0711/-flickdock-build'
OWNER = 'rooogerluo'
ARCHIVE_REPOS = {'windows':'flick-dock-windows-releases-3',
                 'macos':'flick-dock-macos-releases-12'}
MAX_GITEE_BYTES = 100_000_000


def file_metadata(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError('Release asset must be a nonempty regular file.')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(name=path.name, bytes=path.stat().st_size, sha256=digest)


def _session(token):
    if not token:
        raise ValueError('Release archive authorization is missing.')
    client = requests.Session()
    client.headers.update({'Authorization':'Bearer ' + token,
                           'Accept':'application/json'})
    return client


def _check(response, operation):
    if response.status_code not in (200,201):
        raise RuntimeError(operation + ' failed with HTTP ' + str(response.status_code))
    return response.json()


def _gitee_release(client, kind, version, notes):
    repo = ARCHIVE_REPOS[kind]
    url = GITEE + '/repos/' + OWNER + '/' + repo
    info = _check(client.get(url, timeout=30), 'Gitee archive repository lookup')
    if info.get('private') is not False or (info.get('owner') or {}).get('login') != OWNER:
        raise ValueError('Unexpected Gitee archive repository.')
    response = client.get(url + '/releases/tags/v' + version, timeout=30)
    if response.status_code == 404 or (response.status_code == 200 and response.json() is None):
        response = client.post(url + '/releases', data={'tag_name':'v' + version,
            'target_commitish':info.get('default_branch') or 'master',
            'name':'FlickDock ' + version,'body':notes}, timeout=40)
    release = _check(response, 'Gitee release creation/lookup')
    if not isinstance(release,dict) or release.get('tag_name') != 'v' + version or not release.get('id'):
        raise ValueError('Unexpected Gitee release identity.')
    return repo, int(release['id'])


def _gitee_asset(client, path, metadata, kind, version, notes):
    repo, release_id = _gitee_release(client, kind, version, notes)
    endpoint = GITEE + '/repos/' + OWNER + '/' + repo + '/releases/' + str(release_id) + '/attach_files'
    existing = _check(client.get(endpoint, timeout=30), 'Gitee attachment lookup')
    matches = [item for item in existing if item.get('name') == metadata['name']]
    if len(matches) > 1:
        raise ValueError('Gitee release contains duplicate asset names.')
    stable = 'https://gitee.com/' + OWNER + '/' + repo + '/releases/download/v' + version + '/' + metadata['name']
    if not matches:
        with Path(path).open('rb') as stream:
            response = client.post(endpoint, files={'file':(metadata['name'],stream,'application/octet-stream')},
                                   timeout=(30,600))
        uploaded = _check(response, 'Gitee attachment upload')
        if uploaded.get('name') != metadata['name'] or uploaded.get('size') != metadata['bytes']:
            raise ValueError('Gitee attachment metadata differs from the built asset.')
    verify_archive(dict(metadata, archive=stable))
    return stable


def _github_asset(client, path, metadata, version, source_sha, notes):
    response = client.get(GITHUB + '/releases/tags/v' + version, timeout=30)
    if response.status_code == 404:
        tag = client.get(GITHUB + '/git/ref/tags/v' + version, timeout=30)
        if tag.status_code != 404:
            raise ValueError('An existing GitHub release tag cannot be replaced.')
        response = client.post(GITHUB + '/releases', json={'tag_name':'v' + version,
            'target_commitish':source_sha,'name':'FlickDock ' + version,
            'body':notes,'draft':False,'prerelease':False}, timeout=40)
    release = _check(response, 'GitHub release creation/lookup')
    if release.get('tag_name') != 'v' + version or release.get('draft') is not False:
        raise ValueError('Unexpected GitHub release identity.')
    tag = _check(client.get(GITHUB + '/git/ref/tags/v' + version, timeout=30), 'GitHub tag lookup')
    if (tag.get('object') or {}).get('sha') != source_sha:
        raise ValueError('An existing GitHub release tag points to a different source commit.')
    matches = [item for item in release.get('assets',[]) if item.get('name') == metadata['name']]
    if len(matches) > 1:
        raise ValueError('GitHub release contains duplicate asset names.')
    stable = ('https://github.com/los0711/-flickdock-build/releases/download/v'
              + version + '/' + metadata['name'])
    if not matches:
        upload = str(release.get('upload_url','')).split('{',1)[0]
        parts = urlsplit(upload)
        if (parts.scheme != 'https' or parts.hostname != 'uploads.github.com'
                or not parts.path.startswith('/repos/los0711/-flickdock-build/releases/')):
            raise ValueError('Unexpected GitHub release upload URL.')
        with Path(path).open('rb') as stream:
            response = client.post(upload, params={'name':metadata['name']}, data=stream,
                headers={'Content-Type':'application/octet-stream',
                         'Content-Length':str(metadata['bytes'])}, timeout=(30,600))
        uploaded = _check(response, 'GitHub attachment upload')
        if uploaded.get('name') != metadata['name'] or uploaded.get('size') != metadata['bytes']:
            raise ValueError('GitHub attachment metadata differs from the built asset.')
    verify_archive(dict(metadata, archive=stable))
    return stable


def publish_archives(paths, version, source_sha, notes, *, gitee_token, github_token):
    gitee, github = _session(gitee_token), _session(github_token)
    output = []
    for path in paths:
        metadata = file_metadata(path)
        if metadata['name'] == 'FlickDock-Setup-' + version + '.exe':
            kind = 'windows'
        elif metadata['name'] in {'FlickDock-' + version + '-macOS-arm64.' + ext for ext in ('pkg','dmg','zip')}:
            kind = 'macos'
        else:
            raise ValueError('Release asset name does not match the target version.')
        archive = (_gitee_asset(gitee,path,metadata,kind,version,notes)
                   if metadata['bytes'] < MAX_GITEE_BYTES
                   else _github_asset(github,path,metadata,version,source_sha,notes))
        output.append(dict(metadata, url=archive,
                           originKey='v' + version + '/sha256-' + metadata['sha256'] + '/' + metadata['name']))
        print('Verified release archive:', metadata['name'], flush=True)
    return output
