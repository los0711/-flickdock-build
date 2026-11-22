import {isDirectTranslation,usableProviderKey,type ServiceConfig,type TranslationProvider} from './service-config.ts';

export type ChatResult = {content:string; usage:unknown};
type ChatInput = {max_tokens:number; messages:{role:string;content:string}[]; response_format?:{type:string}};
type NativeInput = {source:string;target:string;text:string|{id:string;text:string}[]};
export class ChatFailure extends Error {
  readonly kind:string;
  readonly status:number;
  readonly retryable:boolean;
  constructor(kind:string,status=0,retryable=true){super(kind);this.kind=kind;this.status=status;this.retryable=retryable;}
}

export async function readBounded(stream:ReadableStream<Uint8Array>|null,limit:number):Promise<Uint8Array<ArrayBuffer>> {
  if(!stream)return new Uint8Array();
  const reader=stream.getReader(),parts:Uint8Array[]=[];
  let total=0;
  try {
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      total+=value.byteLength;
      if(total>limit){await reader.cancel().catch(()=>{});throw new Error('Body too large');}
      parts.push(value);
    }
  } finally{reader.releaseLock();}
  const result=new Uint8Array(total);
  let offset=0;
  for(const part of parts){result.set(part,offset);offset+=part.length;}
  return result;
}

export async function chatCompletion(provider:TranslationProvider,input:ChatInput,upstream:typeof fetch,signal:AbortSignal):Promise<ChatResult> {
  if(!usableProviderKey(provider.key))throw new ChatFailure('not_configured');
  let response:Response;
  try{
    response=await upstream(provider.endpoint,{method:'POST',redirect:'manual',signal,
      headers:{Authorization:'Bearer '+provider.key,'Content-Type':'application/json'},
      body:JSON.stringify({...input,model:provider.model,stream:false,
        ...(new URL(provider.endpoint).hostname==='api.deepseek.com'?{thinking:{type:'disabled'}}:{})})});
  }catch{throw new ChatFailure(signal.aborted?'timeout':'network');}
  if(!response.ok){
    let code='';
    try {
      const error=JSON.parse(new TextDecoder().decode(await readBounded(response.body,8192)));
      code=error?.error?.code??'';
    }catch{}
    const unavailable=[401,402,403,404,408,409,425,429].includes(response.status)||response.status>=500
      ||['model_not_found','invalid_model','insufficient_quota'].includes(code);
    throw new ChatFailure('http_error',response.status,unavailable);
  }
  let document:any;
  try {document=JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(await readBounded(response.body,512*1024)));}
  catch{throw new ChatFailure(signal.aborted?'timeout':'invalid_response',response.status);}
  const choice=document?.choices?.[0],content=choice?.message?.content;
  if(choice?.finish_reason==='content_filter'||choice?.message?.refusal)throw new ChatFailure('content_filtered',response.status,false);
  if(choice?.finish_reason!=='stop')throw new ChatFailure('incomplete',response.status);
  if(typeof content!=='string'||!content.trim()||content.includes('\0'))throw new ChatFailure('invalid_response',response.status);
  return {content,usage:document.usage};
}

export async function translateText(provider:TranslationProvider,input:NativeInput,upstream:typeof fetch,signal:AbortSignal):Promise<ChatResult> {
  if(!usableProviderKey(provider.key))throw new ChatFailure('not_configured');
  const paragraphs=/\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*/;
  const units=typeof input.text==='string'?null:input.text;
  if(units?.some(unit=>paragraphs.test(unit.text)))throw new ChatFailure('unsupported_paragraphs');
  const text=typeof input.text==='string'?input.text:input.text.map(unit=>unit.text).join('\n\n');
  const language=(code:string)=>code==='zh'?'zh-CN':code==='zh-Hant'?'zh-TW':code;
  let response:Response;
  try{
    response=await upstream(provider.endpoint,{method:'POST',redirect:'manual',signal,
      headers:{Authorization:'Bearer '+provider.key,'Content-Type':'application/json','ngrok-skip-browser-warning':'true'},
      body:JSON.stringify({text,target_language:language(input.target),
        ...(input.source==='auto'?{}:{source_language:language(input.source)})})});
  }catch{throw new ChatFailure(signal.aborted?'timeout':'network');}
  let body:any;
  try{
    body=JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(await readBounded(response.body,response.ok?512*1024:8192)));
  }catch{
    if(response.ok)throw new ChatFailure(signal.aborted?'timeout':'invalid_response',response.status);
  }
  if(body?.error==='content_filtered'||body?.error?.code==='content_filtered')
    throw new ChatFailure('content_filtered',response.status,false);
  if(!response.ok)throw new ChatFailure('http_error',response.status);
  const translation=body?.translation;
  if(typeof translation!=='string'||!translation.trim()||translation.includes('\0'))
    throw new ChatFailure('invalid_translation',response.status);
  let content=translation.trim();
  if(units){
    const outputs=content.split(paragraphs).map(chunk=>chunk.trim());
    if(outputs.length!==units.length||outputs.some(output=>!output))throw new ChatFailure('missing_units',response.status);
    content=JSON.stringify({units:units.map((unit,index)=>({id:unit.id,text:outputs[index]}))});
  }
  return {content,usage:{
    prompt_tokens:body?.metrics?.input_tokens,completion_tokens:body?.metrics?.output_tokens,
  }};
}

export async function withTranslationFailover<T>(
  providers:Pick<ServiceConfig,'primary'|'backup'>,input:ChatInput & {direct?:NativeInput},
  upstream:typeof fetch,signal:AbortSignal,decode:(result:ChatResult)=>T,
):Promise<T> {
  const {direct,...chat}=input;
  const call=async(provider:TranslationProvider,timeout:number)=>{
    if(signal.aborted)throw new ChatFailure('cancelled',0,false);
    if(isDirectTranslation(provider.endpoint)){
      if(!direct)throw new ChatFailure('unsupported_operation');
      const length=typeof direct.text==='string'?direct.text.length:direct.text.reduce((total,unit)=>total+unit.text.length,0);
      const deadline=AbortSignal.any([signal,AbortSignal.timeout(Math.min(timeout,8_000+length*2))]);
      return decode(await translateText(provider,direct,upstream,deadline));
    }
    return decode(await chatCompletion(provider,chat,upstream,AbortSignal.any([signal,AbortSignal.timeout(timeout)])));
  };
  try{return await call(providers.primary,providers.backup?50_000:90_000);}
  catch(error){
    if(!providers.backup||signal.aborted||!(error instanceof ChatFailure)||!error.retryable)throw error;
  }
  return call(providers.backup,55_000);
}
