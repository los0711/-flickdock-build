import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {registerHooks} from 'node:module';
import {_SHA256} from '@noble/hashes/sha2.js';
// Node's test loader emulates Wrangler's static Wasm import. Production imports
// an already compiled Module and never calls WebAssembly.compile/Module(bytes).
registerHooks({load(url,context,next){
  if(url.endsWith('.wasm'))return {format:'module',shortCircuit:true,
    source:`export default new WebAssembly.Module(Uint8Array.from(${JSON.stringify([...readFileSync(new URL(url))])}));`};
  return next(url,context);
}});
const {PersistentSHA256}=await import('../src/hash-state.ts');
const asset=readFileSync(new URL('../src/vendor/hash-wasm-sha256-4.12.0.wasm',import.meta.url));
assert.equal(createHash('sha256').update(asset).digest('hex'),'c44604aaa9d054401459b0d07f3d6deeb440fa7afdcb0cfd900ef2596d55ce55');
for(const length of [0,1,55,56,63,64,65,127,128,129,1024*1024+123]){
  const data=randomBytes(length),hash=createHash('sha256').update(data).digest('hex');let state=null;
  const step=length>100000?65537:97;
  for(let i=0;i<data.length;i+=step){const rolling=PersistentSHA256.load(state);rolling.update(data.subarray(i,i+step));state=rolling.save();}
  assert.equal(Buffer.from(PersistentSHA256.load(state).digest()).toString('hex'),hash,'state length '+length);
  const split=Math.floor(data.length/2),old=new _SHA256();old.update(data.subarray(0,split));
  const legacy=JSON.stringify({codec:1,words:old.get(),length:old.length,pos:old.pos,buffer:[...old.buffer]});
  const migrated=PersistentSHA256.load(legacy);migrated.update(data.subarray(split));
  assert.equal(Buffer.from(migrated.digest()).toString('hex'),hash,'legacy state length '+length);
}
assert.throws(()=>PersistentSHA256.load('{"codec":2}'));
const valid=JSON.parse(PersistentSHA256.load(null).save());
for(const patch of [{implementation:'other'},{state:valid.state+'='},{state:'AA=='},{codec:99}])
  assert.throws(()=>PersistentSHA256.load(JSON.stringify({...valid,...patch})));
for(const offset of [64,104]){
  const broken=Buffer.from(valid.state,'base64');broken.fill(255,offset,offset+(offset===64?8:4));
  assert.throws(()=>PersistentSHA256.load(JSON.stringify({...valid,state:broken.toString('base64')})));
}
console.log('PASS static Wasm SHA256 differential vectors, legacy migration, pinned asset and malformed state');
const data=randomBytes(1024*1024),samples=[];let state=null;
for(let index=0;index<40;index++){
  const started=performance.now(),hash=PersistentSHA256.load(state);hash.update(data);state=hash.save();
  samples.push(performance.now()-started);
}
const sorted=samples.slice(5).sort((a,b)=>a-b);
console.log(JSON.stringify({local_node_only:true,one_mib_wasm_ms:{cold:samples[0],p50:sorted[Math.floor(sorted.length*.5)],p95:sorted[Math.floor(sorted.length*.95)],max:Math.max(...samples)}}));
