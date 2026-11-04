import assert from 'node:assert/strict';
import {randomBytes,randomUUID,scryptSync,createHash} from 'node:crypto';
import WebSocket from 'ws';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe'],windowsHide:true}),sockets=[];
let logs='',origin='';
server.stderr.on('data',data=>logs+=data);
server.stdout.on('data',data=>{logs+=data;origin=logs.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]||''});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
try {
  for(let i=0;i<150&&!origin;i++){if(server.exitCode!==null)throw Error(logs);await pause(250)}
  assert(origin,logs);
  async function api(method,path,token,data,status=200) {
    const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:data===undefined?undefined:JSON.stringify(data)});
    const value=await response.json();assert.equal(response.status,status,JSON.stringify(value));return value;
  }
  async function sign(username,login=false) {
    const challenge=await api('POST','/v1/auth/challenge',null,{username,purpose:login?'login':'register'});
    const auth_key=scryptSync('peer-sync-qa',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
    return api('POST','/v1/auth/'+(login?'login':'register'),null,{username,challenge_id:challenge.challenge_id,auth_key,device_name:login?'Second':'First',platform:'windows'},login?200:201);
  }
  async function connect(account) {
    const credential=randomBytes(32).toString('hex'),messages=[];
    const socket=new WebSocket(origin.replace('http:','ws:')+'/v1/wan/connect',{headers:{Authorization:'Bearer '+credential}});sockets.push(socket);
    socket.on('message',raw=>messages.push(JSON.parse(raw)));
    const wait=async(predicate,after=0)=>{
      for(let i=0;i<200;i++){const value=messages.slice(after).find(predicate);if(value)return value;await pause(15);}
      throw Error('Expected account synchronization signal was not received');
    };
    await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject)});
    const send=value=>socket.send(JSON.stringify(value));send({type:'hello',name:'Peer QA',platform:'windows'});
    await wait(value=>value.type==='identity');
    const nonce=randomBytes(16).toString('hex');send({type:'account_state',nonce,active:true});
    await wait(value=>value.type==='account_state'&&value.nonce===nonce);
    await api('POST','/v1/wan/account',account.token,{credential,nonce,lan_id:'a'.repeat(32)});
    return {socket,send,messages,wait,nonce,id:createHash('sha256').update(credential).digest('hex')};
  }
  const first=await sign('peer-'+randomUUID()),second=await sign(first.account.username,true),outside=await sign('other-'+randomUUID());
  const a=await connect(first),b=await connect(second),c=await connect(outside);
  const id=randomBytes(16).toString('hex');a.send({type:'sync_open',id,peer:b.id});
  const startedA=await a.wait(value=>value.type==='sync_begin'&&value.id===id);
  const startedB=await b.wait(value=>value.type==='sync_begin'&&value.id===id);
  assert.equal(startedA.account,first.account.id);assert.equal(startedB.account,first.account.id);
  assert.equal(startedA.initiator,true);assert.equal(startedB.initiator,false);
  assert.equal(startedA.peer.id,b.id);assert.equal(startedB.peer.id,a.id);
  const description={type:'description',descriptionType:'offer',sdp:'v=0\r\n'};
  a.send({type:'sync_signal',id,payload:description});
  assert.deepEqual((await b.wait(value=>value.type==='sync_signal'&&value.id===id)).payload,description);
  a.send({type:'sync_lease',id});await a.wait(value=>value.type==='sync_lease'&&value.id===id);
  console.log('PASS same-account connection, canonical identity, SDP forwarding and authorization lease');
  const denied=randomBytes(16).toString('hex');c.send({type:'sync_open',id:denied,peer:b.id});
  await c.wait(value=>value.type==='sync_error'&&value.id===denied);
  c.send({type:'sync_signal',id,payload:description});await c.wait(value=>value.type==='sync_error'&&value.id===id);
  const before=b.messages.length;a.send({type:'sync_signal',id,payload:{type:'file',path:'private-path'}});
  await a.wait(value=>value.type==='sync_error'&&value.id===id);await pause(50);
  assert.equal(b.messages.slice(before).filter(value=>value.type==='sync_signal').length,0);
  console.log('PASS unrelated accounts, guessed sessions and file payloads cannot use the signaling channel');
  b.send({type:'account_state',nonce:randomBytes(16).toString('hex'),active:false});
  await a.wait(value=>value.type==='sync_closed'&&value.id===id);
  const checkpoint=a.messages.length;a.send({type:'sync_lease',id});
  await a.wait(value=>value.type==='sync_error'&&value.id===id,checkpoint);
  console.log('PASS logout revokes existing links and subsequent authorization leases');
} finally {
  for(const socket of sockets)socket.terminate();server.stdin.end();
  if(server.exitCode===null)await new Promise(resolve=>{const timer=setTimeout(resolve,10000);server.once('exit',()=>{clearTimeout(timer);resolve()})});
}
