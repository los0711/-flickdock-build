import assert from 'node:assert/strict';
import {speech} from '../src/speech.ts';
const calls=[];const env={BREEZE_API_KEY:'fixture-key-only',DB:{}};
const provider=async(url,options)=>{
  calls.push({url,options});
  if(url.includes('/v1/voices?'))return Response.json({voices:[{voice_id:'native_voice',name:'Native sample',language_code:'en',preview_url:'https://fixture.example/native.mp3',instructions:'never expose this'}]});
  assert.equal(url,'https://fixture.example/native.mp3');assert(!options.headers);assert.equal(options.redirect,'manual');
  return new Response(Buffer.from('ID3-native-preview-fixture'),{headers:{'Content-Type':'audio/mpeg'}});
};
let result=await speech(new Request('https://fixture/v1/speech/voices?language=en'),env,'fixture',provider);let value=await result.json();
assert.equal(value.voices[0].has_preview,true);assert.equal(value.voices[0].language_code,'en');assert(!JSON.stringify(value).includes('fixture.example'));
result=await speech(new Request('https://fixture/v1/speech/preview?language=en&voice_id=native_voice'),env,'fixture',provider);assert.equal(result.status,200);value=await result.json();
assert.equal(Buffer.from(value.audio_base64,'base64').toString(),'ID3-native-preview-fixture');assert.deepEqual(value.word_timestamps,[]);
const before=calls.length;assert.equal((await speech(new Request('https://fixture/v1/speech/preview?language=en&voice_id=other'),env,'fixture',provider)).status,404);assert.equal(calls.length,before);
console.log('Default voice preview: native sample, no TTS request, no key sent to sample host, catalog validation passed.');
