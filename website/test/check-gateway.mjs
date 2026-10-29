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
assert.equal((await gateway.fetch(new Request('https://site.invalid/v1/auth/login',{method:'PATCH'}),env)).status,405);
assert.equal(await (await gateway.fetch(new Request('https://site.invalid/untrusted-proxy?url=https://example.invalid'),env)).text(),'asset');
assert.equal(requests.length,3);console.log('PASS fixed binding/original client IP, private header stripping, streamed PUT/full SHA, query/WS101 preservation, route boundaries');
