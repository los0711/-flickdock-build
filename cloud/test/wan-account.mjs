/** Actual Worker, D1 sessions and WebSockets: account membership is not public pairing. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,randomBytes,createHash,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import WebSocket from 'ws';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe'],windowsHide:true});
let logs='',origin='';const sockets=[];
server.stderr.on('data',data=>logs+=data);server.stdout.on('data',data=>{logs+=data;origin=logs.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]||''});
const hash=value=>createHash('sha256').update(value).digest('hex');
try{
  for(let i=0;i<150&&!origin;i++){if(server.exitCode!==null)throw Error(logs);await sleep(250)}
  assert(origin,logs);
  async function api(method,path,token,data,status=200){
    const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:data===undefined?undefined:JSON.stringify(data)});
    const result=await response.json();assert.equal(response.status,status,JSON.stringify(result));return result;
  }
  async function sign(name,login=false){
    const challenge=await api('POST','/v1/auth/challenge',null,{username:name,purpose:login?'login':'register'});
    const auth_key=scryptSync('wan-account-fixture',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
    return api('POST','/v1/auth/'+(login?'login':'register'),null,{username:name,challenge_id:challenge.challenge_id,auth_key,device_name:login?'Mac':'Windows',platform:login?'macos':'windows'},login?200:201);
  }
  const a=await sign('wan-'+randomUUID()),b=await sign(a.account.username,true),c=await sign('outside-'+randomUUID());
  async function connect(account,lan){
    const credential=randomBytes(32).toString('hex'),messages=[];
    const socket=new WebSocket(origin.replace('http:','ws:')+'/v1/wan/connect',{headers:{Authorization:'Bearer '+credential}});sockets.push(socket);
    socket.on('message',raw=>messages.push(JSON.parse(raw)));
    const wait=async(predicate,after=0)=>{for(let i=0;i<150;i++){const result=messages.slice(after).find(predicate);if(result)return result;await sleep(20)}throw Error('Missing message: '+JSON.stringify(messages))};
    await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject)});
    socket.send(JSON.stringify({type:'hello',name:'Anonymous',platform:'windows',lan_id:lan}));await wait(row=>row.type==='identity');
    const nonce=randomBytes(16).toString('hex');socket.send(JSON.stringify({type:'account_state',nonce,active:true}));
    await wait(row=>row.type==='account_state'&&row.nonce===nonce);
    await api('POST','/v1/wan/account',account.token,{credential,lan_id:lan,nonce});
    return {socket,messages,wait,id:hash(credential),credential,nonce,send:value=>socket.send(JSON.stringify(value))};
  }
  const sa=await connect(a,'a'.repeat(32)),sb=await connect(b,'b'.repeat(32)),sc=await connect(c,'c'.repeat(32));
  sa.send({type:'recent'});
  const peers=await sa.wait(row=>row.type==='peers'&&row.peers.some(peer=>peer.id===sb.id&&peer.online));
  const peer=peers.peers.find(row=>row.id===sb.id);assert.equal(peer.account_device,true);assert.equal(peer.lan_id,'b'.repeat(32));assert.equal(peer.name,'Mac');
  const spec=()=>({type:'request',id:randomBytes(16).toString('hex'),token:randomBytes(32).toString('hex'),digest:'d'.repeat(64),total:'12',count:1,names:['original.txt']});
  const request={...spec(),peer:sb.id};sa.send(request);
  assert.equal((await sb.wait(row=>row.type==='request'&&row.id===request.id)).peer.id,sa.id);
  console.log('PASS same-account first transfer without code, canonical name, badge and LAN identity');
  const unrelated={...spec(),peer:sb.id};sc.send(unrelated);
  assert.match((await sc.wait(row=>row.type==='error'&&row.id===unrelated.id)).message,/首次传输/);
  sb.socket.close();await new Promise(resolve=>sb.socket.once('close',resolve));
  const offline=await sa.wait(row=>row.type==='peers'&&row.peers.some(peer=>peer.id===sb.id&&!peer.online));assert(offline);
  const denied={...spec(),peer:sb.id};sa.send(denied);assert.match((await sa.wait(row=>row.type==='error'&&row.id===denied.id)).message,/不在线/);
  console.log('PASS cross-account cannot bypass code, live offline presence and server-side send rejection');
  const logoutNonce=randomBytes(16).toString('hex');sa.send({type:'account_state',nonce:logoutNonce,active:false});
  await sa.wait(row=>row.type==='account_state'&&row.nonce===logoutNonce);
  await api('POST','/v1/wan/account',a.token,{credential:sa.credential,lan_id:'a'.repeat(32),nonce:sa.nonce},409);
  sa.send({type:'recent'});assert.equal((await sa.wait(row=>row.type==='peers'&&row.peers.length===0)).peers.length,0);
  console.log('PASS logout intent rejects delayed authenticated bind even while old cloud token remains valid');
  // Intake is visible before any upload reservation/hash exists and is scoped to this account.
  const intake={item_id:randomUUID(),upload_id:randomUUID(),name:'Large folder',kind:'file',size:0};
  await api('POST','/v1/sync/intake',a.token,{items:[intake]});
  const staged=(await api('GET','/v1/sync/changes',b.token)).transfers.find(row=>row.item_id===intake.item_id);
  assert.equal(staged.state,'preparing');assert.equal(staged.size,0);assert.equal(staged.device_id,a.device_id);
  assert.equal((await api('GET','/v1/sync/changes',c.token)).transfers.length,0);
  await api('DELETE','/v1/sync/intake/'+intake.upload_id,a.token);
  await api('POST','/v1/sync/intake',a.token,{items:[intake]});
  assert.equal((await api('GET','/v1/sync/changes',b.token)).transfers.length,0,'Delayed intake cannot resurrect after cancellation');
  console.log('PASS preparing placeholder before reservation, no directory inode size, isolated account, stale intake cannot resurrect');
}finally{
  for(const socket of sockets)socket.terminate();server.stdin.end();
  if(server.exitCode===null)await new Promise(resolve=>{const timer=setTimeout(resolve,10000);server.once('exit',()=>{clearTimeout(timer);resolve()})});
}
