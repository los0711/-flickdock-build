import assert from 'node:assert/strict';
import {translate} from '../src/translation.ts';
const calls=[],nonces=new Set();
const env={DEEPSEEK_API_KEY:'fixture-not-a-real-provider-key',DB:{prepare(){return {bind(k){return {async first(){if(nonces.has(k))return null;nonces.add(k);return {n:1};}}}}}}};
const request=value=>new Request('https://fixture/v1/translation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
const units=()=>({operation:'translate_units',source:'en',target:'zh',glossary:[],request_id:crypto.randomUUID(),units:[{id:'3',text:'Please confirm tomorrow.',before:'We have an order.',after:'Thank you.'}]});
const response=content=>Response.json({choices:[{finish_reason:'stop',message:{content:typeof content==='string'?content:JSON.stringify(content)}}],usage:{prompt_tokens:125,completion_tokens:30,prompt_cache_hit_tokens:64,prompt_cache_miss_tokens:61}});
const upstream=async(url,options)=>{const body=JSON.parse(options.body);calls.push(body);return response(body.messages[0].content.startsWith('Align')?{quote:'交付日期',certain:true}:'请在明天确认。');};
let input=units(),result=await translate(request(input),env,upstream);assert.equal(result.status,200);let body=await result.json();assert.equal(body.units[0].id,'3');assert.equal(body.usage.prompt_tokens,125);
assert.equal(body.units[0].text,'请在明天确认。');assert.equal(body.usage.prompt_cache_miss_tokens,61);
assert.equal(calls[0].thinking.type,'disabled');assert(calls[0].messages[0].content.includes('READ-ONLY'));assert.equal(calls[0].response_format,undefined);
assert.deepEqual(JSON.parse(calls[0].messages[1].content),{target_language:'简体中文',source_language:'英语',text:input.units[0].text,before:input.units[0].before,after:input.units[0].after});
assert.equal((await translate(request(input),env,upstream)).status,409);
assert.equal((await translate(request({...units(),units:[{...input.units[0],before:'😀'.repeat(180)}]}),env,upstream)).status,200);
for(const change of [{target:'invalid'},{source:'ignore rules'},{units:[]},{units:Array(129).fill(input.units[0])},{unexpected:true},
  {units:[...input.units,...input.units]},{units:[{...input.units[0],after:'x'.repeat(181)}]}])assert.equal((await translate(request({...units(),...change}),env,upstream)).status,400);
result=await translate(request({...units(),units:[input.units[0],{...input.units[0],id:'4'}]}),env,async()=>response({units:[]}));assert.equal(result.status,502);
const technical={...units(),glossary:['AI','FlickDock','MQO'],units:[
  {id:'3',text:'The PAID stamp is visible.',before:'Check the invoice.',after:''},
  {id:'4',text:'请在FlickDock中查看交期。',before:'',after:''}
]};
const termsFixture=async(url,options)=>{
  const {messages,response_format}=JSON.parse(options.body);
  assert.equal(response_format.type,'json_object');
  assert.match(messages[0].content,/established terminology for the domain/);
  const context=JSON.parse(messages[1].content);
  assert.deepEqual(context.protected_terms,['FlickDock']);
  assert.deepEqual(context.units,[{id:'3',text:technical.units[0].text,before:technical.units[0].before},
                                  {id:'4',text:technical.units[1].text}]);
  return response({units:[{id:'3',text:'可以看见已付款印章。'},{id:'4',text:'请在FlickDock中查看交期。'}]});
};
assert.equal((await translate(request(technical),env,termsFixture)).status,200);
assert.equal((await translate(request({...technical,request_id:crypto.randomUUID()}),env,async()=>response({units:[
  {id:'3',text:'可以看见已付款印章。'},{id:'4',text:'请在FlickDocking中查看交期。'}]}))).status,502);
input={operation:'align',source_text:'😀 delivery date',target_text:'😀 交付日期',selection:'delivery date',selection_start:2,side:'source',request_id:crypto.randomUUID()};
result=await translate(request(input),env,upstream);assert.equal(result.status,200);body=await result.json();assert.equal(body.quote,'交付日期');assert.equal(body.certain,true);
assert.equal((await translate(request({...input,request_id:crypto.randomUUID(),selection_start:3}),env,upstream)).status,400);
result=await translate(request({...input,request_id:crypto.randomUUID()}),env,async()=>response({quote:'fabricated',certain:true}));assert.equal((await result.json()).certain,false);
result=await translate(request({...input,request_id:crypto.randomUUID(),target_text:'交付日期和交付日期'}),env,upstream);assert.equal((await result.json()).certain,false);
result=await translate(request(units()),env,async()=>{throw Error('private copied text and credentials');});assert.equal(result.status,502);assert(!(await result.text()).includes('private copied'));
console.log('Translation units: bounded context, strict IDs, nonce reuse, exact Unicode alignment, ambiguous/invalid/failure cases passed.');
