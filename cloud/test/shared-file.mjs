/** Independent real workerd/D1/R2 shared clipboard-file review; no deployment. */
import assert from 'node:assert/strict';
import {spawn,spawnSync} from 'node:child_process';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID,createHash,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import net from 'node:net';
import {request as httpRequest} from 'node:http';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const expectFixed=true;
const state=await mkdtemp(join(tmpdir(),'flickdock-shared-review-'));
const source=join(root,'src/index.ts').replaceAll('\\','/'),clipboard=join(root,'src/clipboard.ts').replaceAll('\\','/');
const wrapper=join(state,'review-wrapper.ts');
await writeFile(wrapper,`
import app,{AccountEvents as BaseEvents} from ${JSON.stringify(source)};
import {clipboardAlarm} from ${JSON.stringify(clipboard)};
export {WanHub,AppGateway} from ${JSON.stringify(source)};
export class AccountEvents extends BaseEvents {async fetch(request){const url=new URL(request.url);
 if(url.pathname==='/review/do')return Response.json(Object.fromEntries(await this.ctx.storage.list()));
 if(url.pathname==='/review/expire'){for(const [key,row] of await this.ctx.storage.list({prefix:'clipboard-hold:'}))await this.ctx.storage.put(key,{...row,expires:1});await clipboardAlarm(this.ctx,this.env);return Response.json({ok:true});}
 return super.fetch(request);
}}
export default {async fetch(request,env,ctx){const url=new URL(request.url);
 if(url.pathname==='/review/state'){
  const account=url.searchParams.get('account');
  const sql=async(text)=>{return (await env.DB.prepare(text).bind(account).all()).results};
  return Response.json({allocations:await sql('SELECT id,size FROM allocations WHERE account_id=?'),uploads:await sql('SELECT id,state,size,sha256 FROM uploads WHERE account_id=?'),refs:await sql('SELECT * FROM clipboard_upload_refs WHERE account_id=?'),items:await sql('SELECT id,active_upload,deleted,version,expires_at FROM items WHERE account_id=?'),objects:(await env.FILES.list()).objects.map(r=>({key:r.key,size:r.size})),do:await (await env.EVENTS.get(env.EVENTS.idFromName(account)).fetch('https://internal/review/do')).json()});
 }
 if(url.pathname==='/review/expire')return env.EVENTS.get(env.EVENTS.idFromName(url.searchParams.get('account'))).fetch('https://internal/review/expire');
 if(url.pathname==='/review/cleanup'){const tasks=[];await app.scheduled(null,env,{waitUntil(p){tasks.push(p)}});await Promise.all(tasks);return Response.json({ok:true});}
 return app.fetch(request,env,ctx);
}};`);
const config=JSON.parse(await readFile(join(root,'wrangler.jsonc'),'utf8'));config.main=wrapper;config.d1_databases[0].migrations_dir=join(root,'migrations');
const configFile=join(state,'wrangler-test.json');await writeFile(configFile,JSON.stringify(config));
const wrangler=join(root,'node_modules/wrangler/bin/wrangler.js');
const env={...process.env,WRANGLER_SEND_METRICS:'false',CLOUDFLARE_API_TOKEN:'',CLOUDFLARE_API_KEY:'',CLOUDFLARE_EMAIL:'',CLOUDFLARE_ACCOUNT_ID:''};
const migrated=spawnSync(process.execPath,[wrangler,'--config',configFile,'d1','migrations','apply','DB','--local','--persist-to',state],{cwd:state,env,windowsHide:true,encoding:'utf8',timeout:60000});assert.equal(migrated.status,0,migrated.stderr);
const listener=net.createServer();await new Promise(r=>listener.listen(0,'127.0.0.1',r));const port=listener.address().port;await new Promise(r=>listener.close(r));const base='http://127.0.0.1:'+port;
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
function zipFile(name,data){const filename=Buffer.from(name),crc=(()=>{let c=0xffffffff;for(const b of data){c^=b;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0)}return (c^0xffffffff)>>>0})();const local=Buffer.alloc(30);local.writeUInt32LE(0x04034b50);local.writeUInt16LE(20,4);local.writeUInt16LE(0x800,6);local.writeUInt32LE(crc,14);local.writeUInt32LE(data.length,18);local.writeUInt32LE(data.length,22);local.writeUInt16LE(filename.length,26);const central=Buffer.alloc(46);central.writeUInt32LE(0x02014b50);central.writeUInt16LE(20,4);central.writeUInt16LE(20,6);central.writeUInt16LE(0x800,8);central.writeUInt32LE(crc,16);central.writeUInt32LE(data.length,20);central.writeUInt32LE(data.length,24);central.writeUInt16LE(filename.length,28);const end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(1,8);end.writeUInt16LE(1,10);end.writeUInt32LE(central.length+filename.length,12);end.writeUInt32LE(local.length+filename.length+data.length,16);const prefix=Buffer.concat([local,filename]),suffix=Buffer.concat([central,filename,end]);return {prefix:prefix.toString('base64'),suffix:suffix.toString('base64'),archive:Buffer.concat([prefix,data,suffix])};}
let logs='',server,failure,sequence=0,copiedAt=Date.now();const checks=[],findings=[];
try{
 server=spawn(process.execPath,[wrangler,'dev','--config',configFile,'--local','--ip','127.0.0.1','--port',String(port),'--persist-to',state,'--var','AUTH_PEPPER:review-local-secret-at-least-32-characters'],{cwd:state,env,windowsHide:true,stdio:['ignore','pipe','pipe']});server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
 for(let i=0;i<150;i++){if(server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/v1/health')).ok)break}catch{}await sleep(250)}
 async function req(method,path,body,token,expected=200){const response=await fetch(base+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)});const value=await response.json();assert.equal(response.status,expected,JSON.stringify(value));return value;}
 async function sign(username,login=false){const challenge=await req('POST','/v1/auth/challenge',{username,purpose:login?'login':'register'});const auth_key=scryptSync('local-review-password',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');return req('POST','/v1/auth/'+(login?'login':'register'),{username,challenge_id:challenge.challenge_id,auth_key,device_name:login?'receiver':'sender',platform:login?'macos':'windows'},null,login?200:201);}
 const username='shared-review-'+randomUUID(),a=await sign(username),b=await sign(username,true),outsider=await sign('outsider-'+randomUUID());
 const snapshot=()=>req('GET','/review/state?account='+a.account.id);
 async function stage(id=randomUUID()){copiedAt=Math.max(copiedAt+1,Date.now());await req('POST','/v1/clipboard',{id,protocol:2,sequence:++sequence,copied_at:copiedAt},a.token,201);return id;}
 async function retain(id,item=randomUUID()){await req('POST','/v1/clipboard/'+id+'/retain',{item_id:item},a.token);return item;}
 const release=(id,item)=>req('POST','/v1/clipboard/'+id+'/release',{item_id:item},a.token);
 async function put(id,name,data,wrong=false){const zip=zipFile(name,data),envelope={name,size:data.length,sha256:wrong?'0'.repeat(64):hash(data),prefix:zip.prefix,suffix:zip.suffix};const response=await fetch(base+'/v1/clipboard/'+id+'/content',{method:'PUT',headers:{Authorization:'Bearer '+a.token,'Content-Type':'application/octet-stream','X-Clipboard-Kind':'files','X-Content-SHA256':hash(zip.archive),'X-Clipboard-Raw-File':JSON.stringify(envelope)},body:data});return {status:response.status,body:await response.json(),zip,envelope};}
 const data=Buffer.alloc(5*1024*1024+137,0x63),name='original.txt';data.write('Uncompressed ORIGINAL file bytes\n');
 const id=await stage(),item=randomUUID();await retain(id,item);const first=await put(id,name,data);assert.equal(first.status,200,JSON.stringify(first));
 const oldGet=await fetch(base+'/v1/clipboard/'+id+'/content',{headers:{Authorization:'Bearer '+b.token,'User-Agent':'FlickDock/1.8.20 (CloudSync)'}});assert.equal(oldGet.status,200);const archive=Buffer.from(await oldGet.arrayBuffer());assert.deepEqual(archive,first.zip.archive);assert.equal(hash(archive),oldGet.headers.get('X-Content-SHA256'));
 let snap=await snapshot();assert.equal(snap.objects.length,1);assert.equal(snap.objects[0].size,data.length);assert.equal(snap.allocations.length,1);
 checks.push({name:'one raw R2 original + legacy ZIP bytes/SHA compatibility',ok:true,snapshot:snap});
 const reuseBody={upload_id:randomUUID(),item_id:item,base_version:0,kind:'file',name,mime_type:'text/plain',size:data.length,sha256:hash(data),reuse_only:true};
 const instant=await req('POST','/v1/uploads',reuseBody,a.token,201);assert(instant.instant);await release(id,item);snap=await snapshot();assert.equal(snap.objects.length,1);assert.equal(snap.allocations.length,1);assert.equal(snap.items[0].active_upload,first.body.clipboard.upload_id);
 checks.push({name:'shelf instant reference adds no duplicate object/allocation',ok:true});
 const rangeHeaders={Authorization:'Bearer '+b.token,'X-FlickDock-Range-Bytes':'4194304'};
 const view=await fetch(base+'/v1/items/'+item+'/content',{headers:rangeHeaders});assert.equal(view.status,200);
 const ranges=await view.json();assert.equal(ranges.chunk_size,4*1024*1024);assert.equal(ranges.part_count,2);
 const chunks=[];
 for(let index=0;index<ranges.part_count;index++){
   const part=await fetch(base+'/v1/items/'+item+'/parts/'+index+'?version=1',{headers:rangeHeaders});assert.equal(part.status,200);
   const bytes=Buffer.from(await part.arrayBuffer());assert.deepEqual(bytes,data.subarray(index*4*1024*1024,(index+1)*4*1024*1024));chunks.push(bytes);
 }
 assert.deepEqual(Buffer.concat(chunks),data);checks.push({name:'non-aligned single physical raw object downloads through bounded 4 MiB ranges',ok:true});
 await req('POST','/v1/uploads',{...reuseBody,upload_id:randomUUID(),item_id:randomUUID()},outsider.token,409);
 await req('GET','/v1/clipboard/'+id+'/shared',undefined,b.token,403);
 checks.push({name:'cross-account hash reuse and other-device owner operations denied',ok:true});
 const replacement=await stage(),text=Buffer.from(JSON.stringify({text:'newer clipboard',html:''}));const textPut=await fetch(base+'/v1/clipboard/'+replacement+'/content',{method:'PUT',headers:{Authorization:'Bearer '+a.token,'X-Clipboard-Kind':'text','X-Content-SHA256':hash(text)},body:text});assert.equal(textPut.status,200);await sleep(100);
 const shelf=await fetch(base+'/v1/items/'+item+'/parts/0?version=1',{headers:{Authorization:'Bearer '+b.token,'X-FlickDock-Fast-Content':'1'}});assert.equal(shelf.status,200);assert.deepEqual(Buffer.from(await shelf.arrayBuffer()),data);
 checks.push({name:'clipboard overwrite preserves shelf original',ok:true});
 const detached=randomUUID(),detachedItem=await retain(detached);await stage(detached);const newer=await stage();const newerBody=Buffer.from(JSON.stringify({text:'must stay latest',html:''}));const newerPut=await fetch(base+'/v1/clipboard/'+newer+'/content',{method:'PUT',headers:{Authorization:'Bearer '+a.token,'X-Clipboard-Kind':'text','X-Content-SHA256':hash(newerBody)},body:newerBody});assert.equal(newerPut.status,200);
 const detachedPut=await put(detached,'detached.txt',Buffer.from('old retained original'));assert.equal(detachedPut.status,200,JSON.stringify(detachedPut));assert.equal((await req('GET','/v1/clipboard',undefined,b.token)).clipboard.id,newer);assert.equal((await req('GET','/v1/clipboard/'+detached+'/shared',undefined,a.token)).clipboard.ready,true);
 if(expectFixed){const before=await snapshot();const retry=await put(detached,'detached.txt',Buffer.from('old retained original'));assert.equal(retry.status,200);const after=await snapshot();assert.deepEqual(after.allocations,before.allocations);assert.deepEqual(after.objects,before.objects);assert.equal(retry.body.clipboard.upload_id,detachedPut.body.clipboard.upload_id);checks.push({name:'detached completed PUT retry reuses original object and allocation',ok:true});}
 await release(detached,detachedItem);
 checks.push({name:'detached retained completion never overwrites newer global head',ok:true});
 if(expectFixed){
  const concurrent=randomUUID(),concurrentItem=await retain(concurrent);await stage(concurrent);const latest=await stage();
  const response=await fetch(base+'/v1/clipboard/'+latest+'/content',{method:'PUT',headers:{Authorization:'Bearer '+a.token,'X-Clipboard-Kind':'text','X-Content-SHA256':hash(newerBody)},body:newerBody});assert.equal(response.status,200);
  await sleep(100);const before=await snapshot(),original=Buffer.alloc(2*1024*1024,0x5b);
  const results=await Promise.all([put(concurrent,'concurrent.txt',original),put(concurrent,'concurrent.txt',original)]);assert(results.every(r=>r.status===200));
  const after=await snapshot(),oldIds=new Set(before.uploads.map(u=>u.id)),added=after.uploads.filter(u=>!oldIds.has(u.id));
  if(added.length>1)findings.push({name:'parallel detached PUTs both commit distinct verified objects for one clipboard id',added,returned:results.map(r=>r.body.clipboard),before,after});
  checks.push({name:'concurrent detached completion keeps one object',ok:added.length===1,objectsAdded:added.length});
  await release(concurrent,concurrentItem);
  const released=await snapshot();const initialAllocations=new Set(before.allocations.map(r=>r.id));
  const retainedAllocations=released.allocations.filter(r=>!initialAllocations.has(r.id));
  const initialObjects=new Set(before.objects.map(r=>r.key)),retainedObjects=released.objects.filter(r=>!initialObjects.has(r.key));
  if(retainedAllocations.length||retainedObjects.length)findings.push({name:'concurrent detached release leaves extra allocation or R2 original',retainedAllocations,retainedObjects});
  checks.push({name:'concurrent detached final release removes every extra allocation and object',ok:!retainedAllocations.length&&!retainedObjects.length});
 }
 if(expectFixed){
  for(const mode of ['release_then_finish','abort']){
   const interrupted=randomUUID(),interruptedItem=await retain(interrupted);await stage(interrupted);await stage();
   const before=await snapshot(),body=Buffer.alloc(2*1024*1024,0x4c),zip=zipFile('interrupted.txt',body);
   const descriptor={name:'interrupted.txt',size:body.length,sha256:hash(body),prefix:zip.prefix,suffix:zip.suffix};
   let complete;const completed=new Promise(r=>complete=r);
   const request=httpRequest(base+'/v1/clipboard/'+interrupted+'/content',{method:'PUT',headers:{Authorization:'Bearer '+a.token,'Content-Length':body.length,'Content-Type':'application/octet-stream','X-Clipboard-Kind':'files','X-Content-SHA256':hash(zip.archive),'X-Clipboard-Raw-File':JSON.stringify(descriptor)}},response=>{let text='';response.on('data',b=>text+=b);response.on('end',()=>complete({status:response.statusCode,text}));});
   request.on('error',error=>complete({error:error.code}));request.write(body.subarray(0,128*1024));
   const oldIds=new Set(before.allocations.map(r=>r.id));let observed=false;
   for(let i=0;i<100;i++){if((await snapshot()).allocations.some(r=>!oldIds.has(r.id))){observed=true;break}await sleep(25)}assert(observed,'server started raw upload before interruption');
   await release(interrupted,interruptedItem);
   if(mode==='abort')request.destroy();else request.end(body.subarray(128*1024));
   const response=await completed;if(mode==='release_then_finish')assert.equal(response.status,409,JSON.stringify(response));
   let after;
   for(let i=0;i<150;i++){after=await snapshot();if(!after.allocations.some(r=>!oldIds.has(r.id)))break;await sleep(30)}
   const extra=after.allocations.filter(r=>!oldIds.has(r.id));if(extra.length)findings.push({name:'interrupted raw PUT leaves allocations',mode,response,extra,before,after});
   checks.push({name:mode+' cleans inflight raw allocation',ok:!extra.length,response});
  }
 }
 // Failed retained attempts must not lose cleanup ownership of earlier attempts.
 const bad=await stage(),badItem=await retain(bad);const beforeFailure=await snapshot();const failures=[];
 for(let i=0;i<2;i++){const result=await put(bad,'bad.txt',Buffer.from('same invalid attempt bytes'),true);assert(result.status>=400);failures.push({status:result.status,body:result.body});}
 await release(bad,badItem);await req('GET','/review/expire?account='+a.account.id);await req('GET','/review/cleanup');await sleep(100);const afterFailure=await snapshot();
 const beforeIds=new Set(beforeFailure.allocations.map(r=>r.id)),leaked=afterFailure.allocations.filter(r=>!beforeIds.has(r.id));
 if(leaked.length)findings.push({name:'retained failed attempts overwrite a single held-head cleanup owner and leak earlier allocations',leaked,failures,before:beforeFailure,after:afterFailure});
 checks.push({name:'failed retained retries and release cleanup',ok:!leaked.length,leakedAllocations:leaked.length});
 // A released detached result should not remain advertised ready if storage was deleted.
 const sharedResponse=await fetch(base+'/v1/clipboard/'+detached+'/shared',{headers:{Authorization:'Bearer '+a.token}});assert([200,404].includes(sharedResponse.status));const shared=await sharedResponse.json();const uploadExists=afterFailure.uploads.some(u=>u.id===shared.clipboard?.upload_id);
 if(shared.clipboard?.ready&&!uploadExists)findings.push({name:'shared endpoint advertises deleted detached upload as ready after final release',shared,uploadExists});
 if(expectFixed){assert.equal(leaked.length,0);assert(!shared.clipboard?.ready);checks.push({name:'released detached GET no longer advertises ready',ok:true});}
 console.log(JSON.stringify({checks:checks.map(c=>({name:c.name,ok:c.ok})),findings:findings.map(f=>f.name)},null,2));
}catch(error){failure=error.stack;console.error(failure);process.exitCode=1;}
finally{
 if(server){server.kill();await new Promise(r=>{if(server.exitCode!==null)r();else{server.once('exit',r);setTimeout(r,7000).unref()}});}
 if(process.env.FLICKDOCK_TEST_REPORT)await writeFile(process.env.FLICKDOCK_TEST_REPORT,JSON.stringify({at:new Date().toISOString(),checks,findings,failure:failure||null,logs},null,2));
 try{await rm(state,{recursive:true,force:true,maxRetries:3,retryDelay:300})}catch{}
}
