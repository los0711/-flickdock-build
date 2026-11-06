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
    socket.send(JSON.stringify({type:'hello',name:'Anonymous',platform:'windows',lan_id:lan,features:['syncthing-v2']}));await wait(row=>row.type==='identity');
    const nonce=randomBytes(16).toString('hex');socket.send(JSON.stringify({type:'account_state',nonce,active:true}));
    await wait(row=>row.type==='account_state'&&row.nonce===nonce);
    await api('POST','/v1/wan/account',account.token,{credential,lan_id:lan,nonce});
    return {socket,messages,wait,id:hash(credential),credential,nonce,send:value=>socket.send(JSON.stringify(value))};
  }
  const sa=await connect(a,'a'.repeat(32)),sb=await connect(b,'b'.repeat(32)),sc=await connect(c,'c'.repeat(32));
  sa.send({type:'recent'});
  const peers=await sa.wait(row=>row.type==='peers'&&row.peers.some(peer=>peer.id===sb.id&&peer.online));
  const peer=peers.peers.find(row=>row.id===sb.id);assert.equal(peer.account_device,true);assert.equal(peer.lan_id,'b'.repeat(32));assert.equal(peer.name,'Mac');
  const request={type:'request',id:randomBytes(16).toString('hex'),token:randomBytes(32).toString('hex'),digest:'d'.repeat(64),total:'9',count:1,names:['file.bin'],peer:sb.id};
  sa.send(request);await sb.wait(row=>row.type==='request'&&row.id===request.id);
  const signal=(peer,value,generation=1)=>peer.send({type:'native_signal',id:request.id,generation,value});
  const native={kind:'native',device_id:Array(8).fill('AAAAAAA').join('-'),addresses:['dynamic'],content:{size:5,sha256:hash('hello')}};
  signal(sa,native);assert.match((await sa.wait(row=>row.type==='error'&&row.requestType==='native_signal')).message,/确认/);
  sb.send({type:'accept',id:request.id});const begin=await sa.wait(row=>row.type==='begin'&&row.id===request.id);
  assert.equal(begin.transport,'syncthing-v2');assert.equal(begin.account_device,true);assert.equal(begin.iceServers,undefined);
  const pre=sb.messages.length;signal(sb,{kind:'saved'});await sb.wait(row=>row.type==='error'&&row.requestType==='native_signal',pre);
  signal(sa,native);assert.deepEqual((await sb.wait(row=>row.type==='native_signal'&&row.value.kind==='native')).value,native);
  const bad=async(peer,value,generation=1)=>{const index=peer.messages.length;signal(peer,value,generation);return peer.wait(row=>row.type==='error'&&row.requestType==='native_signal',index)};
  await bad(sa,{...native,content:{...native.content,sha256:'f'.repeat(64)}});
  await bad(sb,{kind:'saved'},0);await bad(sc,{kind:'saved'});
  await bad(sb,native);await bad(sa,{kind:'progress',received:1,total:5});
  await bad(sb,{kind:'progress',received:1,total:6});
  console.log('PASS native consent, generation, peer role, immutable content and sender-only progress restrictions');
  async function upload(kind){
    const id=randomUUID(),created=await api('POST','/v1/uploads',a.token,{item_id:id,base_version:0,kind,name:'content.zip',mime_type:'application/zip',size:5,sha256:hash('hello'),fast_upload:true},201);
    if(created.instant)return created.record;
    const response=await fetch(origin+'/v1/uploads/'+created.upload_id+'/parts/0',{method:'PUT',headers:{Authorization:'Bearer '+a.token,'X-Content-SHA256':hash('hello')},body:'hello'});assert.equal(response.status,200);
    return (await api('POST','/v1/uploads/'+created.upload_id+'/commit',a.token,{})).record;
  }
  const shelf=await upload('file');await bad(sa,{kind:'r2',...Object.fromEntries(['id','version','size','sha256'].map(k=>[k,shelf[k]]))});
  const record=await upload('transfer'),link={kind:'r2',...Object.fromEntries(['id','version','size','sha256'].map(k=>[k,record[k]]))};
  await bad(sb,link);await bad(sa,{...link,version:2});await bad(sa,{...link,sha256:'f'.repeat(64)});
  signal(sa,link);assert.deepEqual((await sb.wait(row=>row.type==='native_signal'&&row.value.kind==='r2')).value,link);
  const hidden=(await api('GET','/v1/sync/changes',b.token));assert(!hidden.changes.some(r=>r.record.id===record.id));assert(hidden.changes.some(r=>r.record.id===shelf.id));
  const nonce=randomBytes(16).toString('hex');sb.send({type:'account_state',nonce,active:false});await sb.wait(row=>row.type==='account_state'&&row.nonce===nonce);
  await bad(sa,link);
  console.log('PASS R2 same-account ownership, committed hidden transfer, exact hash/version and live logout authorization');
  await api('DELETE','/v1/items/'+record.id,a.token,{base_version:record.version});await api('DELETE','/v1/items/'+shelf.id,a.token,{base_version:shelf.version});
}finally{
  for(const socket of sockets)socket.terminate();server.stdin.end();
  if(server.exitCode===null)await new Promise(resolve=>{const timer=setTimeout(resolve,10000);server.once('exit',()=>{clearTimeout(timer);resolve()})});
}
