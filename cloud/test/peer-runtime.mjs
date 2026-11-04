/** Full production Qt clients against an isolated authenticated local Worker. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID,scryptSync} from 'node:crypto';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
const server=spawn(process.execPath,[fileURLToPath(new URL('./qa-server.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe'],windowsHide:true});
let logs='',origin='';
server.stderr.on('data',data=>logs+=data);server.stdout.on('data',data=>{logs+=data;origin=logs.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]||''});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const directory=await mkdtemp(join(tmpdir(),'flickdock-mesh-qa-'));
try {
  for(let i=0;i<150&&!origin;i++){if(server.exitCode!==null)throw Error(logs);await pause(250)}
  assert(origin,logs);
  async function api(path,data,status=200) {
    const response=await fetch(origin+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(15000)});
    const value=await response.json();assert.equal(response.status,status,JSON.stringify(value));return value;
  }
  const username='mesh-runtime-'+randomUUID(),sessions=[];
  for(let index=0;index<3;index++){
    const challenge=await api('/v1/auth/challenge',{username,purpose:index?'login':'register'});
    const auth_key=scryptSync('isolated-runtime-qa',Buffer.from(challenge.salt,'hex'),32,{N:16384,r:8,p:5,maxmem:32*1024*1024}).toString('hex');
    sessions.push(await api('/v1/auth/'+(index?'login':'register'),{username,challenge_id:challenge.challenge_id,auth_key,device_name:'Runtime QA '+index,platform:'windows'},index?200:201));
  }
  const input=join(directory,'connection.json'),output=join(directory,'result.json');
  await writeFile(input,JSON.stringify({origin,sessions}));
  const executable=process.env.FD_QA_EXE||process.env.FD_QA_PYTHON;assert(executable,'Set FD_QA_EXE or FD_QA_PYTHON');
  const args=[...(process.env.FD_QA_EXE?[]:[fileURLToPath(new URL('../../flickdock.py',import.meta.url))]),'--verify-peers','--verify-connection',input,'--verify-output',output];
  if(process.env.FD_QA_CLOUD==='1')args.push('--peer-force-cloud');
  if(process.env.FD_QA_BOOTSTRAP==='1')args.push('--peer-cloud-bootstrap');
  if(process.env.FD_QA_MANUAL==='1')args.push('--peer-manual');
  const child=spawn(executable,args,{stdio:['ignore','pipe','pipe'],windowsHide:true,env:{...process.env,QT_QPA_PLATFORM:'offscreen',APPDATA:directory,LOCALAPPDATA:directory}});
  let diagnostics='';child.stdout.on('data',data=>diagnostics+=data);child.stderr.on('data',data=>diagnostics+=data);
  const timer=setTimeout(()=>child.kill(),180000);
  const code=await new Promise(resolve=>child.once('exit',resolve));clearTimeout(timer);
  const report=JSON.parse(await readFile(output,'utf8'));
  if(process.env.FD_QA_REPORT)await writeFile(process.env.FD_QA_REPORT,JSON.stringify(report,null,2));
  assert.equal(code,0,JSON.stringify(report)+'\n'+diagnostics);
  assert(report.ok&&report.all_pairs_connected&&report.peer_before_cloud&&report.cloud_backup_confirmed&&report.logout_revoked);
  console.log('PASS three production clients: real account binding, full native mesh, original peer bytes before cloud, clipboard, backup and logout');
} finally {
  server.stdin.end();if(server.exitCode===null)await new Promise(resolve=>{const timer=setTimeout(resolve,10000);server.once('exit',()=>{clearTimeout(timer);resolve()})});
  await rm(directory,{recursive:true,force:true});
}
