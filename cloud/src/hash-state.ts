/** SHA-256 rounds are provided by the pinned upstream hash-wasm 4.12.0 binary.
 * Workers imports .wasm as an already compiled Module; there is no runtime
 * WebAssembly.compile/eval or handwritten cryptographic round implementation.
 * Keep the binary, its provenance, license and state ABI together when updating.
 */
import module from './vendor/hash-wasm-sha256-4.12.0.wasm';

const IMPLEMENTATION='sha256:hash-wasm:4.12.0:c44604aaa9d054401459b0d07f3d6deeb440fa7afdcb0cfd900ef2596d55ce55';
const STATE_BYTES=112,INPUT_BYTES=16384,MAX_BYTES=8*1024*1024*1024;
interface HashExports {
  memory:WebAssembly.Memory;STATE_SIZE:WebAssembly.Global;
  Hash_GetBuffer():number;Hash_GetState():number;Hash_Init(bits:number):void;
  Hash_Update(length:number):void;Hash_Final():void;
}

function legacyState(value:any):Uint8Array {
  if(!Array.isArray(value.words)||value.words.length!==8||!value.words.every((v:unknown)=>Number.isInteger(v)&&Number(v)>=-2147483648&&Number(v)<=4294967295)
    ||!Number.isSafeInteger(value.length)||value.length<0||value.length>MAX_BYTES||!Number.isInteger(value.pos)||value.pos<0||value.pos>63||value.pos!==value.length%64
    ||!Array.isArray(value.buffer)||value.buffer.length!==64||!value.buffer.every((v:unknown)=>Number.isInteger(v)&&Number(v)>=0&&Number(v)<=255))throw new Error('Invalid SHA256 state');
  // These are the sha256_ctx fields in the pinned upstream C source. Convert
  // the previous server-created noble codec without rehashing existing parts.
  const result=new Uint8Array(STATE_BYTES),view=new DataView(result.buffer);
  result.set(value.buffer);view.setBigUint64(64,BigInt(value.length),true);
  value.words.forEach((word:number,index:number)=>view.setUint32(72+index*4,word>>>0,true));
  view.setUint32(104,32,true);return result;
}

export class PersistentSHA256 {
  private exports:HashExports;
  private buffer:Uint8Array;
  private state:Uint8Array;
  private length=0;
  private finished=false;

  constructor(){
    this.exports=new WebAssembly.Instance(module).exports as unknown as HashExports;
    const e=this.exports,view=new DataView(e.memory.buffer);
    if(view.getUint32(Number(e.STATE_SIZE.value),true)!==STATE_BYTES)throw new Error('Incompatible SHA256 binary');
    this.buffer=new Uint8Array(e.memory.buffer,e.Hash_GetBuffer(),INPUT_BYTES);
    this.state=new Uint8Array(e.memory.buffer,e.Hash_GetState(),STATE_BYTES);
    e.Hash_Init(256);
  }

  update(data:Uint8Array):this {
    if(this.finished||!(data instanceof Uint8Array)||this.length+data.length>MAX_BYTES)throw new Error('Invalid SHA256 update');
    for(let offset=0;offset<data.length;offset+=INPUT_BYTES){
      const chunk=data.subarray(offset,offset+INPUT_BYTES);
      this.buffer.set(chunk);this.exports.Hash_Update(chunk.length);
    }
    this.length+=data.length;return this;
  }

  digest():Uint8Array {
    if(this.finished)throw new Error('SHA256 already finalized');
    this.exports.Hash_Final();this.finished=true;return this.buffer.slice(0,32);
  }

  save():string {
    if(this.finished)throw new Error('SHA256 already finalized');
    return JSON.stringify({codec:2,implementation:IMPLEMENTATION,state:Buffer.from(this.state).toString('base64')});
  }

  static load(serialized:string|null):PersistentSHA256 {
    const hash=new PersistentSHA256();if(serialized===null)return hash;
    if(typeof serialized!=='string'||serialized.length>2048)throw new Error('Invalid SHA256 state');
    const value=JSON.parse(serialized);let state:Uint8Array;
    if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Invalid SHA256 state');
    if(value.codec===1)state=legacyState(value);
    else if(value.codec===2&&value.implementation===IMPLEMENTATION&&typeof value.state==='string'){
      state=Buffer.from(value.state,'base64');
      if(state.length!==STATE_BYTES||Buffer.from(state).toString('base64')!==value.state)throw new Error('Invalid SHA256 state');
    }else throw new Error('Invalid SHA256 state');
    const view=new DataView(state.buffer,state.byteOffset,state.byteLength),length=view.getBigUint64(64,true);
    if(length>BigInt(MAX_BYTES)||view.getUint32(104,true)!==32)throw new Error('Invalid SHA256 state');
    hash.state.set(state);hash.length=Number(length);return hash;
  }
}
