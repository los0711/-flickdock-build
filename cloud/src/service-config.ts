export const DEFAULT_CHAT_ENDPOINT = 'https://api.deepseek.com/chat/completions';
export const DEFAULT_SPEECH_ROOT = 'https://api.breeze.blue';
const DEFAULT_TRANSLATION_ORIGINS = 'https://api.deepseek.com,https://api.openai.com,https://openrouter.ai';
export interface TranslationProvider {endpoint:string; model:string; key:string}
export interface SpeechProvider {root:string; model:string; key:string; voice:string; language:string}
export interface ServiceConfig {primary:TranslationProvider; backup:TranslationProvider|null; speech:SpeechProvider}
export interface ServiceEnv {
  DB:D1Database;
  DEEPSEEK_API_KEY?:string;
  TRANSLATION_PRIMARY_ENDPOINT?:string;
  TRANSLATION_PRIMARY_API_KEY?:string;
  TRANSLATION_PRIMARY_MODEL?:string;
  BREEZE_API_KEY?:string; BREEZE_TTS_MODEL?:string; BREEZE_VOICE_ID?:string;
  SERVICE_CONFIG_SECRET?:string;
  SERVICE_TRANSLATION_ORIGINS?:string; SERVICE_SPEECH_ORIGINS?:string;
}
export interface ConfigState {
  revision:number; active:string|null; previous:string|null; staged:string|null; test_id:string|null;
  tested_at:number|null; active_at:number|null; health:string|null; health_started_at:number|null;
}
export const TEST_LIFETIME = 10 * 60_000;
const encoder = new TextEncoder();
const context = encoder.encode('flickdock-service-config:v1');
const keyPattern = /^[!-~]{8,512}$/;
const modelPattern = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;

export function legacyServices(env:ServiceEnv):ServiceConfig {
  const deepseek={endpoint:DEFAULT_CHAT_ENDPOINT,model:'deepseek-flash',key:env.DEEPSEEK_API_KEY??''};
  const primary=environmentPrimary(env);
  return {
    primary:primary??deepseek,
    backup:primary&&usableProviderKey(deepseek.key)?deepseek:null,
    speech:{root:DEFAULT_SPEECH_ROOT,model:env.BREEZE_TTS_MODEL||'breeze-tts-2-multilingual',
      key:env.BREEZE_API_KEY??'',voice:env.BREEZE_VOICE_ID??'',language:''},
  };
}

export function usableProviderKey(key:string):boolean { return keyPattern.test(key); }

export function isDirectTranslation(endpoint:string):boolean {
  return new URL(endpoint).pathname === '/v1/translate';
}

function environmentPrimary(env:ServiceEnv):TranslationProvider|null {
  const key=env.TRANSLATION_PRIMARY_API_KEY??'';
  if(!env.TRANSLATION_PRIMARY_ENDPOINT||!usableProviderKey(key))return null;
  try{
    const endpoint=providerUrl(env.TRANSLATION_PRIMARY_ENDPOINT,
      env.SERVICE_TRANSLATION_ORIGINS??DEFAULT_TRANSLATION_ORIGINS,'主翻译').href;
    if(!isDirectTranslation(endpoint)&&!new URL(endpoint).pathname.endsWith('/chat/completions'))return null;
    const model=env.TRANSLATION_PRIMARY_MODEL??(isDirectTranslation(endpoint)?'server-default':'');
    return {endpoint,model:field(model,modelPattern,'主翻译模型'),key};
  }catch{return null;}
}

export async function configState(env:ServiceEnv):Promise<ConfigState> {
  const state=await env.DB.prepare('SELECT * FROM service_configuration WHERE singleton=1').first<ConfigState>();
  if(!state)throw new Error('Service configuration migration is missing');
  return state;
}

function secretKey(env:ServiceEnv):Promise<CryptoKey> {
  const secret=env.SERVICE_CONFIG_SECRET??'';
  if(!/^[a-f0-9]{64}$/i.test(secret))throw new Error('Invalid service configuration encryption key');
  const raw=Uint8Array.from(secret.match(/../g)!, pair=>parseInt(pair,16));
  return crypto.subtle.importKey('raw',raw,{name:'AES-GCM'},false,['encrypt','decrypt']);
}

export async function sealConfig(env:ServiceEnv,value:ServiceConfig):Promise<string> {
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const encrypted=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:context},await secretKey(env),
    encoder.encode(JSON.stringify(value)));
  return btoa(String.fromCharCode(...iv))+'.'+btoa(String.fromCharCode(...new Uint8Array(encrypted)));
}

async function openConfig(env:ServiceEnv,encrypted:string):Promise<ServiceConfig> {
  const [nonce,ciphertext]=encrypted.split('.');
  const binary=(value:string)=>Uint8Array.from(atob(value),char=>char.charCodeAt(0));
  const plaintext=await crypto.subtle.decrypt({name:'AES-GCM',iv:binary(nonce),additionalData:context},
    await secretKey(env),binary(ciphertext));
  return JSON.parse(new TextDecoder().decode(plaintext)) as ServiceConfig;
}

export async function activeServices(env:ServiceEnv,state?:ConfigState):Promise<ServiceConfig> {
  if(!env.SERVICE_CONFIG_SECRET)return legacyServices(env);
  const row=state??await configState(env);
  return row.active===null?legacyServices(env):openConfig(env,row.active);
}

export class ConfigInputError extends Error {}

export function object(value:unknown):Record<string,unknown> {
  if(!value||typeof value!=='object'||Array.isArray(value))throw new ConfigInputError('配置格式无效。');
  return value as Record<string,unknown>;
}

function field(value:unknown,pattern:RegExp,label:string):string {
  if(typeof value!=='string'||!pattern.test(value.trim()))throw new ConfigInputError(label+'格式无效。');
  return value.trim();
}

function providerUrl(value:unknown,origins:string,label:string):URL {
  if(typeof value!=='string'||value.length>512)throw new ConfigInputError(label+'地址无效。');
  let url:URL;
  try{url=new URL(value.trim());}catch{throw new ConfigInputError(label+'请输入完整的 HTTPS 地址。');}
  const allowed=new Set(origins.split(',').map(origin=>origin.trim()).filter(Boolean));
  if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash||!allowed.has(url.origin))
    throw new ConfigInputError(label+'地址必须使用已授权的 HTTPS 服务域名，且不含查询参数。');
  return url;
}

function retainKey(value:unknown,previous:string|undefined,previousOrigin:string|undefined,origin:string,label:string):string {
  if(value===''){
    if(!previous||previousOrigin!==origin)throw new ConfigInputError(label+'请填写密钥。切换域名时需要提供该域名的密钥。');
    return field(previous,keyPattern,label+'密钥');
  }
  return field(value,keyPattern,label+'密钥');
}

export function validateServices(input:unknown,current:ServiceConfig,env:ServiceEnv):ServiceConfig {
  const config=object(input);
  const translationOrigins=env.SERVICE_TRANSLATION_ORIGINS??DEFAULT_TRANSLATION_ORIGINS;
  function translate(value:unknown,prior:TranslationProvider|null,label:string):TranslationProvider {
    const item=object(value);
    const endpoint=providerUrl(item.endpoint,translationOrigins,label);
    const direct=isDirectTranslation(endpoint.href);
    if(!direct&&!endpoint.pathname.endsWith('/chat/completions'))
      throw new ConfigInputError(label+'请填写 /v1/translate 或以 /chat/completions 结尾的完整接口地址。');
    const model=direct&&item.model===''?'server-default':field(item.model,modelPattern,label+'模型');
    return {endpoint:endpoint.href,model,
      key:retainKey(item.key,prior?.key,prior?new URL(prior.endpoint).origin:undefined,endpoint.origin,label)};
  }
  const primary=translate(config.primary,current.primary,'主翻译');
  const backup=config.backup===null?null:translate(config.backup,current.backup,'备翻译');
  if(backup&&backup.endpoint===primary.endpoint&&backup.model===primary.model&&backup.key===primary.key)
    throw new ConfigInputError('两个翻译入口的地址、模型和密钥不能完全相同。');
  const item=object(config.speech);
  const root=providerUrl(item.root,env.SERVICE_SPEECH_ORIGINS??DEFAULT_SPEECH_ROOT,'语音');
  const voice=field(item.voice,/^[A-Za-z0-9_-]{0,120}$/,'默认音色');
  const language=field(item.language,/^(?:[a-z]{2})?$/,'默认音色语言');
  if(voice&&!language)throw new ConfigInputError('请为默认音色填写语言代码，例如 zh 或 en。');
  return {primary,backup,speech:{root:root.href.replace(/\/+$/,''),model:field(item.model,modelPattern,'语音模型'),
    key:retainKey(item.key,current.speech.key,new URL(current.speech.root).origin,root.origin,'语音'),voice,language}};
}

export function maskedServices(config:ServiceConfig) {
  const chat=(provider:TranslationProvider)=>({endpoint:provider.endpoint,model:provider.model,configured:!!provider.key});
  return {primary:chat(config.primary),backup:config.backup&&chat(config.backup),
    speech:{root:config.speech.root,model:config.speech.model,voice:config.speech.voice,
      language:config.speech.language,configured:!!config.speech.key}};
}
