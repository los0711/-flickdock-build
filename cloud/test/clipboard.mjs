/** Real workerd/D1/R2 with two devices, account isolation and in-flight replace. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,createHash,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe'],windowsHide:true});
let log='',origin='';server.stderr.on('data',data=>log+=data);
server.stdout.on('data',data=>{log+=data;const found=log.match(/http:\/\/127\.0\.0\.1:\d+/);if(found)origin=found[0]});
const hash=data=>createHash('sha256').update(data).digest('hex');
try{
  for(let i=0;i<150&&!origin;i++){if(server.exitCode!==null)throw Error(log);await sleep(250)}
  assert(origin,'Worker did not start: '+log);
  const req=async(method,path,token,body,headers={})=>{
    const response=await fetch(origin+path,{method,headers:{...(token?{Authorization:'Bearer '+token}:{}),...headers},body});
    return response;
  };
  const json=async(method,path,token,data,expected=200)=>{
    const response=await req(method,path,token,data===undefined?undefined:JSON.stringify(data),{'Content-Type':'application/json'});
    const value=await response.json();assert.equal(response.status,expected,JSON.stringify(value));return value;
  };
  async function sign(name,login=false){
    const challenge=await json('POST','/v1/auth/challenge',null,{username:name,purpose:login?'login':'register'});
    const auth_key=scryptSync('clipboard-fixture-password',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
    return json('POST','/v1/auth/'+(login?'login':'register'),null,{username:name,challenge_id:challenge.challenge_id,auth_key,device_name:login?'Mac':'Windows',platform:login?'macos':'windows'},login?200:201);
  }
  const a=await sign('clip-'+randomUUID()),b=await sign(a.account.username,true),outsider=await sign('other-'+randomUUID());
  let sequence=0;
  let copied_at=Date.now();
  const stage=async(token)=>{const id=randomUUID();copied_at=Math.max(copied_at+1,Date.now());await json('POST','/v1/clipboard',token,{id,protocol:2,sequence:++sequence,copied_at},201);return id};
  const put=async(token,id,body,kind='text')=>req('PUT',`/v1/clipboard/${id}/content`,token,body,{'Content-Type':'application/octet-stream','X-Clipboard-Kind':kind,'X-Content-SHA256':hash(body)});
  const first=await stage(a.token),body=Buffer.from(JSON.stringify({text:'原始文字\r\nhttps://example.invalid',html:''}));
  assert.equal((await put(a.token,first,body)).status,200);
  const head=(await json('GET','/v1/clipboard',b.token)).clipboard;assert.equal(head.id,first);assert.equal(head.ready,true);assert.equal(head.sha256,hash(body));
  for(const token of [a.token,b.token,b.token,a.token]){
    const response=await req('GET',`/v1/clipboard/${first}/content`,token);assert.equal(response.status,200);assert.deepEqual(Buffer.from(await response.arrayBuffer()),body);
  }
  assert.equal((await json('GET','/v1/clipboard',outsider.token)).clipboard,null);
  assert.equal((await req('GET',`/v1/clipboard/${first}/content`,outsider.token)).status,409);
  const oldHeaders={'User-Agent':'FlickDock/1.8.19 (CloudSync)'};
  const oldWrite=await req('POST','/v1/clipboard',b.token,JSON.stringify({id:randomUUID()}),{'Content-Type':'application/json',...oldHeaders});
  assert.equal(oldWrite.status,409,'Old clients must settle obsolete copies instead of retrying every five seconds');
  const oldHead=await req('GET','/v1/clipboard',b.token,undefined,oldHeaders);
  const oldValue=await oldHead.json();assert.equal(oldValue.clipboard,null);assert.equal(oldValue.upgrade_required,true);
  assert.equal((await req('GET',`/v1/clipboard/${first}/content`,b.token,undefined,oldHeaders)).status,426);
  const current=await req('GET','/v1/clipboard',b.token,undefined,{'User-Agent':'FlickDock/1.8.20 (CloudSync)'});
  assert.equal((await current.json()).clipboard.id,first);
  const currentBody=await req('GET',`/v1/clipboard/${first}/content`,b.token,undefined,{'User-Agent':'FlickDock/1.8.20 (CloudSync)'});
  assert.equal(currentBody.status,200);assert.deepEqual(Buffer.from(await currentBody.arrayBuffer()),body);
  assert.equal((await req('GET','/v1/clipboard')).status,401);
  console.log('PASS original bytes, repeated non-consuming paste, authenticated account isolation');
  const second=await stage(b.token);
  assert.equal((await json('GET','/v1/clipboard',a.token)).clipboard.id,first);
  const previous=await req('GET',`/v1/clipboard/${first}/content`,a.token);
  assert.equal(previous.status,200);assert.deepEqual(Buffer.from(await previous.arrayBuffer()),body);
  assert.equal((await put(b.token,second,body)).status,200);
  assert.equal((await req('GET',`/v1/clipboard/${first}/content`,a.token)).status,409);
  for(let i=0;i<50;i++){if((await json('GET','/v1/me',a.token)).storage.used_bytes===0)break;await sleep(100)}
  assert.equal((await json('GET','/v1/me',a.token)).storage.used_bytes,body.length,'Replacement retains only the committed allocation');
  assert.equal((await put(a.token,first,body)).status,409);assert.equal((await put(b.token,second,body)).status,200);
  await json('POST','/v1/clipboard',a.token,{id:first,protocol:2,sequence:++sequence,copied_at:Date.now()},409);
  await json('POST','/v1/clipboard',a.token,{id:randomUUID()},426);
  await json('POST','/v1/clipboard',a.token,{id:randomUUID(),protocol:2,sequence:1,copied_at:Date.now()},409);
  await json('POST','/v1/clipboard',a.token,{id:randomUUID(),protocol:2,sequence:++sequence,copied_at:copied_at-10000},409);
  assert.equal((await json('GET','/v1/clipboard',a.token)).clipboard.id,second);
  console.log('PASS any-device replacement deletes old storage and rejects stale upload');
  const large=Buffer.alloc(8*1024*1024,73),third=await stage(a.token);let offset=0;
  assert.equal((await json('GET','/v1/clipboard',b.token)).clipboard.id,second,'Staging never removes the valid clipboard');
  const stream=new ReadableStream({async pull(controller){await sleep(4);if(offset===large.length){controller.close();return}const end=Math.min(offset+65536,large.length);controller.enqueue(large.subarray(offset,end));offset=end}});
  const inflight=fetch(origin+`/v1/clipboard/${third}/content`,{method:'PUT',headers:{Authorization:'Bearer '+a.token,'Content-Length':String(large.length),'X-Clipboard-Kind':'files','X-Content-SHA256':hash(large)},body:stream,duplex:'half'}).catch(error=>error);
  await sleep(40);const fourth=await stage(b.token);await put(b.token,fourth,body);
  const result=await inflight;if(result instanceof Response){assert.notEqual(result.status,200);await result.arrayBuffer()}
  assert.equal((await json('GET','/v1/clipboard',a.token)).clipboard.id,fourth);
  console.log('PASS slow old upload cannot resurrect after newer clipboard');
  if(process.env.FD_QA_PYTHON){
    const child=spawn(process.env.FD_QA_PYTHON,[fileURLToPath(new URL('../../verify_clipboard.py',import.meta.url)),...(process.env.FD_QA_NATIVE==='1'?['--native-ci']:[])],{stdio:['pipe','pipe','pipe'],env:{...process.env,QT_QPA_PLATFORM:'offscreen'},windowsHide:true});
    let output='';child.stdout.on('data',data=>output+=data);child.stderr.on('data',data=>output+=data);
    child.stdin.end(JSON.stringify({origin,a,b}));const code=await new Promise(resolve=>child.once('exit',resolve));assert.equal(code,0,output);console.log(output.trim());
  }
  if(process.env.FD_QA_EXE){
    const directory=await mkdtemp(join(tmpdir(),'flickdock-frozen-clipboard-'));
    try{
      const input=join(directory,'connection.json'),output=join(directory,'result.json');
      await writeFile(input,JSON.stringify({origin,a,b}));
      const child=spawn(process.env.FD_QA_EXE,['--verify-clipboard','--verify-connection',input,'--verify-output',output],{stdio:'ignore',env:{...process.env,QT_QPA_PLATFORM:'offscreen',APPDATA:directory,LOCALAPPDATA:directory},windowsHide:true});
      const code=await new Promise(resolve=>child.once('exit',resolve));assert.equal(code,0);
      const result=JSON.parse(await readFile(output,'utf8'));assert(result.ok&&result.frozen&&result.original_bytes&&result.early_shelf_placeholder);
      if(process.env.FD_QA_REPORT)await writeFile(process.env.FD_QA_REPORT,JSON.stringify(result,null,2));
      console.log('PASS frozen Qt executable: original clipboard bytes, screenshot pixels, repeated paste, early shelf placeholder');
    }finally{await rm(directory,{recursive:true,force:true})}
  }
}finally{server.stdin.end();await new Promise(resolve=>{server.once('exit',resolve);setTimeout(resolve,10000).unref()})}
