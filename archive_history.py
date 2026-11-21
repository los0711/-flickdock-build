"""Inventory the publicly released installer archives before migrating them."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re

import requests


GITEE_API = 'https://gitee.com/api/v5'
OWNER = 'rooogerluo'
REPOSITORY = re.compile(r'flick-dock-(?:(?:windows|macos)-)?releases(?:-[0-9]+)?\Z')
VERSION_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


def _gitee_list(path, token, *, paginate=True):
    values = []
    for page in range(1, 51):
        response = requests.get(GITEE_API + path, params={'per_page':100,'page':page} if paginate else None,
            headers={'Authorization':'Bearer ' + token,'Accept':'application/json'},
            timeout=(15,45),allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f'Gitee inventory request failed with HTTP {response.status_code}: {path}')
        rows = response.json()
        if not isinstance(rows,list):
            raise ValueError('Unexpected Gitee inventory response: ' + path)
        values.extend(rows)
        if not paginate or len(rows)<100:
            return values
    raise ValueError('Gitee public release inventory exceeded the page limit.')


def inventory(token):
    repositories = _gitee_list('/users/' + OWNER + '/repos',token)
    names = sorted(row['path'] for row in repositories if row.get('private') is False
                   and (row.get('namespace') or {}).get('path') == OWNER
                   and REPOSITORY.fullmatch(row.get('path','')))
    if 'flick-dock-macos-releases-12' not in names or 'flick-dock-windows-releases-3' not in names:
        raise ValueError('Expected public release repositories are missing from Gitee.')

    def repo_releases(name):
        root = f'/repos/{OWNER}/{name}/releases'
        result = []
        for release in _gitee_list(root,token):
            tag = release.get('tag_name','')
            if not VERSION_TAG.fullmatch(tag) or release.get('prerelease'):
                continue
            release_id = int(release['id'])
            assets = _gitee_list(f'{root}/{release_id}/attach_files',token,paginate=False)
            result.append(dict(repo=name,tag=tag,files=[
                dict(name=asset['name'],size=asset['size']) for asset in assets]))
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for group in pool.map(repo_releases,names) for row in group]
    rows.sort(key=lambda row: (tuple(map(int,row['tag'][1:].split('.'))),row['repo']))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    token = os.environ.get('GITEE_RELEASE_TOKEN','')
    if not token:
        raise ValueError('The Gitee read credential is missing.')
    rows = inventory(token)
    args.output.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Inventoried {len(rows)} public releases with {sum(len(row["files"]) for row in rows)} attachments.')


if __name__ == '__main__':
    main()
