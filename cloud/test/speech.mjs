/** Breeze transport fixtures. Never contacts a voice provider or spends credits. */
import assert from 'node:assert/strict';
import {speech} from '../src/speech.ts';
const nonces=new Set(),calls=[];
const env={BREEZE_API_KEY:'fixture-breeze-key-not-a-real-credential',DB:{prepare(){return {bind(key){return {async first(){if(nonces.has(key))return null;nonces.add(key);return {n:1};}};}};}}};
const input=(extra={})=>({text:'Hello from FlickDock.',language:'en',voice_id:'voc_fixture',speed:1.25,pitch:1,request_id:crypto.randomUUID(),...extra});
const request=value=>new Request('https://fixture/v1/speech/synthesize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
const provider=async(url,options)=>{
  calls.push([url,options]);assert.equal(options.redirect,'manual');assert.equal(options.headers['xi-api-key'],env.BREEZE_API_KEY);
  if(url.includes('/v1/voices?'))return Response.json({voices:[{voice_id:'voc_fixture',name:'Fixture voice',language_code:'en',instructions:'private provider metadata',preview_url:'https://private.invalid'}]});
  assert.match(url,/\/with-timestamps\?output_format=mp3_24000_64/);assert(url.includes('enable_logging=false'));
  return Response.json({audio_base64:Buffer.from('ID3-fixture-no-real-audio').toString('base64'),content_type:'audio/mpeg',word_timestamps:[{word:'Hello',start:0,end:.4},{word:'FlickDock',start:.4,end:1}]});
};
let response=await speech(new Request('https://fixture/v1/speech/voices?language=en'),env,'fixture-account',provider);
assert.equal(response.status,200);let result=await response.json();assert.equal(result.provider,'BreezeBlue');assert.equal(result.voices[0].voice_id,'voc_fixture');
assert(!JSON.stringify(result).includes('private'));assert(!JSON.stringify(result).includes(env.BREEZE_API_KEY));
const value=input();response=await speech(request(value),env,'fixture-account',provider);assert.equal(response.status,200);result=await response.json();assert.equal(result.word_timestamps.length,2);
let payload=JSON.parse(calls.at(-1)[1].body);assert.equal(payload.model_id,'breeze-tts-2-multilingual');assert.equal(payload.voice_settings.speed,1.25);assert(!Object.hasOwn(payload.voice_settings,'pitch'));
const before=calls.length;assert.equal((await speech(request(value),env,'fixture-account',provider)).status,409);assert.equal(calls.length,before);
for(const options of [{text:'x'.repeat(301)},{speed:.4},{speed:2.1},{pitch:1.5},{voice_id:'voc_private-not-in-default-catalog'},
  {request_id:'bad'},{language:'invalid'},{api_key:'never accept a client key'}])assert.equal((await speech(request(input(options)),env,'fixture-account',provider)).status,400);
response=await speech(request(input({pitch:1.2})),env,'fixture-account',provider);assert.equal(response.status,200);payload=JSON.parse(calls.at(-1)[1].body);assert(payload.instructions.includes('higher'));assert.equal(payload.voice_settings.guidance_scale,4);
response=await speech(request(input()),{...env,BREEZE_API_KEY:undefined},'fixture-account',provider);assert.equal(response.status,503);
const unavailableEnv={...env};response=await speech(request(input()),unavailableEnv,'fixture-account',async()=>{throw Error('private secret provider response');});
assert.equal(response.status,502);assert(!(await response.text()).includes('private secret'));
console.log('Breeze speech: bounded requests, public voice sanitization, exact speed field, no key disclosure, per-request dedup passed; no real network');
