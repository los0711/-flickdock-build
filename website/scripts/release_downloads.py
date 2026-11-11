"""Installer archive registry and verified three-version R2 retention."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[2]
CATALOG=ROOT/'website/download-catalog.json'
BUCKET='flickdock-releases'
ACCOUNT='59153da0809ff3d18297d943e1ef5b56'
PUBLIC='https://download.flickdock.cc.cd'
KEEP_VERSIONS=3
MAX_CATALOG_BYTES=2*1024*1024
VERSION=re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')


def version(value):
    if not isinstance(value,str) or len(value)>32 or not VERSION.fullmatch(value):raise ValueError('Invalid release version')
    return tuple(map(int,value.split('.')))


def expected_names(value):
    return {'FlickDock-Setup-'+value+'.exe',*('FlickDock-'+value+'-macOS-arm64.'+ext for ext in ('pkg','zip','dmg'))}


def validate(value):
    if not isinstance(value,dict) or value.get('schema')!=1 or value.get('keepVersions')!=KEEP_VERSIONS or not isinstance(value.get('releases'),list):raise ValueError('Invalid installer catalog')
    seen=set()
    for row in value['releases']:
        key=row['version'];version(key)
        if key in seen or type(row.get('r2Removed')) is not bool:raise ValueError('Invalid release identity')
        seen.add(key);names=expected_names(key)
        if not isinstance(row['assets'],list) or len(row['assets'])!=len(names):raise ValueError('A release needs all four verified installers')
        for asset in row['assets']:
            name=asset['name']
            if name not in names:raise ValueError('Invalid installer name')
            names.remove(name);url=urlsplit(asset['archive'])
            gitee=url.netloc=='gitee.com' and re.fullmatch(r'/rooogerluo/flick-dock-(windows|macos)-releases-\d+/releases/download/v'+re.escape(key)+'/'+re.escape(name),url.path)
            github=url.netloc=='github.com' and url.path=='/los0711/-flickdock-build/releases/download/v'+key+'/'+name
            if url.scheme!='https' or url.query or url.fragment or not (gitee or github):
                raise ValueError('Installer archive must be an exact approved public release URL')
            if type(asset['bytes']) is not int or not 0<asset['bytes']<2*1024**3 or not re.fullmatch('[a-f0-9]{64}',asset['sha256']):raise ValueError('Invalid installer integrity metadata')
    return value


def _unique(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('Duplicate installer metadata key')
        result[key]=value
    return result


def read(path):
    with Path(path).open('rb') as stream:raw=stream.read(MAX_CATALOG_BYTES+1)
    if len(raw)>MAX_CATALOG_BYTES:raise ValueError('Installer catalog too large')
    return json.loads(raw,object_pairs_hook=_unique)


def write(path,value):
    raw=(json.dumps(validate(value),indent=2)+'\n').encode()
    if len(raw)>MAX_CATALOG_BYTES:raise ValueError('Installer catalog too large')
    path=Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as file:file.write(raw);temporary=Path(file.name)
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def plan(value):
    rows=sorted(validate(value)['releases'],key=lambda row:version(row['version']),reverse=True)
    hot=rows[:KEEP_VERSIONS]
    if any(row['r2Removed'] for row in hot):raise ValueError('Retained release was already removed from R2')
    old=[row for row in rows[KEEP_VERSIONS:] if not row['r2Removed']]
    return hot,old


def register(catalog,groups):
    assets=[dict(name=a['name'],bytes=a['bytes'],sha256=a['sha256'],archive=a['url']) for group in groups for a in group['assets']]
    versions=set()
    for asset in assets:
        match=re.fullmatch(r'FlickDock-(?:Setup-)?(\d+\.\d+\.\d+)(?:-macOS-arm64)?\.(?:exe|pkg|zip|dmg)',asset['name'])
        if not match:raise ValueError('Invalid installer filename')
        versions.add(match[1])
    if len(versions)!=1:raise ValueError('Register exactly one complete release')
    key=versions.pop();candidate=dict(version=key,assets=assets,r2Removed=False)
    result=json.loads(json.dumps(catalog));existing=next((row for row in result['releases'] if row['version']==key),None)
    if existing is not None:
        if sorted(existing['assets'],key=lambda a:a['name'])!=sorted(assets,key=lambda a:a['name']):raise ValueError('Published installers are immutable')
    else:
        if result['releases'] and version(key)<=max(version(row['version']) for row in result['releases']):raise ValueError('New release must be newer than the published catalog')
        result['releases'].append(candidate)
    validate(result);plan(result);return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def verify_redirect(row,asset):
    request=Request(PUBLIC+'/v'+row['version']+'/'+asset['name'],method='HEAD',headers={'User-Agent':'FlickDock-Retention','Cache-Control':'no-cache'})
    try:
        with build_opener(NoRedirect()).open(request,timeout=30):raise ValueError('Old installer URL has not switched to its archive')
    except HTTPError as error:
        if error.code!=302 or error.headers.get('Location')!=asset['archive']:raise ValueError('Old installer redirect does not match the verified archive') from error


def verify_archive(asset):
    sys.path.insert(0,str(ROOT))
    from release_manifest import verify_public_file,_NoRedirect
    from urllib.request import ProxyHandler
    hosts={'gitee.com','foruda.gitee.com','raw.giteeusercontent.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
    opener=build_opener(_NoRedirect()) if urlsplit(asset['archive']).hostname=='github.com' else build_opener(ProxyHandler({}),_NoRedirect())
    verify_public_file(asset['archive'],asset['sha256'],asset['bytes'],hosts,
                       pe=asset['name'].endswith('.exe'),opener=opener)


def prune(value,*,check_redirect,check_archive,delete,save):
    hot,old=plan(value)
    # Verify every fallback before deleting any object, not midway through cleanup.
    for row in old:
        for asset in row['assets']:check_redirect(row,asset);check_archive(asset)
    deleted=[]
    for row in old:
        for asset in row['assets']:
            key='v'+row['version']+'/'+asset['name'];delete(key);deleted.append(key)
        row['r2Removed']=True;save(value)
    return dict(ok=True,keepVersions=KEEP_VERSIONS,retained=[row['version'] for row in hot],removed=deleted)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('plan','register','prune'));parser.add_argument('--catalog',type=Path,default=CATALOG)
    parser.add_argument('--assets',type=Path);parser.add_argument('--node');parser.add_argument('--wrangler',type=Path);args=parser.parse_args()
    value=validate(read(args.catalog))
    if args.action=='register':
        if args.assets is None:parser.error('register requires --assets')
        value=register(value,read(args.assets));write(args.catalog,value)
    if args.action!='prune':
        hot,old=plan(value);print(json.dumps(dict(keepVersions=KEEP_VERSIONS,retained=[r['version'] for r in hot],retire=[r['version'] for r in old])));return 0
    node=args.node or str(ROOT/('p2p/runtime/node.exe' if os.name=='nt' else 'p2p/runtime/node'))
    if not Path(node).is_file():node=shutil.which('node')
    wrangler=args.wrangler or ROOT/'cloud/node_modules/wrangler/bin/wrangler.js'
    if not node or not wrangler.is_file():raise ValueError('Install the project Node/Wrangler dependencies before pruning')
    config=ROOT/'website/downloads.wrangler.jsonc';settings=read(config)
    if settings['account_id']!=ACCOUNT or settings['name']!='flickdock-downloads':raise ValueError('Unexpected installer distribution account')
    def delete(key):
        subprocess.run([node,str(wrangler),'r2','object','delete',BUCKET+'/'+key,'--remote','--config',str(config)],
                       check=True,cwd=ROOT,env=dict(os.environ,CLOUDFLARE_ACCOUNT_ID=ACCOUNT),timeout=90)
    result=prune(value,check_redirect=verify_redirect,check_archive=verify_archive,delete=delete,save=lambda data:write(args.catalog,data))
    print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())
