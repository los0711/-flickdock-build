import assert from 'node:assert/strict';
import {translate} from '../src/translation.ts';
const nonces=new Set();
const env={DEEPSEEK_API_KEY:'test-only-key-not-a-real-provider-credential',DB:{prepare(sql){
  assert(!sql.includes('n=n+1'),'No account/global usage quota');
  return {bind(key){return {async first(){if(nonces.has(key))return null;nonces.add(key);return {n:1};}};}};
}}};
const body=(overrides={})=>({text:'Small details make work effortless.',target:'zh',request_id:crypto.randomUUID(),...overrides});
const request=value=>new Request('https://internal/translation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
let calls=0;
const upstream=async(url,options)=>{
 calls++;assert.equal(url,'https://api.deepseek.com/chat/completions');assert.equal(options.redirect,'manual');
 const value=JSON.parse(options.body);assert.equal(value.model,'deepseek-flash');assert.deepEqual(value.thinking,{type:'disabled'});assert.equal(value.stream,false);
 return Response.json({choices:[{finish_reason:'stop',message:{content:'细节让工作更轻松。'}}]});
};
const first=body();let response=await translate(request(first),env,upstream);
assert.equal(response.status,200);assert.equal((await response.json()).translation,'细节让工作更轻松。');
response=await translate(request(first),env,upstream);assert.equal(response.status,409);assert.equal(calls,1);
response=await translate(request(first),env,upstream);assert.equal(response.status,409);
for(let n=0;n<25;n++)assert.equal((await translate(request(body()),env,upstream)).status,200);
assert.equal(calls,26,'Explicit calls remain available without a daily cap');
for(const value of [body({target:'auto'}),body({text:' '}),body({text:'x'.repeat(16001)}),body({request_id:'invalid'}),{...body(),api_key:'must-not-accept-client-keys'}]){
 response=await translate(request(value),env,upstream);assert.equal(response.status,400);
}
response=await translate(request(body()),{...env,DEEPSEEK_API_KEY:undefined},upstream);assert.equal(response.status,503);
response=await translate(request(body()),env,async()=>Response.json({choices:[{finish_reason:'length',message:{content:'partial'}}]}));assert.equal(response.status,502);
response=await translate(request(body()),env,async()=>{throw Error('secret provider response');});
const failure=await response.text();assert(!failure.includes('secret provider'));assert(!failure.includes(env.DEEPSEEK_API_KEY));
console.log('Translation contract, no usage quotas, duplicate billing prevention and safe errors passed; no real API calls');

const {awesomeAlign}=await import('../src/awesome-align.ts');
const alignmentOrigin='https://translation.example.com';
const alignmentEnv={...env, TRANSLATION_PRIMARY_ENDPOINT:alignmentOrigin+'/v1/translate',
  TRANSLATION_PRIMARY_API_KEY:'test-only-alignment-key', SERVICE_TRANSLATION_ORIGINS:alignmentOrigin};
const words={source_text:'😀 café',target_text:'😀 咖啡',source_lang:'fr',target_lang:'zh'};
const mapped={protocol_version:'1',model_version:'awesome-fixture',alignment_version:'1.0.1',
  offset_unit:'unicode_codepoint',alignments:[{source:[2,6],target:[[2,4]]}],
  reverse_alignments:[{target:[2,4],source:[[2,6]]}]};
response=await awesomeAlign(request(words),alignmentEnv,async(url,options)=>{
  assert.equal(url,alignmentOrigin+'/v1/align');
  assert.equal(options.headers.Authorization,'Bearer '+alignmentEnv.TRANSLATION_PRIMARY_API_KEY);
  assert.equal(options.redirect,'manual');
  assert.deepEqual(JSON.parse(options.body),words);
  return Response.json(mapped);
});
assert.deepEqual(await response.json(),{schema_version:1,...mapped});
response=await awesomeAlign(request(words),alignmentEnv,async()=>new Response('private provider error',{
  status:503,headers:{'Retry-After':'2'}
}));
assert.equal(response.status,503);
assert.equal(response.headers.get('Retry-After'),'2');
assert.deepEqual(await response.json(),{schema_version:1,error:{code:'ALIGNMENT_UNAVAILABLE'}});
assert.equal((await awesomeAlign(request(words),env,()=>assert.fail('No matching provider'))).status,503);
assert.equal((await awesomeAlign(request({...words,source_text:'😀'.repeat(6001)}),alignmentEnv,
  ()=>assert.fail('Over-limit payload must be rejected'))).status,413);
console.log('Awesome-Align server-side authentication, code point contract, and isolated outage handling passed');
