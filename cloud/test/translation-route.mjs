/** Real isolated workerd + D1 route verification, without provider credentials. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
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
 const response=await request('/v1/translation',input);
 assert.equal(response.status,503);const result=await response.json();
 assert.equal(result.error.code,'TRANSLATION_UNAVAILABLE');
 assert(!JSON.stringify(result).includes('API_KEY'));
 assert.equal((await request('/v1/translation',input,'expired-session')).status,503);
 const privateResponse=await fetch(origin+'/v1/devices');assert.equal(privateResponse.status,401);
 console.log('Real isolated Worker: public translation, expired-token independence and protected account routes passed');
}finally{
 server.stdin.end('stop');await Promise.race([new Promise(resolve=>server.once('exit',resolve)),sleep(10000)]);
 if(server.exitCode===null)server.kill();
}
