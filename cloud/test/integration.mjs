/** Real workerd HTTP + local D1 SQLite + local R2. No cloud credentials or email. */
import assert from 'node:assert/strict';
import {createHash,createHmac,randomUUID,scryptSync} from 'node:crypto';
import {spawn,spawnSync} from 'node:child_process';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,dirname,sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as sleep} from 'node:timers/promises';
import net from 'node:net';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const wrangler=join(root,'node_modules/wrangler/bin/wrangler.js');
const state=await mkdtemp(join(tmpdir(),'flickdock-cloud-local-'));
const configuration=JSON.parse(await readFile(join(root,'wrangler.jsonc'),'utf8'));
configuration.main=join(root,'src/index.ts');configuration.d1_databases[0].migrations_dir=join(root,'migrations');
configuration.vars.EMAIL_PROVIDER='disabled';
const configFile=join(state,'wrangler-test.json');await writeFile(configFile,JSON.stringify(configuration));
const env={...process.env,WRANGLER_SEND_METRICS:'false',CLOUDFLARE_API_TOKEN:'',CLOUDFLARE_API_KEY:'',CLOUDFLARE_EMAIL:'',CLOUDFLARE_ACCOUNT_ID:''};
const checks=[];let server,output='';
const hash=data=>createHash('sha256').update(data).digest('hex');
const secret='local-test-secret-never-used-for-real-email';
function cli(args){const result=spawnSync(process.execPath,[wrangler,'--config',configFile,...args],{cwd:state,env,encoding:'utf8',timeout:60000,windowsHide:true});assert.equal(result.status,0,result.stderr+result.stdout);return result.stdout;}
function sql(command){return cli(['d1','execute','DB','--local','--persist-to',state,'--command',command,'--json']);}
function sqlRows(command){return JSON.parse(sql(command))[0].results;}
async function localObject(key,expected){
  const destination=join(state,'object-probe.bin');
  const result=spawnSync(process.execPath,[wrangler,'--config',configFile,'r2','object','get',configuration.r2_buckets[0].bucket_name+'/'+key,'--local','--persist-to',state,'--file',destination],{cwd:state,env,encoding:'utf8',timeout:60000,windowsHide:true});
  if(expected===null){assert.notEqual(result.status,0,'The real local R2 object should be absent');assert.match(result.stderr+result.stdout,/does not exist|not found/i);}
  else{assert.equal(result.status,0,result.stderr+result.stdout);assert.deepEqual(await readFile(destination),expected);}
}
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function start(extra=[]){
  const port=await freePort();
  server=spawn(process.execPath,[wrangler,'dev','--config',configFile,'--local','--ip','127.0.0.1','--port',String(port),'--persist-to',state,'--test-scheduled','--var','EMAIL_CODE_SECRET:'+secret,'--var','AUTH_PEPPER:'+secret,...extra],{cwd:state,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
  output='';server.stdout.on('data',b=>{output+=b});server.stderr.on('data',b=>{output+=b});
  const base='http://127.0.0.1:'+port;
  for(let i=0;i<120;i++){if(server.exitCode!==null)throw new Error(output);try{if((await fetch(base+'/v1/health')).ok)return base;}catch{}await sleep(250);}
  throw new Error('Local worker did not start: '+output);
}
async function stop(){if(!server)return;const current=server;server=undefined;current.kill();await new Promise(resolve=>{current.once('exit',resolve);setTimeout(resolve,4000).unref()});}
let base;
async function request(method,path,body,token,expected=200,headers={}){
  if(method==='POST'&&['/v1/auth/register','/v1/auth/login'].includes(path)&&body?.password){
    const challenge=await request('POST','/v1/auth/challenge',{username:body.username,purpose:path.endsWith('register')?'register':'login'});
    body=authBody(body,challenge);
  }
  const response=await fetch(base+path,{method,headers:{...(body===undefined?{}:{'Content-Type':'application/json'}),...(token?{Authorization:'Bearer '+token}:{}),...headers},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();assert.equal(response.status,expected,JSON.stringify(data));assert.equal(data.schema_version,1);return data;
}
const credentials=name=>({username:name,password:'safe testing password',device_name:'Windows test',platform:'windows'});
function authBody(input,challenge){assert.deepEqual(challenge.kdf,{name:'scrypt',N:16384,r:8,p:5,dkLen:32});assert.match(challenge.salt,/^[a-f0-9]{32}$/);const key=scryptSync(input.password,Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024});const {password,...rest}=input;return {...rest,challenge_id:challenge.challenge_id,auth_key:key.toString('hex')};}
async function reserve(token,itemId,data,baseVersion=0,overrides={}){return request('POST','/v1/uploads',{item_id:itemId,base_version:baseVersion,kind:'file',name:'文件.bin',mime_type:'application/octet-stream',size:data.length,sha256:hash(data),...overrides},token,201);}
async function part(token,upload,index,data,expected=200,customHash=hash(data)){
  const response=await fetch(base+`/v1/uploads/${upload}/parts/${index}`,{method:'PUT',headers:{Authorization:'Bearer '+token,'X-Content-SHA256':customHash},body:data});const value=await response.json();assert.equal(response.status,expected,JSON.stringify(value));return value;
}
async function check(name,fn){await fn();checks.push({name,ok:true});console.log('PASS',name);}
try{
  cli(['d1','migrations','apply','DB','--local','--persist-to',state]);
  base=await start();
  let alice,bob,mac,id,uploadId;
  await check('concurrent normalized username registration has one winner',async()=>{
    const names=['用户名Straße','用户名STRASSE','　用户名ＳＴＲＡＳＳＥ　'];
    const challenges=await Promise.all(names.map(username=>request('POST','/v1/auth/challenge',{username,purpose:'register'})));
    const payloads=names.map((name,i)=>authBody(credentials(name),challenges[i]));
    const replies=await Promise.all(payloads.map(async payload=>{const r=await fetch(base+'/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});return {status:r.status,value:await r.json()};}));
    assert.equal(replies.filter(r=>r.status===201).length,1,JSON.stringify(replies));assert.equal(replies.filter(r=>r.status===409).length,2);const stored=sql('SELECT auth_verifier FROM accounts');assert.ok(!stored.includes('safe testing password'));assert.ok(payloads.every(p=>!stored.includes(p.auth_key)));
    const loggedIn=await request('POST','/v1/auth/login',credentials('用户名strasse'));assert.equal(loggedIn.account.id,replies.find(r=>r.status===201).value.account.id);
    await request('POST','/v1/auth/challenge',{username:'bad\u200bcontrol',purpose:'register'},undefined,400);
  });
  await check('register without email; Windows and Mac independent opaque sessions',async()=>{
    alice=await request('POST','/v1/auth/register',credentials('alice'),undefined,201);
    bob=await request('POST','/v1/auth/register',credentials('中'),undefined,201);
    mac=await request('POST','/v1/auth/login',{...credentials('ALICE'),platform:'macos',device_name:'Mac test'});
    assert.equal(alice.account.email,null);assert.equal(mac.account.id,alice.account.id);assert.notEqual(alice.token,mac.token);
    assert.equal((await request('GET','/v1/devices',undefined,alice.token)).devices.length,2);
    assert.ok(!sql('SELECT token_hash FROM sessions').includes(alice.token));
    await request('POST','/v1/auth/login',{...credentials('alice'),password:'wrong testing password'},undefined,401);
  });
  await check('client KDF challenge is stable per user, purpose-bound, expiring and single-use',async()=>{
    const one=await request('POST','/v1/auth/challenge',{username:'alice',purpose:'login'}),two=await request('POST','/v1/auth/challenge',{username:'ALICE',purpose:'login'});
    assert.equal(one.salt,two.salt);assert.ok(Date.parse(one.expires_at)>Date.now());
    const payload=authBody(credentials('alice'),one);
    await request('POST','/v1/auth/register',payload,undefined,401);
    await request('POST','/v1/auth/login',payload);
    await request('POST','/v1/auth/login',payload,undefined,401);
    sql(`UPDATE auth_challenges SET expires_at=1 WHERE id='${two.challenge_id}'`);
    await request('POST','/v1/auth/login',authBody(credentials('alice'),two),undefined,401);
    const response=await fetch(base+'/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(credentials('alice'))});assert.equal(response.status,400);
    const unknown=await request('POST','/v1/auth/challenge',{username:'not-registered',purpose:'login'});await request('POST','/v1/auth/login',authBody(credentials('not-registered'),unknown),undefined,401);
  });
  await check('unknown fields, unauthenticated access and unconfigured sender fail',async()=>{
    await request('GET','/v1/me',undefined,undefined,401);
    await request('POST','/v1/email/code',{email:'nobody@example.com'},alice.token,503);
    assert.equal(JSON.parse(sql('SELECT COUNT(*) n FROM email_challenges'))[0].results[0].n,0);
    await request('POST','/v1/uploads',{unexpected:1},alice.token,400);
  });
  await check('persisted upload UUID recovers a lost initialization reply without duplicate reservation',async()=>{
    const owner=await request('POST','/v1/auth/register',credentials('init-retry-fixture'),undefined,201);
    const data=Buffer.from('unchanged original bytes after an initialization reply is lost'),newId=randomUUID(),clientId=randomUUID();
    const spec={upload_id:clientId,item_id:newId,base_version:0,kind:'file',name:'original.png',mime_type:'image/png',size:data.length,sha256:hash(data)};
    const before=sqlRows(`SELECT allocated_bytes FROM accounts WHERE id='${owner.account.id}'`)[0].allocated_bytes;
    const first=await request('POST','/v1/uploads',spec,owner.token,201);
    // Discarding this reply represents a disconnect after D1 committed.
    assert.equal(first.upload_id,clientId);
    const recovered=await request('POST','/v1/uploads',spec,owner.token,201);assert.deepEqual(recovered,first);
    assert.equal(sqlRows(`SELECT COUNT(*) n FROM allocations WHERE id='${clientId}'`)[0].n,1);
    assert.equal(sqlRows(`SELECT allocated_bytes FROM accounts WHERE id='${owner.account.id}'`)[0].allocated_bytes,before+data.length);
    for(const change of [{item_id:randomUUID()},{base_version:1},{kind:'text'},{name:'other.png'},{mime_type:'application/octet-stream'},{size:data.length+1},{sha256:'0'.repeat(64)}]){
      const conflict=await request('POST','/v1/uploads',{...spec,...change},owner.token,409);assert.equal(conflict.error.code,'UPLOAD_CONFLICT');
    }
    const foreign=await request('POST','/v1/uploads',spec,bob.token,404);assert.equal(foreign.error.code,'NOT_FOUND');assert.equal(foreign.error.current_record,undefined);
    await request('POST','/v1/uploads',{...spec,upload_id:'not-a-uuid'},owner.token,400);
    sql(`UPDATE uploads SET state='verifying',lease='fixture',lease_until=${Date.now()+60000} WHERE id='${clientId}'`);
    assert.deepEqual(await request('POST','/v1/uploads',spec,owner.token,201),first);
    sql(`UPDATE uploads SET state='pending',lease=NULL,lease_until=0 WHERE id='${clientId}'`);
    await part(owner.token,clientId,0,data);const committed=await request('POST',`/v1/uploads/${clientId}/commit`,{},owner.token);
    // The item's version advanced, but repeating the same initialization must
    // still recover the original upload before ordinary item CAS checks.
    const afterCommit=await request('POST','/v1/uploads',spec,owner.token,201);assert.equal(afterCommit.upload_id,clientId);
    assert.equal((await request('POST',`/v1/uploads/${clientId}/commit`,{},owner.token)).record.version,1);
    const cannotCancel=await request('DELETE',`/v1/uploads/${clientId}`,undefined,owner.token,409);
    assert.equal(cannotCancel.error.code,'ALREADY_COMMITTED');assert.deepEqual(cannotCancel.error.current_record,committed.record);
    const denied=await request('DELETE',`/v1/uploads/${clientId}`,undefined,bob.token,404);assert.equal(denied.error.current_record,undefined);
    await request('DELETE',`/v1/items/${newId}`,{base_version:committed.record.version},owner.token);
    assert.equal(sqlRows(`SELECT allocated_bytes FROM accounts WHERE id='${owner.account.id}'`)[0].allocated_bytes,before);
    const expiredSpec={...spec,upload_id:randomUUID(),item_id:randomUUID()};await request('POST','/v1/uploads',expiredSpec,owner.token,201);
    sql(`UPDATE uploads SET expires_at=1 WHERE id='${expiredSpec.upload_id}'`);
    assert.equal((await request('POST','/v1/uploads',expiredSpec,owner.token,409)).error.code,'UPLOAD_EXPIRED');
    await request('DELETE',`/v1/uploads/${expiredSpec.upload_id}`,undefined,owner.token);
  });
  await check('concurrent initialization retries reserve one upload and reject a different spec',async()=>{
    const data=Buffer.from('concurrent idempotent initialization'),clientId=randomUUID(),newId=randomUUID();
    const spec={upload_id:clientId,item_id:newId,base_version:0,kind:'file',name:'concurrent.bin',mime_type:'application/octet-stream',size:data.length,sha256:hash(data)};
    const replies=await Promise.all(Array.from({length:8},()=>request('POST','/v1/uploads',spec,alice.token,201)));
    assert.ok(replies.every(value=>value.upload_id===clientId));assert.equal(sqlRows(`SELECT COUNT(*) n FROM uploads WHERE item_id='${newId}'`)[0].n,1);
    assert.equal(sqlRows(`SELECT COUNT(*) n FROM allocations WHERE id='${clientId}'`)[0].n,1);
    await request('DELETE',`/v1/uploads/${clientId}`,undefined,alice.token);
    const conflictingId=randomUUID(),bodies=[{...spec,upload_id:conflictingId,item_id:randomUUID()},{...spec,upload_id:conflictingId,item_id:randomUUID(),name:'different.bin'}];
    const attempts=await Promise.all(bodies.map(async body=>{
      const response=await fetch(base+'/v1/uploads',{method:'POST',headers:{Authorization:'Bearer '+alice.token,'Content-Type':'application/json'},body:JSON.stringify(body)});
      return {status:response.status,value:await response.json()};
    }));
    assert.deepEqual(attempts.map(value=>value.status).sort(),[201,409]);assert.equal(attempts.find(value=>value.status===409).value.error.code,'UPLOAD_CONFLICT');
    assert.equal(sqlRows(`SELECT COUNT(*) n FROM allocations WHERE id='${conflictingId}'`)[0].n,1);
    await request('DELETE',`/v1/uploads/${conflictingId}`,undefined,alice.token);
  });
  await check('pending and missing parts are not committed/synchronized',async()=>{
    id=randomUUID();const data=Buffer.from('same account cross-platform content');
    const reserved=await reserve(alice.token,id,data);uploadId=reserved.upload_id;
    await request('POST',`/v1/uploads/${uploadId}/commit`,{},alice.token,409);
    assert.deepEqual((await request('GET','/v1/sync/changes?cursor=0',undefined,mac.token)).changes,[]);
    await part(alice.token,uploadId,0,data,400,'0'.repeat(64));
    await part(bob.token,uploadId,0,data,404);
    await part(alice.token,uploadId,0,data);
    await part(alice.token,uploadId,0,data); // exact retry is idempotent
    await part(alice.token,uploadId,0,Buffer.alloc(data.length,7),409);
    const committed=await request('POST',`/v1/uploads/${uploadId}/commit`,{},alice.token);
    assert.equal(committed.record.version,1);
    assert.equal((await request('POST',`/v1/uploads/${uploadId}/commit`,{},alice.token)).record.version,1);
    const metadata=await request('GET',`/v1/items/${id}/content`,undefined,mac.token);assert.equal(metadata.part_count,1);
    const response=await fetch(base+`/v1/items/${id}/parts/0?version=1`,{headers:{Authorization:'Bearer '+mac.token}});assert.equal(response.status,200);assert.deepEqual(Buffer.from(await response.arrayBuffer()),data);
    const denied=await fetch(base+`/v1/items/${id}/content`,{headers:{Authorization:'Bearer '+bob.token}});assert.equal(denied.status,404);
    assert.deepEqual((await request('GET','/v1/sync/changes?cursor=0',undefined,bob.token)).changes,[]);
  });
  await check('full SHA mismatch never creates a live item',async()=>{
    const newId=randomUUID(),data=Buffer.from('wrong declared full hash');const reserved=await reserve(alice.token,newId,data,0,{sha256:'0'.repeat(64)});
    await part(alice.token,reserved.upload_id,0,data);await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token,400);
    assert.ok(!(await request('GET','/v1/sync/changes?cursor=0',undefined,alice.token)).changes.some(c=>c.record.id===newId));
    await request('DELETE',`/v1/uploads/${reserved.upload_id}`,undefined,alice.token);
  });
  await check('parallel CAS commits permit one writer; deletion emits durable tombstone',async()=>{
    const a=await reserve(alice.token,id,Buffer.from('device one'),1),b=await reserve(mac.token,id,Buffer.from('device two'),1);
    await part(alice.token,a.upload_id,0,Buffer.from('device one'));await part(mac.token,b.upload_id,0,Buffer.from('device two'));
    const replies=await Promise.all([a,b].map(async u=>{const r=await fetch(base+`/v1/uploads/${u.upload_id}/commit`,{method:'POST',headers:{Authorization:'Bearer '+alice.token,'Content-Type':'application/json'},body:'{}'});return {status:r.status,body:await r.json()};}));
    assert.deepEqual(replies.map(r=>r.status).sort(),[200,409]);const loser=replies.find(r=>r.status===409);assert.equal(loser.body.error.current_record.version,2);
    await request('DELETE',`/v1/items/${id}`,{base_version:1},alice.token,409);
    const tombstone=await request('DELETE',`/v1/items/${id}`,{base_version:2},mac.token);assert.equal(tombstone.record.deleted,true);assert.equal(tombstone.record.version,3);
    const changes=await request('GET','/v1/sync/changes?cursor=0&limit=1',undefined,alice.token);assert.equal(changes.has_more,true);
    const rest=await request('GET','/v1/sync/changes?cursor='+changes.cursor,undefined,alice.token);assert.equal(rest.changes.at(-1).record.deleted,true);
    assert.equal((await fetch(base+`/v1/items/${id}/content`,{headers:{Authorization:'Bearer '+alice.token}})).status,404);
  });
  await check('cancel and commit use one state CAS; active content never becomes garbage',async()=>{
    for(let attempt=0;attempt<12;attempt++){
      const newId=randomUUID(),data=Buffer.from('cancel vs commit '+attempt),reserved=await reserve(alice.token,newId,data);
      await part(alice.token,reserved.upload_id,0,data);
      const result=await Promise.all([
        fetch(base+`/v1/uploads/${reserved.upload_id}/commit`,{method:'POST',headers:{Authorization:'Bearer '+alice.token,'Content-Type':'application/json'},body:'{}'}),
        fetch(base+`/v1/uploads/${reserved.upload_id}`,{method:'DELETE',headers:{Authorization:'Bearer '+alice.token}}),
      ]);
      assert.ok(result.every(r=>[200,404,409,423].includes(r.status)));
      assert.ok(!result.every(r=>r.status===200),'Both cancellation and commitment cannot succeed');
    }
    const result=JSON.parse(sql("SELECT COUNT(*) n FROM items i JOIN uploads u ON u.id=i.active_upload WHERE i.deleted=0 AND u.state<>'committed'"));assert.equal(result[0].results[0].n,0);
    const committed=await reserve(alice.token,randomUUID(),Buffer.from('immutable'));
    await part(alice.token,committed.upload_id,0,Buffer.from('immutable'));await request('POST',`/v1/uploads/${committed.upload_id}/commit`,{},alice.token);
    await request('DELETE',`/v1/uploads/${committed.upload_id}`,undefined,alice.token,409);
  });
  await check('multipart upload streams through real R2 and verifies complete bytes',async()=>{
    const data=Buffer.alloc(1024*1024+123,0x6b),newId=randomUUID(),reserved=await reserve(alice.token,newId,data);assert.equal(reserved.part_count,2);
    await request('GET',`/v1/uploads/${reserved.upload_id}`,undefined,bob.token,404);
    const initial=await request('GET',`/v1/uploads/${reserved.upload_id}`,undefined,mac.token);
    assert.equal(initial.item_id,newId);assert.equal(initial.base_version,0);assert.equal(initial.state,'pending');assert.equal(initial.next_part,0);assert.equal(initial.size,data.length);assert.equal(initial.sha256,hash(data));assert.equal(initial.chunk_size,reserved.chunk_size);assert.equal(initial.part_count,2);
    await part(mac.token,reserved.upload_id,1,data.subarray(reserved.chunk_size),409);
    await part(alice.token,reserved.upload_id,0,data.subarray(0,reserved.chunk_size));
    const resumable=await request('GET',`/v1/uploads/${reserved.upload_id}`,undefined,alice.token);assert.equal(resumable.next_part,1);assert.equal(resumable.state,'pending');
    sql(`UPDATE uploads SET expires_at=${Date.now()+10000} WHERE id='${reserved.upload_id}'`);
    await part(mac.token,reserved.upload_id,1,data.subarray(reserved.chunk_size));
    const complete=await request('GET',`/v1/uploads/${reserved.upload_id}`,undefined,alice.token);assert.equal(complete.next_part,2);assert.ok(Date.parse(complete.expires_at)>Date.now()+71*3600000,'Each successful part must renew the 72-hour pending lease');
    const committed=await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},mac.token);assert.equal(committed.record.sha256,hash(data));
    assert.equal((await request('GET',`/v1/uploads/${reserved.upload_id}`,undefined,alice.token)).state,'committed');
    const metadata=await request('GET',`/v1/items/${newId}/content`,undefined,alice.token);const checksum=createHash('sha256');let size=0;
    for(let i=0;i<metadata.part_count;i++){const response=await fetch(base+`/v1/items/${newId}/parts/${i}?version=1`,{headers:{Authorization:'Bearer '+alice.token}});assert.equal(response.status,200);for await(const chunk of response.body){size+=chunk.length;checksum.update(chunk);}}
    assert.equal(size,data.length);assert.equal(checksum.digest('hex'),hash(data));
    await request('GET',`/v1/items/${newId}/parts/0?version=2`,undefined,alice.token,409);
  });
  await check('65 MiB original file crosses the former 64 MiB limit and preserves every byte',async()=>{
    const chunk=Buffer.alloc(1024*1024,0x79),fullHash=createHash('sha256');for(let i=0;i<65;i++)fullHash.update(chunk);
    const expected=fullHash.digest('hex'),newId=randomUUID();
    const reserved=await request('POST','/v1/uploads',{item_id:newId,base_version:0,kind:'file',name:'large-video.mp4',mime_type:'video/mp4',size:65*1024*1024,sha256:expected},alice.token,201);
    assert.equal(reserved.part_count,65);for(let i=0;i<65;i++)await part(alice.token,reserved.upload_id,i,chunk);
    const committed=await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token);assert.equal(committed.record.size,65*1024*1024);assert.equal(committed.record.expires_at,null);
    let size=0;const actual=createHash('sha256');
    for(let i=0;i<65;i++){const response=await fetch(base+`/v1/items/${newId}/parts/${i}?version=1`,{headers:{Authorization:'Bearer '+alice.token}});assert.equal(response.status,200);const chunkHash=createHash('sha256');for await(const data of response.body){size+=data.length;actual.update(data);chunkHash.update(data);}assert.equal(chunkHash.digest('hex'),response.headers.get('X-Content-SHA256'));}
    assert.equal(size,65*1024*1024);assert.equal(actual.digest('hex'),expected);
    await request('DELETE',`/v1/items/${newId}`,{base_version:1},alice.token);
    assert.equal(sqlRows(`SELECT COUNT(*) n FROM uploads WHERE id='${reserved.upload_id}'`)[0].n,0);
  });
  await check('deleting an item immediately removes actual R2 objects, parts and allocation with exhausted A/B budgets',async()=>{
    const data=Buffer.concat([Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64'),Buffer.from('original image metadata/trailing bytes\0\xff')]),newId=randomUUID();
    const reserved=await reserve(alice.token,newId,data,0,{name:'original-image.png',mime_type:'image/png'});
    await part(alice.token,reserved.upload_id,0,data);
    await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token);
    const downloaded=await fetch(base+`/v1/items/${newId}/parts/0?version=1`,{headers:{Authorization:'Bearer '+alice.token}});
    assert.equal(downloaded.status,200);assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()),data);assert.equal(downloaded.headers.get('X-Content-SHA256'),hash(data));
    const key=sqlRows(`SELECT key FROM parts WHERE upload_id='${reserved.upload_id}'`)[0].key;
    await localObject(key,data);
    const period=new Date().toISOString().slice(0,7),saved=sqlRows(`SELECT kind,n FROM budgets WHERE period='${period}'`);
    const before=sqlRows(`SELECT allocated_bytes n FROM accounts WHERE id='${alice.account.id}'`)[0].n;
    sql(`INSERT INTO budgets(period,kind,n) VALUES('${period}','A',800001) ON CONFLICT(period,kind) DO UPDATE SET n=excluded.n;INSERT INTO budgets(period,kind,n) VALUES('${period}','B',8000001) ON CONFLICT(period,kind) DO UPDATE SET n=excluded.n;`);
    try{
      await request('GET',`/v1/items/${newId}/parts/0?version=1`,undefined,alice.token,503);
      assert.equal(sqlRows(`SELECT COUNT(*) n FROM read_leases WHERE upload_id='${reserved.upload_id}'`)[0].n,0,'A budget-denied download must release its reader lease');
      const deniedBudgets=sqlRows(`SELECT kind,n FROM budgets WHERE period='${period}' ORDER BY kind`);
      const deleted=await request('DELETE',`/v1/items/${newId}`,{base_version:1},alice.token);
      assert.equal(deleted.record.deleted,true);assert.equal(deleted.record.version,2);
      assert.equal(sqlRows(`SELECT COUNT(*) n FROM uploads WHERE id='${reserved.upload_id}'`)[0].n,0);
      assert.equal(sqlRows(`SELECT COUNT(*) n FROM parts WHERE upload_id='${reserved.upload_id}'`)[0].n,0);
      assert.equal(sqlRows(`SELECT COUNT(*) n FROM allocations WHERE id='${reserved.upload_id}'`)[0].n,0);
      assert.equal(sqlRows(`SELECT allocated_bytes n FROM accounts WHERE id='${alice.account.id}'`)[0].n,before-data.length);
      assert.deepEqual(sqlRows(`SELECT kind,n FROM budgets WHERE period='${period}' ORDER BY kind`),deniedBudgets,'R2 deletion must not consume Class A or B budget');
      await localObject(key,null);
    }finally{
      sql(`DELETE FROM budgets WHERE period='${period}';`+saved.map(row=>`INSERT INTO budgets(period,kind,n) VALUES('${period}','${row.kind}',${row.n});`).join(''));
    }
  });
  let expiringFixture;
  await check('D1 committed-size fixtures: exactly 500 MiB has no TTL; one byte larger expires 24 hours after commit (no large transfer)',async()=>{
    for(const size of [524288000,524288001]){
      const newId=randomUUID(),data=Buffer.from('small actual object for metadata-only retention fixture '+size);
      const reserved=await reserve(alice.token,newId,data);await part(alice.token,reserved.upload_id,0,data);
      // Only the size/verified-size metadata and its matching allocation are
      // simulated. The commit endpoint and D1 retention triggers are real;
      // the object stored in R2 is the small payload above, not a 500 MiB file.
      sql(`DELETE FROM allocations WHERE id='${reserved.upload_id}';INSERT INTO allocations(id,account_id,size) VALUES('${reserved.upload_id}','${alice.account.id}',${size});UPDATE uploads SET size=${size},verified_size=${size} WHERE id='${reserved.upload_id}';`);
      const committed=await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token);
      const stored=sqlRows(`SELECT updated_at,expires_at FROM items WHERE id='${newId}' AND account_id='${alice.account.id}'`)[0];
      assert.equal(committed.record.size,size);
      if(size===524288000){assert.equal(stored.expires_at,null);assert.equal(committed.record.expires_at,null);await request('DELETE',`/v1/items/${newId}`,{base_version:1},alice.token);}
      else{
        assert.equal(stored.expires_at-stored.updated_at,86400000);
        assert.equal(Date.parse(committed.record.expires_at),stored.expires_at);
        expiringFixture={newId,uploadId:reserved.upload_id,data,size,key:sqlRows(`SELECT key FROM parts WHERE upload_id='${reserved.upload_id}'`)[0].key};
      }
    }
  });
  await check('expiry publishes a tombstone immediately, protects an existing reader, and cron reclaims R2 after its lease expires',async()=>{
    const value=expiringFixture;
    const cursor=(await request('GET','/v1/sync/changes?cursor=0&limit=200',undefined,alice.token)).cursor;
    sql(`INSERT INTO read_leases(upload_id,id,expires_at) VALUES('${value.uploadId}','retention-reader',${Date.now()+60000});UPDATE items SET expires_at=1 WHERE account_id='${alice.account.id}' AND id='${value.newId}';`);
    const changed=await request('GET','/v1/sync/changes?cursor='+cursor,undefined,alice.token);
    const tombstone=changed.changes.filter(c=>c.record.id===value.newId).at(-1).record;
    assert.equal(tombstone.deleted,true);assert.equal(tombstone.version,2);assert.equal(tombstone.size,0);assert.equal(tombstone.expires_at,null);
    await request('GET',`/v1/items/${value.newId}/content`,undefined,alice.token,404);
    assert.equal(sqlRows(`SELECT state FROM uploads WHERE id='${value.uploadId}'`)[0].state,'garbage');
    assert.equal(sqlRows(`SELECT size FROM allocations WHERE id='${value.uploadId}'`)[0].size,value.size);
    await localObject(value.key,value.data);
    sql(`UPDATE read_leases SET expires_at=1 WHERE upload_id='${value.uploadId}';`);
    let count=1;
    for(let i=0;i<30&&count;i++){
      const response=await fetch(base+'/__scheduled?cron=*+*+*+*+*');assert.equal(response.status,200);
      await sleep(100);count=sqlRows(`SELECT COUNT(*) n FROM uploads WHERE id='${value.uploadId}'`)[0].n;
    }
    assert.equal(count,0,'Minute cleanup must eventually reclaim the retired reader-protected upload');
    assert.equal(sqlRows(`SELECT COUNT(*) n FROM allocations WHERE id='${value.uploadId}'`)[0].n,0);
    assert.equal(sqlRows(`SELECT COUNT(*) n FROM parts WHERE upload_id='${value.uploadId}'`)[0].n,0);
    await localObject(value.key,null);
    const stored=sqlRows(`SELECT deleted,version,active_upload FROM items WHERE account_id='${alice.account.id}' AND id='${value.newId}'`)[0];
    assert.deepEqual(stored,{deleted:1,version:2,active_upload:null});
  });
  await check('account status that triggers expiry returns the reclaimed storage count in the same response',async()=>{
    const data=Buffer.from('expired status fixture'),newId=randomUUID(),reserved=await reserve(alice.token,newId,data);
    await part(alice.token,reserved.upload_id,0,data);await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token);
    const before=sqlRows(`SELECT allocated_bytes n FROM accounts WHERE id='${alice.account.id}'`)[0].n;
    sql(`UPDATE items SET expires_at=1 WHERE account_id='${alice.account.id}' AND id='${newId}'`);
    const status=await request('GET','/v1/me',undefined,alice.token);assert.equal(status.storage.used_bytes,before-data.length);
    assert.equal(sqlRows(`SELECT deleted FROM items WHERE account_id='${alice.account.id}' AND id='${newId}'`)[0].deleted,1);
  });
  await check('exhausted daily service request budget still permits item deletion and abandoned upload cancellation',async()=>{
    const data=Buffer.from('delete despite the normal request budget'),newId=randomUUID();
    const committed=await reserve(alice.token,newId,data);await part(alice.token,committed.upload_id,0,data);await request('POST',`/v1/uploads/${committed.upload_id}/commit`,{},alice.token);
    const abandoned=await reserve(alice.token,randomUUID(),data);await part(alice.token,abandoned.upload_id,0,data);
    const fixtures=[committed,abandoned].map(value=>({id:value.upload_id,key:sqlRows(`SELECT key FROM parts WHERE upload_id='${value.upload_id}'`)[0].key}));
    const slot=Math.floor(Date.now()/86400000),key='service-requests:'+slot,deleteKey='service-deletions:'+slot;
    const saved=sqlRows(`SELECT key,n,expires_at FROM rates WHERE key='${key}'`);
    const previousDeletions=sqlRows(`SELECT n FROM rates WHERE key='${deleteKey}'`)[0]?.n||0;
    sql(`INSERT INTO rates(key,n,expires_at) VALUES('${key}',5000,${(slot+1)*86400000}) ON CONFLICT(key) DO UPDATE SET n=5000;`);
    try{
      await request('GET','/v1/me',undefined,alice.token,429);
      const committedReply=await request('DELETE',`/v1/uploads/${committed.upload_id}`,undefined,alice.token,409);
      assert.equal(committedReply.error.code,'ALREADY_COMMITTED');assert.equal(committedReply.error.current_record.id,newId);assert.equal(committedReply.error.current_record.version,1);
      const deleted=await request('DELETE',`/v1/items/${newId}`,{base_version:committedReply.error.current_record.version},alice.token);assert.equal(deleted.record.deleted,true);
      assert.equal((await request('DELETE',`/v1/uploads/${abandoned.upload_id}`,undefined,alice.token)).cancelled,true);
      assert.equal(sqlRows(`SELECT n FROM rates WHERE key='${key}'`)[0].n,5001,'Cleanup calls must not consume the exhausted normal request budget');
      assert.equal(sqlRows(`SELECT n FROM rates WHERE key='${deleteKey}'`)[0].n,previousDeletions+3,'Cleanup calls have their own bounded daily budget');
      for(const fixture of fixtures){
        assert.equal(sqlRows(`SELECT COUNT(*) n FROM uploads WHERE id='${fixture.id}'`)[0].n,0);
        assert.equal(sqlRows(`SELECT COUNT(*) n FROM allocations WHERE id='${fixture.id}'`)[0].n,0);
        assert.equal(sqlRows(`SELECT COUNT(*) n FROM parts WHERE upload_id='${fixture.id}'`)[0].n,0);
        await localObject(fixture.key,null);
      }
    }finally{
      sql(`DELETE FROM rates WHERE key='${key}';`+saved.map(row=>`INSERT INTO rates(key,n,expires_at) VALUES('${row.key}',${row.n},${row.expires_at});`).join(''));
    }
  });
  await check('revoked device cannot read, sync or upload immediately',async()=>{
    await request('DELETE','/v1/devices/'+mac.device_id,undefined,alice.token);
    await request('GET','/v1/me',undefined,mac.token,401);await request('GET','/v1/sync/changes',undefined,mac.token,401);
    await request('POST','/v1/uploads',{},mac.token,401);await request('GET','/v1/me',undefined,alice.token);
  });
  await check('email code single-use, expiry, attempts, and ownership enforced by D1',async()=>{
    const challenge=randomUUID(),code='123456',digest=createHmac('sha256',secret).update(challenge+'\0'+code).digest('hex');
    sql(`INSERT INTO email_challenges(id,account_id,email,code_hash,expires_at,sent) VALUES('${challenge}','${alice.account.id}','verified@example.com','${digest}',${Date.now()+60000},1)`);
    await request('POST','/v1/email/bind',{challenge_id:challenge,code},bob.token,400);
    const bound=await request('POST','/v1/email/bind',{challenge_id:challenge,code},alice.token);assert.equal(bound.account.email,'verified@example.com');
    await request('POST','/v1/email/bind',{challenge_id:challenge,code},alice.token,400);
    for(const expired of [true,false]){
      const id=randomUUID(),h=createHmac('sha256',secret).update(id+'\0'+code).digest('hex');
      sql(`INSERT INTO email_challenges(id,account_id,email,code_hash,expires_at,sent,attempts) VALUES('${id}','${alice.account.id}','other@example.com','${h}',${Date.now()+(expired?-1000:60000)},1,${expired?0:5})`);
      await request('POST','/v1/email/bind',{challenge_id:id,code},alice.token,400);
    }
  });
  await check('account and global storage reservations reject concurrent overbooking',async()=>{
    sql(`UPDATE accounts SET quota_bytes=10 WHERE id='${bob.account.id}'`);
    const attempts=await Promise.all([1,2].map(async()=>{const r=await fetch(base+'/v1/uploads',{method:'POST',headers:{Authorization:'Bearer '+bob.token,'Content-Type':'application/json'},body:JSON.stringify({item_id:randomUUID(),base_version:0,kind:'file',name:'a',mime_type:'application/octet-stream',size:6,sha256:hash('123456')})});return r.status;}));assert.deepEqual(attempts.sort(),[201,413]);
    await stop();base=await start(['--var','GLOBAL_QUOTA_BYTES:1']);
    await request('POST','/v1/uploads',{item_id:randomUUID(),base_version:0,kind:'file',name:'q',mime_type:'application/octet-stream',size:2,sha256:hash('12')},alice.token,413);
  });
  await check('twenty expired devices do not permanently lock a registered account',async()=>{
    let command=`UPDATE sessions SET expires_at=1 WHERE account_id='${bob.account.id}';`;
    for(let i=0;i<19;i++){const id=randomUUID();command+=`INSERT INTO devices(id,account_id,name,platform,created_at,last_seen_at) VALUES('${id}','${bob.account.id}','expired','windows',1,1);INSERT INTO sessions(token_hash,account_id,device_id,expires_at) VALUES('${hash(id)}','${bob.account.id}','${id}',1);`;}
    sql(command);
    const recovered=await request('POST','/v1/auth/login',credentials('中'));assert.ok(recovered.token);
    const devices=(await request('GET','/v1/devices',undefined,recovered.token)).devices;assert.equal(devices.filter(d=>d.revoked_at===null).length,1);assert.equal(devices.filter(d=>d.revoked_at!==null).length,20);
    await request('GET','/v1/me',undefined,bob.token,401);
  });
  await check('R2 API request budget denies before a storage write',async()=>{
    await stop();base=await start(['--var','R2_CLASS_A_BUDGET:1']);
    const data=Buffer.from('budget'),reserved=await reserve(alice.token,randomUUID(),data);
    const response=await part(alice.token,reserved.upload_id,0,data,503);assert.equal(response.error.code,'STORAGE_REQUEST_BUDGET');
    const result=JSON.parse(sql(`SELECT ready FROM parts WHERE upload_id='${reserved.upload_id}'`));assert.equal(result[0].results[0].ready,0);
  });
  await check('scheduled cleanup releases cancelled/expired reservations after object cleanup',async()=>{
    await stop();base=await start();
    const data=Buffer.from('abandoned upload'),reserved=await reserve(alice.token,randomUUID(),data);
    await part(alice.token,reserved.upload_id,0,data);await request('DELETE',`/v1/uploads/${reserved.upload_id}`,undefined,alice.token);
    sql(`UPDATE uploads SET garbage_after=1 WHERE id='${reserved.upload_id}'`);
    const response=await fetch(base+'/__scheduled?cron=17+*+*+*+*');assert.equal(response.status,200);
    let count=1;for(let i=0;i<20;i++){count=JSON.parse(sql(`SELECT COUNT(*) n FROM allocations WHERE id='${reserved.upload_id}'`))[0].results[0].n;if(!count)break;await sleep(200);}assert.equal(count,0);
    await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token,404);
  });
  await check('GC respects an existing reader and prevents late reader resurrection',async()=>{
    const data=Buffer.from('leased'),reserved=await reserve(alice.token,randomUUID(),data);await part(alice.token,reserved.upload_id,0,data);
    sql(`INSERT INTO read_leases(upload_id,id,expires_at) VALUES('${reserved.upload_id}','test-reader',${Date.now()+60000})`);
    await request('DELETE',`/v1/uploads/${reserved.upload_id}`,undefined,alice.token);
    sql(`UPDATE uploads SET garbage_after=1 WHERE id='${reserved.upload_id}'`);
    await fetch(base+'/__scheduled?cron=17+*+*+*+*');
    assert.equal(JSON.parse(sql(`SELECT COUNT(*) n FROM uploads WHERE id='${reserved.upload_id}'`))[0].results[0].n,1);
    sql(`DELETE FROM read_leases WHERE upload_id='${reserved.upload_id}';UPDATE uploads SET lease='gc:test',lease_until=${Date.now()+60000} WHERE id='${reserved.upload_id}'`);
    const result=spawnSync(process.execPath,[wrangler,'--config',configFile,'d1','execute','DB','--local','--persist-to',state,'--command',`INSERT INTO read_leases(upload_id,id,expires_at) VALUES('${reserved.upload_id}','late-reader',${Date.now()+60000})`],{cwd:state,env,encoding:'utf8',windowsHide:true});assert.notEqual(result.status,0);
  });
  await check('missing authentication pepper fails closed without creating an account',async()=>{
    await stop();base=await start(['--var','AUTH_PEPPER:short']);
    const response=await request('POST','/v1/auth/challenge',{username:'new-user',purpose:'register'},undefined,503);assert.equal(response.error.code,'AUTH_NOT_CONFIGURED');
  });
  await writeFile(join(root,'test-results.json'),JSON.stringify({mode:'real wrangler local D1/R2; no cloud deployment or email',checks},null,2));
  console.log('ALL',checks.length,'LOCAL INTEGRATION CHECKS PASSED');
}catch(error){console.error(error);console.error(output.slice(-4000));process.exitCode=1;}
finally{await stop();assert.ok(resolve(state).startsWith(resolve(tmpdir())+sep+'flickdock-cloud-local-'));await rm(state,{recursive:true,force:true,maxRetries:30,retryDelay:200});}
