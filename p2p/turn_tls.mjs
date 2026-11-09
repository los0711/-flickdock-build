// RFC 8656 TURN framing over verified TLS. DTLS still encrypts peer file data.
import dgram from 'node:dgram';
import tls from 'node:tls';

const MAX_FRAME=65556,MAX_QUEUE=32*1024*1024,MAX_RECEIVE=2*1024*1024;
export function udpToStream(packet){
  if(packet.length<4)throw Error('Short TURN frame');
  const kind=packet.readUInt16BE(),length=packet.readUInt16BE(2);
  if((kind&0xc000)===0){
    if(packet.length!==20+length||length%4||packet.readUInt32BE(4)!==0x2112a442)throw Error('Invalid STUN frame');
    return packet;
  }
  if((kind&0xc000)!==0x4000)throw Error('Invalid TURN channel');
  const actual=4+length,padded=(actual+3)&~3;
  if(packet.length<actual||packet.length>padded)throw Error('Invalid TURN channel length');
  const output=Buffer.alloc(padded);packet.copy(output,0,0,actual);return output;
}
export class TurnStreamDecoder{
  constructor(emit){this.emit=emit;this.buffer=Buffer.alloc(0);}
  push(chunk){
    if(this.buffer.length+chunk.length>MAX_RECEIVE)throw Error('TURN receive queue exceeded');
    this.buffer=Buffer.concat([this.buffer,chunk]);
    while(this.buffer.length>=4){
      const kind=this.buffer.readUInt16BE(),length=this.buffer.readUInt16BE(2);
      let actual,wire;
      if((kind&0xc000)===0){actual=wire=20+length;if(length%4)throw Error('Invalid STUN length');}
      else if((kind&0xc000)===0x4000){actual=4+length;wire=(actual+3)&~3;}
      else throw Error('Invalid TURN channel');
      if(wire>MAX_FRAME)throw Error('TURN frame exceeded');
      if(this.buffer.length<wire)return;
      if((kind&0xc000)===0&&this.buffer.readUInt32BE(4)!==0x2112a442)throw Error('Invalid STUN cookie');
      const frame=this.buffer.subarray(0,actual);this.buffer=this.buffer.subarray(wire);this.emit(frame);
    }
  }
}

export async function tlsTurnTunnel(server,onError=()=>{}){
  if(server.hostname!=='turn.cloudflare.com'||server.relayType!=='TurnTls'||!Number.isInteger(server.port)||server.port<1||server.port>65535
      ||typeof server.username!=='string'||typeof server.password!=='string')throw Error('Invalid TURN TLS endpoint');
  const socket=tls.connect({host:server.hostname,port:server.port,servername:server.hostname,rejectUnauthorized:true});
  socket.setNoDelay(true);
  let udp,peer,closed=false,ready=false,pendingUDP=0,rejectClosed;
  const ended=new Promise((_,reject)=>{rejectClosed=reject;});
  const close=()=>{
    if(closed)return;closed=true;
    rejectClosed(Error('TURN TLS connection closed'));
    try{udp?.close();}catch{}
    socket.destroy();
  };
  const fail=()=>{if(closed)return;close();if(ready)onError();};
  socket.on('error',fail);socket.on('close',fail);socket.on('end',fail);
  try{
    let timer;
    const connect=new Promise((resolve,reject)=>{
      timer=setTimeout(()=>reject(Error('TURN TLS connect timeout')),7000);
      socket.once('secureConnect',resolve);
    });
    try{await Promise.race([connect,ended]);}finally{clearTimeout(timer);}
    if(closed)throw Error('TURN TLS connection closed');
    udp=dgram.createSocket('udp4');udp.on('error',fail);
    await Promise.race([new Promise(resolve=>udp.bind(0,'127.0.0.1',resolve)),ended]);
    if(closed)throw Error('TURN TLS connection closed');
    // Bursts from SCTP must not be lost in the local transport shim. These
    // affect only this socket; an OS that caps them retains its allowed size.
    try{udp.setRecvBufferSize(4*1024*1024);}catch{}
    try{udp.setSendBufferSize(4*1024*1024);}catch{}
    const decoder=new TurnStreamDecoder(packet=>{
      if(closed||!peer)return;
      pendingUDP+=packet.length;
      if(pendingUDP>MAX_QUEUE)throw Error('TURN local send queue exceeded');
      udp.send(packet,peer.port,'127.0.0.1',error=>{pendingUDP-=packet.length;if(error)fail();});
    });
    socket.on('data',data=>{try{decoder.push(data);}catch{fail();}});
    udp.on('message',(packet,source)=>{
      if(closed||source.address!=='127.0.0.1'||peer&&peer.port!==source.port)return;
      try{
        const frame=udpToStream(packet);peer=peer||{port:source.port};
        if(socket.writableLength+frame.length>MAX_QUEUE)throw Error('TURN send queue exceeded');
        socket.write(frame);
      }catch{fail();}
    });
    ready=true;
    return {server:{hostname:'127.0.0.1',port:udp.address().port,username:server.username,password:server.password,relayType:'TurnUdp'},close};
  }catch(error){close();throw error;}
}

export async function adaptTurnServers(servers,onError,preferTls=true){
  const relays=servers.filter(row=>row&&typeof row==='object'&&row.relayType==='TurnTls').sort((a,b)=>(a.port===443?0:1)-(b.port===443?0:1));
  // Direct sessions contain STUN only: do not introduce any TLS connection.
  if(!relays.length)return {iceServers:servers,close:()=>{},relayTransport:servers.some(row=>row?.relayType==='TurnUdp')?'udp':'direct'};
  for(const relay of preferTls?relays:[]){
    try{
      const tunnel=await tlsTurnTunnel(relay,onError);
      return {iceServers:[...servers.filter(row=>typeof row==='string'&&row.startsWith('stun:')),tunnel.server],close:tunnel.close,relayTransport:'tls'};
    }catch{}
  }
  // TLS may be blocked. Retain only real UDP endpoints from this credential set.
  const fallback=servers.filter(row=>typeof row==='string'||row?.relayType==='TurnUdp');
  if(!fallback.some(row=>row?.relayType==='TurnUdp'))throw Error('No usable TURN endpoint');
  return {iceServers:fallback,close:()=>{},relayTransport:'udp',tlsFallback:preferTls};
}
