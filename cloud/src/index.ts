import {createHash, createHmac, timingSafeEqual} from 'node:crypto';
import {caseFold} from 'unicode-case-folding';
import {PersistentSHA256} from './hash-state';

interface Env {
  DB:D1Database; FILES:R2Bucket;
  ACCOUNT_QUOTA_BYTES?:string; GLOBAL_QUOTA_BYTES?:string; CHUNK_BYTES?:string; REQUESTS_PER_DAY?:string; AUTH_PEPPER?:string;
  R2_CLASS_A_BUDGET?:string; R2_CLASS_B_BUDGET?:string;
  EMAIL_PROVIDER?:string; EMAIL_FROM?:string; EMAIL_CODE_SECRET?:string; RESEND_API_KEY?:string;
  EMAIL?:{send(message:{from:string;to:string;subject:string;text:string}):Promise<{messageId:string}>};
}
type Row=Record<string,any>;
const DAY=86400000;
function chunkSize(env:Env){const size=config(env.CHUNK_BYTES,1024*1024,1024*1024);if(size%64||size<65536)fail(503,'CONFIGURATION','Chunk size must be 64-byte aligned and at least 64 KiB');return size;}
class APIError extends Error {constructor(public status:number,public code:string,message:string,public details:Row={}){super(message);}}
const fail=(status:number,code:string,message:string,details:Row={}):never=>{throw new APIError(status,code,message,details);};
const json=(data:Row,status=200)=>Response.json({schema_version:1,...data},{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
const hex=(data:Uint8Array)=>Buffer.from(data).toString('hex');
const sha=(data:string|Uint8Array)=>createHash('sha256').update(data).digest('hex');
const iso=(value:number)=>new Date(value).toISOString();
function config(value:string|undefined,fallback:number,max=Number.MAX_SAFE_INTEGER){const n=value===undefined?fallback:Number(value);if(!Number.isSafeInteger(n)||n<1||n>max)fail(503,'CONFIGURATION','Service configuration is invalid');return n;}
function text(value:unknown,name:string,min=1,max=255):string {if(typeof value!=='string'||value.length<min||value.length>max||/[\x00-\x1f\x7f]/.test(value))fail(400,'INVALID_REQUEST',`Invalid ${name}`);return value as string;}
function integer(value:unknown,name:string,min=0,max=Number.MAX_SAFE_INTEGER):number{if(typeof value!=='number'||!Number.isSafeInteger(value)||value<min||value>max)fail(400,'INVALID_REQUEST',`Invalid ${name}`);return value as number;}
function uuid(value:unknown,name='id'):string{const s=text(value,name,36,36);if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(s))fail(400,'INVALID_REQUEST',`Invalid ${name}`);return s;}
function digest(value:unknown){const s=text(value,'sha256',64,64);if(!/^[0-9a-f]{64}$/.test(s))fail(400,'INVALID_REQUEST','sha256 must be lowercase hex');return s;}
async function bytes(request:Request,max:number){
  const declared=request.headers.get('Content-Length');
  if(declared!==null&&(!/^\d+$/.test(declared)||Number(declared)>max))fail(413,'BODY_TOO_LARGE','Request body exceeds limit');
  const chunks:Uint8Array[]=[];let size=0;
  const reader=request.body?.getReader();
  if(reader)try{while(true){const {value,done}=await reader.read();if(done)break;size+=value.byteLength;if(size>max){await reader.cancel();fail(413,'BODY_TOO_LARGE','Request body exceeds limit');}chunks.push(value);}}finally{reader.releaseLock();}
  const out=new Uint8Array(size);let offset=0;for(const chunk of chunks){out.set(chunk,offset);offset+=chunk.byteLength;}return out;
}
async function body(request:Request,allowed:string[],required=allowed):Promise<Row>{
  if(!request.headers.get('Content-Type')?.toLowerCase().startsWith('application/json'))fail(415,'JSON_REQUIRED','Expected application/json');
  let data:any;try{data=JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(await bytes(request,16384)));}catch(e){if(e instanceof APIError)throw e;fail(400,'INVALID_JSON','Invalid JSON');}
  if(!data||typeof data!=='object'||Array.isArray(data)||Object.keys(data).some(k=>!allowed.includes(k))||required.some(k=>!(k in data)))fail(400,'INVALID_REQUEST','Missing or unknown request field');
  return data;
}
const query=(env:Env,sql:string,...values:any[])=>env.DB.prepare(sql).bind(...values);
async function first(env:Env,sql:string,...values:any[]){return query(env,sql,...values).first<Row>();}
async function rows(env:Env,sql:string,...values:any[]){return (await query(env,sql,...values).all<Row>()).results;}
async function rate(env:Env,key:string,limit:number,window:number){
  const now=Date.now(),slot=Math.floor(now/window);
  const row=await first(env,'INSERT INTO rates(key,n,expires_at) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET n=n+1 RETURNING n',key+':'+slot,(slot+1)*window);
  if(row!.n>limit)fail(429,'RATE_LIMITED','Too many attempts; retry later',{retry_after:Math.ceil(((slot+1)*window-now)/1000)});
}
async function budget(env:Env,kind:'A'|'B',count:number){
  if(count<=0)return;
  const cap=config(kind==='A'?env.R2_CLASS_A_BUDGET:env.R2_CLASS_B_BUDGET,kind==='A'?800000:8000000);
  const row=await first(env,'INSERT INTO budgets(period,kind,n) VALUES(?,?,?) ON CONFLICT(period,kind) DO UPDATE SET n=n+excluded.n RETURNING n',new Date().toISOString().slice(0,7),kind,count);
  if(row!.n>cap)fail(503,'STORAGE_REQUEST_BUDGET','Storage request budget exhausted');
}
function username(value:unknown){
  if(typeof value!=='string')fail(400,'INVALID_USERNAME','Username must be text');
  const display=(value as string).normalize('NFKC').trim(),length=[...display].length;
  if(length<1||length>64||/[\p{Cc}\p{Cf}\p{Cs}]/u.test(display))fail(400,'INVALID_USERNAME','Use 1–64 characters without control characters');
  return {display,norm:caseFold(display)};
}
function authSecret(env:Env){if(!env.AUTH_PEPPER||env.AUTH_PEPPER.length<32)fail(503,'AUTH_NOT_CONFIGURED','Account authentication secret is not configured');return env.AUTH_PEPPER!;}
function verifier(env:Env,id:string,salt:string,key:string){return createHmac('sha256',authSecret(env)).update(id+'\0'+salt+'\0'+key).digest('hex');}
function account(row:Row){return {id:row.id,username:row.username,email:row.email??null,email_verified_at:row.email_verified_at?iso(row.email_verified_at):null};}
async function session(env:Env,owner:Row,name:string,platform:string){
  const id=crypto.randomUUID(),token=hex(crypto.getRandomValues(new Uint8Array(32))),now=Date.now(),expires=now+30*DAY;
  // The device/session pair is inserted in one batch below. Retiring devices
  // with no active session therefore cannot revoke a half-created new device.
  await query(env,'UPDATE devices SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL AND NOT EXISTS(SELECT 1 FROM sessions s WHERE s.device_id=devices.id AND s.expires_at>?)',now,owner.id,now).run();
  const active=await first(env,'SELECT COUNT(*) n FROM devices WHERE account_id=? AND revoked_at IS NULL',owner.id);
  if(active!.n>=20)fail(409,'DEVICE_LIMIT','Revoke an existing device before signing in again');
  await env.DB.batch([
    query(env,'INSERT INTO devices(id,account_id,name,platform,created_at,last_seen_at) VALUES(?,?,?,?,?,?)',id,owner.id,name,platform,now,now),
    query(env,'INSERT INTO sessions(token_hash,account_id,device_id,expires_at) VALUES(?,?,?,?)',sha(token),owner.id,id,expires),
  ]);
  return {token,expires_at:iso(expires),device_id:id,account:account(owner)};
}
async function auth(request:Request,env:Env){
  const bearer=request.headers.get('Authorization')||'';if(!/^Bearer [a-f0-9]{64}$/.test(bearer))fail(401,'UNAUTHORIZED','Sign in required');
  const row=await first(env,'SELECT a.*,s.device_id,s.token_hash,d.last_seen_at FROM sessions s JOIN devices d ON d.id=s.device_id JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.expires_at>? AND d.revoked_at IS NULL',sha(bearer.slice(7)),Date.now());
  if(!row)fail(401,'UNAUTHORIZED','Session expired or device revoked');
  if(row!.last_seen_at<Date.now()-60000)await query(env,'UPDATE devices SET last_seen_at=? WHERE id=?',Date.now(),row!.device_id).run();
  return row!;
}
async function item(env:Env,accountId:string,id:string){return first(env,'SELECT * FROM items WHERE account_id=? AND id=?',accountId,id);}
function record(row:Row){return {id:row.id,version:row.version,deleted:!!row.deleted,kind:row.kind,name:row.name,mime_type:row.mime_type,size:row.size,sha256:row.sha256,updated_at:iso(row.updated_at),expires_at:row.expires_at?iso(row.expires_at):null};}
async function upload(env:Env,owner:Row,id:string){const row=await first(env,'SELECT * FROM uploads WHERE account_id=? AND id=?',owner.id,id);if(!row)fail(404,'NOT_FOUND','Upload not found');return row!;}
async function conflict(env:Env,owner:Row,id:string):Promise<never>{const current=await item(env,owner.id,id);return fail(409,'VERSION_CONFLICT','Item changed on another device',{current_record:current?record(current):null});}
function emailAddress(value:unknown){const email=text(value,'email',3,254).trim().toLowerCase();if(!/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}$/.test(email))fail(400,'INVALID_EMAIL','Invalid email address');return email;}
function mailConfigured(env:Env){
  const provider=env.EMAIL_PROVIDER;
  if(!env.EMAIL_FROM||!env.EMAIL_CODE_SECRET||env.EMAIL_CODE_SECRET.length<32||!['cloudflare','resend'].includes(provider||'')||(provider==='cloudflare'&&!env.EMAIL)||(provider==='resend'&&!env.RESEND_API_KEY))fail(503,'EMAIL_UNAVAILABLE','Verification email sender is not configured');
}
function codeHash(env:Env,id:string,code:string){if(!env.EMAIL_CODE_SECRET||env.EMAIL_CODE_SECRET.length<32)fail(503,'EMAIL_UNAVAILABLE','Email verification secret is not configured');return createHmac('sha256',env.EMAIL_CODE_SECRET!).update(id+'\0'+code).digest('hex');}
async function sendCode(env:Env,to:string,code:string,id:string){
  const message={from:env.EMAIL_FROM!,to,subject:'FlickDock 邮箱绑定验证码',text:`你的 FlickDock 验证码是 ${code}，10 分钟内有效。若不是你本人请求，请忽略。`};
  try{
    if(env.EMAIL_PROVIDER==='cloudflare'){const result=await env.EMAIL!.send(message);if(!result?.messageId)throw new Error('not accepted');}
    else {
      const response=await fetch('https://api.resend.com/emails',{method:'POST',headers:{Authorization:'Bearer '+env.RESEND_API_KEY,'Content-Type':'application/json','Idempotency-Key':id},body:JSON.stringify(message),signal:AbortSignal.timeout(15000)});
      const value:any=await response.json();if(!response.ok||typeof value.id!=='string')throw new Error('not accepted');
    }
  }catch{fail(503,'EMAIL_UNAVAILABLE','Email provider did not accept the verification message');}
}
async function purgeUpload(env:Env,id:string){
  const now=Date.now(),lease='gc:'+crypto.randomUUID();
  const claimed=await query(env,"UPDATE uploads SET lease=?,lease_until=? WHERE id=? AND state='garbage' AND garbage_after<=? AND lease_until<=? AND NOT EXISTS(SELECT 1 FROM items i WHERE i.active_upload=uploads.id AND i.deleted=0) AND NOT EXISTS(SELECT 1 FROM read_leases r WHERE r.upload_id=uploads.id AND r.expires_at>?)",lease,now+300000,id,now,now,now).run();
  if(!claimed.meta.changes)return false;
  const parts=await rows(env,'SELECT key FROM parts WHERE upload_id=?',id);
  // R2 DELETE operations are free. A depleted read/write budget must never
  // prevent reclaiming storage. Bound each batch and retain allocation on error.
  for(let start=0;start<parts.length;start+=512)await env.FILES.delete(parts.slice(start,start+512).map(p=>p.key));
  await env.DB.batch([
    query(env,'DELETE FROM allocations WHERE id=? AND EXISTS(SELECT 1 FROM uploads WHERE id=? AND lease=?)',id,id,lease),
    query(env,'DELETE FROM uploads WHERE id=? AND lease=?',id,lease),
  ]);
  return true;
}
async function expireItems(env:Env,accountId?:string){
  const now=Date.now();
  const expired=await rows(env,'SELECT account_id,id,version,active_upload FROM items WHERE deleted=0 AND expires_at<=?'+(accountId?' AND account_id=?':'')+' LIMIT 1',now,...(accountId?[accountId]:[]));
  for(const value of expired){
    await query(env,'UPDATE items SET deleted=1,version=version+1,active_upload=NULL,size=0,sha256=NULL,expires_at=NULL,updated_at=? WHERE account_id=? AND id=? AND version=? AND deleted=0 AND expires_at<=?',now,value.account_id,value.id,value.version,now).run();
    if(value.active_upload)await purgeUpload(env,value.active_upload);
  }
}
async function cleanup(env:Env){
  const now=Date.now();
  await expireItems(env);
  const stale=await rows(env,"SELECT * FROM uploads WHERE lease_until<? AND NOT EXISTS(SELECT 1 FROM read_leases r WHERE r.upload_id=uploads.id AND r.expires_at>?) AND ((state IN ('pending','verifying') AND expires_at<?) OR (state='garbage' AND garbage_after<=?)) LIMIT 1",now,now,now-600000,now);
  for(const value of stale){
    // Atomically exclude a reader that began after the candidate query.
    const claimed=await query(env,"UPDATE uploads SET state='garbage',garbage_after=?,lease_until=0 WHERE id=? AND lease_until<? AND NOT EXISTS(SELECT 1 FROM read_leases r WHERE r.upload_id=uploads.id AND r.expires_at>?)",now,value.id,now,now).run();
    if(!claimed.meta.changes)continue;
    await purgeUpload(env,value.id);
  }
  await env.DB.batch([
    query(env,'UPDATE devices SET revoked_at=? WHERE revoked_at IS NULL AND NOT EXISTS(SELECT 1 FROM sessions s WHERE s.device_id=devices.id AND s.expires_at>?)',now,now),
    query(env,'DELETE FROM rates WHERE expires_at<?',now),query(env,'DELETE FROM email_challenges WHERE expires_at<?',now),query(env,'DELETE FROM sessions WHERE expires_at<?',now),
    query(env,'DELETE FROM auth_challenges WHERE expires_at<?',now),query(env,'DELETE FROM read_leases WHERE expires_at<?',now),
  ]);
}

async function route(request:Request,env:Env):Promise<Response>{
  const url=new URL(request.url),path=url.pathname,method=request.method;
  if(method==='GET'&&path==='/v1/health')return json({service:'flickdock-cloud'});
  const deleting=method==='DELETE'&&/^\/v1\/(items|uploads)\/[^/]+$/.test(path);
  // Reclaiming storage must remain possible after transfer traffic reaches its
  // own daily budget. Cleanup still has a bounded, separate request allowance.
  await rate(env,deleting?'service-deletions':'service-requests',deleting?10000:config(env.REQUESTS_PER_DAY,5000,80000),DAY);
  if(method==='POST'&&path==='/v1/auth/challenge'){
    authSecret(env);const input=await body(request,['username','purpose']),name=username(input.username),purpose=text(input.purpose,'purpose',5,8);
    if(!['register','login'].includes(purpose))fail(400,'INVALID_REQUEST','Unknown authentication purpose');
    const ip=sha(request.headers.get('CF-Connecting-IP')||'local');await rate(env,'auth-ip:'+ip,purpose==='register'?8:40,3600000);await rate(env,'auth-user:'+name.norm,30,900000);
    const owner=await first(env,'SELECT * FROM accounts WHERE username_norm=?',name.norm);
    if(purpose==='register'&&owner)fail(409,'USERNAME_TAKEN','Username already registered');
    const id=crypto.randomUUID(),salt=owner?.auth_salt||hex(crypto.getRandomValues(new Uint8Array(16))),accountId=owner?.id||crypto.randomUUID(),expires=Date.now()+300000;
    await query(env,'INSERT INTO auth_challenges(id,username_norm,purpose,salt,account_id,expires_at) VALUES(?,?,?,?,?,?)',id,name.norm,purpose,salt,accountId,expires).run();
    return json({challenge_id:id,salt,kdf:{name:'scrypt',N:16384,r:8,p:5,dkLen:32},expires_at:iso(expires)});
  }
  if(method==='POST'&&['/v1/auth/register','/v1/auth/login'].includes(path)){
    authSecret(env);const input=await body(request,['username','challenge_id','auth_key','device_name','platform']);
    const name=username(input.username),challengeId=uuid(input.challenge_id),key=digest(input.auth_key),device=text(input.device_name,'device_name',1,80),platform=text(input.platform,'platform',1,16);
    if(!['windows','macos','linux'].includes(platform))fail(400,'INVALID_REQUEST','Unknown platform');
    const register=path.endsWith('register');
    const challenge=await first(env,'UPDATE auth_challenges SET consumed=1 WHERE id=? AND username_norm=? AND purpose=? AND consumed=0 AND expires_at>? RETURNING *',challengeId,name.norm,register?'register':'login',Date.now());
    if(!challenge)fail(401,'INVALID_CHALLENGE','Authentication challenge expired, was used or does not match');
    const proof=verifier(env,challenge!.account_id,challenge!.salt,key);
    let owner=await first(env,'SELECT * FROM accounts WHERE username_norm=?',name.norm);
    if(register){
      if(owner)fail(409,'USERNAME_TAKEN','Username already registered');
      const id=challenge!.account_id;
      try{await query(env,'INSERT INTO accounts(id,username,username_norm,auth_verifier,auth_salt,created_at,quota_bytes) VALUES(?,?,?,?,?,?,?)',id,name.display,name.norm,proof,challenge!.salt,Date.now(),config(env.ACCOUNT_QUOTA_BYTES,8*1024**3)).run();}
      catch(e){if(String(e).includes('UNIQUE'))fail(409,'USERNAME_TAKEN','Username already registered');throw e;}
      owner=await first(env,'SELECT * FROM accounts WHERE id=?',id);
    }else if(!owner||owner.id!==challenge!.account_id||!timingSafeEqual(Buffer.from(owner.auth_verifier,'hex'),Buffer.from(proof,'hex')))fail(401,'INVALID_CREDENTIALS','Incorrect username or password');
    return json(await session(env,owner!,device,platform),register?201:200);
  }
  const owner=await auth(request,env);
  if(method==='GET'&&(path==='/v1/me'||path==='/v1/sync/changes'||path.startsWith('/v1/items/')))await expireItems(env,owner.id);
  if(method==='GET'&&path==='/v1/me'){
    const storage=(await first(env,'SELECT allocated_bytes,quota_bytes FROM accounts WHERE id=?',owner.id))!;
    return json({account:account(owner),device_id:owner.device_id,storage:{used_bytes:storage.allocated_bytes,quota_bytes:storage.quota_bytes,max_file_bytes:storage.quota_bytes}});
  }
  if(method==='GET'&&path==='/v1/devices'){
    const devices=await rows(env,'SELECT * FROM devices WHERE account_id=? ORDER BY created_at DESC LIMIT 100',owner.id);
    return json({devices:devices.map(d=>({id:d.id,name:d.name,platform:d.platform,created_at:iso(d.created_at),last_seen_at:iso(d.last_seen_at),revoked_at:d.revoked_at?iso(d.revoked_at):null,current:d.id===owner.device_id}))});
  }
  let match=path.match(/^\/v1\/devices\/([^/]+)$/);
  if(method==='DELETE'&&match){const id=uuid(match[1]);const result=await query(env,'UPDATE devices SET revoked_at=? WHERE id=? AND account_id=?',Date.now(),id,owner.id).run();if(!result.meta.changes)fail(404,'NOT_FOUND','Device not found');return json({revoked:true});}
  if(method==='POST'&&path==='/v1/auth/logout'){await query(env,'UPDATE devices SET revoked_at=? WHERE id=? AND account_id=?',Date.now(),owner.device_id,owner.id).run();return json({revoked:true});}
  if(method==='POST'&&path==='/v1/email/code'){
    const input=await body(request,['email']),email=emailAddress(input.email);mailConfigured(env);
    await rate(env,'email-account:'+owner.id,3,3600000);await rate(env,'email-address:'+sha(email),5,3600000);
    let random:number;do{random=crypto.getRandomValues(new Uint32Array(1))[0];}while(random>=4294000000);
    const code=String(random%1000000).padStart(6,'0'),id=crypto.randomUUID(),expires=Date.now()+600000;
    await query(env,'INSERT INTO email_challenges(id,account_id,email,code_hash,expires_at) VALUES(?,?,?,?,?)',id,owner.id,email,codeHash(env,id,code),expires).run();
    await sendCode(env,email,code,id);
    await query(env,'UPDATE email_challenges SET sent=1 WHERE id=?',id).run();
    return json({challenge_id:id,expires_at:iso(expires)},202);
  }
  if(method==='POST'&&path==='/v1/email/bind'){
    const input=await body(request,['challenge_id','code']),id=uuid(input.challenge_id),code=text(input.code,'code',6,6);
    if(!/^\d{6}$/.test(code))fail(400,'INVALID_CODE','Invalid verification code');
    await query(env,'UPDATE email_challenges SET attempts=attempts+1 WHERE id=? AND account_id=? AND consumed=0 AND sent=1',id,owner.id).run();
    const result=await query(env,'UPDATE email_challenges SET consumed=1 WHERE id=? AND account_id=? AND sent=1 AND consumed=0 AND attempts<=5 AND expires_at>? AND code_hash=?',id,owner.id,Date.now(),codeHash(env,id,code)).run();
    if(!result.meta.changes)fail(400,'INVALID_CODE','Verification code is invalid, expired or already used');
    return json({account:account((await first(env,'SELECT * FROM accounts WHERE id=?',owner.id))!)});
  }
  if(method==='GET'&&path==='/v1/sync/changes'){
    if([...url.searchParams.keys()].some(k=>!['cursor','limit'].includes(k)))fail(400,'INVALID_REQUEST','Unknown query parameter');
    const cursor=url.searchParams.get('cursor')||'0',limit=Number(url.searchParams.get('limit')||'100');
    if(!/^(0|[1-9][0-9]*)$/.test(cursor)||!Number.isSafeInteger(Number(cursor)))fail(400,'INVALID_CURSOR','Invalid cursor');integer(limit,'limit',1,200);
    const all=await rows(env,'SELECT seq,record FROM changes WHERE account_id=? AND seq>? ORDER BY seq LIMIT ?',owner.id,Number(cursor),limit+1),page=all.slice(0,limit);
    return json({changes:page.map(c=>({cursor:String(c.seq),record:JSON.parse(c.record)})),cursor:page.length?String(page.at(-1)!.seq):cursor,has_more:all.length>limit});
  }
  if(method==='POST'&&path==='/v1/uploads'){
    const required=['item_id','base_version','kind','name','mime_type','size','sha256'];
    const input=await body(request,[...required,'upload_id'],required);
    const id=uuid(input.item_id,'item_id'),base=integer(input.base_version,'base_version'),kind=text(input.kind,'kind',1,8),name=text(input.name,'name',1,255),mime=text(input.mime_type,'mime_type',1,127),size=integer(input.size,'size'),hash=digest(input.sha256);
    if(!['file','text','url'].includes(kind)||!/^[a-zA-Z0-9!#$&^_.+-]+\/[a-zA-Z0-9!#$&^_.+-]+$/.test(mime))fail(400,'INVALID_REQUEST','Unknown kind or MIME type');
    // Persisting this optional UUID before POST lets a client recover a lost
    // initialization reply without reserving the entire file size twice.
    const suppliedId=input.upload_id!==undefined,uploadId=suppliedId?uuid(input.upload_id,'upload_id'):crypto.randomUUID();
    const reuse=async()=>{
      const previous=await first(env,'SELECT * FROM uploads WHERE id=?',uploadId);
      if(!previous)return null;
      if(previous.account_id!==owner.id)fail(404,'NOT_FOUND','Upload not found');
      const spec={item_id:id,base_version:base,kind,name,mime_type:mime,size,sha256:hash};
      if(Object.entries(spec).some(([key,value])=>previous[key]!==value))fail(409,'UPLOAD_CONFLICT','Upload identifier already belongs to different content');
      if(previous.state==='garbage'||(previous.state!=='committed'&&previous.expires_at<Date.now()))fail(409,'UPLOAD_EXPIRED','Upload was cancelled or expired');
      return json({upload_id:uploadId,chunk_size:previous.chunk_size,part_count:previous.part_count,expires_at:iso(previous.expires_at)},201);
    };
    if(suppliedId){const response=await reuse();if(response)return response;}
    try{
      if((await item(env,owner.id,id))?.version!==base&&base!==0)await conflict(env,owner,id);
      if(base===0&&await item(env,owner.id,id))await conflict(env,owner,id);
      const count=await first(env,"SELECT COUNT(*) n FROM uploads WHERE account_id=? AND state IN ('pending','verifying')",owner.id);if(count!.n>=10)fail(429,'UPLOAD_LIMIT','Too many pending uploads');
      const expires=Date.now()+3*DAY,chunk=chunkSize(env),partCount=Math.ceil(size/chunk);
      await env.DB.batch([
        query(env,'UPDATE limits SET bytes_limit=? WHERE id=1',config(env.GLOBAL_QUOTA_BYTES,8*1024**3)),
        query(env,'INSERT INTO allocations(id,account_id,size) VALUES(?,?,?)',uploadId,owner.id,size),
        query(env,'INSERT INTO uploads(id,account_id,item_id,base_version,kind,name,mime_type,size,sha256,chunk_size,part_count,state,expires_at,computed_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',uploadId,owner.id,id,base,kind,name,mime,size,hash,chunk,partCount,'pending',expires,size===0?sha(new Uint8Array()):null),
      ]);
      return json({upload_id:uploadId,chunk_size:chunk,part_count:partCount,expires_at:iso(expires)},201);
    }catch(error){
      // A concurrent retry may have won after the initial existence check.
      // D1 batch rollback keeps its allocation atomic with the upload row.
      if(suppliedId){const response=await reuse();if(response)return response;}
      throw error;
    }
  }
  match=path.match(/^\/v1\/uploads\/([^/]+)\/parts\/(\d+)$/);
  if(method==='PUT'&&match){
    const id=uuid(match[1]),partIndex=integer(Number(match[2]),'part_index'),value=await upload(env,owner,id);
    if(value.state!=='pending'||value.expires_at<Date.now())fail(409,'UPLOAD_NOT_PENDING','Upload is not writable');
    if(partIndex>=value.part_count)fail(400,'INVALID_PART','Part index out of range');
    const expected=Math.min(value.chunk_size,value.size-partIndex*value.chunk_size),data=await bytes(request,expected),hash=sha(data);
    if(data.byteLength!==expected||digest(request.headers.get('X-Content-SHA256'))!==hash)fail(400,'PART_MISMATCH','Part size or SHA256 differs');
    const key=`v1/${owner.id}/${id}/${partIndex}-${hash}`;
    await query(env,'INSERT OR IGNORE INTO parts(upload_id,idx,key,size,sha256) VALUES(?,?,?,?,?)',id,partIndex,key,expected,hash).run();
    const part=await first(env,'SELECT * FROM parts WHERE upload_id=? AND idx=?',id,partIndex);
    if(part!.sha256!==hash)fail(409,'PART_CONFLICT','This part number already contains other bytes');
    if(!part!.ready){
      if(partIndex!==value.next_part)fail(409,'PART_OUT_OF_ORDER','Upload parts in increasing order',{next_part:value.next_part});
      // Recheck state after receiving a possibly slow body, before any R2 write.
      const writeLease=crypto.randomUUID();
      const locked=await query(env,"UPDATE uploads SET lease=?,lease_until=? WHERE id=? AND account_id=? AND state='pending' AND next_part=? AND expires_at>? AND lease_until<?",writeLease,Date.now()+600000,id,owner.id,partIndex,Date.now(),Date.now()).run();
      if(!locked.meta.changes)fail(423,'UPLOAD_BUSY','Upload changed or another part write is running');
      try{
        const full=PersistentSHA256.load(value.hash_state);full.update(data);
        const last=partIndex===value.part_count-1,state=last?null:full.save(),computed=last?hex(full.digest()):null;
        await budget(env,'A',1);await env.FILES.put(key,data);
        const result=await env.DB.batch([
          query(env,"UPDATE uploads SET hash_state=?,computed_sha256=?,verified_size=verified_size+?,next_part=next_part+1,expires_at=? WHERE id=? AND state='pending' AND next_part=?",state,computed,data.length,Date.now()+3*DAY,id,partIndex),
          query(env,'UPDATE parts SET ready=1 WHERE upload_id=? AND idx=? AND EXISTS(SELECT 1 FROM uploads WHERE id=? AND next_part>?)',id,partIndex,id,partIndex),
        ]);
        if(!result[0].meta.changes){const latest=await first(env,'SELECT ready FROM parts WHERE upload_id=? AND idx=?',id,partIndex);if(!latest?.ready)fail(409,'UPLOAD_NOT_PENDING','Upload changed while writing the part');}
      }finally{await query(env,'UPDATE uploads SET lease=NULL,lease_until=0 WHERE id=? AND lease=?',id,writeLease).run();}
    }
    return json({part_index:partIndex,size:expected,sha256:hash});
  }
  match=path.match(/^\/v1\/uploads\/([^/]+)\/commit$/);
  if(method==='POST'&&match){
    await body(request,[]);const id=uuid(match[1]),value=await upload(env,owner,id);
    if(value.state==='committed'){const current=await item(env,owner.id,value.item_id);if(current?.active_upload===id)return json({record:record(current)});await conflict(env,owner,value.item_id);}
    if(value.state==='garbage'||value.expires_at<Date.now())fail(409,'UPLOAD_EXPIRED','Upload was cancelled or expired');
    const summary=(await first(env,'SELECT COUNT(*) n,COALESCE(SUM(ready),0) ready FROM parts WHERE upload_id=?',id))!;
    if(summary.n!==value.part_count||summary.ready!==value.part_count||value.next_part!==value.part_count)fail(409,'UPLOAD_INCOMPLETE','All parts must be uploaded before commit');
    const previous=await item(env,owner.id,value.item_id);
    const lease=crypto.randomUUID(),now=Date.now();
    const locked=await query(env,"UPDATE uploads SET state='verifying',lease=?,lease_until=? WHERE id=? AND (state='pending' OR (state='verifying' AND lease_until<?))",lease,now+300000,id,now).run();
    if(!locked.meta.changes)fail(423,'UPLOAD_BUSY','Upload verification is already running');
    try{
      if(value.verified_size!==value.size||value.computed_sha256!==value.sha256)fail(400,'CONTENT_MISMATCH','Full content size or SHA256 differs');
      const result=await query(env,"UPDATE uploads SET state='committed',lease=NULL,lease_until=0 WHERE id=? AND state='verifying' AND lease=?",id,lease).run();
      if(!result.meta.changes)fail(409,'UPLOAD_NOT_PENDING','Upload changed while verifying');
    }catch(error){await query(env,"UPDATE uploads SET state='pending',lease=NULL,lease_until=0 WHERE id=? AND state='verifying' AND lease=?",id,lease).run();if(String(error).includes('VERSION_CONFLICT'))await conflict(env,owner,value.item_id);throw error;}
    // Retire replaced bytes promptly; a reader holds a lease until it finishes.
    if(previous?.active_upload&&previous.active_upload!==id)await purgeUpload(env,previous.active_upload);
    return json({record:record((await item(env,owner.id,value.item_id))!)});
  }
  match=path.match(/^\/v1\/uploads\/([^/]+)$/);
  if(method==='GET'&&match){
    const id=uuid(match[1]),value=await upload(env,owner,id);
    if(value.state==='garbage'||(value.state!=='committed'&&value.expires_at<Date.now()))fail(409,'UPLOAD_EXPIRED','Upload was cancelled or expired');
    return json({upload_id:id,item_id:value.item_id,base_version:value.base_version,state:value.state,next_part:value.next_part,chunk_size:value.chunk_size,part_count:value.part_count,size:value.size,sha256:value.sha256,expires_at:iso(value.expires_at)});
  }
  if(method==='DELETE'&&match){
    const id=uuid(match[1]);
    const result=await query(env,"UPDATE uploads SET state='garbage',garbage_after=? WHERE id=? AND account_id=? AND state IN ('pending','verifying','garbage')",Date.now(),id,owner.id).run();
    if(!result.meta.changes){
      const value=await upload(env,owner,id),current=await item(env,owner.id,value.item_id);
      fail(409,'ALREADY_COMMITTED','Delete the item using its version',{current_record:current?record(current):null});
    }
    await purgeUpload(env,id);return json({cancelled:true});
  }
  match=path.match(/^\/v1\/items\/([^/]+)$/);
  if(method==='DELETE'&&match){
    const input=await body(request,['base_version']),id=uuid(match[1]),base=integer(input.base_version,'base_version',1);
    const current=await item(env,owner.id,id);if(current?.deleted&&current.version===base+1)return json({record:record(current)});
    const result=await query(env,'UPDATE items SET deleted=1,version=version+1,active_upload=NULL,size=0,sha256=NULL,expires_at=NULL,updated_at=? WHERE account_id=? AND id=? AND version=? AND deleted=0',Date.now(),owner.id,id,base).run();
    if(!result.meta.changes)await conflict(env,owner,id);
    if(current?.active_upload)await purgeUpload(env,current.active_upload);
    return json({record:record((await item(env,owner.id,id))!)});
  }
  match=path.match(/^\/v1\/items\/([^/]+)\/content$/);
  if(method==='GET'&&match){
    const id=uuid(match[1]),value=await item(env,owner.id,id);if(!value||value.deleted)fail(404,'NOT_FOUND','Item not found');
    const upload=await first(env,'SELECT chunk_size,part_count FROM uploads WHERE id=? AND state=?',value!.active_upload,'committed');if(!upload)fail(503,'STORAGE_MISSING','Content metadata is unavailable');
    return json({record:record(value!),chunk_size:upload!.chunk_size,part_count:upload!.part_count});
  }
  match=path.match(/^\/v1\/items\/([^/]+)\/parts\/(\d+)$/);
  if(method==='GET'&&match){
    const id=uuid(match[1]),index=integer(Number(match[2]),'part_index'),value=await item(env,owner.id,id);if(!value||value.deleted)fail(404,'NOT_FOUND','Item not found');
    if(url.searchParams.has('version')){const version=Number(url.searchParams.get('version'));integer(version,'version',1);if(version!==value!.version)await conflict(env,owner,id);}
    const part=await first(env,'SELECT * FROM parts WHERE upload_id=? AND idx=? AND ready=1',value!.active_upload,index);if(!part)fail(404,'NOT_FOUND','Content part not found');
    const lease=crypto.randomUUID();await query(env,'INSERT INTO read_leases(upload_id,id,expires_at) VALUES(?,?,?)',value!.active_upload,lease,Date.now()+1800000).run();
    const release=async()=>{
      await query(env,'DELETE FROM read_leases WHERE upload_id=? AND id=?',value!.active_upload,lease).run();
      try{await purgeUpload(env,value!.active_upload);}catch{/* Allocation stays reserved until the next cleanup. */}
    };
    let object:R2ObjectBody|null;
    try{await budget(env,'B',1);object=await env.FILES.get(part!.key);if(!object)fail(503,'STORAGE_MISSING','Content part is unavailable');}
    catch(error){await release();throw error;}
    const reader=object!.body.getReader();
    const stream=new ReadableStream<Uint8Array>({async pull(controller){try{const next=await reader.read();if(next.done){controller.close();await release();}else controller.enqueue(next.value);}catch(e){controller.error(e);await release();}},async cancel(){await reader.cancel();await release();}});
    return new Response(stream,{headers:{'Content-Type':'application/octet-stream','Content-Length':String(part!.size),'X-Content-SHA256':part!.sha256,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
  }
  return fail(404,'NOT_FOUND','API route not found');
}

export default {
  async fetch(request:Request,env:Env):Promise<Response>{
    try{
      const url=new URL(request.url);
      if(url.protocol!=='https:'&&!['127.0.0.1','localhost','[::1]'].includes(url.hostname))fail(426,'HTTPS_REQUIRED','HTTPS is required');
      return await route(request,env);
    }catch(error){
      if(error instanceof APIError)return json({error:{code:error.code,message:error.message,...error.details}},error.status);
      const message=String(error);if(message.includes('CONTENT_RETIRED'))return json({error:{code:'VERSION_CONFLICT',message:'This content version is being retired; refresh metadata'}},409);
      if(message.includes('ACCOUNT_QUOTA')||message.includes('GLOBAL_QUOTA'))return json({error:{code:message.includes('ACCOUNT_QUOTA')?'ACCOUNT_QUOTA':'GLOBAL_QUOTA',message:'Storage quota exceeded; pending uploads also reserve space'}},413);
      if(message.includes('DEVICE_LIMIT')||message.includes('UPLOAD_LIMIT'))return json({error:{code:message.includes('DEVICE_LIMIT')?'DEVICE_LIMIT':'UPLOAD_LIMIT',message:'Too many active devices or pending uploads'}},409);
      // Do not echo SQL, provider responses, tokens, emails or credentials.
      return json({error:{code:'SERVICE_UNAVAILABLE',message:'Service operation failed; retry later'}},503);
    }
  },
  async scheduled(_event:ScheduledController,env:Env,ctx:ExecutionContext){ctx.waitUntil(cleanup(env));},
} satisfies ExportedHandler<Env>;
