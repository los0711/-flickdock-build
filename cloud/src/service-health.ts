import {chatCompletion,ChatFailure,translateText} from './chat-provider.ts';
import {activeServices,configState,isDirectTranslation,type TranslationProvider,type ServiceConfig,type ServiceEnv} from './service-config.ts';
import {testSpeechProvider} from './speech.ts';

type ProbeState={state:'ok'|'failed'|'disabled'; ms:number; reason?:string};

async function probe(work:(()=>Promise<void>)|null):Promise<ProbeState>{
  if(!work)return {state:'disabled',ms:0};
  const start=Date.now();
  try{await work();return {state:'ok',ms:Date.now()-start};}
  catch(error){return {state:'failed',ms:Date.now()-start,reason:error instanceof ChatFailure?error.kind:'speech_unavailable'};}
}

async function testTranslationProvider(provider:TranslationProvider,upstream:typeof fetch):Promise<void>{
  const signal=AbortSignal.timeout(12_000);
  if(isDirectTranslation(provider.endpoint)){
    const result=await translateText(provider,{text:'Hello.',source:'en',target:'zh'},upstream,signal);
    if(!/\p{Script=Han}/u.test(result.content))throw new ChatFailure('invalid_translation',200);
    return;
  }
  const result=await chatCompletion(provider,{max_tokens:128,response_format:{type:'json_object'},
    messages:[{role:'system',content:'Translate into simplified Chinese and return a JSON object with one field named translation.'},
      {role:'user',content:'Hello.'}]},upstream,signal);
  let translated:unknown;
  try{translated=JSON.parse(result.content)?.translation;}catch{}
  if(typeof translated!=='string'||!/\p{Script=Han}/u.test(translated))throw new ChatFailure('invalid_translation',200);
}

export async function probeServices(env:ServiceEnv,config:ServiceConfig,upstream:typeof fetch){
  const [primary,backup,speech]=await Promise.all([
    probe(()=>testTranslationProvider(config.primary,upstream)),
    probe(config.backup?()=>testTranslationProvider(config.backup!,upstream):null),
    probe(()=>testSpeechProvider(env,config.speech,upstream)),
  ]);
  return {at:Date.now(),primary,backup,speech};
}

const HOUR=60*60_000;
export async function checkActiveServices(env:ServiceEnv,scheduledAt:number,upstream:typeof fetch=globalThis.fetch.bind(globalThis)){
  if((!env.SERVICE_CONFIG_SECRET&&!env.TRANSLATION_PRIMARY_API_KEY)||new Date(scheduledAt).getUTCMinutes()!==0)return;
  const current=await configState(env);
  if(!current.active&&!env.TRANSLATION_PRIMARY_API_KEY)return;
  const claim=await env.DB.prepare(`UPDATE service_configuration SET health_started_at=?
    WHERE singleton=1 AND revision=? AND (health_started_at IS NULL OR health_started_at<=?) RETURNING singleton`)
    .bind(scheduledAt,current.revision,scheduledAt-HOUR).first();
  if(!claim)return;
  const health=await probeServices(env,await activeServices(env,current),upstream);
  await env.DB.prepare('UPDATE service_configuration SET health=? WHERE singleton=1 AND revision=? AND health_started_at=?')
    .bind(JSON.stringify(health),current.revision,scheduledAt).run();
}
