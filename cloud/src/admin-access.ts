export interface AdminAccessEnv {
  SERVICE_ADMIN_ORIGIN?:string;
  SERVICE_ADMIN_ACCESS_ISSUER?:string;
  SERVICE_ADMIN_ACCESS_AUD?:string;
  SERVICE_ADMIN_EMAIL?:string;
}
const jwks=new Map<string,{expires:number; keys:Promise<Map<string,CryptoKey>>}>();
const issuerPattern=/^https:\/\/[a-z0-9][a-z0-9-]*\.cloudflareaccess\.com$/i;
const base64url=/^[A-Za-z0-9_-]+$/;
const encoder=new TextEncoder();

function decode(value:string):Uint8Array<ArrayBuffer> {
  if(!base64url.test(value))throw new Error('Invalid JWT encoding');
  return Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')),char=>char.charCodeAt(0));
}

function publicKeys(issuer:string,upstream:typeof fetch):Promise<Map<string,CryptoKey>> {
  const now=Date.now(),previous=jwks.get(issuer);
  if(previous&&previous.expires>now)return previous.keys;
  const keys=(async()=>{
    const response=await upstream(issuer+'/cdn-cgi/access/certs',
      {redirect:'manual',signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error('Access signing keys unavailable');
    const document=await response.json<{keys?: (JsonWebKey & {kid?:string})[]}>();
    if(!Array.isArray(document.keys)||document.keys.length>20)throw new Error('Invalid signing keys');
    const accepted=new Map<string,CryptoKey>();
    for(const JWK of document.keys){
      if(JWK.kty==='RSA'&&typeof JWK.kid==='string')
        accepted.set(JWK.kid,await crypto.subtle.importKey('jwk',JWK,
          {name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']));
    }
    return accepted;
  })();
  jwks.set(issuer,{expires:now+60_000,keys});
  return keys;
}

export async function adminIdentity(request:Request, env:AdminAccessEnv, upstream:typeof fetch):Promise<string|null> {
  const {SERVICE_ADMIN_ORIGIN:origin,SERVICE_ADMIN_ACCESS_ISSUER:issuer,
    SERVICE_ADMIN_ACCESS_AUD:audience,SERVICE_ADMIN_EMAIL:owner}=env;
  if(!origin||new URL(request.url).origin!==origin||!issuer||!issuerPattern.test(issuer)||!audience||!owner)return null;
  const token=request.headers.get('Cf-Access-Jwt-Assertion')??'';
  if(token.length>12_000)return null;
  const [rawHeader,rawPayload,signature,...excess]=token.split('.');
  if(!rawHeader||!rawPayload||!signature||excess.length)return null;
  try{
    const decoder=new TextDecoder('utf-8',{fatal:true,ignoreBOM:false});
    const header=JSON.parse(decoder.decode(decode(rawHeader)));
    const claims=JSON.parse(decoder.decode(decode(rawPayload)));
    const now=Date.now()/1000;
    if(header?.alg!=='RS256'||typeof header.kid!=='string'||claims?.iss!==issuer||
        !Array.isArray(claims.aud)||!claims.aud.includes(audience)||!Number.isFinite(claims.exp)||claims.exp<=now||
        (claims.nbf!==undefined&&(!Number.isFinite(claims.nbf)||claims.nbf>now))||
        typeof claims.sub!=='string'||!claims.sub||typeof claims.email!=='string'||
        claims.email.toLowerCase()!==owner.trim().toLowerCase())return null;
    const key=(await publicKeys(issuer,upstream)).get(header.kid);
    if(!key)return null;
    return await crypto.subtle.verify({name:'RSASSA-PKCS1-v1_5'},key,decode(signature),
      encoder.encode(rawHeader+'.'+rawPayload))?claims.sub:null;
  }catch{return null;}
}
