const chatCard=(id:string,title:string,details:string)=>`<section class="service" id="${id}-card" aria-labelledby="${id}-title">
  <header class="service-head"><div><h3 id="${id}-title">${title}</h3><p>${details}</p></div><span class="state" id="${id}-state">等待检查</span></header>
  <fieldset id="${id}-fields" class="fields">
    <label class="wide">API 地址<input id="${id}-endpoint" type="url" maxlength="512" required placeholder="https://api.example.com/v1/chat/completions"></label>
    <label>模型<input id="${id}-model" type="text" maxlength="128" required spellcheck="false"></label>
    <label>API 密钥<input id="${id}-key" type="password" maxlength="512" autocomplete="new-password" spellcheck="false"></label>
  </fieldset>
</section>`;

export const adminHtml=`<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>服务配置 · FlickDock</title>
<link rel="stylesheet" href="/admin/styles.css"><script defer src="/admin/app.js"></script></head>
<body><div class="top"><div class="shell masthead"><div class="brand"><span class="brand-mark" aria-hidden="true">F</span>FlickDock<span class="brand-divider"></span><span class="muted">管理后台</span></div><span class="private">管理员</span></div></div>
<main class="shell">
  <header class="page-head"><div><h1>服务配置</h1><p>管理翻译和转语音服务。</p></div><div class="revision"><span id="revision">正在读取配置</span><small id="active-time"></small></div></header>
  <p id="notice" class="notice" role="status" aria-live="polite" hidden></p>
  <button id="reload" class="secondary" type="button" hidden>重新读取配置</button>
  <form id="services" hidden>
    <div class="group-head"><div><h2>翻译</h2><p>优先使用主接口。遇到限流、配置失效或服务故障时自动尝试备接口。</p></div>
      <label class="toggle"><input id="backup-toggle" type="checkbox" role="switch" aria-controls="backup-fields">使用备接口</label></div>
    <div class="translations">
      ${chatCard('primary','主翻译接口','优先使用。专用翻译接口的模型由服务端决定。')}
      ${chatCard('backup','备翻译接口','兼容 Chat Completions 和 JSON 模式')}
    </div>
    <div class="group-head"><div><h2>转语音</h2><p>使用兼容 BreezeBlue 的语音服务。</p></div></div>
    <section class="service" aria-labelledby="speech-title">
      <header class="service-head"><div><h3 id="speech-title">语音接口</h3><p>默认音色按语言匹配，留空时由服务选择。</p></div><span class="state" id="speech-state">等待检查</span></header>
      <fieldset class="fields speech-fields" id="speech-fields">
        <label class="wide">API 地址<input id="speech-root" type="url" maxlength="512" required placeholder="https://api.breeze.blue"></label>
        <label>模型<input id="speech-model" type="text" maxlength="128" required spellcheck="false"></label>
        <label>API 密钥<input id="speech-key" type="password" maxlength="512" autocomplete="new-password" spellcheck="false"></label>
        <label>默认音色 ID<input id="speech-voice" type="text" maxlength="120" spellcheck="false" placeholder="留空时自动选择"></label>
        <label>默认音色语言<input id="speech-language" type="text" minlength="2" maxlength="2" pattern="[a-z]{2}" spellcheck="false" placeholder="例如 zh"></label>
      </fieldset>
    </section>
    <footer class="actions"><div class="action-meta"><strong id="check-title">每小时自动健康检查</strong><small id="check-time">配置生效后运行</small></div>
      <div class="action-buttons"><button id="rollback" class="secondary" type="button" disabled>回退上一版</button><button id="test" class="secondary" type="submit">测试草稿</button><button id="apply" class="primary" type="button" disabled>应用配置</button></div></footer>
  </form>
  <dialog id="rollback-dialog"><h2>回退到上一版配置？</h2><p>后续的翻译和转语音请求将使用上一版配置。</p><div class="action-buttons"><form method="dialog"><button class="secondary" autofocus>取消</button></form><button id="rollback-confirm" class="primary" type="button">确认回退</button></div></dialog>
</main></body></html>`;

export const adminCss=String.raw`
:root{color-scheme:light dark;--bg:#e3e4e8;--surface:#fafafc;--input:#fff;--line:#cfd2da;--ink:#111114;--muted:#505764;--action:#111114;--action-ink:#fff;--ok:#126041;--error:#9c2c24;--focus:#295dc4;--disabled:#e4e5eb}
@media(prefers-color-scheme:dark){:root{--bg:#0e0f11;--surface:#18191d;--input:#202126;--line:#393b45;--ink:#f5f6f8;--muted:#aeb3c0;--action:#f5f6f8;--action-ink:#111114;--ok:#93d9b5;--error:#ffaca5;--focus:#97b7ff;--disabled:#282b32}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Microsoft YaHei","PingFang SC",sans-serif}
.shell{width:min(100% - 48px,1104px);margin-inline:auto}
.top{background:var(--surface);border-bottom:1px solid var(--line)}
.masthead,.brand,.page-head,.group-head,.service-head,.actions,.action-buttons,.toggle{display:flex;align-items:center}
.masthead,.page-head,.group-head,.service-head,.actions{justify-content:space-between;gap:16px}
.masthead{min-height:64px}
.brand{gap:12px;font-weight:700;white-space:nowrap}
.brand-mark{display:grid;place-items:center;width:26px;height:26px;background:var(--action);color:var(--action-ink);border-radius:8px;font-size:16px}
.brand-divider{width:1px;height:16px;background:var(--line)}
.private,.state{border:1px solid var(--line);padding:4px 10px;border-radius:999px;white-space:nowrap;font-size:12px}
.private,.muted,p,small,.state{color:var(--muted)}
main{padding-block:36px 32px}
h1,h2,h3,p{margin:0}
h1{font-size:34px;letter-spacing:-.04em;line-height:1.25}
h2{font-size:19px} h3{font-size:15px}
.page-head{margin-bottom:28px}
.page-head p,.group-head p,.service-head p{margin-top:4px}
.revision{text-align:right;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}
small{display:block;font-size:12px}
.group-head{margin:28px 0 12px}
.translations{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.service{min-width:0;padding:18px;background:var(--surface);border:1px solid var(--line);border-radius:16px}
.fields{margin:16px 0 0;padding:0;border:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;min-width:0}
.speech-fields{grid-template-columns:repeat(3,minmax(0,1fr))}
label{min-width:0;font-weight:600;display:block}
.wide{grid-column:1/-1}
input:not([type=checkbox]){width:100%;min-height:40px;margin-top:4px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--input);color:var(--ink);font:inherit;font-weight:400}
input::placeholder{color:var(--muted);opacity:1}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.toggle{gap:8px;align-self:flex-start;border:1px solid var(--line);background:var(--surface);padding:6px 10px;border-radius:8px;cursor:pointer;white-space:nowrap}
.toggle input{accent-color:var(--focus);width:16px;height:16px}
fieldset:disabled{opacity:.56}
.state[data-state=ok]{color:var(--ok);border-color:var(--ok)}
.state[data-state=failed],.notice[data-state=failed]{color:var(--error);border-color:var(--error)}
.notice{margin:0 0 16px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
.actions{margin-top:20px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 16px}
.action-buttons{gap:8px;flex-wrap:wrap}
button{min-height:40px;padding:6px 14px;border:1px solid var(--line);border-radius:8px;cursor:pointer;font:inherit;font-weight:600}
button.primary{background:var(--action);color:var(--action-ink);border-color:var(--action)}
button.secondary{background:var(--input);color:var(--ink)}
button:enabled:hover{outline:1px solid var(--muted)}
button:disabled{cursor:default;background:var(--disabled);border-color:var(--line);color:var(--muted)}
dialog{width:min(100% - 32px,384px);padding:20px;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:16px}
dialog::backdrop{background:#0007}
dialog p{margin:8px 0 16px} dialog .action-buttons{justify-content:flex-end}
@media(max-width:760px){.shell{width:calc(100% - 32px)}.translations{grid-template-columns:1fr}.speech-fields{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding-top:24px}}
@media(max-width:480px){h1{font-size:28px}.page-head,.group-head,.actions{align-items:flex-start;flex-direction:column}.revision{text-align:left}.speech-fields,.fields{grid-template-columns:1fr}.service{padding:12px}.brand{gap:8px}.action-buttons{gap:6px}}
`;

export const adminScript=String.raw`
'use strict';
const byId=id=>document.getElementById(id);
const form=byId('services'), notice=byId('notice');
const options=['primary','backup','speech'];
const fieldNames={primary:['endpoint','model','key'],backup:['endpoint','model','key'],
  speech:['root','model','key','voice','language']};
const timestamp=value=>new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'short'}).format(value);
let revision=0,canRollback=false,stamp=null,expiryTimer=null,health=null,busy=false;
const descriptions={not_configured:'缺少密钥',timeout:'请求超时',network:'网络异常',http_error:'服务拒绝请求',
  invalid_response:'响应无效',incomplete:'结果不完整',content_filtered:'内容受限',invalid_translation:'翻译格式有误',
  speech_unavailable:'语音或音色不可用'};
function tell(message,failed=false){
  notice.hidden=!message;
  notice.textContent=message;
  notice.dataset.state=failed?'failed':'';
}
function status(results){
  for(const id of options){
    const value=results?.[id],element=byId(id+'-state');
    element.dataset.state=value?.state||'';
    element.textContent=!value?'等待检查':value.state==='ok'?'通过 · '+value.ms+' ms':
      value.state==='disabled'?'已停用':descriptions[value.reason]||'检查失败';
  }
}
function updateButtons(){
  const alternate=byId('backup-toggle').checked;
  byId('backup-fields').disabled=busy||!alternate;
  byId('primary-fields').disabled=busy;
  byId('speech-fields').disabled=busy;
  byId('backup-toggle').disabled=busy;
  byId('test').disabled=busy;
  byId('apply').disabled=busy||!stamp||stamp.expiresAt<=Date.now();
  byId('rollback').disabled=busy||!canRollback;
}
function invalidate(){
  stamp=null;
  clearTimeout(expiryTimer);
  updateButtons();
}
async function api(path,body){
  const response=await fetch('/admin/api/'+path,body===undefined?{}:
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let data;
  try{data=await response.json();}catch{throw new Error('会话已结束，请刷新页面重新登录。');}
  if(!response.ok)throw new Error(typeof data.error==='string'?data.error:'操作失败，请重试。');
  return data;
}
function showState(state){
  revision=state.revision;canRollback=state.canRollback;health=state.health;
  byId('revision').textContent=revision?'生效配置 · 第 '+revision+' 版':'生效配置 · 服务端默认';
  byId('active-time').textContent=state.updatedAt?'更新于 '+timestamp(state.updatedAt):'';
  byId('check-time').textContent=health?'最近一次 · '+timestamp(health.at):'等待首次检查';
  byId('backup-toggle').checked=!!state.config.backup;
  for(const id of options){
    const item=state.config[id];
    for(const key of fieldNames[id]){
      const el=byId(id+'-'+key);
      el.value=key==='key'?'':item?.[key]||'';
      if(key==='key')el.placeholder=item?.configured?'已配置，留空沿用':'输入该服务的密钥';
    }
  }
  status(health);
  if(!state.config.backup){byId('backup-state').textContent='已停用';}
  invalidate();
  form.hidden=false;
}
function draft(){
  const result={};
  for(const id of options){
    result[id]=null;
    if(id==='backup'&&!byId('backup-toggle').checked)continue;
    result[id]=Object.fromEntries(fieldNames[id].map(name=>[name,byId(id+'-'+name).value.trim()]));
  }
  return result;
}
async function run(work){
  busy=true;updateButtons();
  try{await work();}catch(error){tell(error.message||'操作失败，请重试。',true);}
  finally{busy=false;updateButtons();}
}
async function reload(){
  byId('reload').hidden=true;
  tell('');
  await run(async()=>{
    try{showState(await api('config'));}
    catch(error){byId('reload').hidden=false;throw error;}
  });
}
byId('reload').addEventListener('click',reload);
form.addEventListener('input',()=>{
  invalidate();
  status(health);
  if(!byId('backup-toggle').checked)byId('backup-state').textContent='已停用';
  tell('');
});
form.addEventListener('submit',event=>{
  event.preventDefault();
  invalidate();
  const config=draft();
  run(async()=>{
    tell('正在用短文本测试接口…');
    const result=await api('test',{revision,config});
    status(result.results);
    if(!result.testId){tell('有接口未通过测试，配置仍为当前版本。',true);return;}
    stamp=result;
    expiryTimer=setTimeout(()=>{invalidate();tell('测试结果已过期，请重新测试。',true);},Math.max(0,result.expiresAt-Date.now()));
    tell('草稿已通过测试，可以应用配置。');
  });
});
byId('apply').addEventListener('click',()=>{
  if(!stamp)return;
  const testId=stamp.testId;
  run(async()=>{showState(await api('apply',{revision,testId}));tell('新配置已生效。');});
});
byId('rollback').addEventListener('click',()=>byId('rollback-dialog').showModal());
byId('rollback-confirm').addEventListener('click',()=>{
  byId('rollback-dialog').close();
  run(async()=>{showState(await api('rollback',{revision}));tell('已回退到上一版配置。');});
});
reload();
`;
