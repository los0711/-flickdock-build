/** Advanced translation contract; fixture provider, no real API/key/account. */
import assert from 'node:assert/strict';
import {translate} from '../src/translation.ts';
const nonces=new Set();
const env={DEEPSEEK_API_KEY:'fixture-key-not-a-real-provider-credential',DB:{prepare(){return {bind(key){return {async first(){if(nonces.has(key))return null;nonces.add(key);return {n:1};}};}};}}};
const body=(overrides={})=>({text:'FlickDock keeps ideas moving.',target:'zh',request_id:crypto.randomUUID(),...overrides});
const request=value=>new Request('https://fixture/v1/translation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
let seen,calls=0;
const provider=async(url,options)=>{calls++;seen=JSON.parse(options.body);return Response.json({choices:[{finish_reason:'stop',message:{content:'FlickDock 让灵感流动。'}}]});};
let response=await translate(request(body({source:'en',glossary:['FlickDock']})),env,provider);
assert.equal(response.status,200);let result=await response.json();assert.equal(result.operation,'translate');assert.equal(result.capabilities_version,2);
assert.match(seen.messages[0].content,/selected source language is 英语/);assert.match(seen.messages[0].content,/FlickDock/);
const polish=body({operation:'polish',instruction:'使用亲切的语气',original:'A source text',glossary:['FlickDock']});
response=await translate(request(polish),env,provider);assert.equal(response.status,200);result=await response.json();assert.equal(result.operation,'polish');
assert.equal(seen.messages.length,2);assert.equal(seen.messages[0].role,'system');assert.match(seen.messages[0].content,/same language/);
assert.deepEqual(seen.messages[1],{role:'user',content:JSON.stringify({instruction:polish.instruction,text:polish.text})});
const before=calls;assert.equal((await translate(request(polish),env,provider)).status,409);assert.equal(calls,before);
for(const options of [{operation:'unknown'},{source:'invalid'},{glossary:[{}]},{glossary:['a'.repeat(81)]},{glossary:['x\0y']},
  {instruction:'must require polish'},{operation:'polish',instruction:'',original:''},{operation:'polish',instruction:'x'.repeat(1001),original:''},
  {operation:'polish',instruction:'改写',original:'x'.repeat(16001)},{api_key:'must-not-be-accepted'}]){
  assert.equal((await translate(request(body(options)),env,provider)).status,400,JSON.stringify(Object.keys(options)));
}
response=await translate(request(body({glossary:['FlickDock']})),env,async()=>Response.json({choices:[{finish_reason:'stop',message:{content:'没有保留指定品牌。'}}]}));
assert.equal(response.status,502);assert.equal((await response.json()).error.code,'TRANSLATION_TERMS');
response=await translate(request(body({text:'The PAID stamp is visible.',glossary:['AI']})),env,async(url,options)=>{
  const {messages}=JSON.parse(options.body);
  assert.match(messages[0].content,/established terminology for the domain/);
  assert.doesNotMatch(messages[0].content,/Preserve these literal terms/);
  return Response.json({choices:[{finish_reason:'stop',message:{content:'可以看见已付款印章。'}}]});
});
assert.equal(response.status,200);
response=await translate(request(body({operation:'polish',instruction:'精简',original:'',text:'a'.repeat(24000)})),env,provider);assert.equal(response.status,200);
for(const [scope,text,instruction,rewritten] of [
  ['source','FlickDock 已发布 Beta。', '删掉所有英文单词，合成一句。','新版已发布。'],
  ['target','FlickDock 适合随手存放资料。', '用英语写成两句对话。','Can I leave this here?\nOf course.']
]){
  const input=body({operation:'polish',polish_scope:scope,original:'Unselected context',text,instruction,glossary:['FlickDock']});
  response=await translate(request(input),env,async(url,options)=>{
    const {messages}=JSON.parse(options.body);
    assert.equal(messages.length,2);assert.equal(messages[0].role,'system');assert.match(messages[0].content,/same language/);
    assert.deepEqual(messages[1],{role:'user',content:JSON.stringify({instruction,text})});
    return Response.json({choices:[{finish_reason:'stop',message:{content:rewritten}}]});
  });
  assert.equal(response.status,200);
  result=await response.json();
  assert.equal(result.polish_scope,scope);assert.equal(result.translation,rewritten);
}
assert.equal((await translate(request(body({polish_scope:'source'})),env,provider)).status,400);
console.log('Native translator API: backward compatibility, polish identity, glossary enforcement, cancellation nonce and bounded validation passed; no real network');
