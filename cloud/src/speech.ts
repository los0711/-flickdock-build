/** BreezeBlue proxy. The desktop never receives provider keys/private voices. */
export interface SpeechEnv {DB:D1Database;BREEZE_API_KEY?:string;BREEZE_TTS_MODEL?:string;BREEZE_VOICE_ID?:string;}
type Voice={voice_id:string;name:string;language_code:string;preview_url?:string};
const ROOT='https://api.breeze.blue',MAX_REPLY=1792*1024;
const LANGUAGES=new Set('af ar bg bn bs ca cs da de el en es et eu fa fi fr gl he hi hu id is it ja kn ko lt mr ms ne nl no pl pt ro ru sk sl sq sv sw ta te th tl tr uk ur vi zh'.split(' '));
const caches=new WeakMap<object,Map<string,{at:number;value:Promise<Voice[]>}>>();
class SpeechError extends Error {
  status:number;code:string;
  constructor(status:number,code:string,message:string){super(message);this.status=status;this.code=code;}
}
function fail(status:number,code:string,message:string):never{throw new SpeechError(status,code,message);}
const json=(value:Record<string,unknown>,status=200)=>Response.json({schema_version:1,...value},{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
async function provider(upstream:typeof fetch,url:string,options:RequestInit):Promise<Response>{
  try{return await upstream(url,options);}
  catch{fail(502,'SPEECH_PROVIDER','Speech provider is temporarily unavailable');}
}
async function bytes(stream:ReadableStream<Uint8Array>|null,maximum:number){
  if(!stream)return new Uint8Array();const reader=stream.getReader();const parts:Uint8Array[]=[];let total=0;
  try{while(true){const {done,value}=await reader.read();if(done)break;total+=value.byteLength;
    if(total>maximum){await reader.cancel();fail(502,'SPEECH_RESPONSE_LIMIT','Speech response exceeds limit');}parts.push(value);}}
  finally{reader.releaseLock();}
  const result=new Uint8Array(total);let offset=0;for(const part of parts){result.set(part,offset);offset+=part.byteLength;}return result;
}
async function document(response:Response,maximum:number){
  if(!response.ok){
    if(response.body)await response.body.cancel();
    fail(502,response.status===429?'SPEECH_PROVIDER_BUSY':'SPEECH_PROVIDER','Speech provider is temporarily unavailable');
  }
  try{return JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(await bytes(response.body,maximum)));}
  catch(error){if(error instanceof SpeechError)throw error;fail(502,'SPEECH_INVALID_RESPONSE','Invalid speech response');}
}
function language(value:unknown){
  if(typeof value!=='string')fail(400,'INVALID_REQUEST','Invalid speech language');
  const code=value==='zh-Hant'?'zh':value==='nb'?'no':value==='fil'?'tl':value;
  if(!LANGUAGES.has(code))fail(400,'SPEECH_LANGUAGE','This speech language is not available');return code;
}
function key(env:SpeechEnv){
  if(!env.BREEZE_API_KEY||!/^[\x21-\x7e]{8,512}$/.test(env.BREEZE_API_KEY))fail(503,'SPEECH_NOT_CONFIGURED','BreezeBlue speech service is not configured');
  return env.BREEZE_API_KEY!;
}
async function voices(env:SpeechEnv,code:string,upstream:typeof fetch):Promise<Voice[]>{
  let cache=caches.get(env);if(!cache){cache=new Map();caches.set(env,cache);}
  const existing=cache.get(code);if(existing&&Date.now()-existing.at<300000)return existing.value;
  const promise=(async()=>{
    const response=await provider(upstream,ROOT+'/v1/voices?voice_type=default&page_size=50&language_code='+encodeURIComponent(code),
      {headers:{'xi-api-key':key(env),Accept:'application/json'},redirect:'manual',signal:AbortSignal.timeout(15000)});
    const value=await document(response,256*1024);
    if(!value||!Array.isArray(value.voices))fail(502,'SPEECH_INVALID_RESPONSE','Invalid voice catalog');
    const rows:Voice[]=[];
    for(const row of value.voices.slice(0,50)){
      if(!row||typeof row.voice_id!=='string'||!/^[A-Za-z0-9_-]{1,120}$/.test(row.voice_id)||typeof row.name!=='string'||!row.name.trim()||row.name.length>120)continue;
      rows.push({voice_id:row.voice_id,name:row.name,language_code:LANGUAGES.has(row.language_code)?row.language_code:code,
        ...(typeof row.preview_url==='string'&&row.preview_url.startsWith('https://')?{preview_url:row.preview_url}:{})});
    }
    return rows;
  })();
  if(cache.size>=64)cache.clear();cache.set(code,{at:Date.now(),value:promise});
  try{return await promise;}catch(error){cache.delete(code);throw error;}
}
function pitchInstruction(code:string,pitch:number){
  if(Math.abs(pitch-1)<.025)return '';
  if(code==='zh')return pitch<1?'用略低沉、自然的音调朗读，保持所选音色和清晰发音。':'用略明亮、自然的音调朗读，保持所选音色和清晰发音。';
  if(code==='en')return pitch<1?'Use a slightly lower natural pitch, preserving the selected voice and clear articulation.':'Use a slightly higher natural pitch, preserving the selected voice and clear articulation.';
  return '';
}
export async function speech(request:Request,env:SpeechEnv,accountId:string,upstream:typeof fetch=globalThis.fetch.bind(globalThis)):Promise<Response>{
  try{
    key(env);const url=new URL(request.url);
    if(request.method==='GET'&&url.pathname==='/v1/speech/voices'){
      const code=language(url.searchParams.get('language'));
      const catalog=await voices(env,code,upstream);
      return json({provider:'BreezeBlue',voices:catalog.map(({preview_url,...row})=>({...row,has_preview:!!preview_url})),default_available:!!env.BREEZE_VOICE_ID||catalog.length>0,pitch_supported:['en','zh'].includes(code)});
    }
    if(request.method==='GET'&&url.pathname==='/v1/speech/preview'){
      const code=language(url.searchParams.get('language')),id=url.searchParams.get('voice_id');
      const row=(await voices(env,code,upstream)).find(row=>row.voice_id===id);
      if(!row?.preview_url)fail(404,'SPEECH_VOICE','This voice has no preview');
      const response=await provider(upstream,row.preview_url,{redirect:'manual',signal:AbortSignal.timeout(20000)});
      if(!response.ok){await response.body?.cancel();fail(502,'SPEECH_PROVIDER','Voice preview unavailable');}
      const audio=await bytes(response.body,1200*1024);let binary='';
      for(let index=0;index<audio.length;index+=8192)binary+=String.fromCharCode(...audio.subarray(index,index+8192));
      const wav=audio[0]===82&&audio[1]===73&&audio[2]===70&&audio[3]===70;
      return json({provider:'BreezeBlue',audio_base64:btoa(binary),content_type:wav?'audio/wav':'audio/mpeg',word_timestamps:[],voice_id:id});
    }
    if(request.method!=='POST'||url.pathname!=='/v1/speech/synthesize')fail(404,'NOT_FOUND','Speech route not found');
    if(!request.headers.get('Content-Type')?.startsWith('application/json'))fail(415,'JSON_REQUIRED','Expected JSON');
    let value:any;
    try{value=JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(await bytes(request.body,16384)));}
    catch(error){if(error instanceof SpeechError)throw error;fail(400,'INVALID_REQUEST','Invalid speech request');}
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).some(name=>!['text','language','voice_id','speed','pitch','request_id'].includes(name)))
      fail(400,'INVALID_REQUEST','Unknown speech fields');
    const code=language(value.language),text=value.text,id=value.request_id,speed=value.speed??1,pitch=value.pitch??1;
    if(typeof text!=='string'||!text.trim()||[...text].length>300||text.includes('\0')||typeof id!=='string'||
       !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(id)||
       typeof speed!=='number'||!Number.isFinite(speed)||speed<.5||speed>2||typeof pitch!=='number'||!Number.isFinite(pitch)||pitch<.8||pitch>1.2)
      fail(400,'INVALID_REQUEST','Invalid speech input');
    const catalog=await voices(env,code,upstream);
    const requested=value.voice_id;
    if(requested!==undefined&&(typeof requested!=='string'||requested.length>120))fail(400,'INVALID_REQUEST','Invalid voice');
    const configured=env.BREEZE_VOICE_ID;
    const voice=requested||configured||catalog[0]?.voice_id;
    if(typeof voice!=='string'||!/^[A-Za-z0-9_-]{1,120}$/.test(voice)||
       voice!==configured&&!catalog.some(row=>row.voice_id===voice))fail(400,'SPEECH_VOICE','Choose an available speech voice');
    const reserved=await env.DB.prepare('INSERT INTO rates(key,n,expires_at) VALUES(?,1,?) ON CONFLICT(key) DO NOTHING RETURNING n')
      .bind('speech-request:'+accountId+':'+id,Date.now()+86400000).first();
    if(!reserved)fail(409,'SPEECH_REPEATED','Speech request already submitted');
    const instruction=pitchInstruction(code,pitch);
    const response=await provider(upstream,ROOT+'/v1/text-to-speech/'+encodeURIComponent(voice)+'/with-timestamps?output_format=mp3_24000_64&delivery=sync&enable_logging=false',
      {method:'POST',redirect:'manual',headers:{'xi-api-key':key(env),'Content-Type':'application/json'},signal:AbortSignal.any([request.signal,AbortSignal.timeout(90000)]),
       body:JSON.stringify({text,model_id:env.BREEZE_TTS_MODEL||'breeze-tts-2-multilingual',language_code:code,
         voice_settings:{speed,...(instruction?{guidance_scale:4}:{})},...(instruction?{instructions:instruction}:{})})});
    const result=await document(response,MAX_REPLY);
    if(!result||typeof result.audio_base64!=='string'||!result.audio_base64.length||result.audio_base64.length>1500*1024||
       !/^[A-Za-z0-9+/]+={0,2}$/.test(result.audio_base64)||result.content_type!=='audio/mpeg'||!Array.isArray(result.word_timestamps)||result.word_timestamps.length>2000)
      fail(502,'SPEECH_INVALID_RESPONSE','Invalid generated audio');
    const timing=[];
    for(const row of result.word_timestamps){
      if(!row||typeof row.word!=='string'||row.word.length>300||typeof row.start!=='number'||typeof row.end!=='number'||
         !Number.isFinite(row.start)||!Number.isFinite(row.end)||row.start<0||row.end<row.start||row.end>600)
        fail(502,'SPEECH_INVALID_RESPONSE','Invalid speech timing');
      timing.push({word:row.word,start:row.start,end:row.end});
    }
    timing.sort((a,b)=>a.start-b.start);
    return json({provider:'BreezeBlue',audio_base64:result.audio_base64,content_type:'audio/mpeg',word_timestamps:timing,voice_id:voice});
  }catch(error){
    if(error instanceof SpeechError)return json({error:{code:error.code,message:error.message}},error.status);
    throw error;
  }
}
