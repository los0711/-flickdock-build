/** Isolated real workerd/D1/R2 endpoint for frozen Qt client verification. */
import {spawn,spawnSync} from 'node:child_process';
import {mkdtemp,writeFile,rm,readFile,mkdir,cp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import net from 'node:net';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const wrangler=join(root,'node_modules/wrangler/bin/wrangler.js');
const state=await mkdtemp(join(tmpdir(),'flickdock-frozen-qa-'));
const config=JSON.parse(await readFile(join(root,'wrangler.jsonc'),'utf8'));
config.main=join(root,'src/index.ts');
if(process.env.FD_QA_HIBERNATION==='1'){
  // A ten-minute idle probe must not be invalidated by development hot reload.
  const snapshot=join(state,'immutable-input');
  await mkdir(join(snapshot,'test'),{recursive:true});
  await cp(join(root,'src'),join(snapshot,'src'),{recursive:true});
  await cp(join(root,'test/hibernation-entry.ts'),join(snapshot,'test/hibernation-entry.ts'));
  for(const name of Object.keys(configureDependencies())){
    const destination=join(snapshot,'node_modules',name);await mkdir(dirname(destination),{recursive:true});
    await cp(join(root,'node_modules',name),destination,{recursive:true});
  }
  config.main=join(snapshot,'test/hibernation-entry.ts');
}
function configureDependencies(){return {'@noble/hashes':true,'unicode-case-folding':true};}
config.d1_databases[0].migrations_dir=join(root,'migrations');
const configFile=join(state,'wrangler-test.json');
await writeFile(configFile,JSON.stringify(config));
const env={...process.env,WRANGLER_LOG_PATH:join(state,'logs'),WRANGLER_SEND_METRICS:'false',CLOUDFLARE_API_TOKEN:'',CLOUDFLARE_API_KEY:'',CLOUDFLARE_EMAIL:'',CLOUDFLARE_ACCOUNT_ID:''};
const migrated=spawnSync(process.execPath,[wrangler,'--config',configFile,'d1','migrations','apply','DB','--local','--persist-to',state],{cwd:state,env,encoding:'utf8',timeout:60000,windowsHide:true});
if(migrated.status!==0)throw Error(migrated.stderr+migrated.stdout);
const socket=net.createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));
const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));
const secret='qa-local-only-secret-longer-than-32-bytes';
const server=spawn(process.execPath,[wrangler,'dev','--config',configFile,'--local','--ip','127.0.0.1','--port',String(port),'--persist-to',state,'--var','EMAIL_CODE_SECRET:'+secret,'--var','AUTH_PEPPER:'+secret],{cwd:state,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
let logs='';server.stdout.on('data',data=>logs+=data);server.stderr.on('data',data=>logs+=data);
const origin='http://127.0.0.1:'+port;
for(let attempt=0;attempt<120;attempt++){
  if(server.exitCode!==null)throw Error(logs);
  try{if((await fetch(origin+'/v1/health')).ok)break;}catch{}
  if(attempt===119)throw Error('local Worker startup timed out: '+logs);
  await new Promise(resolve=>setTimeout(resolve,250));
}
process.stdout.write(origin+'\n');
let stopping=false;
async function stop(){if(stopping)return;stopping=true;server.kill();await new Promise(resolve=>server.once('exit',resolve));try{await rm(state,{recursive:true,force:true,maxRetries:10,retryDelay:300});}catch{}process.exit(0);}
process.on('SIGINT',stop);process.on('SIGTERM',stop);
process.stdin.resume();process.stdin.on('data',stop);process.stdin.on('end',stop);
