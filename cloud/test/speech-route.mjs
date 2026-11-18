/** Local workerd + D1 auth routes; no real account, credentials or provider. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,scryptSync} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe'],windowsHide:true});
let logs='',origin='';server.stdout.on('data',chunk=>{logs+=chunk;origin=logs.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]||'';});server.stderr.on('data',chunk=>logs+=chunk);
try{
  for(let n=0;n<150&&!origin;n++){if(server.exitCode!==null)throw Error('Local worker exited');await sleep(250);}
  assert(origin,'Local worker did not start');
  async function api(method,path,data,token){
    const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:data===undefined?undefined:JSON.stringify(data)});
    return {status:response.status,value:await response.json()};
  }
  assert.equal((await api('GET','/v1/speech/voices?language=en')).status,401);
  const username='speech-qa-'+randomUUID();const challenge=(await api('POST','/v1/auth/challenge',{username,purpose:'register'})).value;
  const auth_key=scryptSync('synthetic-only-password',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
  const registration=await api('POST','/v1/auth/register',{username,challenge_id:challenge.challenge_id,auth_key,device_name:'Synthetic speech QA',platform:'windows'});
  assert.equal(registration.status,201);const token=registration.value.token;
  const voices=await api('GET','/v1/speech/voices?language=en',undefined,token);assert.equal(voices.status,503);assert.equal(voices.value.error.code,'SPEECH_NOT_CONFIGURED');
  const synth=await api('POST','/v1/speech/synthesize',{text:'Hello',language:'en',request_id:randomUUID()},token);assert.equal(synth.status,503);assert.equal(synth.value.error.code,'SPEECH_NOT_CONFIGURED');
  const translation=await api('POST','/v1/translation',{text:'Hello',target:'zh',request_id:randomUUID()});assert.equal(translation.status,503);assert.equal(translation.value.error.code,'TRANSLATION_UNAVAILABLE');
  assert.equal((await api('GET','/v1/devices',undefined,token)).status,200);
  console.log('Local real Worker: speech authentication, configured-service errors, public translation and unchanged device auth passed');
}finally{
  server.stdin.end('stop');await Promise.race([new Promise(resolve=>server.once('exit',resolve)),sleep(10000)]);
  if(server.exitCode===null)server.kill();
}
