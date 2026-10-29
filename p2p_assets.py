"""Build-only native WebRTC assets, pinned npm integrity and official Node SHA."""
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
    npm=shutil.which('npm.cmd' if sys.platform=='win32' else 'npm')
    if not npm:raise RuntimeError('npm is required only on the build host')
    subprocess.run([npm,'ci','--ignore-scripts','--omit=dev','--no-audit','--no-fund'],cwd=folder,check=True)
    result=subprocess.run([str(node),'--input-type=module','-e',"import rtc from 'node-datachannel'; console.log(JSON.stringify({node:process.versions.node,rtc:rtc.getLibraryVersion()}))"],cwd=folder,capture_output=True,text=True,check=True)
    probe=json.loads(result.stdout);assert probe['node']==NODE_VERSION and probe['rtc']=='0.24.6',probe
    records=[]
    for path,destination in selected():records.append(dict(path=str(path.relative_to(ROOT)).replace('\\','/'),bytes=path.stat().st_size,sha256=checksum(path)))
    (folder/'asset-manifest.json').write_text(json.dumps(dict(node=NODE_VERSION,rtc=probe['rtc'],platform=package,files=records),indent=2),encoding='utf8')
    print(json.dumps(dict(ok=True,node=NODE_VERSION,rtc=probe['rtc'],platform=package,files=len(records))))

def selected():
    package,_=target();folder=ROOT/'p2p'
    names=['p2p/bridge.mjs','p2p/turn_tls.mjs','p2p/package.json','p2p/runtime/'+('node' if sys.platform=='darwin' else 'node.exe'),'p2p/runtime/Node-LICENSE.txt']
    for module in ('node-datachannel','detect-libc','@node-datachannel/'+package):
        base=folder/'node_modules'/module
        if not base.is_dir():raise FileNotFoundError('Missing native WebRTC package '+module)
        for path in base.rglob('*'):
            if path.is_file() and (path.suffix in ('.mjs','.cjs','.js','.json','.node') or path.name.upper().startswith(('LICENSE','COPYING'))):names.append(path.relative_to(ROOT).as_posix())
    result=[]
    for name in sorted(set(names)):
        path=ROOT/name
        if path.is_symlink() or not path.is_file():raise FileNotFoundError(name)
        result.append((path,path.parent.relative_to(ROOT).as_posix()))
    return result

def bundle_assets():
    manifest=ROOT/'p2p/asset-manifest.json'
    if not manifest.is_file():raise FileNotFoundError('Run p2p_assets.py before building')
    entries=selected();data=[];binaries=[]
    for path,dest in entries:
        (binaries if path.suffix in ('.exe','.node') or path.name=='node' else data).append((str(path),dest))
    data.append((str(manifest),'p2p'))
    return binaries,data

if __name__=='__main__':prepare()
