/** One account-wide, non-consuming clipboard slot. Bodies stay in private R2. */
type Env={DB:D1Database;FILES:R2Bucket};
type Head={id:string;version:number;account:string;device:string;ready:boolean;expired?:boolean;kind?:string;size?:number;sha256?:string;at:number;object_key?:string;allocation_id?:string};
const MAX=20*1024*1024,UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const json=(value:object,status=200)=>Response.json({schema_version:1,...value},{status,headers:{'Cache-Control':'no-store'}});
const fail=(status:number,code:string)=>json({error:{code,message:'Clipboard content unavailable'}},status);
const key=(head:Head)=>head.object_key||`clipboard/${head.account}/${head.id}`;
const allocation=(head:Head)=>head.allocation_id||'clipboard:'+head.id;
const retiredKey=(head:Head)=>'clipboard-retired:'+allocation(head);
async function schedule(ctx:DurableObjectState,at:number){const old=await ctx.storage.getAlarm();if(old===null||old>at)await ctx.storage.setAlarm(at);}

async function retire(ctx:DurableObjectState,env:Env,head:Head){
  const entry=retiredKey(head);
  await ctx.storage.put(entry,head);
  try{
    await env.FILES.delete(key(head));
    await env.DB.prepare('DELETE FROM allocations WHERE id=? AND account_id=?').bind(allocation(head),head.account).run();
    await ctx.storage.delete(entry);
  }catch{await schedule(ctx,Date.now()+60000);}
}

export async function clipboardAlarm(ctx:DurableObjectState,env:Env){
  const seen=await ctx.storage.list<number>({prefix:'clipboard-seen:',limit:1000});
  for(const [id,at] of seen)if(Date.now()-at>172800000)await ctx.storage.delete(id);
  const retired=await ctx.storage.list<Head>({prefix:'clipboard-retired:',limit:30});
  for(const head of retired.values())await retire(ctx,env,head);
  const attempts=await ctx.storage.list<Head>({prefix:'clipboard-attempt:',limit:30});
  for(const [entry,attempt] of attempts){
    const active=await ctx.storage.get<Head>('clipboard-head');
    if(active?.ready&&key(active)===key(attempt)){await ctx.storage.delete(entry);continue;}
    if(active?.id!==attempt.id||Date.now()-attempt.at>=600000){await retire(ctx,env,attempt);await ctx.storage.delete(entry);}
  }
  const head=await ctx.storage.get<Head>('clipboard-head');
  if(head&&!head.expired&&(Date.now()-head.at>=86400000||!head.ready&&Date.now()-head.at>=600000)){
    const retiredHead=await ctx.storage.transaction(async tx=>{
      const current=await tx.get<Head>('clipboard-head');
      if(current?.id!==head.id||current.expired)return null;
      const empty:Head={id:crypto.randomUUID(),version:head.version+1,account:head.account,device:'',ready:false,expired:true,at:Date.now()};
      await tx.put('clipboard-head',empty);await tx.put(retiredKey(head),head);return head;
    });
    if(retiredHead)await retire(ctx,env,retiredHead);
  }
  const deadlines:number[]=[];
  if((await ctx.storage.list({prefix:'clipboard-retired:',limit:1})).size)deadlines.push(Date.now()+60000);
  const latest=await ctx.storage.get<Head>('clipboard-head');
  if(latest&&!latest.expired)deadlines.push(latest.at+(latest.ready?86400000:600000));
  for(const attempt of (await ctx.storage.list<Head>({prefix:'clipboard-attempt:',limit:30})).values())deadlines.push(attempt.at+600000);
  if(deadlines.length)await ctx.storage.setAlarm(Math.max(Date.now()+1000,Math.min(...deadlines)));
}

export async function clipboardRequest(request:Request,ctx:DurableObjectState,env:Env,
    spend:(kind:'A'|'B')=>Promise<void>,notify:(device:string)=>Promise<unknown>):Promise<Response>{
  const path=new URL(request.url).pathname,account=request.headers.get('X-Clipboard-Account')||'',device=request.headers.get('X-Clipboard-Device')||'';
  if(!UUID.test(account)||!UUID.test(device))return fail(401,'UNAUTHORIZED');
  if(path==='/clipboard'&&request.method==='POST'){
    let value:{id?:string};
    const reader=request.body?.getReader();let raw='';
    try{
      let size=0;const decoder=new TextDecoder('utf-8',{fatal:true,ignoreBOM:false});
      if(reader)while(true){const chunk=await reader.read();if(chunk.done)break;size+=chunk.value.byteLength;if(size>200){await reader.cancel();return fail(413,'BODY_TOO_LARGE');}raw+=decoder.decode(chunk.value,{stream:true});}
      raw+=decoder.decode();value=JSON.parse(raw);
    }catch{return fail(400,'INVALID_REQUEST');}finally{reader?.releaseLock();}
    if(!value||typeof value!=='object'||Object.keys(value).length!==1||!UUID.test(value.id||''))return fail(400,'INVALID_REQUEST');
    let previous:Head|undefined;
    const head=await ctx.storage.transaction(async tx=>{
      const old=await tx.get<Head>('clipboard-head');
      if(old&&old.id===value.id&&old.device===device)return old;
      if(await tx.get('clipboard-seen:'+value.id))return null;
      previous=old;
      const next:Head={id:value.id!,version:(old?.version||0)+1,account,device,ready:false,at:Date.now()};
      if(old)await tx.put(retiredKey(old),old);
      await tx.put('clipboard-head',next);await tx.put('clipboard-seen:'+value.id,Date.now());return next;
    });
    if(head===null)return fail(409,'CLIPBOARD_REPLACED');
    await schedule(ctx,Date.now()+600000);
    if(previous)ctx.waitUntil(retire(ctx,env,previous));
    ctx.waitUntil(notify(device));return json({clipboard:head},201);
  }
  let head=await ctx.storage.get<Head>('clipboard-head');
  if(head&&!head.expired&&Date.now()-head.at>=86400000){await clipboardAlarm(ctx,env);head=await ctx.storage.get<Head>('clipboard-head');}
  if(request.method==='GET'&&path==='/clipboard')return json({clipboard:head||null});
  const id=path.match(/^\/clipboard\/([a-f0-9-]{36})\/content$/)?.[1];
  if(!id||!head||head.id!==id||head.account!==account)return fail(409,'CLIPBOARD_REPLACED');
  if(request.method==='GET'){
    if(!head.ready)return fail(404,'CLIPBOARD_PENDING');
    await spend('B');const object=await env.FILES.get(key(head));
    if(!object)return fail(404,'CLIPBOARD_MISSING');
    return new Response(object.body,{headers:{'Content-Type':'application/octet-stream','Content-Length':String(head.size),
      'X-Content-SHA256':head.sha256!,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
  }
  if(request.method!=='PUT'||head.device!==device)return fail(403,'CLIPBOARD_OWNER');
  if(head.ready)return json({clipboard:head});
  const size=Number(request.headers.get('Content-Length')),kind=request.headers.get('X-Clipboard-Kind')||'',sha256=request.headers.get('X-Content-SHA256')||'';
  if(!Number.isSafeInteger(size)||size<1||size>=(kind==='files'?MAX+1024*1024:MAX)||!['text','image','files'].includes(kind)||!/^[a-f0-9]{64}$/.test(sha256)||!request.body)return fail(413,'CLIPBOARD_LIMIT');
  const current=async()=>{const latest=await ctx.storage.get<Head>('clipboard-head');return latest?.id===id&&!latest.expired;};
  const attemptId=crypto.randomUUID(),attempt:Head={...head,object_key:`clipboard/${account}/${id}/${attemptId}`,allocation_id:`clipboard:${id}:${attemptId}`};
  const attemptEntry='clipboard-attempt:'+attemptId;
  await ctx.storage.put(attemptEntry,attempt);await schedule(ctx,Date.now()+600000);
  let committed=false;
  try{
    await env.DB.prepare('INSERT INTO allocations(id,account_id,size) VALUES(?,?,?)').bind(allocation(attempt),account,size).run();
    if(!await current())return fail(409,'CLIPBOARD_REPLACED');
    await spend('A');
    const abort=new AbortController(),fixed=new FixedLengthStream(size),pumping=request.body.pipeTo(fixed.writable,{signal:abort.signal});
    try{await Promise.all([pumping,env.FILES.put(key(attempt),fixed.readable,{sha256,httpMetadata:{contentType:'application/octet-stream'}})]);}
    catch(error){abort.abort();await fixed.readable.cancel().catch(()=>{});await pumping.catch(()=>{});throw error;}
    const valid=await env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL')
      .bind(request.headers.get('X-Clipboard-Session')||'',account,device,Date.now()).first();
    if(!valid)return fail(401,'UNAUTHORIZED');
    const result=await ctx.storage.transaction(async tx=>{
      const latest=await tx.get<Head>('clipboard-head');if(latest?.id!==id||latest.expired)return null;
      if(latest.ready)return latest;
      const ready={...attempt,ready:true,kind,size,sha256};await tx.put('clipboard-head',ready);return ready;
    });
    if(!result)return fail(409,'CLIPBOARD_REPLACED');
    committed=key(result)===key(attempt);
    if(result.sha256!==sha256||result.kind!==kind||result.size!==size)return fail(409,'CLIPBOARD_REPLACED');
    await schedule(ctx,result.at+86400000);ctx.waitUntil(notify(device));return json({clipboard:result});
  }finally{
    if(!committed)await retire(ctx,env,attempt);
    await ctx.storage.delete(attemptEntry);
  }
}
