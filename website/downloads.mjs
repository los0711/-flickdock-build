import catalog from './download-catalog.json' with {type:'json'};

export const DOWNLOAD_HOST='download.flickdock.cc.cd';
export const ORIGIN_HOST='download-origin.flickdock.cc.cd';
const HEADER_TIMEOUT_MS=20000;
const version=value=>value.split('.').map(Number);
const newest=(a,b)=>{const x=version(a.version),y=version(b.version);return y[0]-x[0]||y[1]-x[1]||y[2]-x[2];};

export function releaseIndex(value){
  if(value.schema!==1||value.keepVersions!==3||!Array.isArray(value.releases))throw new Error('Invalid installer catalog');
  const rows=[...value.releases].sort(newest),hot=new Set(rows.slice(0,value.keepVersions).map(row=>row.version));
  const index=new Map();
  for(const row of rows){
    if(!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(row.version))throw new Error('Invalid release version');
    const expected=new Set(['FlickDock-Setup-'+row.version+'.exe',...['pkg','zip','dmg'].map(ext=>'FlickDock-'+row.version+'-macOS-arm64.'+ext)]);
    if(!Array.isArray(row.assets)||row.assets.length!==expected.size)throw new Error('Incomplete installer release');
    for(const asset of row.assets){
      const url=new URL(asset.archive);
      if(!expected.delete(asset.name)||url.origin!=='https://gitee.com'||url.username||url.password||url.search||url.hash||!/^\/rooogerluo\/flick-dock-(?:windows|macos)-releases-\d+\/releases\/download\//.test(url.pathname)||!url.pathname.endsWith('/releases/download/v'+row.version+'/'+asset.name))throw new Error('Invalid installer archive');
      if(!Number.isSafeInteger(asset.bytes)||asset.bytes<=0||asset.bytes>2*1024**3||!/^([a-f0-9]{64})$/.test(asset.sha256)||index.has(asset.name))throw new Error('Invalid installer metadata');
      index.set(asset.name,{...asset,version:row.version,hot:hot.has(row.version)});
    }
  }
  return index;
}
const releases=releaseIndex(catalog);

export async function installerDownload(request,name,{index=releases,fetcher=globalThis.fetch.bind(globalThis)}={}){
  const asset=index.get(name);
  if(!asset)return new Response('Not found',{status:404});
  if(!['GET','HEAD'].includes(request.method))return new Response('Method not allowed',{status:405,headers:{Allow:'GET, HEAD'}});
  if(!asset.hot)return new Response(null,{status:302,headers:{Location:asset.archive,'Cache-Control':'public, max-age=3600'}});
  const headers=new Headers({Accept:'application/octet-stream'}),range=request.headers.get('Range');
  if(range){if(!/^bytes=(?:\d+-\d*|-\d+)$/.test(range))return new Response('Invalid range',{status:416});headers.set('Range',range);}
  const ifRange=request.headers.get('If-Range');if(ifRange)headers.set('If-Range',ifRange);
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),HEADER_TIMEOUT_MS);
  let upstream;
  try{upstream=await fetcher('https://'+ORIGIN_HOST+'/v'+asset.version+'/'+asset.name,{method:request.method,headers,redirect:'manual',signal:controller.signal});}
  catch(error){console.error('Installer origin failed',error.name);return new Response('Download temporarily unavailable',{status:502});}
  finally{clearTimeout(timer);}
  if(![200,206,416].includes(upstream.status)){await upstream.body?.cancel();return new Response('Download unavailable',{status:upstream.status===404?404:502});}
  const length=upstream.headers.get('Content-Length'),type=upstream.headers.get('Content-Type')||'';
  const contentRange=upstream.headers.get('Content-Range'),part=contentRange?.match(/^bytes (\d+)-(\d+)\/(\d+)$/);
  if(type.toLowerCase().startsWith('text/')||(length!==null&&(!/^\d+$/.test(length)||Number(length)>asset.bytes))
      ||upstream.status===200&&length!==null&&Number(length)!==asset.bytes
      ||upstream.status===206&&(!part||Number(part[3])!==asset.bytes||Number(part[1])>Number(part[2])||Number(part[2])>=asset.bytes||length!==null&&Number(length)!==Number(part[2])-Number(part[1])+1)){
    await upstream.body?.cancel();return new Response('Invalid download response',{status:502});
  }
  const output=new Headers({'Content-Type':asset.name.endsWith('.dmg')?'application/x-apple-diskimage':asset.name.endsWith('.zip')?'application/zip':'application/octet-stream',
    'X-Content-Type-Options':'nosniff','Cache-Control':upstream.status===416?'no-store':'public, max-age=31536000, immutable',
    'Content-Disposition':'attachment; filename="'+asset.name+'"'});
  for(const key of ['Content-Length','Content-Range','Accept-Ranges','ETag','Last-Modified']){const value=upstream.headers.get(key);if(value!==null)output.set(key,value);}
  return new Response(request.method==='HEAD'?null:upstream.body,{status:upstream.status,headers:output});
}

export function installerName(path){
  const match=path.match(/^\/v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))\/(FlickDock-[A-Za-z0-9.-]+)$/);
  if(!match)return null;
  const asset=releases.get(match[2]);return asset&&asset.version===match[1]?asset.name:null;
}
