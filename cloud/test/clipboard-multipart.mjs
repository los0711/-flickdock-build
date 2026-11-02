/** Exact production multipart path on disposable real workerd/D1/R2, no cloud. */
import assert from 'node:assert/strict';
import {spawn,spawnSync} from 'node:child_process';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,dirname,resolve,basename} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import net from 'node:net';
import http from 'node:http';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..'),state=await mkdtemp(join(tmpdir(),'flickdock-multipart-local-'));
const sourceNames=['src/clipboard-multipart.ts','src/clipboard.ts','src/shared-file.ts','src/index.ts'];
const sourceHashes=async()=>Object.fromEntries(await Promise.all(sourceNames.map(async name=>[name,createHash('sha256').update(await readFile(join(root,name))).digest('hex')])));
const sourceBefore=await sourceHashes(),startedAt=new Date().toISOString(),started=performance.now();
const wrangler=join(root,'node_modules/wrangler/bin/wrangler.js'),source=join(root,'src/index.ts').replaceAll('\\','/');
const wrapper=join(state,'review.ts');
await writeFile(wrapper,`
import app,{AccountEvents as BaseEvents} from ${JSON.stringify(source)};
import {sweepMultiparts} from ${JSON.stringify(join(root,'src/clipboard-multipart.ts').replaceAll('\\','/'))};
export {WanHub,AppGateway} from ${JSON.stringify(source)};
export class AccountEvents extends BaseEvents {
 constructor(ctx,env){
  let self;const wrap=statement=>new Proxy(statement,{get(target,prop){
   if(prop==='bind')return (...args)=>wrap(target.bind(...args));
   if(prop==='first')return async(...args)=>{const result=await target.first(...args);if(self?.hold&&++self.hold.seen===self.hold.at){self.hold.held=true;await new Promise(resolve=>self.hold.release=resolve);}return result;};
   const value=Reflect.get(target,prop);return typeof value==='function'?value.bind(target):value;
  }});
  const db=new Proxy(env.DB,{get(target,prop){if(prop==='prepare')return sql=>sql.startsWith('SELECT 1 valid FROM sessions')?wrap(target.prepare(sql)):target.prepare(sql);const value=Reflect.get(target,prop);return typeof value==='function'?value.bind(target):value;}});
  const files=new Proxy(env.FILES,{get(target,prop){if(prop==='delete')return async key=>{self.deletes=(self.deletes||0)+1;if(self?.failKey&&(key===self.failKey||Array.isArray(key)&&key.includes(self.failKey))){self.failKey=null;throw Error('Synthetic local delete failure');}return target.delete(key);};if(prop==='list')return async options=>{self.lists=(self.lists||0)+1;return target.list(options);};const value=Reflect.get(target,prop);return typeof value==='function'?value.bind(target):value;}});
  super(ctx,{...env,DB:db,FILES:files});self=this;
 }
 async fetch(request){const url=new URL(request.url);
  if(url.pathname==='/review/attempts'){const rows=await this.ctx.storage.list({prefix:'clipboard-multipart:'}),writes=await this.ctx.storage.list({prefix:'clipboard-multipart-write:'}),scanner=await this.ctx.storage.get('clipboard-multipart-orphans');return Response.json({attempts:[...rows.values()].map(a=>({id:a.id,clipboard:a.head.id,count:a.parts.length,cancelled:!!a.cancelled,lease:!!a.lease,hash_states_private:!!a.body_state})),writes:[...writes.values()].map(w=>({attempt:w.attempt,terminal:!!w.terminal,fenced:!!w.gc_claim})),held:!!this.hold?.held,scanner:scanner?{cursor:!!scanner.cursor,next_at:scanner.next_at}:null,lists:this.lists||0,deletes:this.deletes||0});}
  if(url.pathname==='/review/hold'){this.hold={seen:0,held:false,at:Number(url.searchParams.get('at')||2)};return Response.json({ok:true});}
  if(url.pathname==='/review/expire-lease'){const key='clipboard-multipart:'+url.searchParams.get('upload'),a=await this.ctx.storage.get(key);a.lease_until=1;await this.ctx.storage.put(key,a);if(url.searchParams.get('fail'))this.failKey=a.inflight_key;return Response.json({ok:true});}
  if(url.pathname==='/review/release'){this.hold?.release?.();this.hold=null;return Response.json({ok:true});}
  if(url.pathname==='/review/sweep'){const writes=await this.ctx.storage.list({prefix:'clipboard-multipart-write:'});for(const [key,w]of writes)if(w.terminal||url.searchParams.has('force')){w.retry_at=1;if(url.searchParams.has('force'))w.until=1;await this.ctx.storage.put(key,w);}const scanner=await this.ctx.storage.get('clipboard-multipart-orphans');if(scanner){scanner.next_at=1;await this.ctx.storage.put('clipboard-multipart-orphans',scanner);}this.env.R2_CLASS_A_BUDGET=url.searchParams.has('deny')?'1':'1000000';await super.alarm();return Response.json({ok:true});}
  return super.fetch(request);
 }
}
export default {
 async fetch(request,env,ctx){const url=new URL(request.url),account=url.searchParams.get('account');
  if(url.pathname==='/review/state'){
   const sql=async text=>(await env.DB.prepare(text).bind(account).all()).results;
   return Response.json({allocations:await sql('SELECT id,size FROM allocations WHERE account_id=?'),uploads:await sql('SELECT id,state,size,chunk_size,part_count,computed_sha256,sha256 FROM uploads WHERE account_id=?'),items:await sql('SELECT id,active_upload,deleted,version,expires_at FROM items WHERE account_id=?'),refs:await sql('SELECT upload_id,clipboard_id,expires_at FROM clipboard_upload_refs WHERE account_id=?'),leases:await sql('SELECT r.id FROM read_leases r JOIN uploads u ON u.id=r.upload_id WHERE u.account_id=?'),objects:(await env.FILES.list()).objects.map(o=>({key:o.key,size:o.size})),...await (await env.EVENTS.get(env.EVENTS.idFromName(account)).fetch('https://internal/review/attempts')).json()});
  }
  if(url.pathname==='/review/late-orphan'){const id='00000000-0000-4000-8000-000000000000',sha='6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d';await env.DB.prepare('INSERT INTO allocations(id,account_id,size) VALUES(?,?,1)').bind('clipboard-write:'+id,account).run();await env.FILES.put('clipboard/'+account+'/'+id+'/'+id+'/0-'+id+'-'+sha,new Uint8Array([0]),{sha256:sha});return Response.json({ok:true});}
  if(url.pathname.startsWith('/review/')&&!['/review/quota','/review/expire-session'].includes(url.pathname))return env.EVENTS.get(env.EVENTS.idFromName(account)).fetch(request);
  if(url.pathname==='/review/quota'){await env.DB.prepare('UPDATE accounts SET quota_bytes=? WHERE id=?').bind(Number(url.searchParams.get('bytes')),account).run();return Response.json({ok:true});}
  if(url.pathname==='/review/expire-session'){await env.DB.prepare('UPDATE sessions SET expires_at=1 WHERE account_id=? AND device_id=?').bind(account,url.searchParams.get('device')).run();return Response.json({ok:true});}
  return app.fetch(request,env,ctx);
 },scheduled:app.scheduled
};
`);
const config=JSON.parse(await readFile(join(root,'wrangler.jsonc'),'utf8'));config.main=wrapper;config.d1_databases[0].migrations_dir=join(root,'migrations');
const configFile=join(state,'wrangler.json');await writeFile(configFile,JSON.stringify(config));
const env={...process.env,WRANGLER_SEND_METRICS:'false',CLOUDFLARE_API_TOKEN:'',CLOUDFLARE_API_KEY:'',CLOUDFLARE_EMAIL:'',CLOUDFLARE_ACCOUNT_ID:'',HTTP_PROXY:'',HTTPS_PROXY:'',ALL_PROXY:'',http_proxy:'',https_proxy:'',all_proxy:''};
const migrated=spawnSync(process.execPath,[wrangler,'--config',configFile,'d1','migrations','apply','DB','--local','--persist-to',state],{cwd:state,env,encoding:'utf8',windowsHide:true,timeout:60000});assert.equal(migrated.status,0,migrated.stderr);
const portServer=net.createServer();await new Promise(r=>portServer.listen(0,'127.0.0.1',r));const port=portServer.address().port;await new Promise(r=>portServer.close(r));
const base='http://127.0.0.1:'+port,checks=[];let logs='',failure;
const server=spawn(process.execPath,[wrangler,'dev','--config',configFile,'--local','--ip','127.0.0.1','--port',String(port),'--persist-to',state,'--var','AUTH_PEPPER:local-multipart-secret-at-least32characters'],{cwd:state,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
const hash=data=>createHash('sha256').update(data).digest('hex'),PART=4*1024*1024,MAX=100*1024*1024;
async function wait(predicate){for(let i=0;i<200;i++){const result=await predicate();if(result)return result;await sleep(25)}throw Error('Local real-state observation timeout')}
async function check(name,action){const at=performance.now();await action();checks.push({name,ok:true,wall_ms:Math.round((performance.now()-at)*1000)/1000});console.log('PASS',name)}
try{
 for(let i=0;i<150;i++){if(server.exitCode!==null)throw Error('Local worker exited');try{if((await fetch(base+'/v1/health')).ok)break}catch{}await sleep(250)}
 async function req(method,path,body,token,expected=200){const response=await fetch(base+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)});const value=await response.json();assert.equal(response.status,expected,JSON.stringify(value));return value;}
 const username='multipart-'+randomUUID();
 async function sign(login=false){const challenge=await req('POST','/v1/auth/challenge',{username,purpose:login?'login':'register'});const auth_key=scryptSync('local-multipart-password',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');return req('POST','/v1/auth/'+(login?'login':'register'),{username,challenge_id:challenge.challenge_id,auth_key,device_name:login?'receiver':'sender',platform:login?'macos':'windows'},null,login?200:201);}
 let owner=await sign(),peer=await sign(true),sequence=0,copied=Date.now(),lastReady;
 const snapshot=()=>req('GET','/review/state?account='+owner.account.id);
 async function stage(inline){const id=randomUUID();copied=Math.max(copied+1,Date.now());const result=await req('POST','/v1/clipboard',{id,protocol:2,sequence:++sequence,copied_at:copied,...(inline?{inline:{kind:'text',size:inline.length,sha256:hash(inline),body:inline.toString('base64')}}:{})},owner.token,201);return {id,result};}
 async function seed(){const text=Buffer.from('previous committed original'),prefix=Buffer.alloc(12);prefix.write('FDCLIP2\0');prefix.writeUInt32BE(text.length,8);const result=await stage(Buffer.concat([prefix,text]));lastReady=result.id;return result.id;}
 async function unchanged(){const head=(await req('GET','/v1/clipboard',undefined,peer.token)).clipboard;assert.equal(head.id,lastReady);assert(head.ready);}
 async function init(id,data,kind='image',raw,sha=hash(data),expected=201){return req('POST',`/v1/clipboard/${id}/upload`,{kind,size:data.length,sha256:sha,...(raw?{raw_file:raw}:{})},owner.token,expected);}
 async function put(id,upload,index,data,expected=200,sha=hash(data)){
  const response=await fetch(base+`/v1/clipboard/${id}/upload/${upload}/parts/${index}`,{method:'PUT',headers:{Authorization:'Bearer '+owner.token,'X-Content-SHA256':sha},body:data});
  const result=await response.json();assert.equal(response.status,expected,JSON.stringify(result));return result;
 }
 async function allParts(id,upload,data,start=0){for(let i=start;i<Math.ceil(data.length/PART);i++)await put(id,upload,i,data.subarray(i*PART,Math.min(data.length,(i+1)*PART)));}
 const commit=(id,upload,expected=200)=>req('POST',`/v1/clipboard/${id}/upload/${upload}/commit`,{},owner.token,expected);
 async function originalGet(id,expectedSize,expectedHash){const response=await fetch(base+`/v1/clipboard/${id}/content`,{headers:{Authorization:'Bearer '+peer.token}});assert.equal(response.status,200);assert.equal(response.headers.get('X-Content-SHA256'),expectedHash);const checksum=createHash('sha256');let size=0;for await(const bytes of response.body){size+=bytes.length;checksum.update(bytes)}assert.equal(size,expectedSize);assert.equal(checksum.digest('hex'),expectedHash);}
 await check('actual capability negotiation keeps small inline path and old ready head',async()=>{
  const {result}=await stage();assert.deepEqual(result.multipart_v1,{part_size:PART,threshold:24*1024*1024,max_bytes:MAX,max_archive_bytes:MAX+1024*1024});await seed();
 });
 const data=Buffer.alloc(25*1024*1024+37,0x61);
 await check('interrupted TCP part resumes same attempt with no duplicate R2 parts or allocation',async()=>{
  const {id}=await stage(),first=await init(id,data);await unchanged();
  await put(id,first.upload_id,0,data.subarray(0,PART));
  const aborted=http.request(base+`/v1/clipboard/${id}/upload/${first.upload_id}/parts/1`,{method:'PUT',headers:{Authorization:'Bearer '+owner.token,'Content-Length':String(PART),'X-Content-SHA256':hash(data.subarray(PART,2*PART))}});aborted.on('error',()=>{});aborted.write(data.subarray(PART,PART+65536));await sleep(80);aborted.destroy();
  await wait(async()=>!(await snapshot()).attempts.find(a=>a.id===first.upload_id)?.lease);
  const resumed=await init(id,data);assert.equal(resumed.upload_id,first.upload_id);assert.equal(resumed.next_part,1);
  const replay=await put(id,first.upload_id,0,data.subarray(0,PART));assert.equal(replay.next_part,1);
  await put(id,first.upload_id,1,data.subarray(PART,2*PART),400,'0'.repeat(64));
  await allParts(id,first.upload_id,data,1);await unchanged();
  const before=await snapshot();assert.equal(before.objects.filter(o=>o.key.includes(first.upload_id)).length,Math.ceil(data.length/PART));assert.equal(before.allocations.filter(a=>a.id===first.upload_id).length,1);assert.equal(before.items.length,0);
  const ready=await commit(id,first.upload_id);assert(ready.clipboard.ready);assert.equal(ready.clipboard.sha256,hash(data));lastReady=id;
  assert.equal((await commit(id,first.upload_id)).clipboard.upload_id,first.upload_id);
  assert.equal((await snapshot()).items.length,0);await originalGet(id,data.length,hash(data));
 });
 await check('out-of-order and conflicting replay do not advance state; cancel frees pending bytes',async()=>{
  const {id}=await stage(),a=await init(id,data);await put(id,a.upload_id,1,data.subarray(PART,2*PART),409);
  await put(id,a.upload_id,0,data.subarray(0,PART));await put(id,a.upload_id,0,data.subarray(0,PART),409,'0'.repeat(64));await unchanged();
  await req('DELETE',`/v1/clipboard/${id}/upload/${a.upload_id}`,undefined,owner.token);await req('GET',`/v1/clipboard/${id}/upload/${a.upload_id}`,undefined,owner.token,410);
  const state=await snapshot();assert(!state.objects.some(o=>o.key.includes(a.upload_id)));assert(!state.allocations.some(x=>x.id===a.upload_id));await unchanged();
 });
 await check('near-quota init retry preserves same allocation and uploaded part; new over-quota init is413',async()=>{
  const {id}=await stage(),a=await init(id,data);await put(id,a.upload_id,0,data.subarray(0,PART));
  const before=await snapshot(),allocated=before.allocations.reduce((total,row)=>total+row.size,0);
  await req('POST','/review/quota?account='+owner.account.id+'&bytes='+(allocated+Math.floor(data.length/2)));
  try{const retry=await init(id,data);assert.equal(retry.upload_id,a.upload_id);assert.equal(retry.next_part,1);assert.equal((await snapshot()).allocations.filter(row=>row.id===a.upload_id).length,1);
   await req('POST','/review/quota?account='+owner.account.id+'&bytes='+(allocated+PART-1));
   const deniedPart=await put(id,a.upload_id,1,data.subarray(PART,2*PART),413);assert.equal(deniedPart.error.code,'ACCOUNT_QUOTA');const retained=await snapshot();assert.equal(retained.attempts.find(row=>row.id===a.upload_id).count,1);assert(!retained.allocations.some(row=>row.id.startsWith('clipboard-write:')));await unchanged();
  }
  finally{await req('POST','/review/quota?account='+owner.account.id+'&bytes='+8*1024*1024*1024);}
  await req('DELETE',`/v1/clipboard/${id}/upload/${a.upload_id}`,undefined,owner.token);
  const another=await stage();await req('POST','/review/quota?account='+owner.account.id+'&bytes=1');
  try{const denied=await init(another.id,data,'image',undefined,hash(data),413);assert.equal(denied.error.code,'ACCOUNT_QUOTA');assert(!(await snapshot()).allocations.some(row=>row.id===denied.upload_id));await unchanged();}
  finally{await req('POST','/review/quota?account='+owner.account.id+'&bytes='+8*1024*1024*1024);}
 });
 await check('real old R2 writer cleanup transient failure retains independent GC ownership after attempt cancellation',async()=>{
  const {id}=await stage(),a=await init(id,data),control=path=>req('POST','/review/'+path+'?account='+owner.account.id);
  await control('hold');const old=put(id,a.upload_id,0,data.subarray(0,PART),423);
  await wait(async()=>(await snapshot()).held);
  await req('POST','/review/expire-lease?account='+owner.account.id+'&upload='+a.upload_id+'&fail=1');
  await put(id,a.upload_id,0,data.subarray(0,PART));await control('release');await old;
  const before=await snapshot();assert.equal(before.objects.filter(row=>row.key.includes(a.upload_id)).length,2);assert(before.writes.some(row=>row.attempt===a.upload_id&&row.terminal));assert.equal(before.allocations.filter(row=>row.id.startsWith('clipboard-write:')).reduce((sum,row)=>sum+row.size,0),PART);
  await req('DELETE',`/v1/clipboard/${id}/upload/${a.upload_id}`,undefined,owner.token);
  const retained=await snapshot();assert(retained.writes.some(row=>row.attempt===a.upload_id&&row.terminal));assert.equal(retained.objects.filter(row=>row.key.includes(a.upload_id)).length,1);assert.equal(retained.allocations.filter(row=>row.id.startsWith('clipboard-write:')).reduce((sum,row)=>sum+row.size,0),PART);
  await control('sweep');const after=await snapshot();assert(!after.objects.some(row=>row.key.includes(a.upload_id)));assert(!after.writes.some(row=>row.attempt===a.upload_id));assert(!after.allocations.some(row=>row.id.startsWith('clipboard-write:')));await unchanged();
 });
 await check('persistent fenced scanner recovers late real PUT after empty sweep and respects actual A budget denial',async()=>{
  const {id}=await stage(),a=await init(id,data);await req('POST','/review/hold?account='+owner.account.id+'&at=1');
  const late=put(id,a.upload_id,0,data.subarray(0,PART),423);await wait(async()=>(await snapshot()).held);
  await req('POST','/review/sweep?account='+owner.account.id+'&force=1');
  let current=await snapshot();assert(current.scanner);assert(!current.writes.some(row=>row.attempt===a.upload_id));assert.equal(current.allocations.filter(row=>row.id.startsWith('clipboard-write:')).reduce((sum,row)=>sum+row.size,0),PART);
  await req('POST','/review/sweep?account='+owner.account.id+'&force=1');current=await snapshot();assert(current.scanner);assert(!current.objects.some(row=>row.key.includes(a.upload_id)));assert.equal(current.allocations.filter(row=>row.id.startsWith('clipboard-write:')).reduce((sum,row)=>sum+row.size,0),PART);const registeredBefore=current.objects.filter(row=>!row.key.includes(a.upload_id)).map(row=>row.key).sort();
  await req('POST','/review/release?account='+owner.account.id);await late;
  current=await snapshot();assert(!current.objects.some(row=>row.key.includes(a.upload_id)));assert(!current.allocations.some(row=>row.id.startsWith('clipboard-write:')));
  // Model a crashed request's later R2 completion using a real orphan object
  // and reservation; the live request above did execute its terminal finally.
  await req('POST','/review/late-orphan?account='+owner.account.id);current=await snapshot();const lists=current.lists,deletes=current.deletes,marker='/00000000-0000-4000-8000-000000000000/';
  await req('POST','/review/sweep?account='+owner.account.id+'&force=1&deny=1');current=await snapshot();assert.equal(current.lists,lists);assert.equal(current.deletes,deletes);assert(current.scanner.next_at>Date.now()+3600000);assert(current.objects.some(row=>row.key.includes(marker)));assert(current.allocations.some(row=>row.id==='clipboard-write:00000000-0000-4000-8000-000000000000'));
  await req('POST','/review/sweep?account='+owner.account.id+'&force=1');current=await snapshot();assert(!current.objects.some(row=>row.key.includes(marker)));assert(!current.allocations.some(row=>row.id==='clipboard-write:00000000-0000-4000-8000-000000000000'));assert(current.scanner);assert.deepEqual(current.objects.map(row=>row.key).sort(),registeredBefore);
  await req('DELETE',`/v1/clipboard/${id}/upload/${a.upload_id}`,undefined,owner.token);await unchanged();
 });
 await check('complete per-part hashes cannot fake full SHA or replace old ready head',async()=>{
  const {id}=await stage(),a=await init(id,data,'image',undefined,'0'.repeat(64));await allParts(id,a.upload_id,data);await commit(id,a.upload_id,400);await unchanged();
  const state=await snapshot();assert(!state.allocations.some(x=>x.id===a.upload_id));assert(!state.objects.some(o=>o.key.includes(a.upload_id)));
 });
 await check('superseding copy during TCP body prevents late part publication and cleans allocation',async()=>{
  const {id}=await stage(),a=await init(id,data);let complete;
  const result=new Promise(resolve=>complete=resolve);
  const slow=http.request(base+`/v1/clipboard/${id}/upload/${a.upload_id}/parts/0`,{method:'PUT',headers:{Authorization:'Bearer '+owner.token,'Content-Length':String(PART),'X-Content-SHA256':hash(data.subarray(0,PART))}},response=>{let text='';response.on('data',b=>text+=b);response.on('end',()=>complete({status:response.statusCode,text}))});slow.on('error',error=>complete({error:error.code}));slow.write(data.subarray(0,65536));await sleep(100);await seed();slow.end(data.subarray(65536,PART));
  const response=await result;assert.equal(response.status,409,JSON.stringify(response));await unchanged();
  await wait(async()=>!(await snapshot()).allocations.some(x=>x.id===a.upload_id));assert(!(await snapshot()).objects.some(o=>o.key.includes(a.upload_id)));
 });
 await check('revoked session cannot commit a fully uploaded attempt',async()=>{
  const {id}=await stage(),a=await init(id,data);await allParts(id,a.upload_id,data);
  await req('POST','/review/expire-session?account='+owner.account.id+'&device='+owner.device_id);
  await commit(id,a.upload_id,401);await unchanged();owner=await sign(true);sequence=0;
 });
 function zipEnvelope(data){const name=Buffer.from('original.bin'),table=Uint32Array.from({length:256},(_,i)=>{let c=i;for(let k=0;k<8;k++)c=(c>>>1)^((c&1)?0xedb88320:0);return c});let c=0xffffffff;for(const b of data)c=(c>>>8)^table[(c^b)&255];const crc=(c^0xffffffff)>>>0;
  const local=Buffer.alloc(30);local.writeUInt32LE(0x04034b50);local.writeUInt16LE(20,4);local.writeUInt32LE(crc,14);local.writeUInt32LE(data.length,18);local.writeUInt32LE(data.length,22);local.writeUInt16LE(name.length,26);
  const central=Buffer.alloc(46);central.writeUInt32LE(0x02014b50);central.writeUInt16LE(20,4);central.writeUInt16LE(20,6);central.writeUInt32LE(crc,16);central.writeUInt32LE(data.length,20);central.writeUInt32LE(data.length,24);central.writeUInt16LE(name.length,28);
  const end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(1,8);end.writeUInt16LE(1,10);end.writeUInt32LE(central.length+name.length,12);end.writeUInt32LE(local.length+name.length+data.length,16);const prefix=Buffer.concat([local,name]),suffix=Buffer.concat([central,name,end]);return {prefix,suffix,raw:{name:'original.bin',size:data.length,sha256:hash(data),prefix:prefix.toString('base64'),suffix:suffix.toString('base64')},sha:createHash('sha256').update(prefix).update(data).update(suffix).digest('hex')};}
 await check('raw original hash and reconstructed ZIP wire hash are independently checked',async()=>{
  const zip=zipEnvelope(data),{id}=await stage(),a=await init(id,data,'files',zip.raw,'0'.repeat(64));await allParts(id,a.upload_id,data);await commit(id,a.upload_id,400);await unchanged();
 });
 await check('actual100MiB UTF8 text plus FDCLIP2 framing uses bounded parts and transparent full-hash GET',async()=>{
  const original=Buffer.alloc(MAX,0x61),prefix=Buffer.alloc(12);prefix.write('FDCLIP2\0');prefix.writeUInt32BE(original.length,8);const wire=Buffer.concat([prefix,original]);
  const {id}=await stage(),a=await init(id,wire,'text');assert.equal(a.part_count,26);await allParts(id,a.upload_id,wire);await unchanged();const ready=await commit(id,a.upload_id);lastReady=id;assert.equal(ready.clipboard.size,MAX+12);await originalGet(id,wire.length,hash(wire));
  const next=await stage(),oversize=await req('POST',`/v1/clipboard/${next.id}/upload`,{kind:'text',size:MAX+1024*1024+1,sha256:'0'.repeat(64)},owner.token,413);assert.equal(oversize.error.code,'CLIPBOARD_LIMIT');await unchanged();
 });
 await check('new multipart metadata and first text part reject unsupported or over-limit framing without replacing ready head',async()=>{
  const {id}=await stage();await req('POST',`/v1/clipboard/${id}/upload`,{kind:'files',size:data.length,sha256:hash(data)},owner.token,400);
  await req('POST',`/v1/clipboard/${id}/upload`,{kind:'text',size:MAX+13,sha256:'0'.repeat(64)},owner.token,413);
  await req('POST',`/v1/clipboard/${id}/upload`,{kind:'image',size:MAX+1,sha256:'0'.repeat(64)},owner.token,413);await unchanged();
  const invalid=await init(id,data,'text');await put(id,invalid.upload_id,0,data.subarray(0,PART),400);assert.equal((await snapshot()).attempts.find(row=>row.id===invalid.upload_id).count,0);await unchanged();await req('DELETE',`/v1/clipboard/${id}/upload/${invalid.upload_id}`,undefined,owner.token);
 });
 await check('actual100MiB raw original uses25 bounded PUTs, transparent repeated ZIP GET and shelf reuse',async()=>{
  const raw=Buffer.alloc(MAX,0x39),zip=zipEnvelope(raw),{id}=await stage(),a=await init(id,raw,'files',zip.raw,zip.sha);assert.equal(a.part_count,25);await allParts(id,a.upload_id,raw);await unchanged();
  const ready=await commit(id,a.upload_id);lastReady=id;assert.equal(ready.clipboard.raw_file.sha256,hash(raw));assert.equal(ready.clipboard.sha256,zip.sha);assert.equal((await snapshot()).items.length,0);
  await originalGet(id,MAX+zip.prefix.length+zip.suffix.length,zip.sha);await originalGet(id,MAX+zip.prefix.length+zip.suffix.length,zip.sha);
  const item=randomUUID();const reused=await req('POST','/v1/uploads',{upload_id:randomUUID(),item_id:item,base_version:0,kind:'file',name:'original.bin',mime_type:'application/octet-stream',size:MAX,sha256:hash(raw),reuse_only:true},owner.token,201);assert(reused.instant);assert.equal(reused.record.size,MAX);assert.equal(Date.parse(reused.record.expires_at)-Date.parse(reused.record.updated_at),7200000);
  await seed();const state=await snapshot();assert(state.items.some(row=>row.id===item&&!row.deleted));assert.equal(state.objects.filter(o=>o.key.includes(a.upload_id)).length,25);assert(state.allocations.some(row=>row.id===a.upload_id));
  const view=await req('GET',`/v1/items/${item}/content`,undefined,peer.token);assert.equal(view.part_count,25);const full=createHash('sha256');for(let i=0;i<25;i++){const response=await fetch(base+`/v1/items/${item}/parts/${i}`,{headers:{Authorization:'Bearer '+peer.token}});assert.equal(response.status,200);for await(const bytes of response.body)full.update(bytes);}assert.equal(full.digest('hex'),hash(raw));
 });
 await check('actual R2 cursor resets preserve registered shelf parts and collect late key inserted before cursor',async()=>{
  const control=()=>req('POST','/review/sweep?account='+owner.account.id+'&force=1');
  // Finish any prior page, then start a fresh page over25 registered parts.
  for(let i=0;i<8&&(await snapshot()).scanner.cursor;i++)await control();await control();assert((await snapshot()).scanner.cursor);
  await req('POST','/review/late-orphan?account='+owner.account.id);const marker='/00000000-0000-4000-8000-000000000000/';
  for(let i=0;i<8&&(await snapshot()).scanner.cursor;i++)await control();let current=await snapshot();assert(!current.scanner.cursor);assert(current.objects.some(row=>row.key.includes(marker)));
  const retained=current.objects.filter(row=>!row.key.includes(marker)).map(row=>row.key).sort();await control();current=await snapshot();assert(!current.objects.some(row=>row.key.includes(marker)));assert.deepEqual(current.objects.map(row=>row.key).sort(),retained);assert(current.scanner);
 });
 console.log('ALL',checks.length,'REAL LOCAL MULTIPART CHECKS PASSED');
}catch(error){failure=error.stack;console.error(failure);process.exitCode=1}
finally{
 server.kill();await new Promise(r=>{if(server.exitCode!==null)r();else{server.once('exit',r);setTimeout(r,8000).unref()}});
 const sourceAfter=await sourceHashes();if(JSON.stringify(sourceBefore)!==JSON.stringify(sourceAfter)){failure='Production source changed during test';process.exitCode=1;}
 if(process.env.FLICKDOCK_TEST_REPORT)await writeFile(process.env.FLICKDOCK_TEST_REPORT,JSON.stringify({checks,failure:failure||null,started_at:startedAt,finished_at:new Date().toISOString(),wall_ms:performance.now()-started,source_before:sourceBefore,source_after:sourceAfter,mode:'real local workerd/D1/R2; no deployed edge/CPU claim',log_tail:logs.slice(-4000)},null,2));
 // Only this explicitly verified disposable directory may be removed.
 assert.equal(dirname(resolve(state)),resolve(tmpdir()));assert(basename(state).startsWith('flickdock-multipart-local-'));
 try{await rm(resolve(state),{recursive:true,force:true,maxRetries:20,retryDelay:200})}catch{}
}
