import {adminIdentity,type AdminAccessEnv} from './admin-access.ts';
import {adminHtml,adminCss,adminScript} from './admin-ui.ts';
import {readBounded} from './chat-provider.ts';
import {
  activeServices,ConfigInputError,configState,maskedServices,object,sealConfig,TEST_LIFETIME,validateServices,
  type ConfigState,type ServiceEnv,
} from './service-config.ts';
import {probeServices} from './service-health.ts';

export type ServiceAdminEnv=ServiceEnv & AdminAccessEnv;

const headers={
  'Cache-Control':'no-store',
  'X-Content-Type-Options':'nosniff',
  'Referrer-Policy':'no-referrer',
  'X-Frame-Options':'DENY',
  'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
};
function json(value:unknown,status=200){return Response.json(value,{status,headers});}
function error(message:string,status:number){return json({error:message},status);}
const conflict=()=>error('当前配置已变更或测试已过期，请刷新后重新测试。',409);

async function stateResponse(env:ServiceEnv,row:ConfigState){
  return json({revision:row.revision,updatedAt:row.active_at,canRollback:!!row.previous,
    health:row.health?JSON.parse(row.health):null,config:maskedServices(await activeServices(env,row))});
}

async function readInput(request:Request) {
  if(request.headers.get('Content-Type')?.split(';')[0].trim()!=='application/json')
    throw new ConfigInputError('请求必须使用 JSON。');
  try{
    const body=await readBounded(request.body,12*1024);
    return object(JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:false}).decode(body)));
  }catch{throw new ConfigInputError('请求内容无效。');}
}
const name=/^[a-f0-9-]{36}$/;
export async function serviceAdmin(request:Request,env:ServiceAdminEnv,
    upstream:typeof fetch=globalThis.fetch.bind(globalThis)):Promise<Response>{
  if(!/^[a-f0-9]{64}$/i.test(env.SERVICE_CONFIG_SECRET??'')||!await adminIdentity(request,env,upstream))
    return new Response('Not found',{status:404,headers});
  const {pathname}=new URL(request.url);
  const files:Record<string,[string,string]>={
    '/admin':[adminHtml,'text/html; charset=utf-8'],'/admin/':[adminHtml,'text/html; charset=utf-8'],
    '/admin/styles.css':[adminCss,'text/css; charset=utf-8'],'/admin/app.js':[adminScript,'text/javascript; charset=utf-8'],
  };
  if(request.method==='GET'&&Object.hasOwn(files,pathname)){
    const [content,type]=files[pathname];
    return new Response(content,{headers:{...headers,'Content-Type':type}});
  }
  try{
    if(request.method==='GET'&&pathname==='/admin/api/config')return stateResponse(env,await configState(env));
    if(request.method!=='POST'||!['/admin/api/test','/admin/api/apply','/admin/api/rollback'].includes(pathname))
      return error('页面不存在。',404);
    if(request.headers.get('Origin')!==env.SERVICE_ADMIN_ORIGIN)return error('请求来源不匹配。',403);
    const data=await readInput(request),row=await configState(env);
    if(data.revision!==row.revision)return conflict();
    if(pathname==='/admin/api/test'){
      const proposal=validateServices(data.config,await activeServices(env,row),env);
      const results=await probeServices(env,proposal,upstream);
      if(results.primary.state!=='ok'||results.backup.state==='failed'||results.speech.state!=='ok')
        return json({results,testId:null,expiresAt:null});
      const testId=crypto.randomUUID(),staged=await sealConfig(env,proposal);
      const stored=await env.DB.prepare(`UPDATE service_configuration SET staged=?,test_id=?,tested_at=?,staged_health=?
        WHERE singleton=1 AND revision=? RETURNING singleton`)
        .bind(staged,testId,results.at,JSON.stringify(results),row.revision).first();
      return stored?json({results,testId,expiresAt:results.at+TEST_LIFETIME}):conflict();
    }
    if(pathname==='/admin/api/apply'){
      const now=Date.now();
      if(typeof data.testId!=='string'||!name.test(data.testId)||data.testId!==row.test_id||!row.staged||
          row.tested_at===null||row.tested_at<now-TEST_LIFETIME)return conflict();
      const prior=row.active??await sealConfig(env,await activeServices(env,row));
      const applied=await env.DB.prepare(`UPDATE service_configuration
        SET previous=?,active=staged,revision=revision+1,active_at=?,health=staged_health,health_started_at=NULL,
        staged=NULL,test_id=NULL,tested_at=NULL,staged_health=NULL
        WHERE singleton=1 AND revision=? AND test_id=? AND tested_at>=? RETURNING *`)
        .bind(prior,now,row.revision,row.test_id,now-TEST_LIFETIME).first<ConfigState>();
      return applied?stateResponse(env,applied):conflict();
    }
    if(!row.previous)return error('上一版配置尚未建立。',409);
    const restored=await env.DB.prepare(`UPDATE service_configuration
      SET active=previous,previous=active,revision=revision+1,active_at=?,health=NULL,health_started_at=NULL,
      staged=NULL,test_id=NULL,tested_at=NULL,staged_health=NULL
      WHERE singleton=1 AND revision=? AND previous IS NOT NULL RETURNING *`)
      .bind(Date.now(),row.revision).first<ConfigState>();
    return restored?stateResponse(env,restored):conflict();
  }catch(cause){
    if(cause instanceof ConfigInputError)return error(cause.message,400);
    return error('无法完成操作，请稍后重试。',503);
  }
}
