/** Actual workerd: four devices, ten idle minutes, constructor re-entry and original push protocol. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,createHash,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {writeFile} from 'node:fs/promises';
import WebSocket from 'ws';

const idleMs=Number(process.env.FD_QA_IDLE_MS||600000);
assert(Number.isSafeInteger(idleMs)&&idleMs>=1000&&idleMs<=900000,'Invalid idle test duration');
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],
  {stdio:['pipe','pipe','pipe'],windowsHide:true,env:{...process.env,FD_QA_HIBERNATION:'1'}});
let logs='',origin='';const peers=[],testBorn=performance.now();
server.stderr.on('data',data=>logs+=data);
server.stdout.on('data',data=>{logs+=data;origin=logs.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]||'';});
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
async function api(method,path,token,data,status=200){
  const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},
    body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();assert.equal(response.status,status,JSON.stringify(result));return result;
}
async function sign(username,login=false){
  const challenge=await api('POST','/v1/auth/challenge',null,{username,purpose:login?'login':'register'});
  const auth_key=scryptSync('isolated-push-test-password',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
  return api('POST','/v1/auth/'+(login?'login':'register'),null,{username,challenge_id:challenge.challenge_id,auth_key,
    device_name:'Disposable push peer',platform:'windows'},login?200:201);
}
async function connect(account){
  const rows=[],socket=new WebSocket(origin.replace('http:','ws:')+'/v1/events',{headers:{Authorization:'Bearer '+account.token}});
  const peer={socket,rows};peers.push(peer);
  socket.on('close',(code,reason)=>peer.closed={at_ms:Math.round(performance.now()-testBorn),code,reason:reason.toString()});
  socket.on('message',raw=>rows.push({at:performance.now(),value:JSON.parse(raw)}));
  await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject)});
  return peer;
}
async function until(predicate){for(let i=0;i<400;i++){if(predicate())return;await sleep(10)}throw Error('Push did not arrive');}
try{
  for(let i=0;i<150&&!origin;i++){if(server.exitCode!==null)throw Error(logs);await sleep(250)}
  assert(origin,'Worker startup failed: '+logs);
  const first=await sign('push-'+randomUUID()),accounts=[first];
  for(let i=0;i<3;i++)accounts.push(await sign(first.account.username,true));
  const outsider=await sign('outside-'+randomUUID());
  const sockets=[];for(const account of accounts)sockets.push(await connect(account));
  const outside=await connect(outsider);
  const probe=()=>api('GET','/__qa/events?account='+first.account.id);
  const before=await probe();assert.equal(before.connections,4);
  const baseline=sockets.map(peer=>peer.rows.length),started=performance.now();
  console.log('READY: '+sockets.length+' same-account devices idle for '+idleMs+' ms; no heartbeats or state requests');
  await sleep(idleMs);
  assert(sockets.every(peer=>peer.socket.readyState===WebSocket.OPEN),'Idle WebSocket closed');
  assert.deepEqual(sockets.map(peer=>peer.rows.length),baseline,'Unexpected idle application messages');
  const after=await probe();assert.equal(after.connections,4);
  const hibernated=before.bootId!==after.bootId;
  if(idleMs>=600000)assert(hibernated,'Constructor identity did not change: hibernation was not demonstrated');
  const publish=async(sequence)=>{
    const text=Buffer.from('original pushed text '+sequence+' 中文'),size=Buffer.alloc(4);size.writeUInt32BE(text.length);
    const body=Buffer.concat([Buffer.from('FDCLIP2\0'),size,text]),id=randomUUID(),at=performance.now();
    const committed=await api('POST','/v1/clipboard',first.token,{id,protocol:2,sequence,copied_at:Date.now(),
      inline:{kind:'text',body:body.toString('base64'),size:body.length,sha256:hash(body)}},201);
    return {id,at,committed,body};
  };
  const copy=await publish(1);
  await until(()=>sockets.slice(1).every(peer=>peer.rows.some(row=>row.value.type==='clipboard_inline'&&row.value.clipboard.id===copy.id)));
  const latency=sockets.slice(1).map(peer=>Math.round(peer.rows.find(row=>row.value.type==='clipboard_inline'&&row.value.clipboard.id===copy.id).at-copy.at));
  assert(!sockets[0].rows.some(row=>row.value.type==='clipboard_inline'),'Sender received its own copy');
  assert(!outside.rows.some(row=>row.value.type==='clipboard_inline'),'Clipboard crossed account boundary');
  sockets[3].socket.close();await new Promise(resolve=>sockets[3].socket.once('close',resolve));
  const missed=await publish(2);const reconnected=await connect(accounts[3]);
  const current=await api('GET','/v1/clipboard',accounts[3].token);
  assert.equal(current.clipboard.id,missed.id);assert.equal(current.clipboard.inline,missed.body.toString('base64'));
  assert.equal(reconnected.socket.readyState,WebSocket.OPEN);
  const result={ok:true,idle_ms:Math.round(performance.now()-started),requested_idle_ms:idleMs,devices:4,
    constructor_recreated:hibernated,open_connections:after.connections,idle_application_messages:0,
    idle_client_state_requests:0,short_text_push_ms:latency,sender_excluded:true,account_isolation:true,reconnect_latest:true};
  if(process.env.FD_QA_REPORT)await writeFile(process.env.FD_QA_REPORT,JSON.stringify(result,null,2));
  console.log('PASS '+JSON.stringify(result));
}catch(error){
  if(process.env.FD_QA_REPORT)await writeFile(process.env.FD_QA_REPORT,JSON.stringify({ok:false,error:error.message,
    sockets:peers.map(peer=>peer.closed||{open:peer.socket.readyState===WebSocket.OPEN}),
    runtime_events:logs.split(/\r?\n/).filter(line=>/reload|rebuilt|rebuilding|error/i.test(line)).slice(-20)},null,2));
  throw error;
}finally{
  for(const peer of peers)peer.socket.terminate();
  server.stdin.end();await new Promise(resolve=>{if(server.exitCode!==null)return resolve();server.once('exit',resolve);setTimeout(resolve,10000).unref()});
}
