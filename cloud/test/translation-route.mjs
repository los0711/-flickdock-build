/** Real isolated workerd + D1 route verification, without provider credentials. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe'],windowsHide:true});
let log='',origin='';server.stderr.on('data',value=>log+=value);
server.stdout.on('data',value=>{log+=value;origin=log.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]||'';});
try{
 for(let n=0;n<150&&!origin;n++){if(server.exitCode!==null)throw Error('Isolated worker failed to start');await sleep(250);}
 assert(origin,'Isolated worker did not start');
 async function request(path,value,token){
  return fetch(origin+path,{method:'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(value)});
 }
 const input={text:'A translation sample.',target:'zh',request_id:randomUUID()};
 assert.equal((await request('/v1/translation',input)).status,401);
 const username='translation-'+randomUUID();
 const challenge=await (await request('/v1/auth/challenge',{username,purpose:'register'})).json();
 const auth_key=scryptSync('translation-only-local-fixture',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
 const registered=await request('/v1/auth/register',{username,challenge_id:challenge.challenge_id,auth_key,device_name:'Translation fixture',platform:'windows'});
 assert.equal(registered.status,201);const account=await registered.json();
 const response=await request('/v1/translation',input,account.token);
 assert.equal(response.status,503);const result=await response.json();
 assert.equal(result.error.code,'TRANSLATION_UNAVAILABLE');
 assert(!JSON.stringify(result).includes('API_KEY'));
 console.log('Real isolated Worker: authentication, translation routing and unconfigured-service response passed');
}finally{
 server.stdin.end('stop');await Promise.race([new Promise(resolve=>server.once('exit',resolve)),sleep(10000)]);
 if(server.exitCode===null)server.kill();
}
