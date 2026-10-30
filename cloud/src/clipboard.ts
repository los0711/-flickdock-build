import {fileEnvelope,envelopeSize,registerClipboardFile,releaseClipboardFile,archiveBody,type FileEnvelope} from './shared-file';
/** One account-wide, non-consuming clipboard slot. Bodies stay in private R2. */
type Env={DB:D1Database;FILES:R2Bucket};
type Head={id:string;version:number;account:string;device:string;ready:boolean;expired?:boolean;kind?:string;size?:number;sha256?:string;at:number;copied_at?:number;sequence?:number;object_key?:string;allocation_id?:string;inline?:string;upload_id?:string;raw_file?:FileEnvelope};
type Hold={device:string;items:string[];expires:number};
const MAX=20*1024*1024,UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const json=(value:object,status=200)=>Response.json({schema_version:1,server_time:Date.now(),...value},{status,headers:{'Cache-Control':'no-store'}});
const fail=(status:number,code:string)=>json({error:{code,message:'Clipboard content unavailable'}},status);
const key=(head:Head)=>head.object_key||`clipboard/${head.account}/${head.id}`;
const allocation=(head:Head)=>head.allocation_id||'clipboard:'+head.id;
const retiredKey=(head:Head)=>'clipboard-retired:'+allocation(head);
function legacyReader(request:Request){
  const match=/^FlickDock\/(\d+)\.(\d+)\.(\d+)(?:\s|$)/.exec(request.headers.get('User-Agent')||'');
  if(!match)return false;
  const [major,minor,patch]=match.slice(1).map(Number);
  return major<1||major===1&&(minor<8||minor===8&&patch<20);
}
async function schedule(ctx:DurableObjectState,at:number){const old=await ctx.storage.getAlarm();if(old===null||old>at)await ctx.storage.setAlarm(at);}

async function retained(ctx:DurableObjectState,id:string){
  const hold=await ctx.storage.get<Hold>('clipboard-hold:'+id);
  return !!(hold&&hold.expires>Date.now()&&hold.items.length);
}

async function retire(ctx:DurableObjectState,env:Env,head:Head){
  const entry=retiredKey(head);
  // A hold protects a verified winning result, never failed PUT attempts.
  // Multiple failures have distinct allocation IDs and must each be retired.
  if(head.ready&&await retained(ctx,head.id)){
    await ctx.storage.put('clipboard-held-head:'+head.id,head);await ctx.storage.delete(entry);return;
  }
  await ctx.storage.put(entry,head);
  try{
    if(head.upload_id&&await env.DB.prepare('SELECT 1 ok FROM uploads WHERE id=? AND account_id=?').bind(head.upload_id,head.account).first()){
      await releaseClipboardFile(env,head.id,head.upload_id,head.account);
    }
    else if(head.inline===undefined){
      await env.FILES.delete(key(head));
      await env.DB.prepare('DELETE FROM allocations WHERE id=? AND account_id=?').bind(allocation(head),head.account).run();
    }
    await ctx.storage.transaction(async tx=>{
      const shared=await tx.get<Head>('clipboard-shared-ready:'+head.id);
      if(shared&&key(shared)===key(head))await tx.delete('clipboard-shared-ready:'+head.id);
    });
    await ctx.storage.delete(entry);
  }catch{await schedule(ctx,Date.now()+60000);}
}

export async function clipboardAlarm(ctx:DurableObjectState,env:Env){
  const holds=await ctx.storage.list<Hold>({prefix:'clipboard-hold:',limit:128});
  for(const [entry,hold] of holds)if(hold.expires<=Date.now())await ctx.storage.delete(entry);
  const heldHeads=await ctx.storage.list<Head>({prefix:'clipboard-held-head:',limit:128});
  for(const [entry,head] of heldHeads)if(!await retained(ctx,head.id)){
    await retire(ctx,env,head);await ctx.storage.delete(entry);
  }
  const reservations=await ctx.storage.list<Head>({prefix:'clipboard-reserved:',limit:128});
  for(const [entry,head] of reservations)if(Date.now()-head.at>=86400000&&!await retained(ctx,head.id))await ctx.storage.delete(entry);
  const shared=await ctx.storage.list<Head>({prefix:'clipboard-shared-ready:',limit:128});
  for(const [entry,head] of shared)if(Date.now()-head.at>=86400000&&!await retained(ctx,head.id))await ctx.storage.delete(entry);
  const seen=await ctx.storage.list<number>({prefix:'clipboard-seen:',limit:1000});
  for(const [id,at] of seen)if(Date.now()-at>172800000)await ctx.storage.delete(id);
  const retired=await ctx.storage.list<Head>({prefix:'clipboard-retired:',limit:30});
  for(const head of retired.values())await retire(ctx,env,head);
  const attempts=await ctx.storage.list<Head>({prefix:'clipboard-attempt:',limit:30});
  for(const [entry,attempt] of attempts){
    const active=await ctx.storage.get<Head>('clipboard-head');
    if(active?.ready&&key(active)===key(attempt)){await ctx.storage.delete(entry);continue;}
    const pending=await ctx.storage.get<Head>('clipboard-pending');
    if((pending?.id!==attempt.id||Date.now()-attempt.at>=600000)&&!await retained(ctx,attempt.id)){await retire(ctx,env,attempt);await ctx.storage.delete(entry);}
  }
  const head=await ctx.storage.get<Head>('clipboard-head');
  if(head&&!head.expired&&(Date.now()-head.at>=86400000||!head.ready&&Date.now()-head.at>=600000)){
    const retiredHead=await ctx.storage.transaction(async tx=>{
      const current=await tx.get<Head>('clipboard-head');
      if(current?.id!==head.id||current.expired)return null;
      const pending=await tx.get<Head>('clipboard-pending');
      const empty:Head={id:crypto.randomUUID(),version:Math.max(current.version,pending?.version||0)+1,account:head.account,device:'',ready:false,expired:true,at:Date.now()};
      await tx.put('clipboard-head',empty);await tx.put(retiredKey(head),head);return head;
    });
    if(retiredHead)await retire(ctx,env,retiredHead);
  }
  const deadlines:number[]=[];
  const pending=await ctx.storage.get<Head>('clipboard-pending');
  if(pending){
    if(Date.now()-pending.at>=600000)await ctx.storage.transaction(async tx=>{
      const current=await tx.get<Head>('clipboard-pending');if(current?.id===pending.id)await tx.delete('clipboard-pending');
    });
    else deadlines.push(pending.at+600000);
  }
  if((await ctx.storage.list({prefix:'clipboard-retired:',limit:1})).size)deadlines.push(Date.now()+60000);
  const latest=await ctx.storage.get<Head>('clipboard-head');
  if(latest&&!latest.expired)deadlines.push(latest.at+(latest.ready?86400000:600000));
  for(const attempt of (await ctx.storage.list<Head>({prefix:'clipboard-attempt:',limit:30})).values())deadlines.push(attempt.at+600000);
  for(const hold of (await ctx.storage.list<Hold>({prefix:'clipboard-hold:',limit:128})).values())deadlines.push(hold.expires);
  if(deadlines.length)await ctx.storage.setAlarm(Math.max(Date.now()+1000,Math.min(...deadlines)));
}

export async function clipboardRequest(request:Request,ctx:DurableObjectState,env:Env,
    spend:(kind:'A'|'B')=>Promise<void>,notify:(device:string,head?:Head)=>Promise<unknown>):Promise<Response>{
  const path=new URL(request.url).pathname,account=request.headers.get('X-Clipboard-Account')||'',device=request.headers.get('X-Clipboard-Device')||'';
  if(!UUID.test(account)||!UUID.test(device))return fail(401,'UNAUTHORIZED');
  const holdMatch=path.match(/^\/clipboard\/([a-f0-9-]{36})\/(retain|release|shared)$/);
  if(holdMatch&&UUID.test(holdMatch[1])){
    const id=holdMatch[1],operation=holdMatch[2];
    let ready=await ctx.storage.get<Head>('clipboard-shared-ready:'+id);
    if(ready&&(!ready.upload_id||Date.now()-ready.at>=86400000||
        !await env.DB.prepare("SELECT 1 ok FROM uploads WHERE id=? AND account_id=? AND state='committed'").bind(ready.upload_id,account).first())){
      await ctx.storage.delete('clipboard-shared-ready:'+id);ready=undefined;
    }
    const active=await ctx.storage.get<Head>('clipboard-head');
    const pending=await ctx.storage.get<Head>('clipboard-reserved:'+id);
    const known=ready||(active?.id===id?active:pending);
    if(known&&known.device!==device)return fail(403,'CLIPBOARD_OWNER');
    if(operation==='shared'&&request.method==='GET'){
      if(!known)return fail(404,'SHARED_FILE_PENDING');
      return json({clipboard:known,shared_files:true});
    }
    if(request.method!=='POST')return fail(405,'INVALID_REQUEST');
    let input:{item_id?:string};
    const reader=request.body?.getReader();let text='';
    try{
      let size=0;const decoder=new TextDecoder('utf-8',{fatal:true,ignoreBOM:false});
      if(reader)while(true){const chunk=await reader.read();if(chunk.done)break;size+=chunk.value.byteLength;
        if(size>200){await reader.cancel();return fail(413,'BODY_TOO_LARGE');}text+=decoder.decode(chunk.value,{stream:true});}
      text+=decoder.decode();input=JSON.parse(text);
    }catch{return fail(400,'INVALID_REQUEST');}finally{reader?.releaseLock();}
    if(!input||!UUID.test(input.item_id||'')||Object.keys(input).some(k=>k!=='item_id'))return fail(400,'INVALID_REQUEST');
    const result=await ctx.storage.transaction(async tx=>{
      const key='clipboard-hold:'+id;
      const previous=await tx.get<Hold>(key);
      if(previous&&previous.device!==device)return null;
      if(operation==='retain'){
        if(!previous&&(await tx.list({prefix:'clipboard-hold:',limit:129})).size>=128)return null;
        const items=Array.from(new Set([...(previous?.items||[]),input.item_id!]));
        if(items.length>128)return null;
        const hold={device,items,expires:previous?.expires||Date.now()+86400000};await tx.put(key,hold);return hold;
      }
      const items=(previous?.items||[]).filter(item=>item!==input.item_id);
      if(items.length)await tx.put(key,{...previous!,items});else await tx.delete(key);
      return {device,items,expires:previous?.expires||0};
    });
    if(!result)return fail(409,'SHARED_FILE_HOLD_CONFLICT');
    if(!result.items.length&&active?.id!==id){
      const held=await ctx.storage.get<Head>('clipboard-held-head:'+id)||ready;
      if(held){await retire(ctx,env,held);await ctx.storage.delete('clipboard-held-head:'+id);}
    }
    await schedule(ctx,Date.now()+600000);
    return json({retained:operation==='retain',clipboard:known||null,shared_files:true});
  }
  if(path==='/clipboard'&&request.method==='POST'){
    let value:{id?:string;protocol?:number;sequence?:number;copied_at?:number;inline?:{body?:string;size?:number;sha256?:string;kind?:string}};
    const reader=request.body?.getReader();let raw='';
    try{
      let size=0;const decoder=new TextDecoder('utf-8',{fatal:true,ignoreBOM:false});
      if(reader)while(true){const chunk=await reader.read();if(chunk.done)break;size+=chunk.value.byteLength;if(size>48*1024){await reader.cancel();return fail(413,'BODY_TOO_LARGE');}raw+=decoder.decode(chunk.value,{stream:true});}
      raw+=decoder.decode();value=JSON.parse(raw);
    }catch{return fail(400,'INVALID_REQUEST');}finally{reader?.releaseLock();}
    if(!value||typeof value!=='object'||Object.keys(value).some(key=>!['id','protocol','sequence','copied_at','inline'].includes(key))||!UUID.test(value.id||''))return fail(400,'INVALID_REQUEST');
    const modern=value.protocol===2&&Number.isSafeInteger(value.sequence)&&value.sequence!>0&&Number.isSafeInteger(value.copied_at);
    if(!modern&&(value.protocol!==undefined||value.sequence!==undefined||value.copied_at!==undefined))return fail(400,'INVALID_REQUEST');
    if(modern&&(value.copied_at!>Date.now()+5000||value.copied_at!<Date.now()-86400000))return fail(409,'CLIPBOARD_CLOCK_EXPIRED');
    const inline=value.inline;
    if(inline!==undefined){
      if(!modern||!inline||typeof inline!=='object'||Object.keys(inline).some(k=>!['body','size','sha256','kind'].includes(k))
          ||inline.kind!=='text'||typeof inline.body!=='string'||inline.body.length>43692||!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(inline.body)
          ||!Number.isSafeInteger(inline.size)||inline.size!<1||inline.size!>32768||!/^[a-f0-9]{64}$/.test(inline.sha256||''))return fail(400,'INVALID_REQUEST');
      const blob=Uint8Array.from(atob(inline.body),c=>c.charCodeAt(0));
      const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',blob)),n=>n.toString(16).padStart(2,'0')).join('');
      if(blob.byteLength!==inline.size||digest!==inline.sha256)return fail(400,'CLIPBOARD_INTEGRITY');
    }else if(new TextEncoder().encode(raw).byteLength>200)return fail(413,'BODY_TOO_LARGE');
    const head=await ctx.storage.transaction(async tx=>{
      const old=await tx.get<Head>('clipboard-head');
      // Once a current client has used the slot, pre-sequence clients cannot
      // replay an old copy and replace a newer version after a reconnect.
      if(!modern&&await tx.get('clipboard-protocol')===2)return 'upgrade' as const;
      if(old&&old.id===value.id&&old.device===device)return old;
      const pending=await tx.get<Head>('clipboard-pending');
      if(pending&&pending.id===value.id&&pending.device===device)return pending;
      const hold=await tx.get<Hold>('clipboard-hold:'+value.id);
      const keep=hold&&hold.device===device&&hold.expires>Date.now()&&hold.items.length;
      const reserved=await tx.get<Head>('clipboard-reserved:'+value.id);
      if(keep&&reserved&&reserved.device===device)return reserved;
      const detached=async()=>{
        if(!keep)return null;
        const next:Head={id:value.id!,version:Math.max(old?.version||0,pending?.version||0)+1,account,device,ready:false,at:Date.now(),copied_at:value.copied_at,sequence:value.sequence};
        await tx.put('clipboard-reserved:'+value.id,next);return next;
      };
      if(await tx.get('clipboard-seen:'+value.id))return detached();
      if(modern){
        const previous=await tx.get<number>('clipboard-sequence:'+device)||0;
        if(value.sequence!<=previous)return detached();
        const order=await tx.get<Head>('clipboard-order');
        if(order?.copied_at!==undefined&&(value.copied_at!<order.copied_at||
            value.copied_at===order.copied_at&&(order.device!==device?device<order.device:value.sequence!<=(order.sequence||0))))return detached();
        await tx.put('clipboard-sequence:'+device,value.sequence!);await tx.put('clipboard-protocol',2);
      }
      const version=Math.max(old?.version||0,pending?.version||0)+1;
      const next:Head={id:value.id!,version,account,device,ready:false,at:Date.now(),
                       copied_at:modern?value.copied_at:Date.now(),sequence:modern?value.sequence:undefined};
      // Reserve order immediately; keep the last committed body until its
      // replacement has completely uploaded and passed its hash check.
      await tx.put('clipboard-pending',next);await tx.put('clipboard-order',next);await tx.put('clipboard-reserved:'+value.id,next);
      await tx.put('clipboard-seen:'+value.id,Date.now());return next;
    });
    // 1.8.19 retries unknown HTTP failures every five seconds. Its existing
    // conflict path settles a superseded copy, so use that compatible status
    // to stop retrying an intent which must never replace v2 content.
    if(head==='upgrade')return fail(legacyReader(request)?409:426,'CLIPBOARD_UPGRADE_REQUIRED');
    if(head===null)return fail(409,'CLIPBOARD_REPLACED');
    if(inline!==undefined){
      const valid=await env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL')
        .bind(request.headers.get('X-Clipboard-Session')||'',account,device,Date.now()).first();
      if(!valid)return fail(401,'UNAUTHORIZED');
      let previous:Head|undefined;
      const ready=await ctx.storage.transaction(async tx=>{
        const active=await tx.get<Head>('clipboard-head');
        if(active&&active.id===value.id&&active.ready)return active;
        const pending=await tx.get<Head>('clipboard-pending');if(!pending||pending.id!==value.id||pending.device!==device)return null;
        const next:Head={...pending,version:Math.max(pending.version,(active?.version||0)+1),ready:true,kind:'text',size:inline.size,sha256:inline.sha256,inline:inline.body};
        previous=active;await tx.put('clipboard-head',next);await tx.delete('clipboard-pending');
        if(active)await tx.put(retiredKey(active),active);
        return next;
      });
      if(!ready||ready.kind!=='text'||ready.sha256!==inline.sha256||ready.size!==inline.size)return fail(409,'CLIPBOARD_REPLACED');
      await schedule(ctx,ready.at+86400000);
      if(previous)ctx.waitUntil(retire(ctx,env,previous));
      ctx.waitUntil(notify(device,ready));return json({clipboard:ready},201);
    }
    await schedule(ctx,Date.now()+600000);
    return json({clipboard:head,shared_files:true},201);
  }
  let head=await ctx.storage.get<Head>('clipboard-head');
  if(head&&!head.expired&&Date.now()-head.at>=86400000){await clipboardAlarm(ctx,env);head=await ctx.storage.get<Head>('clipboard-head');}
  // Pre-v2 clients cannot decode the new lossless text envelope. Do not let
  // them download and cache it again on every poll while awaiting upgrade.
  // This is capability negotiation only; account/device auth stays above.
  const needsUpgrade=head?.sequence!==undefined&&legacyReader(request);
  if(request.method==='GET'&&path==='/clipboard')return needsUpgrade?
    json({clipboard:null,upgrade_required:true}):json({clipboard:head||null});
  const id=path.match(/^\/clipboard\/([a-f0-9-]{36})\/content$/)?.[1];
  if(!id)return fail(409,'CLIPBOARD_REPLACED');
  if(request.method==='GET'){
    if(needsUpgrade)return fail(426,'CLIPBOARD_UPGRADE_REQUIRED');
    if(!head||head.id!==id||head.account!==account)return fail(409,'CLIPBOARD_REPLACED');
    if(!head.ready)return fail(404,'CLIPBOARD_PENDING');
    if(head.inline!==undefined){
      const blob=Uint8Array.from(atob(head.inline),c=>c.charCodeAt(0));
      return new Response(blob,{headers:{'Content-Type':'application/octet-stream','Content-Length':String(head.size),
        'X-Content-SHA256':head.sha256!,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
    }
    await spend('B');const object=await env.FILES.get(key(head));
    if(!object)return fail(404,'CLIPBOARD_MISSING');
    return new Response(head.raw_file?archiveBody(object.body,head.raw_file):object.body,{headers:{'Content-Type':'application/octet-stream','Content-Length':String(head.size),
      'X-Content-SHA256':head.sha256!,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
  }
  if(request.method!=='PUT')return fail(405,'INVALID_REQUEST');
  if(head?.id===id&&head.ready&&head.device===device)return json({clipboard:head});
  const completed=await ctx.storage.get<Head>('clipboard-shared-ready:'+id);
  if(completed?.ready&&completed.device===device&&completed.upload_id&&Date.now()-completed.at<86400000&&
      await env.DB.prepare("SELECT 1 ok FROM uploads WHERE id=? AND account_id=? AND state='committed'").bind(completed.upload_id,account).first())
    return json({clipboard:completed,shared_files:true});
  head=await ctx.storage.get<Head>('clipboard-pending');
  if(head?.id!==id&&await retained(ctx,id))head=await ctx.storage.get<Head>('clipboard-reserved:'+id);
  if(!head||head.id!==id||head.account!==account)return fail(409,'CLIPBOARD_REPLACED');
  if(head.device!==device)return fail(403,'CLIPBOARD_OWNER');
  const size=Number(request.headers.get('Content-Length')),kind=request.headers.get('X-Clipboard-Kind')||'',sha256=request.headers.get('X-Content-SHA256')||'';
  if(!Number.isSafeInteger(size)||size<1||size>=(['files','text'].includes(kind)?MAX+1024*1024:MAX)||!['text','image','files'].includes(kind)||!/^[a-f0-9]{64}$/.test(sha256)||!request.body)return fail(413,'CLIPBOARD_LIMIT');
  let rawFile:FileEnvelope|undefined;
  try{rawFile=fileEnvelope(request,kind,size);}catch{return fail(400,'CLIPBOARD_RAW_FILE_INVALID');}
  const current=async()=>{const latest=await ctx.storage.get<Head>('clipboard-pending');return latest?.id===id||await retained(ctx,id);};
  const attemptId=crypto.randomUUID(),attempt:Head={...head,object_key:`clipboard/${account}/${id}/${attemptId}`,allocation_id:rawFile?attemptId:`clipboard:${id}:${attemptId}`,...(rawFile?{upload_id:attemptId,raw_file:rawFile}:{})};
  const attemptEntry='clipboard-attempt:'+attemptId;
  await ctx.storage.put(attemptEntry,attempt);await schedule(ctx,Date.now()+600000);
  let committed=false;
  try{
    await env.DB.prepare('INSERT INTO allocations(id,account_id,size) VALUES(?,?,?)').bind(allocation(attempt),account,size).run();
    if(!await current())return fail(409,'CLIPBOARD_REPLACED');
    await spend('A');
    const abort=new AbortController(),fixed=new FixedLengthStream(size),pumping=request.body.pipeTo(fixed.writable,{signal:abort.signal});
    try{await Promise.all([pumping,env.FILES.put(key(attempt),fixed.readable,{sha256:rawFile?.sha256||sha256,httpMetadata:{contentType:'application/octet-stream'}})]);}
    catch(error){abort.abort();await fixed.readable.cancel().catch(()=>{});await pumping.catch(()=>{});throw error;}
    const valid=await env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL')
      .bind(request.headers.get('X-Clipboard-Session')||'',account,device,Date.now()).first();
    if(!valid)return fail(401,'UNAUTHORIZED');
    if(rawFile)await registerClipboardFile(env,{id,account,device,object_key:key(attempt),upload_id:attemptId},rawFile);
    let previous:Head|undefined;
    const result=await ctx.storage.transaction(async tx=>{
      const active=await tx.get<Head>('clipboard-head');
      if(active?.id===id&&active.ready)return active;
      // Both retries can pass the preflight while their bodies are in flight.
      // Elect the detached winner inside the same transaction as publication.
      const shared=await tx.get<Head>('clipboard-shared-ready:'+id);
      if(shared?.ready&&shared.device===device&&Date.now()-shared.at<86400000)return shared;
      const latest=await tx.get<Head>('clipboard-pending');
      const ready={...attempt,version:Math.max(attempt.version,(active?.version||0)+1),ready:true,kind,size:rawFile?envelopeSize(rawFile):size,sha256};
      const hold=await tx.get<Hold>('clipboard-hold:'+id);
      if(latest?.id!==id){
        if(!rawFile||!hold||hold.device!==device||hold.expires<=Date.now()||!hold.items.length)return null;
        await tx.put('clipboard-shared-ready:'+id,ready);await tx.put('clipboard-held-head:'+id,ready);return ready;
      }
      previous=active;await tx.put('clipboard-head',ready);
      if(rawFile)await tx.put('clipboard-shared-ready:'+id,ready);
      await tx.delete('clipboard-pending');
      if(active)await tx.put(retiredKey(active),active);
      return ready;
    });
    if(!result)return fail(409,'CLIPBOARD_REPLACED');
    committed=key(result)===key(attempt);
    if(result.sha256!==sha256||result.kind!==kind||result.size!==(rawFile?envelopeSize(rawFile):size))return fail(409,'CLIPBOARD_REPLACED');
    await schedule(ctx,result.at+86400000);
    if(previous)ctx.waitUntil(retire(ctx,env,previous));
    if((await ctx.storage.get<Head>('clipboard-head'))?.id===id)ctx.waitUntil(notify(device,result));
    return json({clipboard:result,shared_files:true});
  }finally{
    if(!committed)await retire(ctx,env,attempt);
    await ctx.storage.delete(attemptEntry);
  }
}
