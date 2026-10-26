/** Pinned noble 2.4.0 SHA-256 state codec; only server-generated state reaches D1.
 * No hash rounds are implemented here. Upgrading noble requires codec/vector tests.
 */
import {_SHA256} from '@noble/hashes/sha2.js';

export class PersistentSHA256 extends _SHA256 {
  save():string {
    return JSON.stringify({codec:1,words:this.get(),length:this.length,pos:this.pos,buffer:Array.from(this.buffer)});
  }
  static load(serialized:string|null):PersistentSHA256 {
    const hash=new PersistentSHA256();if(!serialized)return hash;
    const value=JSON.parse(serialized);
    if(value.codec!==1||!Array.isArray(value.words)||value.words.length!==8||!value.words.every((v:unknown)=>Number.isInteger(v)&&Number(v)>=-2147483648&&Number(v)<=4294967295)
      ||!Number.isSafeInteger(value.length)||value.length<0||value.length>8*1024*1024*1024||!Number.isInteger(value.pos)||value.pos<0||value.pos>63||value.pos!==value.length%64
      ||!Array.isArray(value.buffer)||value.buffer.length!==64||!value.buffer.every((v:unknown)=>Number.isInteger(v)&&Number(v)>=0&&Number(v)<=255))throw new Error('Invalid SHA256 state');
    hash.set(...value.words as [number,number,number,number,number,number,number,number]);hash.length=value.length;hash.pos=value.pos;hash.buffer.set(value.buffer);return hash;
  }
}
