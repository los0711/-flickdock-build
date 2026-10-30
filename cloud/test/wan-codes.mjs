/** Production signaling handlers with deterministic time/storage; no provider traffic. */
import assert from 'node:assert/strict';
import {registerHooks} from 'node:module';
registerHooks({
  resolve(specifier,context,next){return specifier==='cloudflare:workers'?{url:'wan-test:workers',shortCircuit:true}:next(specifier==='./relay-budget'?specifier+'.ts':specifier,context);},
  load(url,context,next){return url==='wan-test:workers'?{format:'module',shortCircuit:true,source:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env;}}'}:next(url,context);},
});
const {WanHub}=await import('../src/wan.ts');
const originalNow=Date.now;let now=10000000;Date.now=()=>now;
const values=new Map(),copy=value=>value===undefined?value:structuredClone(value);
const storage={
  async get(key){return copy(values.get(key));},
  async put(key,value){if(typeof key==='string')values.set(key,copy(value));else for(const [k,v] of Object.entries(key))values.set(k,copy(v));},
  async delete(keys){for(const key of Array.isArray(keys)?keys:[keys])values.delete(key);},
  async transaction(fn){return fn(storage);},async getAlarm(){return null;},async setAlarm(){},
  async list(options={}){return new Map([...values].filter(([key])=>key.startsWith(options.prefix||'')).slice(0,options.limit||1000).map(([key,value])=>[key,copy(value)]));},
};
const peers=[];
function socket(id){const seat={id:id.repeat(64),name:id,platform:'windows',connected:now,window:now,messages:0,ip:id.repeat(64)};
  const value={readyState:1,rows:[],deserializeAttachment:()=>copy(seat),serializeAttachment:data=>Object.assign(seat,copy(data)),close:()=>value.readyState=3,send:raw=>value.rows.push(JSON.parse(raw))};peers.push(value);return value;}
const alice=socket('a'),bob=socket('b'),mallory=socket('c');
const ctx={storage,getWebSockets:id=>peers.filter(peer=>!id||peer.deserializeAttachment().id===id)};
const hub=new WanHub(ctx,{TURN_ENABLED:'false'});let sequence=0;
const send=async(socket,value)=>{
  const before=socket.rows.length;
  await hub.webSocketMessage(socket,JSON.stringify(value));
  if(['accept','cancel','complete'].includes(value.type))assert.deepEqual(socket.rows.slice(before).filter(row=>row.type==='error'),[],`Unexpected ${value.type} failure`);
};
const last=(socket,type)=>socket.rows.filter(row=>row.type===type).at(-1);
const next=()=>String(++sequence).padStart(32,'0');
const input=id=>({type:'request',id,token:'d'.repeat(64),digest:'e'.repeat(64),total:'12',count:1,names:['original.bin']});
try{
  for(const peer of peers)await send(peer,{type:'hello',name:peer.deserializeAttachment().name,platform:'windows'});
  const first=last(bob,'identity');assert.equal(first.expires-first.serverNow,300000);assert.equal(first.refreshAt-first.serverNow,240000);
  now+=240000;await send(bob,{type:'refresh'});const refreshed=last(bob,'identity');assert.notEqual(first.code,refreshed.code);
  const id=next(),prepare={type:'prepare',id,token:'d'.repeat(64),code:first.code};
  await send(alice,prepare);assert.equal(last(alice,'prepared').id,id);assert.equal(bob.rows.filter(row=>row.type==='request').length,0);
  await send(alice,prepare);assert.equal(last(alice,'prepared').id,id,'Lost prepare responses must replay the same pairing');
  await send(mallory,{...prepare,code:refreshed.code,id:next()});assert.equal(last(mallory,'error').requestType,'prepare','Using an old/new alias once invalidates the whole epoch');
  await send(mallory,{...input(id),prepared:true});assert.equal(last(mallory,'error').requestType,'request');
  await send(alice,{...input(id),prepared:true,token:'f'.repeat(64)});assert.equal(last(alice,'error').requestType,'request');
  now+=120000;await send(alice,{...input(id),prepared:true});assert.equal(last(bob,'request').id,id,'Manifest preparation may outlive the original code');
  await send(bob,{type:'accept',id});assert.equal(last(alice,'begin').id,id);
  await hub.webSocketClose(bob);assert.equal(last(alice,'peers').peers[0].online,false);
  bob.readyState=1;await send(bob,{type:'hello',name:'bob',platform:'macos'});
  assert.equal(last(alice,'peers').peers[0].online,true);
  await send(alice,{type:'complete',id});await send(bob,{type:'complete',id});
  // All codes may expire; the mutually accepted recent-device identity remains usable.
  now+=360000;const recentId=next();await send(alice,{...input(recentId),peer:bob.deserializeAttachment().id});assert.equal(last(bob,'request').id,recentId);
  await send(alice,{type:'cancel',id:recentId});
  const expiredId=next();await send(mallory,{type:'prepare',id:expiredId,token:'d'.repeat(64),code:last(bob,'identity').code});assert.equal(last(mallory,'error').id,expiredId);
  await send(alice,{type:'remove',peer:bob.deserializeAttachment().id});assert.deepEqual(last(alice,'peers').peers,[]);assert.equal(last(bob,'peers').peers[0].id,alice.deserializeAttachment().id,'Removing local recent history preserves the other device history');
  await send(alice,{type:'complete',id});await send(bob,{type:'complete',id});
  assert.deepEqual(await storage.get('recent:'+alice.deserializeAttachment().id),[],'Late completion replays must not restore a deleted device');
  const removedId=next();await send(alice,{...input(removedId),peer:bob.deserializeAttachment().id});assert.equal(last(bob,'request').id,removedId,'Removing displayed history preserves previously confirmed pairing');
  await send(alice,{type:'cancel',id:removedId});
  now+=60000;await send(bob,{type:'refresh'});const valid=last(bob,'identity').code,cancelled=next();
  await send(alice,{type:'cancel',id:cancelled});await send(alice,{type:'prepare',id:cancelled,token:'d'.repeat(64),code:valid});assert.equal(last(alice,'error').id,cancelled);
  const legacyId=next();await send(alice,{...input(legacyId),code:valid});assert.equal(last(bob,'request').id,legacyId,'Existing desktop versions retain the code request protocol');
  console.log('PASS refresh deadlines, old-code overlap/single use, early reservation, sender/token binding, recent device expiry independence, deletion, cancellation and legacy requests');
}finally{Date.now=originalNow;}
