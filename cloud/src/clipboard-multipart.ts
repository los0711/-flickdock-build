/** Additive large-clipboard transport. Hash state stays private in DO storage. */
import {PersistentSHA256} from './hash-state';
import {fileEnvelope,envelopeSize,archiveBody,registerClipboardParts,releaseClipboardFile,purgeUpload,CLIPBOARD_MAX_BYTES,CLIPBOARD_MAX_ARCHIVE_BYTES,type FileEnvelope,type ClipboardPart} from './shared-file';
import type {Head} from './clipboard';
type Env={DB:D1Database;FILES:R2Bucket};
type Spend=(kind:'A'|'B',count?:number)=>Promise<void>;
type Hold={device:string;items:string[];expires:number};
type Attempt={id:string;head:Head;kind:string;size:number;sha256:string;raw_file?:FileEnvelope;
  parts:ClipboardPart[];body_state:string;wire_state?:string;expires:number;cancelled?:boolean;
  lease?:string;lease_until?:number;inflight_key?:string};
type Write={key:string;attempt:string;account:string;lease:string;until:number;size:number;terminal?:boolean;gc_claim?:string;retry_at?:number;tries?:number};
type Scanner={account:string;cursor?:string;next_at:number;lease?:string;lease_until?:number};
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const SHA=/^[a-f0-9]{64}$/;
export const MULTIPART_V1={part_size:4*1024*1024,threshold:24*1024*1024,max_bytes:CLIPBOARD_MAX_BYTES,max_archive_bytes:CLIPBOARD_MAX_ARCHIVE_BYTES};
const entry=(id:string)=>'clipboard-multipart:'+id,alias=(id:string)=>'clipboard-multipart-current:'+id;
const writeEntry=(lease:string)=>'clipboard-multipart-write:'+lease;
const writeAllocation=(lease:string)=>'clipboard-write:'+lease;
const scannerEntry='clipboard-multipart-orphans';
const json=(value:object,status=200)=>Response.json({schema_version:1,server_time:Date.now(),...value},{status,headers:{'Cache-Control':'no-store'}});
const fail=(status:number,code:string,extra:object={})=>json({error:{code,message:'Clipboard multipart content unavailable',...extra}},status);
const hex=(bytes:Uint8Array)=>Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join('');
const from64=(value:string)=>Uint8Array.from(atob(value),c=>c.charCodeAt(0));
async function arm(ctx:DurableObjectState,when:number){const old=await ctx.storage.getAlarm();if(old===null||old>when)await ctx.storage.setAlarm(when);}
async function held(ctx:DurableObjectState,id:string,device:string){const h=await ctx.storage.get<Hold>('clipboard-hold:'+id);return !!(h&&h.device===device&&h.expires>Date.now()&&h.items.length);}
async function allowed(ctx:DurableObjectState,a:Attempt){const p=await ctx.storage.get<Head>('clipboard-pending');return p?.id===a.head.id&&p.device===a.head.device||!!a.raw_file&&await held(ctx,a.head.id,a.head.device);}
async function validSession(request:Request,env:Env,account:string,device:string){return !!await env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL').bind(request.headers.get('X-Clipboard-Session')||'',account,device,Date.now()).first();}
function status(a:Attempt){return {multipart_v1:true,upload_id:a.id,part_size:MULTIPART_V1.part_size,part_count:Math.ceil(a.size/MULTIPART_V1.part_size),next_part:a.parts.length,expires_at:new Date(a.expires).toISOString()};}
async function bodyBytes(request:Request,max:number){
  const declared=request.headers.get('Content-Length');if(declared!==null&&(!/^\d+$/.test(declared)||Number(declared)>max))throw new Error('length');
  const chunks:Uint8Array[]=[];let size=0;const reader=request.body?.getReader();
  if(reader)try{while(true){const result=await reader.read();if(result.done)break;size+=result.value.byteLength;if(size>max){await reader.cancel();throw new Error('length');}chunks.push(result.value);}}finally{reader.releaseLock();}
  const bytes=new Uint8Array(size);let at=0;for(const chunk of chunks){bytes.set(chunk,at);at+=chunk.byteLength;}return bytes;
}
async function input(request:Request){try{const value=JSON.parse(new TextDecoder('utf8',{fatal:true,ignoreBOM:false}).decode(await bodyBytes(request,8192)));return value&&typeof value==='object'&&!Array.isArray(value)?value:null;}catch{return null;}}

export async function discardMultipart(ctx:DurableObjectState,env:Env,a:Attempt){
  const current=await ctx.storage.transaction(async tx=>{
    const active=await tx.get<Head>('clipboard-head'),shared=await tx.get<Head>('clipboard-shared-ready:'+a.head.id);
    if(active?.upload_id===a.id||shared?.upload_id===a.id){await tx.delete(entry(a.id));if(await tx.get(alias(a.head.id))===a.id)await tx.delete(alias(a.head.id));return null;}
    const latest=await tx.get<Attempt>(entry(a.id))||a;latest.cancelled=true;await tx.put(entry(a.id),latest);return latest;
  });
  if(!current)return;a=current;
  if((a.lease_until||0)>Date.now()){await arm(ctx,Math.min(a.lease_until!,Date.now()+60000));return;}
  try{
    const backing=await env.DB.prepare('SELECT 1 ok FROM uploads WHERE id=? AND account_id=?').bind(a.id,a.head.account).first();
    if(backing)await releaseClipboardFile(env,a.head.id,a.id,a.head.account);
    else{
      const keys=[...new Set([...a.parts.map(part=>part.key),...(a.inflight_key?[a.inflight_key]:[])])];
      if(keys.length)await env.FILES.delete(keys);
      await env.DB.prepare('DELETE FROM allocations WHERE id=? AND account_id=?').bind(a.id,a.head.account).run();
    }
    await ctx.storage.transaction(async tx=>{await tx.delete(entry(a.id));if(await tx.get(alias(a.head.id))===a.id)await tx.delete(alias(a.head.id));});
  }catch{await arm(ctx,Date.now()+60000);}
}

export async function sweepMultiparts(ctx:DurableObjectState,env:Env,spend?:Spend){
  const attempts=await ctx.storage.list<Attempt>({prefix:'clipboard-multipart:',limit:4});let discarded=false;
  for(const a of attempts.values())if(a.cancelled||a.expires<=Date.now()||!await allowed(ctx,a)){
    if(discarded)await arm(ctx,Date.now()+1000);else{await discardMultipart(ctx,env,a);discarded=true;}
  }
  const remaining=await ctx.storage.list<Attempt>({prefix:'clipboard-multipart:',limit:4});
  for(const a of remaining.values())await arm(ctx,a.cancelled?Date.now()+60000:a.expires);
  const writes=await sweepWrites(ctx,env);
  // Keep a scan page separate from real retry/discard work so its DB/R2
  // operations do not stack with another large cleanup batch in one event.
  if(!discarded&&!writes)await sweepOrphans(ctx,env,spend);
  else{const scanner=await ctx.storage.get<Scanner>(scannerEntry);if(scanner)await arm(ctx,Math.max(Date.now()+1000,scanner.next_at));}
}

async function cleanWrite(ctx:DurableObjectState,env:Env,w:Write){
  try{
  // Fence adoption in the same transaction used by writer publication. A DB
  // ownership read alone can race with adoption while its result is in flight.
  const claimed=await ctx.storage.transaction(async tx=>{
    const record=await tx.get<Write>(writeEntry(w.lease));if(!record)return null;
    const current=await tx.get<Attempt>(entry(record.attempt));
    if(current?.parts.some(part=>part.key===record.key))return {record,owned:true};
    record.gc_claim=record.gc_claim||crypto.randomUUID();await tx.put(writeEntry(record.lease),record);return {record,owned:false};
  });
  if(!claimed)return;w=claimed.record;
  if(claimed.owned||await env.DB.prepare('SELECT 1 ok FROM parts p JOIN uploads u ON u.id=p.upload_id WHERE p.upload_id=? AND p.key=? AND p.ready=1 AND u.account_id=?').bind(w.attempt,w.key,w.account).first()){
    await env.DB.prepare('DELETE FROM allocations WHERE id=? AND account_id=?').bind(writeAllocation(w.lease),w.account).run();
    await ctx.storage.delete(writeEntry(w.lease));return;
  }
  let removed=false;try{await env.FILES.delete(w.key);removed=true;}catch{/* Durable key below remains retryable. */}
  // Lease expiry is not proof that a previous R2 PUT has finished. Keep its
  // independent key ledger until that writer's finally marks it terminal.
  if(removed&&w.terminal){await env.DB.prepare('DELETE FROM allocations WHERE id=? AND account_id=?').bind(writeAllocation(w.lease),w.account).run();await ctx.storage.delete(writeEntry(w.lease));return;}
  const retry=await ctx.storage.transaction(async tx=>{
    const record=await tx.get<Write>(writeEntry(w.lease));if(!record)return null;
    if(!record.terminal){
      // A crashed writer has no finally. Atomically hand its fenced, unique
      // key to one persistent account scanner before freeing this ledger.
      // A late writer cannot adopt without its ledger, and a late R2 PUT is
      // found on a subsequent scan even after an earlier empty sweep.
      const scanner=await tx.get<Scanner>(scannerEntry);
      if(scanner&&scanner.account!==record.account)throw new Error('Clipboard GC account mismatch');
      const next=scanner||{account:record.account,next_at:Date.now()+3600000};
      await tx.put(scannerEntry,next);await tx.delete(writeEntry(record.lease));return next.next_at;
    }
    record.tries=Math.min(16,(record.tries||0)+1);record.retry_at=Date.now()+Math.min(3600000,60000*2**Math.min(record.tries-1,6));
    await tx.put(writeEntry(w.lease),record);return record.retry_at;
  });
  if(retry)await arm(ctx,retry);
  }catch{
    // Quota reservation and ledger survive failed cleanup/ownership checks.
    await arm(ctx,Date.now()+60000);
  }
}

async function sweepOrphans(ctx:DurableObjectState,env:Env,spend?:Spend){
  const scanner=await ctx.storage.get<Scanner>(scannerEntry);if(!scanner)return;
  if(scanner.next_at>Date.now()||!spend){await arm(ctx,Math.max(Date.now()+1000,scanner.next_at));return;}
  const lease=crypto.randomUUID();
  const claimed=await ctx.storage.transaction(async tx=>{const value=await tx.get<Scanner>(scannerEntry);if(!value||value.next_at>Date.now()||(value.lease_until||0)>Date.now())return null;value.lease=lease;value.lease_until=Date.now()+120000;await tx.put(scannerEntry,value);return value;});
  if(!claimed)return;
  // An isolate dying during list/delete cannot lose the retry alarm along
  // with its finally. The next invocation can reclaim this expired lease.
  await arm(ctx,claimed.lease_until!);
  let cursor=claimed.cursor,next=Date.now()+3600000;
  try{
    await spend('A');
    const page=await env.FILES.list({prefix:`clipboard/${claimed.account}/`,limit:8,...(cursor?{cursor}:{})});
    for(const object of page.objects){
      const pieces=object.key.split('/'),tail=pieces[4]?.match(/^(0|[1-9]\d?)-([a-f0-9-]{36})-([a-f0-9]{64})$/);
      if(pieces.length!==5||pieces[0]!=='clipboard'||pieces[1]!==claimed.account||!UUID.test(pieces[2])||!UUID.test(pieces[3])||!tail||Number(tail[1])>25||!UUID.test(tail[2]))continue;
      const safe=await ctx.storage.transaction(async tx=>{
        const current=await tx.get<Attempt>(entry(pieces[3])),writer=await tx.get<Write>(writeEntry(tail[2]));
        if(current?.parts.some(part=>part.key===object.key)||writer&&!writer.gc_claim)return false;
        // A missing/fenced writer cannot publish this non-reusable lease key.
        return true;
      });
      if(!safe)continue;
      // Preserve every registered part, including valid clipboard/shelf refs
      // and read leases. Their existing purgeUpload owns DB/R2 GC arbitration.
      const registered=await env.DB.prepare('SELECT p.ready,u.state,EXISTS(SELECT 1 FROM clipboard_upload_refs c WHERE c.upload_id=u.id AND c.expires_at>?) clipboard_ref,EXISTS(SELECT 1 FROM items i WHERE i.active_upload=u.id AND i.deleted=0) shelf_ref,EXISTS(SELECT 1 FROM read_leases r WHERE r.upload_id=u.id AND r.expires_at>?) read_lease FROM parts p JOIN uploads u ON u.id=p.upload_id WHERE p.upload_id=? AND p.key=? AND u.account_id=?').bind(Date.now(),Date.now(),pieces[3],object.key,claimed.account).first();
      if(!registered){
        // A listed key proves its sole PUT reached R2. Reconstitute terminal
        // cleanup ownership before deleting it, so a later DB failure cannot
        // strand the extra quota reservation with no retryable ledger.
        const fenced=await ctx.storage.transaction(async tx=>{
          const current=await tx.get<Attempt>(entry(pieces[3])),writer=await tx.get<Write>(writeEntry(tail[2]));
          if(current?.parts.some(part=>part.key===object.key)||writer&&!writer.gc_claim)return false;
          await tx.put(writeEntry(tail[2]),{key:object.key,attempt:pieces[3],account:claimed.account,lease:tail[2],size:object.size,until:0,terminal:true,gc_claim:writer?.gc_claim||crypto.randomUUID()} satisfies Write);return true;
        });
        if(!fenced)continue;
        await env.FILES.delete(object.key);
        // This key was really present: its sole PUT has reached R2. It is
        // fenced against publication and cannot be PUT a second time.
        await env.DB.prepare('DELETE FROM allocations WHERE id=? AND account_id=?').bind(writeAllocation(tail[2]),claimed.account).run();
        await ctx.storage.delete(writeEntry(tail[2]));
      }
    }
    cursor=page.truncated?page.cursor:undefined;next=Date.now()+(page.truncated?60000:3600000);
  }catch(error){
    const retry=Number((error as {details?:{retry_after?:number}})?.details?.retry_after);
    next=Date.now()+Math.max(3600000,Number.isFinite(retry)&&retry>0?retry*1000:0);
  }finally{
    await ctx.storage.transaction(async tx=>{const value=await tx.get<Scanner>(scannerEntry);if(value?.lease!==lease)return;value.cursor=cursor;value.next_at=next;delete value.lease;delete value.lease_until;await tx.put(scannerEntry,value);});
    await arm(ctx,next);
  }
}

async function sweepWrites(ctx:DurableObjectState,env:Env){
  const writes=await ctx.storage.list<Write>({prefix:'clipboard-multipart-write:',limit:128});let used=0,next=Infinity;
  for(const w of writes.values()){
    const due=Math.max(w.retry_at||0,w.terminal?0:w.until);
    if(due>Date.now()){next=Math.min(next,due);continue;}
    if(used++>=8){next=Math.min(next,Date.now()+1000);continue;}
    await cleanWrite(ctx,env,w);
  }
  if(Number.isFinite(next))await arm(ctx,next);
  return Math.min(used,8);
}

export async function multipartRequest(request:Request,ctx:DurableObjectState,env:Env,spend:Spend,
    notify:(device:string,head?:Head)=>Promise<unknown>,retire:(ctx:DurableObjectState,env:Env,head:Head)=>Promise<void>):Promise<Response|undefined>{
  if(!/^\/clipboard\/[a-f0-9-]{36}\/upload(?:\/|$)/.test(new URL(request.url).pathname))return;
  try{return await routeMultipart(request,ctx,env,spend,notify,retire);}
  finally{
    // A duplicate commit or an out-of-order part needs no semantic bytes, but
    // its legitimate bounded HTTP body still has to finish before the DO
    // binding returns a response. Cancelling that body alone leaves the outer
    // Worker producer reading after response on workerd's streaming transport.
    if(request.body&&!request.bodyUsed){
      const max=request.method==='PUT'?MULTIPART_V1.part_size:8192;
      try{await bodyBytes(request,max);}catch{await request.body.cancel().catch(()=>{});}
    }
  }
}

async function routeMultipart(request:Request,ctx:DurableObjectState,env:Env,spend:Spend,
    notify:(device:string,head?:Head)=>Promise<unknown>,retire:(ctx:DurableObjectState,env:Env,head:Head)=>Promise<void>):Promise<Response|undefined>{
  const match=new URL(request.url).pathname.match(/^\/clipboard\/([a-f0-9-]{36})\/upload(?:\/([a-f0-9-]{36})(?:\/(commit|parts\/(\d+)))?)?$/);
  if(!match)return;
  const [,id,uploadId,operation,indexText]=match,account=request.headers.get('X-Clipboard-Account')||'',device=request.headers.get('X-Clipboard-Device')||'';
  if(!UUID.test(id)||uploadId&&!UUID.test(uploadId))return fail(400,'INVALID_REQUEST');
  if(!uploadId){
    if(request.method!=='POST')return fail(405,'INVALID_REQUEST');
    const value=await input(request);
    if(!value||Object.keys(value).some(k=>!['kind','size','sha256','raw_file'].includes(k))||!['image','text','files'].includes(value.kind)
        ||!Number.isSafeInteger(value.size)||value.size<1||value.size>(value.kind==='image'?CLIPBOARD_MAX_BYTES:CLIPBOARD_MAX_ARCHIVE_BYTES)||!SHA.test(value.sha256))return fail(413,'CLIPBOARD_LIMIT');
    let raw:FileEnvelope|undefined;
    try{raw=value.raw_file===undefined?undefined:fileEnvelope(new Request('https://internal',{headers:{'X-Clipboard-Raw-File':JSON.stringify(value.raw_file)}}),value.kind,value.size);}catch{return fail(400,'CLIPBOARD_RAW_FILE_INVALID');}
    // This is the new negotiated transport only. Legacy ZIP/JSON single PUT
    // remains unchanged; new large files use the verified raw descriptor and
    // new large text uses exactly12 framing bytes plus at most100MiB content.
    if(value.kind==='files'&&!raw)return fail(400,'CLIPBOARD_RAW_FILE_INVALID');
    if(value.kind==='text'&&value.size>CLIPBOARD_MAX_BYTES+12)return fail(413,'CLIPBOARD_LIMIT');
    if((raw?envelopeSize(raw):value.size)<=MULTIPART_V1.threshold)return fail(400,'CLIPBOARD_SINGLE_UPLOAD_REQUIRED');
    const head=await ctx.storage.get<Head>('clipboard-head');
    if(head?.id===id&&head.device===device&&head.ready){
      if(head.kind!==value.kind||head.size!==(raw?envelopeSize(raw):value.size)||head.sha256!==value.sha256)return fail(409,'CLIPBOARD_PART_CONFLICT');
      return json({multipart_v1:true,complete:true,upload_id:head.upload_id,part_size:MULTIPART_V1.part_size,part_count:head.multipart_v1?.part_count,next_part:head.multipart_v1?.part_count,clipboard:head,shared_files:true});
    }
    const pending=await ctx.storage.get<Head>('clipboard-pending'),reserved=await ctx.storage.get<Head>('clipboard-reserved:'+id);
    const base=pending?.id===id?pending:raw&&await held(ctx,id,device)?reserved:undefined;
    if(!base||base.account!==account)return fail(409,'CLIPBOARD_REPLACED');
    if(base.device!==device)return fail(403,'CLIPBOARD_OWNER');
    await sweepMultiparts(ctx,env,spend);
    const created=await ctx.storage.transaction(async tx=>{
      const previous=await tx.get<string>(alias(id));const old=previous?await tx.get<Attempt>(entry(previous)):undefined;
      if(old&&!old.cancelled&&old.expires>Date.now())return old;
      if((await tx.list({prefix:'clipboard-multipart:',limit:5})).size>=4)return null;
      const next:Attempt={id:crypto.randomUUID(),head:base,kind:value.kind,size:value.size,sha256:value.sha256,raw_file:raw,
        parts:[],body_state:PersistentSHA256.load(null).save(),expires:Date.now()+600000,
        ...(raw?{wire_state:PersistentSHA256.load(null).update(from64(raw.prefix)).save()}:{} )};
      await tx.put(entry(next.id),next);await tx.put(alias(id),next.id);return next;
    });
    if(!created)return fail(429,'CLIPBOARD_UPLOAD_LIMIT');
    if(created.kind!==value.kind||created.size!==value.size||created.sha256!==value.sha256||JSON.stringify(created.raw_file)!==JSON.stringify(raw))return fail(409,'CLIPBOARD_PART_CONFLICT');
    // Allocation can succeed remotely while its DB response is lost. Its
    // durable attempt must already own an expiry alarm before that await.
    await arm(ctx,created.expires);
    try{
      // BEFORE INSERT quota triggers run even before SQLite OR IGNORE checks
      // the duplicate key. A true retry must not reserve the same size twice.
      await env.DB.prepare('INSERT INTO allocations(id,account_id,size) SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM allocations WHERE id=?)').bind(created.id,account,value.size,created.id).run();
      const reserved=await env.DB.prepare('SELECT account_id,size FROM allocations WHERE id=?').bind(created.id).first<{account_id:string;size:number}>();
      if(reserved?.account_id!==account||reserved.size!==value.size)return fail(409,'CLIPBOARD_PART_CONFLICT');
    }catch(error){
      const message=String(error),quota=message.includes('ACCOUNT_QUOTA')?'ACCOUNT_QUOTA':message.includes('GLOBAL_QUOTA')?'GLOBAL_QUOTA':null;
      if(quota){await discardMultipart(ctx,env,created);return fail(413,quota);}
      // A lost DB reply may follow a successful allocation; keep that durable
      // attempt so another init can recover instead of destroying valid parts.
      return fail(503,'CLIPBOARD_STORAGE_UNAVAILABLE');
    }
    return json(status(created),201);
  }
  const active=await ctx.storage.get<Head>('clipboard-head'),shared=await ctx.storage.get<Head>('clipboard-shared-ready:'+id);
  const ready=[active,shared].find(head=>head?.id===id&&head.upload_id===uploadId&&head.device===device&&head.ready);
  let a=await ctx.storage.get<Attempt>(entry(uploadId));
  if(ready){
    if(!operation&&request.method==='DELETE')return json({cancelled:false,clipboard:ready});
    if(!operation&&request.method==='GET'||operation==='commit'&&request.method==='POST')return json({multipart_v1:true,complete:true,clipboard:ready,shared_files:true,next_part:ready.multipart_v1?.part_count});
  }
  if(!a||a.head.id!==id||a.head.account!==account)return fail(410,'CLIPBOARD_UPLOAD_EXPIRED');
  if(a.head.device!==device)return fail(403,'CLIPBOARD_OWNER');
  if(!operation&&request.method==='DELETE'){await discardMultipart(ctx,env,a);return json({cancelled:true});}
  if(a.cancelled||a.expires<=Date.now()){await discardMultipart(ctx,env,a);return fail(410,'CLIPBOARD_UPLOAD_EXPIRED');}
  if(!await allowed(ctx,a)){await discardMultipart(ctx,env,a);return fail(409,'CLIPBOARD_REPLACED');}
  if(!operation&&request.method==='GET')return json(status(a));
  if(operation?.startsWith('parts/')&&request.method==='PUT'){
    const index=Number(indexText),count=Math.ceil(a.size/MULTIPART_V1.part_size),expected=Math.min(MULTIPART_V1.part_size,a.size-index*MULTIPART_V1.part_size),sha=request.headers.get('X-Content-SHA256')||'';
    if(!Number.isSafeInteger(index)||index<0||index>=count||!SHA.test(sha)||Number(request.headers.get('Content-Length'))!==expected)return fail(400,'CLIPBOARD_INTEGRITY');
    if(index<a.parts.length){
      if(a.parts[index].sha256!==sha)return fail(409,'CLIPBOARD_PART_CONFLICT');
      try{const data=await bodyBytes(request,expected);if(data.length!==expected||hex(PersistentSHA256.load(null).update(data).digest())!==sha)return fail(400,'CLIPBOARD_INTEGRITY');}
      catch{return fail(400,'CLIPBOARD_INTEGRITY');}
      return json(status(a));
    }
    if(index!==a.parts.length)return fail(409,'CLIPBOARD_PART_OUT_OF_ORDER',{next_part:a.parts.length});
    // Every writer owns a distinct key, even when a timed-out old lease and
    // its retry have exactly the same index/hash. Old cleanup cannot delete
    // the verified object published by the new lease.
    const lease=crypto.randomUUID(),key=`clipboard/${account}/${id}/${uploadId}/${index}-${lease}-${sha}`;
    const claimed=await ctx.storage.transaction(async tx=>{
      const current=await tx.get<Attempt>(entry(uploadId));if(!current||current.cancelled||current.parts.length!==index||(current.lease_until||0)>Date.now())return false;
      if((await tx.list({prefix:'clipboard-multipart-write:',limit:129})).size>=128)return false;
      current.lease=lease;current.lease_until=Date.now()+120000;current.inflight_key=key;
      await tx.put(writeEntry(lease),{key,attempt:uploadId,account,lease,size:expected,until:current.lease_until} satisfies Write);
      await tx.put(entry(uploadId),current);return true;
    });
    if(!claimed)return fail(423,'CLIPBOARD_UPLOAD_BUSY');
    let accepted=false;
    try{
      // The attempt reserves its eventual committed bytes. An independent
      // in-flight key can temporarily coexist with a successful retry, so it
      // owns an additional real quota reservation until adoption/true cleanup.
      try{
        await env.DB.prepare('INSERT INTO allocations(id,account_id,size) SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM allocations WHERE id=?)').bind(writeAllocation(lease),account,expected,writeAllocation(lease)).run();
      }catch(error){const value=String(error);return fail(value.includes('ACCOUNT_QUOTA')||value.includes('GLOBAL_QUOTA')?413:503,value.includes('ACCOUNT_QUOTA')?'ACCOUNT_QUOTA':value.includes('GLOBAL_QUOTA')?'GLOBAL_QUOTA':'CLIPBOARD_STORAGE_UNAVAILABLE');}
      let data:Uint8Array;try{data=await bodyBytes(request,expected);}catch{return fail(400,'CLIPBOARD_INTEGRITY');}
      if(data.length!==expected)return fail(400,'CLIPBOARD_INTEGRITY');
      if(a.kind==='text'&&index===0&&(data.length<12||!data.subarray(0,8).every((byte,i)=>byte===[70,68,67,76,73,80,50,0][i])||new DataView(data.buffer,data.byteOffset,data.byteLength).getUint32(8)>a.size-12))return fail(400,'CLIPBOARD_INTEGRITY');
      if(!await validSession(request,env,account,device))return fail(401,'UNAUTHORIZED');
      const check=await ctx.storage.get<Attempt>(entry(uploadId));if(!check||check.lease!==lease||check.cancelled||!await allowed(ctx,check))return fail(409,'CLIPBOARD_REPLACED');
      const body=PersistentSHA256.load(a.body_state).update(data),wire=a.wire_state?PersistentSHA256.load(a.wire_state).update(data):null;
      await spend('A');
      try{const stored=await env.FILES.put(key,data,{sha256:sha});if(!stored||stored.size!==expected||!stored.checksums.sha256||hex(new Uint8Array(stored.checksums.sha256))!==sha)return fail(400,'CLIPBOARD_INTEGRITY');}
      catch{return fail(400,'CLIPBOARD_INTEGRITY');}
      if(!await validSession(request,env,account,device))return fail(401,'UNAUTHORIZED');
      const next=await ctx.storage.transaction(async tx=>{
        const current=await tx.get<Attempt>(entry(uploadId)),pending=await tx.get<Head>('clipboard-pending'),hold=await tx.get<Hold>('clipboard-hold:'+id);
        if(!current||current.cancelled||current.expires<=Date.now()
          ||pending?.id!==id&&!(current.raw_file&&hold&&hold.device===device&&hold.expires>Date.now()&&hold.items.length))return null;
        const writer=await tx.get<Write>(writeEntry(lease));
        if(current.lease!==lease||current.parts.length!==index||!writer||writer.gc_claim)return 'retry' as const;
        current.parts.push({key,size:expected,sha256:sha});current.body_state=body.save();if(wire)current.wire_state=wire.save();
        delete current.lease;delete current.lease_until;delete current.inflight_key;await tx.put(entry(uploadId),current);return current;
      });
      if(!next)return fail(409,'CLIPBOARD_REPLACED');if(next==='retry')return fail(423,'CLIPBOARD_UPLOAD_BUSY');accepted=true;return json(status(next));
    }finally{
      try{
        const record=await ctx.storage.transaction(async tx=>{const record=await tx.get<Write>(writeEntry(lease))||(!accepted?{key,attempt:uploadId,account,lease,size:expected,until:0,gc_claim:crypto.randomUUID()} satisfies Write:null);if(!record)return null;record.terminal=true;await tx.put(writeEntry(lease),record);return record;});
        if(record)await cleanWrite(ctx,env,record);
      }finally{
        const current=await ctx.storage.get<Attempt>(entry(uploadId));
        if(current?.lease===lease){delete current.lease;delete current.lease_until;if(!accepted)delete current.inflight_key;await ctx.storage.put(entry(uploadId),current);if(current.cancelled||!await allowed(ctx,current))await discardMultipart(ctx,env,current);}
      }
    }
  }
  if(operation==='commit'&&request.method==='POST'){
    const value=await input(request);if(!value||Object.keys(value).length)return fail(400,'INVALID_REQUEST');
    if(a.parts.length!==Math.ceil(a.size/MULTIPART_V1.part_size)||a.parts.reduce((sum,part)=>sum+part.size,0)!==a.size)return fail(409,'CLIPBOARD_PART_OUT_OF_ORDER',{next_part:a.parts.length});
    if((a.lease_until||0)>Date.now())return fail(423,'CLIPBOARD_UPLOAD_BUSY');
    const bodyHash=hex(PersistentSHA256.load(a.body_state).digest()),wireHash=a.raw_file?hex(PersistentSHA256.load(a.wire_state!).update(from64(a.raw_file.suffix)).digest()):bodyHash;
    if(bodyHash!==(a.raw_file?.sha256||a.sha256)||wireHash!==a.sha256){await discardMultipart(ctx,env,a);return fail(400,'CLIPBOARD_INTEGRITY');}
    if(!await validSession(request,env,account,device))return fail(401,'UNAUTHORIZED');
    const lease=crypto.randomUUID();
    const claimed=await ctx.storage.transaction(async tx=>{const current=await tx.get<Attempt>(entry(uploadId));if(!current||current.cancelled||(current.lease_until||0)>Date.now())return false;current.lease=lease;current.lease_until=Date.now()+120000;await tx.put(entry(uploadId),current);return true;});
    if(!claimed)return fail(423,'CLIPBOARD_UPLOAD_BUSY');
    try{
    await registerClipboardParts(env,{id,account,device,upload_id:a.id},
      {name:a.raw_file?.name||'clipboard-'+a.kind+'.bin',size:a.size,sha256:bodyHash},MULTIPART_V1.part_size,a.parts);
    if(!await validSession(request,env,account,device)){await discardMultipart(ctx,env,a);return fail(401,'UNAUTHORIZED');}
    let previous:Head|undefined;
    const result=await ctx.storage.transaction(async tx=>{
      const active=await tx.get<Head>('clipboard-head');if(active?.id===id&&active.ready)return active;
      const shared=await tx.get<Head>('clipboard-shared-ready:'+id);if(shared?.ready&&shared.device===device)return shared;
      const current=await tx.get<Attempt>(entry(uploadId));if(!current||current.cancelled||current.lease!==lease||current.expires<=Date.now())return null;
      const pending=await tx.get<Head>('clipboard-pending'),hold=await tx.get<Hold>('clipboard-hold:'+id);
      const ready:Head={...a.head,ready:true,kind:a.kind,size:a.raw_file?envelopeSize(a.raw_file):a.size,sha256:a.sha256,
        version:Math.max(a.head.version,(active?.version||0)+1),upload_id:uploadId,allocation_id:uploadId,
        object_key:`clipboard/${account}/${id}/${uploadId}`,raw_file:a.raw_file,multipart_v1:{part_size:MULTIPART_V1.part_size,part_count:a.parts.length}};
      if(pending?.id!==id){if(!a.raw_file||!hold||hold.device!==device||hold.expires<=Date.now()||!hold.items.length)return null;
        await tx.put('clipboard-shared-ready:'+id,ready);await tx.put('clipboard-held-head:'+id,ready);return ready;}
      previous=active;await tx.put('clipboard-head',ready);await tx.delete('clipboard-pending');
      if(a.raw_file)await tx.put('clipboard-shared-ready:'+id,ready);if(active)await tx.put('clipboard-retired:'+(active.allocation_id||'clipboard:'+active.id),active);return ready;
    });
    if(!result||result.upload_id!==uploadId){await discardMultipart(ctx,env,a);return fail(409,'CLIPBOARD_REPLACED');}
    await ctx.storage.delete(entry(uploadId));if(await ctx.storage.get(alias(id))===uploadId)await ctx.storage.delete(alias(id));
    await arm(ctx,result.at+86400000);if(previous)ctx.waitUntil(retire(ctx,env,previous));
    if((await ctx.storage.get<Head>('clipboard-head'))?.id===id)ctx.waitUntil(notify(device,result));
    return json({clipboard:result,shared_files:true,multipart_v1:true});
    }finally{
      const current=await ctx.storage.get<Attempt>(entry(uploadId));
      if(current?.lease===lease){delete current.lease;delete current.lease_until;await ctx.storage.put(entry(uploadId),current);if(current.cancelled||!await allowed(ctx,current))await discardMultipart(ctx,env,current);}
    }
  }
  return fail(405,'INVALID_REQUEST');
}

/** One lazy R2 reader; <=26 parts, one budget reservation, bounded connections. */
export async function multipartContent(ctx:DurableObjectState,env:Env,head:Head,spend:Spend):Promise<Response>{
  const parts=(await env.DB.prepare('SELECT idx,key,size,sha256 FROM parts WHERE upload_id=? AND ready=1 ORDER BY idx').bind(head.upload_id).all<ClipboardPart&{idx:number}>()).results;
  const size=head.raw_file?.size||head.size!;
  if(!parts.length||parts.length>26||parts.length!==head.multipart_v1?.part_count||parts.some((p,i)=>p.idx!==i||p.size!==Math.min(MULTIPART_V1.part_size,size-i*MULTIPART_V1.part_size)))return fail(503,'CLIPBOARD_MISSING');
  const lease=crypto.randomUUID();await env.DB.prepare('INSERT INTO read_leases(upload_id,id,expires_at) VALUES(?,?,?)').bind(head.upload_id,lease,Date.now()+1800000).run();
  let released=false,index=0,reader:ReadableStreamDefaultReader<Uint8Array>|undefined,received=0;
  const release=async()=>{if(released)return;released=true;await env.DB.prepare('DELETE FROM read_leases WHERE upload_id=? AND id=?').bind(head.upload_id,lease).run();try{await purgeUpload(env,head.upload_id!);}catch{/* Keep reserved allocation until scheduled cleanup. */}};
  try{await spend('B',parts.length);}catch(error){await release();throw error;}
  const stream=new ReadableStream<Uint8Array>({
    async pull(controller){
      try{
        while(true){
          if(reader){const result=await reader.read();if(result.value?.byteLength){received+=result.value.byteLength;if(received>parts[index-1].size)throw new Error('Multipart length');controller.enqueue(result.value);return;}
            if(!result.done)continue;reader.releaseLock();reader=undefined;if(received!==parts[index-1].size)throw new Error('Multipart length');}
          if(index===parts.length){controller.close();ctx.waitUntil(release());return;}
          const object=await env.FILES.get(parts[index].key);if(!object||object.size!==parts[index].size)throw new Error('Multipart missing');
          received=0;index++;reader=object.body.getReader();
        }
      }catch(error){controller.error(error);if(reader)await reader.cancel().catch(()=>{});ctx.waitUntil(release());}
    },
    async cancel(reason){if(reader)await reader.cancel(reason).catch(()=>{});await release();}
  },{highWaterMark:0});
  return new Response(head.raw_file?archiveBody(stream,head.raw_file):stream,{headers:{'Content-Type':'application/octet-stream','Content-Length':String(head.size),
    'X-Content-SHA256':head.sha256!,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
}
