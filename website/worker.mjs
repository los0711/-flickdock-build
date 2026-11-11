// Installer bytes live in the dedicated public R2 bucket, never user storage.
const INSTALLER_HEADER_TIMEOUT_MS=20000;
const MAX_INSTALLER_BYTES=2*1024**3;
async function installerDownload(request,path){
  const match=path.match(/^\/downloads\/(FlickDock-(?:Setup-)?((?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d{0,2}))(?:-macOS-arm64)?\.(?:exe|pkg|zip|dmg))$/);
  if(!match)return new Response('Not found',{status:404});
  if(!/^FlickDock-Setup-[\d.]+\.exe$|^FlickDock-[\d.]+-macOS-arm64\.(?:pkg|zip|dmg)$/.test(match[1]))return new Response('Not found',{status:404});
  if(!['GET','HEAD'].includes(request.method))return new Response('Method not allowed',{status:405,headers:{Allow:'GET, HEAD'}});
  const headers=new Headers({Accept:'application/octet-stream'}),range=request.headers.get('Range');
  if(range){if(!/^bytes=(?:\d+-\d*|-\d+)$/.test(range))return new Response('Invalid range',{status:416});headers.set('Range',range);}
  const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),INSTALLER_HEADER_TIMEOUT_MS);
  let upstream;
  try{upstream=await fetch('https://download.flickdock.cc.cd/v'+match[2]+'/'+match[1],
    {method:request.method,headers,redirect:'follow',signal:controller.signal});}
  catch(error){console.error('Installer upstream failed',error);return new Response('Download temporarily unavailable',{status:502});}
  finally{clearTimeout(deadline);}
  if(![200,206,416].includes(upstream.status)){
    await upstream.body?.cancel();return new Response('Download unavailable',{status:upstream.status===404?404:502});
  }
  const length=upstream.headers.get('Content-Length'),type=upstream.headers.get('Content-Type')||'';
  if((length!==null&&(!/^\d+$/.test(length)||Number(length)>MAX_INSTALLER_BYTES))||type.toLowerCase().startsWith('text/')){
    await upstream.body?.cancel();return new Response('Invalid download response',{status:502});
  }
  const output=new Headers({'Content-Type':match[1].endsWith('.dmg')?'application/x-apple-diskimage':match[1].endsWith('.zip')?'application/zip':'application/octet-stream','X-Content-Type-Options':'nosniff',
    'Cache-Control':upstream.status===416?'no-store':'public, max-age=31536000, immutable',
    'Content-Disposition':'attachment; filename="'+match[1]+'"'});
  for(const name of ['Content-Length','Content-Range','Accept-Ranges','ETag','Last-Modified']){
    const value=upstream.headers.get(name);if(value!==null)output.set(name,value);
  }
  return new Response(request.method==='HEAD'?null:upstream.body,{status:upstream.status,headers:output});
}
// Verified alternative cloud entrance. Transfer payloads remain peer to peer.
const backend='https://flickdock-cloud.roger000416.workers.dev';
export default {
  async fetch(request,env){
    const path=new URL(request.url).pathname;
    if(path.startsWith('/downloads/'))return installerDownload(request,path);
    if(path.startsWith('/v1/')){
      if(!['GET','POST','PUT','PATCH','DELETE','OPTIONS'].includes(request.method))return new Response('Method not allowed',{status:405});
      if(path==='/v1/wan/connect'&&request.headers.get('Upgrade')?.toLowerCase()!=='websocket')return new Response('WebSocket required',{status:400});
      // No arbitrary upstream URL, cookie forwarding or public proxy route.
      const headers=new Headers(request.headers);headers.delete('Cookie');headers.delete('Referer');
      headers.set('X-FlickDock-Client-IP',request.headers.get('CF-Connecting-IP')||'');
      return env.WAN_SERVICE.fetch(new Request(backend+path+new URL(request.url).search,{method:request.method,headers,body:['GET','HEAD'].includes(request.method)?undefined:request.body,duplex:'half'}));
    }
    return env.ASSETS.fetch(request);
  },
};
