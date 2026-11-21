/** Reconnects keep anonymous identity; shared NAT new-device abuse stays bounded. */
import assert from 'node:assert/strict';
import {registerHooks} from 'node:module';
registerHooks({resolve(s,c,next){return s==='cloudflare:workers'?{url:'wan-test:workers',shortCircuit:true}:next(['./relay-budget','./manual-signal','./cloud-pipe','./storage-budget'].includes(s)?s+'.ts':s,c);},
  load(u,c,next){return u==='wan-test:workers'?{format:'module',shortCircuit:true,source:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env;}}'}:next(u,c);}});
const {WanHub}=await import('../src/wan.ts');
const realNow=Date.now,NativeResponse=globalThis.Response,realFetch=globalThis.fetch;let now=100000;Date.now=()=>now;
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
  now+=120000;peers.length=0;
  const sender='a'.repeat(64),receiver='b'.repeat(64),out=[];
  for(const device of [sender,receiver])peers.push({readyState:1,seat:{id:device,ip,connected:now,window:now,messages:0},
    deserializeAttachment(){return this.seat;},serializeAttachment(value){this.seat=value;},send(raw){out.push({device,value:JSON.parse(raw)});}});
  const transfer='c'.repeat(32),session={id:transfer,sender:{id:sender,name:'sender',platform:'macos'},receiver:{id:receiver,name:'receiver',platform:'windows'},
    token:'d'.repeat(64),digest:'e'.repeat(64),total:'4096',count:1,names:['fixture.bin'],accepted:true,generation:1,expires:now+60000,done:[],connected:[]};
  await storage.put('session:'+transfer,session);let grants=0;
  hub.turn=async()=>{grants++;return {iceServers:[{urls:['turn:turn.cloudflare.com:3478?transport=udp']}],epoch:0};};
  await hub.webSocketMessage(peers[1],JSON.stringify({type:'retry',id:transfer,relayTransport:'tls'}));
  assert.equal(grants,0,'The receiver cannot spend a relay grant or restart the sender');out.length=0;
  await hub.webSocketMessage(peers[0],JSON.stringify({type:'retry',id:transfer}));
  assert.equal(grants,0);assert.equal(out.length,2);assert(out.every(row=>row.value.type==='begin'&&row.value.generation===2&&row.value.initiator===receiver));out.length=0;
  const description=async(peer,type,generation)=>hub.webSocketMessage(peer,JSON.stringify({type:'description',id:transfer,generation,sdp:'v=0\r\n',descriptionType:type}));
  await description(peers[1],'offer',2);await description(peers[0],'answer',2);
  assert.deepEqual(out.map(row=>[row.device,row.value.type,row.value.descriptionType]),[[sender,'description','offer'],[receiver,'description','answer']]);out.length=0;
  await description(peers[0],'offer',2);assert.equal(out[0].value.type,'error');out.length=0;
  await hub.webSocketMessage(peers[0],JSON.stringify({type:'relay',id:transfer,generation:2}));
  assert.equal(grants,1);assert.equal(out.length,2);assert(out.every(row=>row.value.type==='begin'&&row.value.relayTransport==='udp'&&row.value.generation===3&&row.value.recovery===false));
  out.length=0;await description(peers[0],'offer',3);await description(peers[1],'answer',3);
  assert.deepEqual(out.map(row=>[row.device,row.value.type,row.value.descriptionType]),[[receiver,'description','offer'],[sender,'description','answer']]);
  await storage.put('session:'+transfer,{...await storage.get('session:'+transfer),connected:[sender,receiver]});out.length=0;
  await hub.webSocketMessage(peers[0],JSON.stringify({type:'retry',id:transfer,relayTransport:'tls'}));
  assert.equal(grants,2);assert.equal(out.length,2);assert(out.every(row=>row.value.type==='begin'&&row.value.relayTransport==='tls'&&row.value.generation===4&&row.value.recovery===true));
  assert.deepEqual((await storage.get('session:'+transfer)).connected,[]);
  console.log('PASS authenticated native UDP relay, coordinated TLS recovery and receiver restart rejection');
  const {relayUsage}=await import('../src/relay-budget.ts');
  const usageValues=new Map(),usageStorage={async get(key){return copy(usageValues.get(key));},async put(key,value){usageValues.set(key,copy(value));},async transaction(f){return f(usageStorage);}};
  const env={CF_ANALYTICS_TOKEN:'synthetic-only',CF_ACCOUNT_ID:'a'.repeat(32)};let requests=0,release;
  globalThis.fetch=async()=>{requests++;await new Promise(resolve=>release=resolve);return NativeResponse.json({data:{viewer:{accounts:[{callsTurnUsageAdaptiveGroups:[{sum:{egressBytes:128}}],callsUsageAdaptiveGroups:[]}]}}});};
  const concurrent=Array.from({length:6},()=>relayUsage(env,usageStorage));
  while(!release)await new Promise(resolve=>setImmediate(resolve));release();
  assert.deepEqual((await Promise.all(concurrent)).map(value=>value.bytes),Array(6).fill(128));assert.equal(requests,1);
  await relayUsage(env,usageStorage);assert.equal(requests,1,'Fresh validated usage is reused');
  now+=31000;globalThis.fetch=async()=>{requests++;throw new Error('synthetic provider unavailable');};
  const failed=await Promise.allSettled(Array.from({length:3},()=>relayUsage(env,usageStorage)));
  assert(failed.every(value=>value.status==='rejected'));assert.equal(requests,2);
  globalThis.fetch=async()=>{requests++;return NativeResponse.json({data:{viewer:{accounts:[{callsTurnUsageAdaptiveGroups:[],callsUsageAdaptiveGroups:[]}]}}});};
  assert.equal((await relayUsage(env,usageStorage)).bytes,0);assert.equal(requests,3,'A failed shared read must permit a later retry');
  console.log('PASS concurrent relay monitoring shares one validated read, keeps failures closed and recovers after the provider returns');
}finally{Date.now=realNow;globalThis.Response=NativeResponse;globalThis.fetch=realFetch;delete globalThis.WebSocketPair;}
