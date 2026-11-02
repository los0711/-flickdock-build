/** Verified raw file ownership shared by clipboard and existing shelf uploads. */
type Env={DB:D1Database;FILES:R2Bucket};
export type FileEnvelope={name:string;size:number;sha256:string;prefix:string;suffix:string};
// Original content is inclusive; legacy ZIP/text framing has a separate cap.
export const CLIPBOARD_MAX_BYTES=100*1024*1024;
export const CLIPBOARD_MAX_ARCHIVE_BYTES=CLIPBOARD_MAX_BYTES+1024*1024;
const SHA=/^[a-f0-9]{64}$/;
const query=(env:Env,sql:string,...args:unknown[])=>env.DB.prepare(sql).bind(...args);

export function fileEnvelope(request:Request,kind:string,bodySize:number):FileEnvelope|undefined{
  const header=request.headers.get('X-Clipboard-Raw-File');
  if(header===null)return undefined;
  if(header.length>6000||kind!=='files')throw new Error('Invalid raw clipboard file');
  const value=JSON.parse(header) as FileEnvelope;
  if(!value||typeof value.name!=='string'||value.name.length<1||value.name.length>255
      ||/[\x00-\x1f\\/]/.test(value.name)||value.name==='.'||value.name==='..'
      ||!Number.isSafeInteger(value.size)||value.size!==bodySize||value.size<1||value.size>CLIPBOARD_MAX_BYTES
      ||!SHA.test(value.sha256)||typeof value.prefix!=='string'||typeof value.suffix!=='string'
      ||value.prefix.length>2000||value.suffix.length>3000)throw new Error('Invalid raw clipboard file');
  for(const text of [value.prefix,value.suffix])if(!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(text))throw new Error('Invalid ZIP envelope');
  const prefix=atob(value.prefix),suffix=atob(value.suffix);
  if(!prefix.startsWith('PK\x03\x04')||!suffix.startsWith('PK\x01\x02')||prefix.length<30||suffix.length<68)throw new Error('Invalid ZIP envelope');
  return {name:value.name,size:value.size,sha256:value.sha256,prefix:value.prefix,suffix:value.suffix};
}

export const envelopeSize=(value:FileEnvelope)=>atob(value.prefix).length+value.size+atob(value.suffix).length;

export async function registerClipboardFile(env:Env,value:{id:string;account:string;device:string;object_key:string;upload_id:string},file:FileEnvelope){
  const chunk=Math.max(65536,file.size),now=Date.now();
  await env.DB.batch([
    query(env,"INSERT INTO uploads(id,account_id,item_id,base_version,kind,name,mime_type,size,sha256,chunk_size,part_count,state,expires_at,computed_sha256,verified_size,next_part,device_id) VALUES(?,?,?,0,'file',?,'application/octet-stream',?,?,?,1,'committed',?,?,?,1,?)",
      value.upload_id,value.account,value.id,file.name,file.size,file.sha256,chunk,now+86400000,file.sha256,file.size,value.device),
    query(env,'INSERT INTO parts(upload_id,idx,key,size,sha256,ready) VALUES(?,0,?,?,?,1)',value.upload_id,value.object_key,file.size,file.sha256),
    query(env,'INSERT INTO clipboard_upload_refs(clipboard_id,account_id,upload_id,expires_at) VALUES(?,?,?,?)',value.id,value.account,value.upload_id,now+86400000),
  ]);
}

export type ClipboardPart={key:string;size:number;sha256:string};
/** Verified multipart backing store. INSERT committed never fires commit_item. */
export async function registerClipboardParts(env:Env,value:{id:string;account:string;device:string;upload_id:string},
    file:{name:string;size:number;sha256:string},chunk:number,parts:ClipboardPart[]){
  if(!Number.isSafeInteger(chunk)||chunk<65536||chunk>4*1024*1024||parts.length!==Math.ceil(file.size/chunk)
      ||parts.length>26||parts.some((part,index)=>part.size!==Math.min(chunk,file.size-index*chunk)||!SHA.test(part.sha256)))throw new Error('Invalid verified clipboard parts');
  const old=await query(env,'SELECT * FROM uploads WHERE id=?',value.upload_id).first<any>();
  if(old){
    if(old.account_id!==value.account||old.item_id!==value.id||old.state!=='committed'||old.size!==file.size
        ||old.sha256!==file.sha256||old.computed_sha256!==file.sha256||old.chunk_size!==chunk||old.part_count!==parts.length)throw new Error('Clipboard backing conflict');
    const actual=(await query(env,'SELECT idx,key,size,sha256 FROM parts WHERE upload_id=? AND ready=1 ORDER BY idx',value.upload_id).all<any>()).results;
    if(actual.length!==parts.length||actual.some((part,index)=>part.idx!==index||part.key!==parts[index].key||part.size!==parts[index].size||part.sha256!==parts[index].sha256))throw new Error('Clipboard part conflict');
    return;
  }
  const now=Date.now();
  await env.DB.batch([
    query(env,"INSERT INTO uploads(id,account_id,item_id,base_version,kind,name,mime_type,size,sha256,chunk_size,part_count,state,expires_at,computed_sha256,verified_size,next_part,device_id) VALUES(?,?,?,0,'file',?,'application/octet-stream',?,?,?,?,'committed',?,?,?,?,?)",
      value.upload_id,value.account,value.id,file.name,file.size,file.sha256,chunk,parts.length,now+86400000,file.sha256,file.size,parts.length,value.device),
    ...parts.map((part,index)=>query(env,'INSERT INTO parts(upload_id,idx,key,size,sha256,ready) VALUES(?,?,?,?,?,1)',value.upload_id,index,part.key,part.size,part.sha256)),
    query(env,'INSERT INTO clipboard_upload_refs(clipboard_id,account_id,upload_id,expires_at) VALUES(?,?,?,?)',value.id,value.account,value.upload_id,now+86400000),
  ]);
}

export async function purgeUpload(env:Env,id:string){
  const now=Date.now(),lease='gc:'+crypto.randomUUID();
  const claimed=await query(env,"UPDATE uploads SET lease=?,lease_until=? WHERE id=? AND state='garbage' AND garbage_after<=? AND lease_until<=? AND NOT EXISTS(SELECT 1 FROM items i WHERE i.active_upload=uploads.id AND i.deleted=0) AND NOT EXISTS(SELECT 1 FROM clipboard_upload_refs p WHERE p.upload_id=uploads.id AND p.expires_at>?) AND NOT EXISTS(SELECT 1 FROM read_leases r WHERE r.upload_id=uploads.id AND r.expires_at>?)",lease,now+300000,id,now,now,now,now).run();
  if(!claimed.meta.changes)return false;
  const parts=(await query(env,'SELECT key FROM parts WHERE upload_id=?',id).all<{key:string}>()).results;
  for(let start=0;start<parts.length;start+=512)await env.FILES.delete(parts.slice(start,start+512).map(p=>p.key));
  await env.DB.batch([
    query(env,'DELETE FROM allocations WHERE id=? AND EXISTS(SELECT 1 FROM uploads WHERE id=? AND lease=?)',id,id,lease),
    query(env,'DELETE FROM uploads WHERE id=? AND lease=?',id,lease),
  ]);
  return true;
}

export async function releaseClipboardFile(env:Env,id:string,upload:string,account:string){
  await env.DB.batch([
    query(env,'DELETE FROM clipboard_upload_refs WHERE clipboard_id=? AND upload_id=? AND account_id=?',id,upload,account),
    query(env,"UPDATE uploads SET state='garbage',garbage_after=? WHERE id=? AND account_id=? AND NOT EXISTS(SELECT 1 FROM items i WHERE i.active_upload=uploads.id AND i.deleted=0) AND NOT EXISTS(SELECT 1 FROM clipboard_upload_refs p WHERE p.upload_id=uploads.id AND p.expires_at>?)",Date.now(),upload,account,Date.now()),
  ]);
  await purgeUpload(env,upload);
}

export function archiveBody(body:ReadableStream<Uint8Array>,file:FileEnvelope){
  const reader=body.getReader();let started=false,done=false;
  return new ReadableStream<Uint8Array>({
    async pull(controller){
      try{
        if(!started){started=true;controller.enqueue(Uint8Array.from(atob(file.prefix),c=>c.charCodeAt(0)));return;}
        if(done){controller.close();return;}
        const next=await reader.read();
        if(next.done){done=true;controller.enqueue(Uint8Array.from(atob(file.suffix),c=>c.charCodeAt(0)));controller.close();reader.releaseLock();}
        else controller.enqueue(next.value);
      }catch(error){controller.error(error);}
    },
    async cancel(reason){await reader.cancel(reason);},
  });
}
