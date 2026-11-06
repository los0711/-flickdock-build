/** Bounded consent metadata only. File bytes use upstream Syncthing or R2. */
import {isIP} from 'node:net';
const DEVICE=/^[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}$/;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const SHA=/^[a-f0-9]{64}$/;
export type NativeOffer={kind:'native';device_id:string;addresses:string[];content?:{size:number;sha256:string}};
export type CloudFile={kind:'r2';id:string;version:number;size:number;sha256:string};
export type NativeSignal=NativeOffer|CloudFile|{kind:'fallback'|'saved'}|{kind:'progress';received:number;total:number};
export type NativeState={sender?:NativeOffer;receiver?:NativeOffer;cloud?:CloudFile;fallback?:boolean;saved?:boolean};
const positive=(value:unknown):value is number=>typeof value==='number'&&Number.isSafeInteger(value)&&value>0;
export function manualSignal(input:any,sender:boolean):NativeSignal{
  if(!input||typeof input!=='object'||Array.isArray(input))throw Error('传输内核信令无效。');
  if(input.kind==='native'){
    if(typeof input.device_id!=='string'||!DEVICE.test(input.device_id)||!Array.isArray(input.addresses)||!input.addresses.length||input.addresses.length>12)
      throw Error('传输内核设备信息无效。');
    const addresses=input.addresses.map((address:unknown)=>{
      if(address==='dynamic')return address;
      if(typeof address!=='string'||address.length>256)throw Error('传输地址无效。');
      let url:URL;try{url=new URL(address);}catch{throw Error('传输地址无效。');}
      const host=url.hostname.replace(/^\[|\]$/g,'');
      if(!['tcp:','tcp4:','tcp6:','quic:','quic4:','quic6:'].includes(url.protocol)||!isIP(host)||!url.port||!positive(Number(url.port))||Number(url.port)>65535
        ||url.username||url.password||url.pathname||url.search||url.hash)throw Error('传输地址无效。');
      return address;
    });
    let content;
    if(sender){
      if(!positive(input.content?.size)||typeof input.content?.sha256!=='string'||!SHA.test(input.content.sha256))throw Error('发送内容描述无效。');
      content={size:input.content.size,sha256:input.content.sha256};
    }else if(input.content!==undefined)throw Error('接收方不能更改发送内容。');
    return {kind:'native',device_id:input.device_id,addresses,...(content?{content}:{})};
  }
  if(input.kind==='r2'&&sender&&typeof input.id==='string'&&UUID.test(input.id)&&positive(input.version)&&positive(input.size)&&typeof input.sha256==='string'&&SHA.test(input.sha256))
    return {kind:'r2',id:input.id,version:input.version,size:input.size,sha256:input.sha256};
  if(input.kind==='fallback')return {kind:'fallback'};
  if(input.kind==='saved'&&!sender)return {kind:'saved'};
  if(input.kind==='progress'&&!sender&&positive(input.total)&&Number.isSafeInteger(input.received)&&input.received>=0&&input.received<=input.total)
    return {kind:'progress',received:input.received,total:input.total};
  throw Error('传输内核信令无效。');
}
