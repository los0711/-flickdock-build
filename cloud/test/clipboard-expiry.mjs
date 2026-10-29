/** Exercise the exact production alarm with a controllable clock, no cloud. */
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {clipboardAlarm} from '../src/clipboard.ts';
const id=randomUUID(),account=randomUUID(),now=Date.now(),data=new Map([['clipboard-head',{id,account,device:randomUUID(),version:7,at:now,ready:true,size:8,kind:'image'}]]),deleted=[],freed=[];
const storage={async get(key){return data.get(key)},async put(key,value){data.set(key,value)},async delete(key){data.delete(key)},
  async list({prefix,limit}){return new Map([...data].filter(([key])=>key.startsWith(prefix)).slice(0,limit))},
  async transaction(fn){return fn(storage)},async setAlarm(at){storage.alarm=at}};
const env={FILES:{async delete(key){deleted.push(key)}},DB:{prepare(sql){return {bind(...values){return {async run(){freed.push(values)}}}}}}};
const original=Date.now;
try{
  Date.now=()=>now+86400001;
  await clipboardAlarm({storage},env);
  assert.equal(data.get('clipboard-head').expired,true);assert.equal(data.get('clipboard-head').version,8);assert.notEqual(data.get('clipboard-head').id,id);
  assert.deepEqual(deleted,[`clipboard/${account}/${id}`]);assert.deepEqual(freed,[['clipboard:'+id,account]]);
  console.log('PASS exact 24-hour alarm clears clipboard object and allocation without a paste');
}finally{Date.now=original}
