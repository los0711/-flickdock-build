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
    await part(mac.token,reserved.upload_id,1,data.subarray(reserved.chunk_size),409);
    await part(alice.token,reserved.upload_id,0,data.subarray(0,reserved.chunk_size));await part(mac.token,reserved.upload_id,1,data.subarray(reserved.chunk_size));
    const committed=await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},mac.token);assert.equal(committed.record.sha256,hash(data));
    const metadata=await request('GET',`/v1/items/${newId}/content`,undefined,alice.token);const checksum=createHash('sha256');let size=0;
    for(let i=0;i<metadata.part_count;i++){const response=await fetch(base+`/v1/items/${newId}/parts/${i}?version=1`,{headers:{Authorization:'Bearer '+alice.token}});assert.equal(response.status,200);for await(const chunk of response.body){size+=chunk.length;checksum.update(chunk);}}
    assert.equal(size,data.length);assert.equal(checksum.digest('hex'),hash(data));
    await request('GET',`/v1/items/${newId}/parts/0?version=2`,undefined,alice.token,409);
  });
  await check('64 MiB file uses 64 bounded requests with persisted server SHA256',async()=>{
    const chunk=Buffer.alloc(1024*1024,0x79),fullHash=createHash('sha256');for(let i=0;i<64;i++)fullHash.update(chunk);
    const expected=fullHash.digest('hex'),newId=randomUUID();
    const reserved=await request('POST','/v1/uploads',{item_id:newId,base_version:0,kind:'file',name:'large-video.mp4',mime_type:'video/mp4',size:64*1024*1024,sha256:expected},alice.token,201);
    assert.equal(reserved.part_count,64);for(let i=0;i<64;i++)await part(alice.token,reserved.upload_id,i,chunk);
    const committed=await request('POST',`/v1/uploads/${reserved.upload_id}/commit`,{},alice.token);assert.equal(committed.record.size,64*1024*1024);
    let size=0;const actual=createHash('sha256');
    for(let i=0;i<64;i++){const response=await fetch(base+`/v1/items/${newId}/parts/${i}?version=1`,{headers:{Authorization:'Bearer '+alice.token}});assert.equal(response.status,200);const chunkHash=createHash('sha256');for await(const data of response.body){size+=data.length;actual.update(data);chunkHash.update(data);}assert.equal(chunkHash.digest('hex'),response.headers.get('X-Content-SHA256'));}
    assert.equal(size,64*1024*1024);assert.equal(actual.digest('hex'),expected);
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
