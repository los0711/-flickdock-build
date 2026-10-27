/** Deterministic storage races using the real route + migrations and SQLite.
 * R2.put alone is gated so cancellation lands at the exact dangerous boundary.
 */
import assert from 'node:assert/strict';
import {createHash,randomUUID} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {registerHooks} from 'node:module';
import {DatabaseSync} from 'node:sqlite';
import {transformSync} from 'esbuild';

registerHooks({resolve(specifier,context,next){
  if(specifier==='./hash-state')specifier+='.ts';
  return next(specifier,context);
},load(url,context,next){
  if(url==='cloudflare:workers')return {format:'module',shortCircuit:true,source:'export class DurableObject { constructor(ctx,env){this.ctx=ctx;this.env=env} }'};
  if(url.endsWith('.wasm'))return {format:'module',shortCircuit:true,source:`export default new WebAssembly.Module(Uint8Array.from(${JSON.stringify([...readFileSync(new URL(url))])}));`};
  if(url.endsWith('.ts'))return {format:'module',shortCircuit:true,source:transformSync(readFileSync(new URL(url),'utf8'),{loader:'ts',format:'esm',target:'es2022'}).code};
  return next(url,context);
}});
const worker=(await import('../src/index.ts')).default;
const sha=value=>createHash('sha256').update(value).digest('hex');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject};};

async function race(failPut,fastUpload=false,fastMultipart=false){
  const sqlite=new DatabaseSync(':memory:');
  for(const file of ['0001_schema.sql','0002_retention.sql','0003_transfers_password.sql'])sqlite.exec(readFileSync(new URL('../migrations/'+file,import.meta.url),'utf8'));
  class Statement{
    constructor(sql,values=[]){this.sql=sql;this.values=values;}
    bind(...values){return new Statement(this.sql,values);}
    async first(){return sqlite.prepare(this.sql).get(...this.values)??null;}
    async all(){return {results:sqlite.prepare(this.sql).all(...this.values)};}
    execute(){const result=sqlite.prepare(this.sql).run(...this.values);return {meta:{changes:Number(result.changes)}};}
    async run(){return this.execute();}
  }
  const DB={prepare:sql=>new Statement(sql),async batch(statements){sqlite.exec('BEGIN');try{const result=statements.map(s=>s.execute());sqlite.exec('COMMIT');return result;}catch(error){sqlite.exec('ROLLBACK');throw error;}}};
  const now=Date.now(),account=randomUUID(),device=randomUUID(),token='a'.repeat(64);
  sqlite.prepare('INSERT INTO accounts(id,username,username_norm,auth_salt,auth_verifier,created_at,quota_bytes) VALUES(?,?,?,?,?,?,?)').run(account,'test','test','0'.repeat(32),'0'.repeat(64),now,8589934592);
  sqlite.prepare('INSERT INTO devices(id,account_id,name,platform,created_at,last_seen_at) VALUES(?,?,?,?,?,?)').run(device,account,'test','windows',now,now);
  sqlite.prepare('INSERT INTO sessions VALUES(?,?,?,?)').run(sha(token),account,device,now+86400000);
  const objects=new Map(),entered=deferred(),finish=deferred();
  const env={DB,FILES:{async put(key,data){entered.resolve();await finish.promise;if(failPut)throw new Error('simulated storage failure');const bytes=Buffer.from(data instanceof ReadableStream?await new Response(data).arrayBuffer():data);objects.set(key,bytes);return {size:bytes.length,checksums:{sha256:Buffer.from(sha(bytes),'hex')}};},async delete(keys){for(const key of typeof keys==='string'?[keys]:keys)objects.delete(key);}},CHUNK_BYTES:'131072',SINGLE_PUT_LIMIT_BYTES:fastMultipart?'4194304':undefined,AUTH_PEPPER:'fixture-secret-that-never-leaves-this-test'};
  async function call(method,path,data,raw=false){
    const headers={Authorization:'Bearer '+token};if(data!==undefined)headers[raw?'X-Content-SHA256':'Content-Type']=raw?sha(data):'application/json';
    return worker.fetch(new Request('http://localhost'+path,{method,headers,body:data===undefined?undefined:raw?data:JSON.stringify(data)}),env);
  }
  const data=Buffer.alloc(fastMultipart?72*1024*1024:fastUpload?5*1024*1024:131072,117),item=randomUUID();
  const reserved=await (await call('POST','/v1/uploads',{item_id:item,base_version:0,kind:'file',name:'original.png',mime_type:'image/png',size:data.length,sha256:sha(data),fast_upload:fastUpload,fast_multipart:fastMultipart})).json();
  assert.equal(reserved.part_count,fastMultipart?2:1);
  const uploading=call('PUT',`/v1/uploads/${reserved.upload_id}/parts/0`,
    fastMultipart?data.subarray(0,64*1024*1024):data,true);
  await entered.promise;
  const cancelled=await call('DELETE',`/v1/uploads/${reserved.upload_id}`);assert.equal(cancelled.status,200);
  assert.equal(sqlite.prepare('SELECT state FROM uploads').get().state,'garbage');
  assert.equal((await (await call('GET','/v1/transfers')).json()).transfers.length,0);
  finish.resolve();const stopped=await uploading;assert.equal(stopped.status,410);assert.equal((await stopped.json()).error.code,'UPLOAD_CANCELLED');
  assert.equal(objects.size,0,'The delayed R2 write must not resurrect a cancelled object');
  for(const table of ['uploads','parts','allocations'])assert.equal(sqlite.prepare(`SELECT COUNT(*) n FROM ${table}`).get().n,0,table);
  assert.equal(sqlite.prepare('SELECT allocated_bytes FROM accounts').get().allocated_bytes,0);
  for(const [method,path,payload] of [
    ['GET',`/v1/uploads/${reserved.upload_id}`],
    ['POST',`/v1/uploads/${reserved.upload_id}/commit`,{}],
    ['POST','/v1/uploads',{upload_id:reserved.upload_id,item_id:item,base_version:0,kind:'file',name:'original.png',mime_type:'image/png',size:data.length,sha256:sha(data)}],
  ])assert.equal((await call(method,path,payload)).status,410);
  assert.equal((await call('DELETE',`/v1/uploads/${reserved.upload_id}`)).status,200,'Cancellation is idempotent after physical cleanup');
  const delayed=randomUUID();assert.equal((await call('DELETE',`/v1/uploads/${delayed}`)).status,200);
  const late=await call('POST','/v1/uploads',{upload_id:delayed,item_id:randomUUID(),base_version:0,kind:'file',name:'late.bin',mime_type:'application/octet-stream',size:data.length,sha256:sha(data)});
  assert.equal(late.status,410);assert.equal(sqlite.prepare('SELECT COUNT(*) n FROM allocations').get().n,0,'A delayed init cannot reserve space after cancellation');
  sqlite.close();
}
await race(false);await race(true);await race(false,true);await race(true,true);await race(false,true,true);await race(true,true,true);
console.log('PASS cancel during successful/failed buffered, single-stream and multi-stream R2.put removes bytes, rows and reserved quota');
