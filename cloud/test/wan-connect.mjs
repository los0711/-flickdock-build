/** Reconnects keep anonymous identity; shared NAT new-device abuse stays bounded. */
import assert from 'node:assert/strict';
import {registerHooks} from 'node:module';
registerHooks({resolve(s,c,next){return s==='cloudflare:workers'?{url:'wan-test:workers',shortCircuit:true}:next(s==='./relay-budget'?s+'.ts':s,c);},
  load(u,c,next){return u==='wan-test:workers'?{format:'module',shortCircuit:true,source:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env;}}'}:next(u,c);}});
const {WanHub}=await import('../src/wan.ts');
const realNow=Date.now,NativeResponse=globalThis.Response;let now=100000;Date.now=()=>now;
const values=new Map(),peers=[],copy=v=>v===undefined?v:structuredClone(v);
const storage={async get(k){return copy(values.get(k));},async put(k,v){values.set(k,copy(v));},async transaction(f){return f(storage);},async getAlarm(){return null;},async setAlarm(){}};
const ctx={storage,getWebSockets:id=>peers.filter(peer=>!id||peer.seat?.id===id),acceptWebSocket(peer){peers.push(peer);}};
globalThis.WebSocketPair=class {constructor(){for(const i of [0,1])this[i]={readyState:1,seat:null,serializeAttachment(v){this.seat=v;},close(){this.readyState=3;}};}};
globalThis.Response=class {static json(...args){return NativeResponse.json(...args);}constructor(body,init){this.status=init.status;this.webSocket=init.webSocket;}};
const hub=new WanHub(ctx,{}),id='a'.repeat(64),ip='f'.repeat(64);
const connect=(device,network=ip)=>hub.fetch(new Request('https://internal/connect',{headers:{'X-Wan-Device':device,'X-Wan-IP':network}}));
try{
  await storage.put('device:'+id,{id,name:'Known anonymous device',platform:'windows'});
  for(let i=0;i<45;i++){assert.equal((await connect(id)).status,101);now+=45000;}
  assert.equal(await storage.get('ip:'+ip),undefined,'Proven anonymous reconnects must not consume new identity allowance');
  for(let i=0;i<40;i++)assert.equal((await connect(i.toString(16).padStart(64,'0'),'e'.repeat(64))).status,101);
  assert.equal((await connect('b'.repeat(64),'e'.repeat(64))).status,429,'Fresh identity allowance remains enforced');
  let limited=false;for(let i=0;i<31;i++)limited||=(await connect(id)).status===429;
  assert(limited,'Per-device reconnect bursts stay bounded');
  console.log('PASS 45 proven anonymous reconnects inside one hour, shared NAT new identity cap, per-device burst cap');
}finally{Date.now=realNow;globalThis.Response=NativeResponse;delete globalThis.WebSocketPair;}
