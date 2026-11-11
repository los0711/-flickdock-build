import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import gateway from '../worker.mjs';
const requests=[],payloads=[];
const upgraded={status:101,webSocket:{fixture:true}};
const env={WAN_SERVICE:{async fetch(request){requests.push(request);payloads.push(new Uint8Array(await request.arrayBuffer()));return request.headers.get('Upgrade')==='websocket'?upgraded:new Response('ok');}},ASSETS:{async fetch(){return new Response('asset');}}};
for(const path of ['/v1/wan/connect','/v1/wan/health']){
  const headers={Authorization:'Bearer '+('a'.repeat(64)),Cookie:'private-cookie',Referer:'private-referrer','CF-Connecting-IP':'192.0.2.1','X-FlickDock-Client-IP':'forged'};
  if(path.endsWith('connect'))headers.Upgrade='websocket';
  const result=await gateway.fetch(new Request('https://flickdock-site.roger000416.workers.dev'+path,{headers}),env);
  if(path.endsWith('connect'))assert.equal(result,upgraded,'101 and attached WebSocket must pass through untouched');
  const request=requests.at(-1);assert.equal(request.url,'https://flickdock-cloud.roger000416.workers.dev'+path);
  assert.equal(request.headers.get('Authorization'),headers.Authorization);assert.equal(request.headers.get('Cookie'),null);assert.equal(request.headers.get('Referer'),null);
  assert.equal(request.headers.get('X-FlickDock-Client-IP'),'192.0.2.1');
}
const bytes=crypto.getRandomValues(new Uint8Array(65536)),chunks=32;
const body=new ReadableStream({start(controller){for(let i=0;i<chunks;i++)controller.enqueue(bytes);controller.close();}});
await gateway.fetch(new Request('https://site.invalid/v1/uploads/fixture/parts/0?version=7',{method:'PUT',headers:{'CF-Connecting-IP':'2001:db8::2','Content-Length':String(bytes.length*chunks)},body,duplex:'half'}),env);
assert.equal(requests.at(-1).method,'PUT');assert.equal(new URL(requests.at(-1).url).search,'?version=7');
const sha=value=>createHash('sha256').update(value).digest('hex');assert.equal(sha(payloads.at(-1)),sha(Buffer.concat(Array.from({length:chunks},()=>bytes))));
assert.equal((await gateway.fetch(new Request('https://site.invalid/v1/wan/connect'),env)).status,400);
await gateway.fetch(new Request('https://site.invalid/v1/devices/fixture',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'Synthetic QA device'})}),env);
assert.equal(requests.at(-1).method,'PATCH');assert.deepEqual(JSON.parse(new TextDecoder().decode(payloads.at(-1))),{name:'Synthetic QA device'});
assert.equal((await gateway.fetch(new Request('https://site.invalid/v1/auth/login',{method:'PROPFIND'}),env)).status,405);
assert.equal(await (await gateway.fetch(new Request('https://site.invalid/untrusted-proxy?url=https://example.invalid'),env)).text(),'asset');
assert.equal(requests.length,4);console.log('PASS fixed binding/original client IP, private header stripping, streamed PUT/full SHA, PATCH device updates, query/WS101 preservation, route boundaries');

const {installerDownload,releaseIndex,ORIGIN_HOST}=await import('../downloads.mjs');
const {default:catalog}=await import('../download-catalog.json',{with:{type:'json'}});
const {default:downloadWorker}=await import('../download-worker.mjs');
const fixture={schema:1,keepVersions:3,releases:[30,31,32,33,34].map(patch=>{
  const version='1.8.'+patch,names=['FlickDock-Setup-'+version+'.exe',...['pkg','zip','dmg'].map(ext=>'FlickDock-'+version+'-macOS-arm64.'+ext)];
  return {version,r2Removed:false,assets:names.map(name=>({name,bytes:100,sha256:'a'.repeat(64),archive:'https://gitee.com/rooogerluo/flick-dock-windows-releases-2/releases/download/v'+version+'/'+name}))};
})};
const index=releaseIndex(fixture);let originCalls=0;
for(const patch of [30,31]){
  const name='FlickDock-Setup-1.8.'+patch+'.exe';
  const result=await installerDownload(new Request('https://download.flickdock.cc.cd/v1.8.'+patch+'/'+name),name,{index,fetcher:()=>{throw new Error('Archived files must not fetch R2');}});
  assert.equal(result.status,302);assert.equal(result.headers.get('Location'),index.get(name).archive);
}
for(const patch of [32,33,34]){
  const name='FlickDock-Setup-1.8.'+patch+'.exe';
  const result=await installerDownload(new Request('https://download.flickdock.cc.cd/v1.8.'+patch+'/'+name,{headers:{Range:'bytes=0-3',Authorization:'private',Cookie:'private'}}),name,{index,fetcher:async(url,options)=>{
    originCalls++;assert.equal(new URL(url).hostname,ORIGIN_HOST);assert.equal(options.headers.get('Range'),'bytes=0-3');
    assert.equal(options.headers.get('Authorization'),null);assert.equal(options.headers.get('Cookie'),null);
    return new Response(new Uint8Array([77,90,1,2]),{status:206,headers:{'Content-Range':'bytes 0-3/100','Content-Length':'4'}});
  }});
  assert.equal(result.status,206);assert.deepEqual(new Uint8Array(await result.arrayBuffer()),new Uint8Array([77,90,1,2]));
}
assert.equal(originCalls,3);
assert.equal((await installerDownload(new Request('https://download.flickdock.cc.cd/x',{method:'POST'}),'FlickDock-Setup-1.8.34.exe',{index})).status,405);
assert.equal((await downloadWorker.fetch(new Request('https://download.flickdock.cc.cd/v1.8.99/FlickDock-Setup-1.8.32.exe'))).status,404);
const bad=structuredClone(fixture);bad.releases[0].assets[0].archive='https://evil.invalid/file';assert.throws(()=>releaseIndex(bad));
const previousFetch=globalThis.fetch;globalThis.fetch=async(url,options)=>{
  const row=catalog.releases.find(row=>row.version==='1.8.33'),asset=row.assets.find(asset=>asset.name.endsWith('.exe'));
  assert.equal(new URL(url).hostname,ORIGIN_HOST);assert.equal(options.method,'HEAD');
  return new Response(null,{headers:{'Content-Length':String(asset.bytes)}});
};
try{assert.equal((await gateway.fetch(new Request('https://flickdock.cc.cd/downloads/FlickDock-Setup-1.8.33.exe',{method:'HEAD'}),env)).status,200);}
finally{globalThis.fetch=previousFetch;}
console.log('PASS three newest versions use cached R2; older URLs redirect to exact archives; Range, HEAD, private-header isolation and malformed metadata');
