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
const first=body();let response=await translate(request(first),env,'account-a',upstream);
assert.equal(response.status,200);assert.equal((await response.json()).translation,'细节让工作更轻松。');
response=await translate(request(first),env,'account-a',upstream);assert.equal(response.status,409);assert.equal(calls,1);
response=await translate(request(first),env,'account-b',upstream);assert.equal(response.status,200);
for(let n=0;n<25;n++)assert.equal((await translate(request(body()),env,'account-a',upstream)).status,200);
assert.equal(calls,27,'Explicit calls remain available without a daily cap');
for(const value of [body({target:'auto'}),body({text:' '}),body({text:'x'.repeat(16001)}),body({request_id:'invalid'}),{...body(),api_key:'must-not-accept-client-keys'}]){
 response=await translate(request(value),env,'account-a',upstream);assert.equal(response.status,400);
}
response=await translate(request(body()),{...env,DEEPSEEK_API_KEY:undefined},'account-a',upstream);assert.equal(response.status,503);
response=await translate(request(body()),env,'account-a',async()=>Response.json({choices:[{finish_reason:'length',message:{content:'partial'}}]}));assert.equal(response.status,502);
response=await translate(request(body()),env,'account-a',async()=>{throw Error('secret provider response');});
const failure=await response.text();assert(!failure.includes('secret provider'));assert(!failure.includes(env.DEEPSEEK_API_KEY));
console.log('Translation contract, no usage quotas, duplicate billing prevention and safe errors passed; no real API calls');
