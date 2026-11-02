/** Real local workerd/D1/R2 inclusive clipboard boundary; no public deployment. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,createHash,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {writeFile} from 'node:fs/promises';
import http from 'node:http';

const MAX=100*1024*1024,ARCHIVE_MAX=MAX+1024*1024;
const checks=[];let failure,origin='',logs='';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],
 {stdio:['pipe','pipe','pipe'],windowsHide:true});
server.stdout.on('data',data=>{logs+=data;const found=logs.match(/http:\/\/127\.0\.0\.1:\d+/);if(found)origin=found[0]});
server.stderr.on('data',data=>logs+=data);
const hash=data=>createHash('sha256').update(data).digest('hex');
async function check(name,action){await action();checks.push({name,ok:true});console.log('PASS',name)}
try{
 for(let i=0;i<150&&!origin;i++){if(server.exitCode!==null)throw Error('Local worker exited');await sleep(250)}
 assert(origin,'Local worker did not start');
 async function json(method,path,body,token,expected=200){
  const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)});
  const value=await response.json();assert.equal(response.status,expected,JSON.stringify(value));return value;
 }
 const username='limit-'+randomUUID();
 const challenge=await json('POST','/v1/auth/challenge',{username,purpose:'register'});
 const auth_key=scryptSync('local-boundary-fixture-password',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
 const owner=await json('POST','/v1/auth/register',{username,challenge_id:challenge.challenge_id,auth_key,device_name:'Synthetic boundary device',platform:'windows'},null,201);
 let sequence=0,copied=Date.now(),lastReady;
 async function stage(){const id=randomUUID();copied=Math.max(Date.now(),copied+1);await json('POST','/v1/clipboard',{id,protocol:2,sequence:++sequence,copied_at:copied},owner.token,201);return id}
 async function put(id,kind,data,{raw,sha=hash(data)}={}){
  const response=await fetch(origin+'/v1/clipboard/'+id+'/content',{method:'PUT',headers:{Authorization:'Bearer '+owner.token,'X-Clipboard-Kind':kind,'X-Content-SHA256':sha,...(raw?{'X-Clipboard-Raw-File':JSON.stringify(raw)}:{})},body:data});
  return {status:response.status,value:await response.json()};
 }
 async function rejectHeaders(id,kind,size,raw){
  // These are intentionally invalid headers, rejected before a body is read.
  // A low-level HTTP client observes that response without Undici trying to
  // finish sending a100MiB buffer into an already closed rejected connection.
  return new Promise((resolve,reject)=>{
   let responseStarted=false;
   const request=http.request(origin+'/v1/clipboard/'+id+'/content',{method:'PUT',headers:{Authorization:'Bearer '+owner.token,
    'X-Clipboard-Kind':kind,'Content-Length':String(size),'X-Content-SHA256':'0'.repeat(64),Connection:'close',
    ...(raw?{'X-Clipboard-Raw-File':JSON.stringify(raw)}:{})}},response=>{
     responseStarted=true;let text='';response.setEncoding('utf8');response.on('data',chunk=>text+=chunk);
     response.on('error',reject);response.on('end',()=>{try{resolve({status:response.statusCode,value:JSON.parse(text)})}catch(error){reject(error)}});
    });
   request.setTimeout(15000,()=>request.destroy(Error('Header-rejection response timed out')));
   request.on('error',error=>{if(!responseStarted)reject(error)});request.flushHeaders();request.end();
  });
 }
 async function unchanged(){const head=(await json('GET','/v1/clipboard',undefined,owner.token)).clipboard;assert.equal(head.id,lastReady);assert(head.ready)}
 async function downloadedHash(id){const response=await fetch(origin+'/v1/clipboard/'+id+'/content',{headers:{Authorization:'Bearer '+owner.token}});assert.equal(response.status,200);const digest=createHash('sha256');let size=0;for await(const chunk of response.body){size+=chunk.length;digest.update(chunk)}return {size,sha256:digest.digest('hex'),header:response.headers.get('X-Content-SHA256')};}
 const bytes=Buffer.alloc(MAX+1,0x39),exact=bytes.subarray(0,MAX),expected=hash(exact);
 await check('image-kind protocol payload exactly100MiB succeeds with actual verified R2 bytes',async()=>{
  const id=await stage(),result=await put(id,'image',exact);assert.equal(result.status,200,JSON.stringify(result.value));
  assert(result.value.clipboard.ready);assert.equal(result.value.clipboard.size,MAX);lastReady=id;
  assert.deepEqual(await downloadedHash(id),{size:MAX,sha256:expected,header:expected});
 });
 await check('image100MiB+1 declared-header rejection retains prior ready head',async()=>{
  const result=await rejectHeaders(await stage(),'image',MAX+1);assert.equal(result.status,413);assert.equal(result.value.error.code,'CLIPBOARD_LIMIT');await unchanged();
 });
 await check('text original100MiB plus exact FDCLIP2 framing succeeds unchanged',async()=>{
  const header=Buffer.alloc(12);header.write('FDCLIP2\0',0,'ascii');header.writeUInt32BE(MAX,8);
  const body=Buffer.concat([header,exact]),checksum=hash(body),id=await stage(),result=await put(id,'text',body);
  assert.equal(result.status,200,JSON.stringify(result.value));assert.equal(result.value.clipboard.size,MAX+12);lastReady=id;
  assert.deepEqual(await downloadedHash(id),{size:MAX+12,sha256:checksum,header:checksum});
 });
 // A valid single-file ZIP_STORED envelope; no compression/re-encoding.
 function envelope(data){
  const name=Buffer.from('original.bin'),table=Uint32Array.from({length:256},(_,index)=>{let c=index;for(let k=0;k<8;k++)c=(c>>>1)^((c&1)?0xedb88320:0);return c});
  let c=0xffffffff;for(const b of data)c=(c>>>8)^table[(c^b)&255];const crc=(c^0xffffffff)>>>0;
  const local=Buffer.alloc(30);local.writeUInt32LE(0x04034b50);local.writeUInt16LE(20,4);local.writeUInt32LE(crc,14);local.writeUInt32LE(data.length,18);local.writeUInt32LE(data.length,22);local.writeUInt16LE(name.length,26);
  const central=Buffer.alloc(46);central.writeUInt32LE(0x02014b50);central.writeUInt16LE(20,4);central.writeUInt16LE(20,6);central.writeUInt32LE(crc,16);central.writeUInt32LE(data.length,20);central.writeUInt32LE(data.length,24);central.writeUInt16LE(name.length,28);
  const end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(1,8);end.writeUInt16LE(1,10);end.writeUInt32LE(central.length+name.length,12);end.writeUInt32LE(local.length+name.length+data.length,16);
  const prefix=Buffer.concat([local,name]),suffix=Buffer.concat([central,name,end]);
  return {prefix,suffix,raw:{name:'original.bin',size:data.length,sha256:hash(data),prefix:prefix.toString('base64'),suffix:suffix.toString('base64')}};
 }
 const zip=envelope(exact),archiveDigest=createHash('sha256').update(zip.prefix).update(exact).update(zip.suffix).digest('hex');
 await check('raw original100MiB inclusive is stored once and legacy ZIP download preserves bytes/hash',async()=>{
  const id=await stage(),result=await put(id,'files',exact,{raw:zip.raw,sha:archiveDigest});
  assert.equal(result.status,200,JSON.stringify(result.value));assert.equal(result.value.clipboard.raw_file.size,MAX);
  const size=zip.prefix.length+MAX+zip.suffix.length;assert.equal(result.value.clipboard.size,size);assert(size<ARCHIVE_MAX);lastReady=id;
  for(let n=0;n<2;n++)assert.deepEqual(await downloadedHash(id),{size,sha256:archiveDigest,header:archiveDigest});
 });
 await check('raw original100MiB+1 is rejected even inside101MiB wire budget',async()=>{
  const result=await rejectHeaders(await stage(),'files',MAX+1,{...zip.raw,size:MAX+1,sha256:hash(bytes)});
  assert.equal(result.status,400);assert.equal(result.value.error.code,'CLIPBOARD_RAW_FILE_INVALID');await unchanged();
 });
 await check('legacy file ZIP includes envelope beyond100MiB without changing protocol',async()=>{
  const archive=Buffer.concat([zip.prefix,exact,zip.suffix]);const id=await stage(),result=await put(id,'files',archive);
  assert.equal(result.status,200,JSON.stringify(result.value));assert.equal(result.value.clipboard.size,archive.length);assert.equal(result.value.clipboard.sha256,archiveDigest);lastReady=id;
 });
 await check('legacy wire budget exact101MiB is inclusive and101MiB+1 rejects',async()=>{
  // Opaque legacy transport bytes exercise HTTP size/hash only; this is not
  // claimed to be a user-decodable ZIP fixture (the valid ZIP is tested above).
  const wire=Buffer.alloc(ARCHIVE_MAX+1,0x32),id=await stage(),result=await put(id,'files',wire.subarray(0,ARCHIVE_MAX));
  assert.equal(result.status,200,JSON.stringify(result.value));assert.equal(result.value.clipboard.size,ARCHIVE_MAX);lastReady=id;
  const bad=await rejectHeaders(await stage(),'files',ARCHIVE_MAX+1);assert.equal(bad.status,413);assert.equal(bad.value.error.code,'CLIPBOARD_LIMIT');await unchanged();
 });
 await check('authentication remains required after limit change',async()=>{await json('GET','/v1/clipboard',undefined,null,401)});
 console.log('ALL',checks.length,'LOCAL CLIPBOARD LIMIT CHECKS PASSED');
}catch(error){failure=error.stack;console.error(failure);process.exitCode=1}
finally{
 server.stdin.end();await new Promise(resolve=>{if(server.exitCode!==null)resolve();else{server.once('exit',resolve);setTimeout(()=>{server.kill();resolve()},12000).unref()}});
 if(process.env.FLICKDOCK_TEST_REPORT)await writeFile(process.env.FLICKDOCK_TEST_REPORT,JSON.stringify({checks,failure:failure||null,mode:'real local workerd/D1/R2; original100MiB and wire101MiB inclusive; no public deployment',server_log_tail:logs.slice(-3000)},null,2));
}
