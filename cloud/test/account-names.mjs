/** Actual pre-upgrade D1 -> current workerd API, isolated from cloud accounts. */
import assert from 'node:assert/strict';
import {createHash,createHmac,randomUUID,scryptSync} from 'node:crypto';
import {spawn,spawnSync} from 'node:child_process';
import {mkdtemp,writeFile,readFile,rm,mkdir,copyFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as sleep} from 'node:timers/promises';
import net from 'node:net';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const state=await mkdtemp(join(tmpdir(),'flickdock-account-migration-'));
const migrations=join(state,'migrations');await mkdir(migrations);
const config=JSON.parse(await readFile(join(root,'wrangler.jsonc'),'utf8'));
config.main=join(root,'src/index.ts');config.d1_databases[0].migrations_dir=migrations;
const configFile=join(state,'wrangler-test.json');await writeFile(configFile,JSON.stringify(config));
const env={...process.env,WRANGLER_SEND_METRICS:'false',CLOUDFLARE_API_TOKEN:'',CLOUDFLARE_API_KEY:'',CLOUDFLARE_EMAIL:'',CLOUDFLARE_ACCOUNT_ID:''};
const wrangler=join(root,'node_modules/wrangler/bin/wrangler.js');
const secret='local-account-migration-test-only-secret-123456';
const hash=value=>createHash('sha256').update(value).digest('hex');
const key=(password,salt)=>scryptSync(password,Buffer.from(salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
const quote=value=>"'"+String(value).replaceAll("'","''")+"'";
function cli(args){const value=spawnSync(process.execPath,[wrangler,'--config',configFile,...args],{cwd:state,env,encoding:'utf8',timeout:60000,windowsHide:true});assert.equal(value.status,0,value.stderr+value.stdout);return value.stdout;}
function sql(command){return JSON.parse(cli(['d1','execute','DB','--local','--persist-to',state,'--command',command,'--json']))[0].results;}
let server,base,logs='';
async function request(path,data,expected=200,token){
  const response=await fetch(base+path,{method:data===undefined?'GET':'POST',headers:{Connection:'close',...(data===undefined?{}:{'Content-Type':'application/json'}),...(token?{Authorization:'Bearer '+token}:{})},body:data===undefined?undefined:JSON.stringify(data)});
  const value=await response.json();assert.equal(response.status,expected,JSON.stringify(value));return value;
}
async function login(username,password){const challenge=await request('/v1/auth/challenge',{username,purpose:'login'});return request('/v1/auth/login',{username,challenge_id:challenge.challenge_id,auth_key:key(password,challenge.salt),device_name:'Migration fixture',platform:'windows'});}
try{
  for(const name of ['0001_schema.sql','0002_retention.sql','0003_transfers_password.sql','0004_instant_uploads.sql'])await copyFile(join(root,'migrations',name),join(migrations,name));
  cli(['d1','migrations','apply','DB','--local','--persist-to',state]);
  const names=[['LegacyStraße','legacystraße'],['LegacySTRASSE','legacystrasse'],['完全同名','old-exact-one'],['完全同名','old-exact-two'],['Kelvin','old-normalization-key']];
  const legacy=names.map(([username,norm],index)=>({id:randomUUID(),username,norm,password:'legacy password '+index,salt:Buffer.alloc(16,index+1).toString('hex'),created:index+1}));
  legacy.push({id:randomUUID(),username:'id:'+legacy[0].id,norm:'id:'+legacy[0].id,password:'literal id password',salt:'06'.repeat(16),created:6});
  const fillers=Array.from({length:120},(_,index)=>({id:randomUUID(),username:'Older account '+index,norm:'older account '+index,password:'filler legacy password',salt:'07'.repeat(16),created:index+100}));
  // Put each colliding partner beyond the previous 50-row boundary, including
  // the literal id: name which must prevent an alias from claiming its name.
  const ordered=[legacy[0],legacy[2],legacy[4],...fillers.slice(0,60),legacy[1],legacy[3],legacy[5],...fillers.slice(60)];
  const fixtureKeys=new Map();
  const seedFile=join(state,'legacy-accounts.sql');
  await writeFile(seedFile,ordered.map(row=>{const cache=row.password+'\0'+row.salt;if(!fixtureKeys.has(cache))fixtureKeys.set(cache,key(row.password,row.salt));const proof=createHmac('sha256',secret).update(row.id+'\0'+row.salt+'\0'+fixtureKeys.get(cache)).digest('hex');return `INSERT INTO accounts(id,username,username_norm,auth_verifier,auth_salt,created_at,quota_bytes) VALUES(${[row.id,row.username,row.norm,proof,row.salt].map(quote).join(',')},${row.created},8589934592);`;}).join('\n'));
  cli(['d1','execute','DB','--local','--persist-to',state,'--file',seedFile]);
  const device=randomUUID(),token='ab'.repeat(32),item=randomUUID();
  sql(`INSERT INTO devices(id,account_id,name,platform,created_at,last_seen_at) VALUES('${device}','${legacy[0].id}','Existing device','windows',1,1);INSERT INTO sessions(token_hash,account_id,device_id,expires_at) VALUES('${hash(token)}','${legacy[0].id}','${device}',${Date.now()+86400000});INSERT INTO items(account_id,id,version,kind,name,mime_type,size,updated_at) VALUES('${legacy[0].id}','${item}',1,'text','Existing item','text/plain',5,1)`);
  const before=sql('SELECT id,username,username_norm,auth_verifier,auth_salt,created_at FROM accounts ORDER BY id');
  await copyFile(join(root,'migrations','0005_account_names.sql'),join(migrations,'0005_account_names.sql'));
  cli(['d1','migrations','apply','DB','--local','--persist-to',state]);
  assert.deepEqual(sql('SELECT id,username,username_norm,auth_verifier,auth_salt,created_at FROM accounts ORDER BY id'),before);
  const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
  server=spawn(process.execPath,[wrangler,'dev','--config',configFile,'--local','--ip','127.0.0.1','--port',String(port),'--persist-to',state,'--var','AUTH_PEPPER:'+secret],{cwd:state,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);base='http://127.0.0.1:'+port;
  for(let attempt=0;;attempt++){if(server.exitCode!==null||attempt===120)throw Error(logs);try{if((await fetch(base+'/v1/health')).ok)break;}catch{}await sleep(250);}
  assert.equal(sql('SELECT COUNT(*) n FROM accounts WHERE username_key IS NULL')[0].n,126);
  assert.equal((await request('/v1/me',undefined,200,token)).account.id,legacy[0].id);
  // Login and registration must both work on the very first request, without
  // pre-warming, retrying, or returning a migration-related 503.
  const [firstLogin,firstRegistration]=await Promise.all([
    request('/v1/auth/challenge',{username:legacy[1].username,purpose:'login'}),
    request('/v1/auth/challenge',{username:'Fresh first-request account',purpose:'register'}),
  ]);
  assert.equal((await request('/v1/auth/login',{username:legacy[1].username,challenge_id:firstLogin.challenge_id,auth_key:key(legacy[1].password,firstLogin.salt),device_name:'First legacy device',platform:'macos'})).account.id,legacy[1].id);
  assert.match(firstRegistration.challenge_id,/^[a-f0-9-]{36}$/);
  assert.equal(sql('SELECT COUNT(*) n FROM accounts WHERE username_key IS NULL')[0].n,0);
  console.log('PASS first concurrent login/register challenges work across 126 old accounts without migration downtime');
  for(const row of [legacy[0],legacy[1],legacy[4],legacy[5]])assert.equal((await login(row.username,row.password)).account.id,row.id);
  console.log('PASS legacy lower/NFKC accounts and literal id usernames retain their identity');
  const aliases=sql('SELECT id,login_alias FROM accounts WHERE login_alias IS NOT NULL ORDER BY id');assert.equal(aliases.length,4);
  for(const row of aliases){const source=legacy.find(account=>account.id===row.id);assert.equal((await login(row.login_alias,source.password)).account.id,row.id);}
  const alias0=aliases.find(row=>row.id===legacy[0].id).login_alias;assert.ok(alias0.startsWith('id::'));
  assert.equal((await login(legacy[2].username,legacy[2].password)).account.id,legacy[2].id);
  assert.deepEqual(sql('SELECT id,login_alias FROM accounts WHERE login_alias IS NOT NULL ORDER BY id'),aliases);
  assert.equal((await request('/v1/me',undefined,200,token)).account.id,legacy[0].id);
  assert.equal(sql(`SELECT COUNT(*) n FROM items WHERE account_id='${legacy[0].id}' AND id='${item}'`)[0].n,1);
  assert.deepEqual(sql('SELECT id,username,username_norm,auth_verifier,auth_salt,created_at FROM accounts ORDER BY id'),before);
  console.log('PASS exact duplicate accounts, existing sessions/content, stable non-hijacking aliases survive migration');
  assert.equal((await request('/v1/auth/challenge',{username:'ＫＥＬＶＩＮ',purpose:'register'},409)).error.code,'USERNAME_TAKEN');
  const variants=['新Straße','新STRASSE','　新ＳＴＲＡＳＳＥ　'];
  const challenges=await Promise.all(variants.map(username=>request('/v1/auth/challenge',{username,purpose:'register'})));
  const replies=await Promise.all(variants.map(async(username,index)=>{const r=await fetch(base+'/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json',Connection:'close'},body:JSON.stringify({username,challenge_id:challenges[index].challenge_id,auth_key:key('123456',challenges[index].salt),device_name:'New device',platform:'macos'})});return {status:r.status,value:await r.json()};}));
  assert.equal(replies.filter(row=>row.status===201).length,1);assert.equal(replies.filter(row=>row.status===409).length,2);
  const winner=replies.find(row=>row.status===201).value;assert.equal((await login('新strasse','123456')).account.id,winner.account.id);
  await request('/v1/auth/challenge',{username:'id:'+randomUUID(),purpose:'register'},400);
  assert.equal(sql('SELECT COUNT(*) n FROM accounts WHERE username_key IS NULL')[0].n,0);
  assert.equal(sql('SELECT complete FROM account_name_migration WHERE id=1')[0].complete,1);
  console.log('PASS six-character proof flow, canonical registration race and reserved aliases');
}finally{
  if(server&&server.exitCode===null){const exited=new Promise(resolve=>server.once('exit',resolve));server.kill();await exited;}
  await rm(state,{recursive:true,force:true,maxRetries:10,retryDelay:200});
}
