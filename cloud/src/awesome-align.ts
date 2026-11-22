import {activeServices, isDirectTranslation, usableProviderKey, type ServiceEnv} from './service-config.ts';

const MAX_CHARS = 6000;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_REPLY_BYTES = 512 * 1024;
const LANG = /^(?:auto|[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3})$/;

function reply(status: number, code: string, retry?: string | null) {
  const headers = new Headers({'Cache-Control':'no-store'});
  if (retry && /^\d{1,4}$/.test(retry)) headers.set('Retry-After',retry);
  return Response.json({error:{code}}, {status,headers});
}

async function boundedJson(body: ReadableStream<Uint8Array> | null, limit: number) {
  if (!body) throw new TypeError('Missing body');
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8',{fatal:true,ignoreBOM:false});
  let total=0, raw='';
  try {
    for (;;) {
      const {done,value}=await reader.read();
      if(done) break;
      total+=value.byteLength;
      if(total>limit) {
        await reader.cancel();
        throw new RangeError('Alignment body exceeds limit');
      }
      raw+=decoder.decode(value,{stream:true});
    }
    return JSON.parse(raw+decoder.decode());
  } finally {
    reader.releaseLock();
  }
}

export async function awesomeAlign(request: Request,env: ServiceEnv,upstream: typeof fetch=globalThis.fetch.bind(globalThis)) {
  if (!request.headers.get('Content-Type')?.startsWith('application/json')) return reply(415,'JSON_REQUIRED');
  let input;
  try {
    input=await boundedJson(request.body,MAX_BODY_BYTES);
  } catch (error) {
    return error instanceof RangeError ? reply(413,'ALIGNMENT_TOO_LARGE') : reply(400,'INVALID_ALIGNMENT');
  }
  if (!input || typeof input!=='object' || Array.isArray(input)
      || Object.keys(input).some(key=>!['source_text','target_text','source_lang','target_lang'].includes(key))) {
    return reply(400,'INVALID_ALIGNMENT');
  }
  const {source_text,target_text,source_lang='auto',target_lang='auto'}=input;
  for (const text of [source_text,target_text]) {
    if (typeof text!=='string' || !text.trim() || text.includes('\0')) return reply(422,'INVALID_ALIGNMENT');
    if ([...text].length>MAX_CHARS) return reply(413,'ALIGNMENT_TOO_LARGE');
  }
  if (![source_lang,target_lang].every(language=>typeof language==='string'&&LANG.test(language))) {
    return reply(422,'INVALID_ALIGNMENT_LANGUAGE');
  }
  try {
    const {primary,backup}=await activeServices(env);
    const provider=[primary,backup].find(p=>p&&isDirectTranslation(p.endpoint)&&usableProviderKey(p.key));
    if (!provider) return reply(503,'ALIGNMENT_PROVIDER_NOT_CONFIGURED');
    const endpoint=new URL('/v1/align',provider.endpoint);
    const response=await upstream(endpoint.href,{
      method:'POST',redirect:'error',
      headers:{Authorization:'Bearer '+provider.key,'Content-Type':'application/json','ngrok-skip-browser-warning':'1'},
      body:JSON.stringify({source_text,target_text,source_lang,target_lang}),
      signal:AbortSignal.any([request.signal,AbortSignal.timeout(8000)]),
    });
    if (!response.ok) {
      const status=[413,422,429,503].includes(response.status)?response.status:502;
      await response.body?.cancel();
      return reply(status,'ALIGNMENT_UNAVAILABLE',response.headers.get('Retry-After'));
    }
    const result=await boundedJson(response.body,MAX_REPLY_BYTES);
    const {protocol_version,model_version,alignment_version,offset_unit,alignments,reverse_alignments}=result??{};
    if (protocol_version!=='1' || offset_unit!=='unicode_codepoint'
        || typeof model_version!=='string' || !model_version || model_version.length>128
        || typeof alignment_version!=='string' || !alignment_version || alignment_version.length>64
        || !Array.isArray(alignments) || !Array.isArray(reverse_alignments)) return reply(502,'INVALID_ALIGNMENT_RESULT');
    return Response.json({protocol_version,model_version,alignment_version,offset_unit,alignments,reverse_alignments},
      {headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
  } catch {
    return reply(503,'ALIGNMENT_UNAVAILABLE');
  }
}
