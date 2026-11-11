import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url),root=fileURLToPath(new URL('../',import.meta.url));
const wrangler=require.resolve('wrangler/bin/wrangler.js');
function run(command,args){const result=spawnSync(command,args,{cwd:root,stdio:'inherit',env:process.env});if(result.error)throw result.error;if(result.status!==0)throw new Error(command+' failed: '+result.status);}
run(process.execPath,['test/check-site.mjs']);run(process.execPath,['test/check-gateway.mjs']);
run(process.execPath,[wrangler,'deploy','--config','downloads.wrangler.jsonc','--keep-vars']);
run(process.execPath,[wrangler,'deploy','--config','wrangler.jsonc','--keep-vars']);
// The archive route is live before the first R2 delete is even considered.
run(process.env.PYTHON||'python',['scripts/release_downloads.py','prune','--node',process.execPath,'--wrangler',wrangler]);
