/** Exercise the exact production alarm with a controllable clock, no cloud. */
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {clipboardAlarm} from '../src/clipboard.ts';
const id=randomUUID(),account=randomUUID(),now=Date.now(),data=new Map([['clipboard-head',{id,account,device:randomUUID(),version:7,at:now,ready:true,size:8,kind:'image'}]]),deleted=[],freed=[];
const storage={async get(key){return data.get(key)},async put(key,value){data.set(key,value)},async delete(key){data.delete(key)},
  async list({prefix,limit}){return new Map([...data].filter(([key])=>key.startsWith(prefix)).slice(0,limit))},
  async transaction(fn){return fn(storage)},async setAlarm(at){storage.alarm=at}};
const env={FILES:{async delete(key){deleted.push(key)}},DB:{prepare(sql){return {bind(...values){return {async run(){freed.push(values)}}}}}}};
const original=Date.now;
try{
  Date.now=()=>now+86400001;
  await clipboardAlarm({storage},env);
  assert.equal(data.get('clipboard-head').expired,true);assert.equal(data.get('clipboard-head').version,8);assert.notEqual(data.get('clipboard-head').id,id);
  assert.deepEqual(deleted,[`clipboard/${account}/${id}`]);assert.deepEqual(freed,[['clipboard:'+id,account]]);
  console.log('PASS exact 24-hour alarm clears clipboard object and allocation without a paste');
}finally{Date.now=original}
/** Alarm/commit ordering must not reuse a version already applied by clients. */
import {clipboardRequest} from '../src/clipboard.ts';
for(const inline of [true,false]){
 const oldId=randomUUID(),nextId=randomUUID(),device=randomUUID(),clock=now+86400001;
 data.clear();data.set('clipboard-head',{id:oldId,account,device,version:1,at:now,ready:true,size:8,kind:'text'});
 const pending={id:nextId,account,device,version:2,at:clock-2,ready:false,sequence:1,copied_at:clock-2};
 data.set('clipboard-pending',pending);data.set('clipboard-order',pending);
 const tasks=[];const ctx={storage,waitUntil(promise){tasks.push(promise)}};
 storage.getAlarm=async()=>storage.alarm??null;
 const content=Buffer.from('new copy after old expiry');
 const digest=Buffer.from(await crypto.subtle.digest('SHA-256',content)).toString('hex');
 const headers={'X-Clipboard-Account':account,'X-Clipboard-Device':device,'X-Clipboard-Session':'fixture'};
 const db={prepare(){return {bind(){return this},async run(){return {}},async first(){return {valid:1}}}}};
 let alarmDuringPut=false;
 // Stream plumbing alone is replaced in this unit. Real R2 SHA256 enforcement
 // and the HTTP handlers are additionally exercised by clipboard.mjs/workerd.
 const previousFixed=globalThis.FixedLengthStream;
 globalThis.FixedLengthStream=class extends TransformStream{constructor(size){super()}};
 const provider={DB:db,FILES:{async delete(){},async put(key,stream){
   const body=await new Response(stream).arrayBuffer();assert.equal(Buffer.from(body).toString(),content.toString());
   if(alarmDuringPut)await clipboardAlarm(ctx,provider);
 }}};
 try{
  Date.now=()=>clock;
  if(inline)await clipboardAlarm(ctx,provider);else alarmDuringPut=true;
  const request=inline?
    new Request('https://internal/clipboard',{method:'POST',headers,body:JSON.stringify({id:nextId,protocol:2,sequence:1,copied_at:pending.copied_at,
      inline:{kind:'text',size:content.length,sha256:digest,body:content.toString('base64')}})}):
    new Request('https://internal/clipboard/'+nextId+'/content',{method:'PUT',headers:{...headers,'Content-Length':String(content.length),'X-Clipboard-Kind':'text','X-Content-SHA256':digest},body:content});
  const response=await clipboardRequest(request,ctx,provider,async()=>{},async()=>{});
  assert.equal(response.status,inline?201:200);const ready=(await response.json()).clipboard;
  assert.equal(ready.id,nextId);assert.equal(ready.version,4);assert.equal(ready.ready,true);
  const latest=await clipboardRequest(new Request('https://internal/clipboard',{headers}),ctx,provider,async()=>{},async()=>{});
  assert.equal((await latest.json()).clipboard.version,4);
  await Promise.all(tasks);
  console.log('PASS expiry during '+(inline?'inline reservation':'R2 upload')+' publishes a strictly newer ready version');
 }finally{Date.now=original;globalThis.FixedLengthStream=previousFixed}
}
