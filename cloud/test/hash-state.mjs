import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {PersistentSHA256} from '../src/hash-state.ts';
for(const length of [0,1,55,56,63,64,65,127,128,129,1024*1024+123]){
  const data=randomBytes(length),hash=createHash('sha256').update(data).digest('hex');let state=null;
  for(let i=0;i<data.length;i+=97){const rolling=PersistentSHA256.load(state);rolling.update(data.subarray(i,i+97));state=rolling.save();}
  assert.equal(Buffer.from(PersistentSHA256.load(state).digest()).toString('hex'),hash,'state length '+length);
}
assert.throws(()=>PersistentSHA256.load('{"codec":2}'));
console.log('PASS persisted SHA256 differential vectors and malformed state');
