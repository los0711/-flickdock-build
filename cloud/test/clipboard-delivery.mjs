/** Actual authenticated delivery handler: metadata only, no R2 payload reads. */
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {registerHooks} from 'node:module';
registerHooks({resolve(specifier,context,next){return next(specifier==='./shared-file'?specifier+'.ts':specifier,context);}});
const {clipboardRequest}=await import('../src/clipboard.ts');
const account=randomUUID(),otherAccount=randomUUID(),origin=randomUUID(),devices=[randomUUID(),randomUUID(),randomUUID()],outsider=randomUUID();
const members=new Map([[origin,account],...devices.map(device=>[device,account]),[outsider,otherAccount]]);
const head={id:randomUUID(),sha256:'a'.repeat(64),device:origin,account,version:1,ready:true,at:Date.now()};
const values=new Map([['clipboard-head',head]]),notifications=[];let writes=0;
const clone=value=>value===undefined?undefined:structuredClone(value);
const storage={async get(key){return clone(values.get(key));},async put(key,value){writes++;values.set(key,clone(value));},
  async delete(key){values.delete(key);},async transaction(fn){return fn(storage);}};
const ctx={storage,waitUntil(promise){notifications.push(promise);}};
const env={DB:{prepare(){return {bind(owner,...targets){return {async all(){return {results:targets.filter(id=>members.get(id)===owner).map(id=>({id}))};}};}};}},
  FILES:{get(){throw new Error('Delivery receipts must not read R2');},put(){throw new Error('Delivery receipts must not write R2');}}};
const events=[];
async function call(device,input,expected=200,id=head.id){
  const response=await clipboardRequest(new Request(`https://internal/clipboard/${id}/delivery`,{method:'POST',
    headers:{'X-Clipboard-Account':account,'X-Clipboard-Device':device},body:JSON.stringify(input)}),ctx,env,
    async()=>{throw new Error('Receipt charged a payload operation');},async()=>{throw new Error('Receipt published a new clipboard version');},
    async(device,delivery)=>events.push({device,delivery}));
  const result=await response.json();assert.equal(response.status,expected,JSON.stringify(result));return result;
}
const plan=Object.fromEntries(devices.map((device,index)=>[device,['lan','p2p','cloud'][index]]));
let reply=await call(origin,{sha256:head.sha256,plan});
assert.deepEqual(reply.delivery.receipts,{},'R2 backup cannot confirm any device');
for(const [index,device] of devices.entries())reply=await call(device,{sha256:head.sha256,route:['lan','p2p','cloud'][index]});
assert.deepEqual(reply.delivery.plan,plan);assert.deepEqual(reply.delivery.receipts,plan);
const before=writes,count=events.length;
await call(devices[2],{sha256:head.sha256,route:'cloud'});
assert.equal(writes,before);assert.equal(events.length,count,'Duplicate receipts must be idempotent');
await call(devices[0],{sha256:head.sha256,plan:{}},403);
await call(origin,{sha256:head.sha256,route:'cloud'},403);
await call(origin,{sha256:head.sha256,plan:{[outsider]:'p2p'}},403);
await call(origin,{sha256:head.sha256,plan:{[devices[0]]:'unknown'}},400);
await call(devices[0],{sha256:'b'.repeat(64),route:'lan'},409);
await call(devices[0],{sha256:head.sha256,route:'p2p'});
assert.equal(values.get('clipboard-delivery:'+head.id).receipts[devices[0]],'p2p','Fallback records the route actually used');
const previous=head.id;head.id=randomUUID();values.set('clipboard-head',head);
await call(devices[0],{sha256:head.sha256,route:'lan'},409,previous);
await Promise.all(notifications);
console.log('PASS actual delivery receipts, per-route targets, integrity/owner/account fences, idempotence and no R2 traffic');
