"""Build-only original Node runtime for the video parser's JavaScript engine."""
from pathlib import Path
import hashlib,json,os,platform,shutil,subprocess,sys,tarfile,urllib.request,zipfile
ROOT=Path(__file__).resolve().parent
NODE_VERSION='24.12.0'

def checksum(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def target():
    arch='arm64' if platform.machine().lower() in ('arm64','aarch64') else 'x64'
    return ('darwin-'+arch if sys.platform=='darwin' else 'win32-'+arch+'-msvc'),arch

def prepare():
    package,arch=target();folder=ROOT/'p2p';runtime=folder/'runtime';runtime.mkdir(exist_ok=True)
    filename=f'node-v{NODE_VERSION}-'+('darwin-'+arch+'.tar.gz' if sys.platform=='darwin' else 'win-'+arch+'.zip')
    hashes=json.loads((folder/'node-shasums.json').read_text())
    expected=hashes[filename];cache=ROOT/'build/p2p-cache';cache.mkdir(parents=True,exist_ok=True);archive=cache/filename
    if not archive.exists():
        with urllib.request.urlopen(f'https://nodejs.org/dist/v{NODE_VERSION}/'+filename,timeout=120) as response,archive.open('xb') as output:shutil.copyfileobj(response,output)
    if checksum(archive)!=expected:raise ValueError('Official Node runtime SHA256 mismatch')
    node=runtime/('node' if sys.platform=='darwin' else 'node.exe')
    prefix=filename.removesuffix('.tar.gz').removesuffix('.zip')
    if sys.platform=='darwin':
        with tarfile.open(archive) as source:
            for member,out in ((prefix+'/bin/node',node),(prefix+'/LICENSE',runtime/'Node-LICENSE.txt')):
                with source.extractfile(member) as stream,out.open('wb') as output:shutil.copyfileobj(stream,output)
        node.chmod(0o755)
    else:
        with zipfile.ZipFile(archive) as source:
            for member,out in ((prefix+'/node.exe',node),(prefix+'/LICENSE',runtime/'Node-LICENSE.txt')):out.write_bytes(source.read(member))
    result=subprocess.run([str(node),'--version'],cwd=folder,capture_output=True,text=True,check=True)
    if result.stdout.strip()!='v'+NODE_VERSION:raise ValueError('Original Node version mismatch')
    records=[]
    for path,destination in selected():records.append(dict(path=str(path.relative_to(ROOT)).replace('\\','/'),bytes=path.stat().st_size,sha256=checksum(path)))
    (folder/'asset-manifest.json').write_text(json.dumps(dict(node=NODE_VERSION,platform=package,files=records),indent=2),encoding='utf8')
    print(json.dumps(dict(ok=True,node=NODE_VERSION,platform=package,files=len(records))))

def selected():
    names=['p2p/runtime/'+('node' if sys.platform=='darwin' else 'node.exe'),'p2p/runtime/Node-LICENSE.txt']
    result=[]
    for name in sorted(set(names)):
        path=ROOT/name
        if path.is_symlink() or not path.is_file():raise FileNotFoundError(name)
        result.append((path,path.parent.relative_to(ROOT).as_posix()))
    return result

def bundle_assets():
    manifest=ROOT/'p2p/asset-manifest.json'
    if not manifest.is_file():raise FileNotFoundError('Run p2p_assets.py before building')
    value=json.loads(manifest.read_bytes());entries=selected()
    if value.get('node')!=NODE_VERSION or value.get('platform')!=target()[0]:raise ValueError('Node assets do not match the build target')
    records={row['path']:row for row in value['files']}
    if set(records)!={path.relative_to(ROOT).as_posix() for path,dest in entries}:raise ValueError('Node asset list differs')
    data=[];binaries=[]
    for path,dest in entries:
        row=records[path.relative_to(ROOT).as_posix()]
        if path.stat().st_size!=row['bytes'] or checksum(path)!=row['sha256']:raise ValueError('Node asset checksum mismatch')
        (binaries if path.suffix in ('.exe','.node') or path.name=='node' else data).append((str(path),dest))
    data.append((str(manifest),'p2p'))
    return binaries,data

if __name__=='__main__':prepare()
