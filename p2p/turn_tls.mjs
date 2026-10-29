// RFC 8656 TURN framing over verified TLS. DTLS still encrypts peer file data.
import dgram from 'node:dgram';
import tls from 'node:tls';
import net from 'node:net';

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

// Use only the proxy selected by Qt for this fixed TURN destination. The
// proxy sees encrypted TLS records; peer file bytes are also DTLS encrypted.
export function httpConnectSocket(server,proxy,timeoutMs=7000){
  if(server.hostname!=='turn.cloudflare.com'||!Number.isInteger(server.port)||server.port<1||server.port>65535
      ||!proxy||proxy.type!=='http'||typeof proxy.host!=='string'||!proxy.host||proxy.host.length>253
      ||/[\s/\\\x00-\x1f\x7f]/.test(proxy.host)||!Number.isInteger(proxy.port)||proxy.port<1||proxy.port>65535
      ||['username','password'].some(key=>proxy[key]!==undefined&&(typeof proxy[key]!=='string'||proxy[key].length>2048)))
    return Promise.reject(Error('Invalid TURN proxy'));
  return new Promise((resolve,reject)=>{
    const socket=net.connect({host:proxy.host,port:proxy.port});let buffer=Buffer.alloc(0),settled=false;
    const timer=setTimeout(()=>finish(Error('TURN proxy timeout')),timeoutMs);
    function finish(error){
      if(settled)return;settled=true;clearTimeout(timer);
      socket.removeListener('data',receive);socket.removeListener('error',failed);
      socket.removeListener('end',ended);socket.removeListener('close',ended);socket.removeListener('connect',connected);
      if(error){socket.destroy();reject(error);}else resolve(socket);
    }
    function failed(){finish(Error('TURN proxy connection failed'));}
    function ended(){finish(Error('TURN proxy connection closed'));}
    function receive(data){
      if(buffer.length+data.length>16384){finish(Error('TURN proxy header exceeded'));return;}
      buffer=Buffer.concat([buffer,data]);const end=buffer.indexOf('\r\n\r\n');if(end<0)return;
      if(!/^HTTP\/1\.[01] 200(?: |\r\n)/.test(buffer.toString('latin1'))||buffer.length!==end+4){
        finish(Error('TURN proxy refused tunnel'));return;
      }
      // Hand the socket to TLS before it can emit further data.
      socket.pause();finish();
    }
    function connected(){
      const authority=server.hostname+':'+server.port;
      const authorization=proxy.username||proxy.password
        ? 'Proxy-Authorization: Basic '+Buffer.from((proxy.username||'')+':'+(proxy.password||'')).toString('base64')+'\r\n':'';
      socket.write('CONNECT '+authority+' HTTP/1.1\r\nHost: '+authority+'\r\n'+authorization+'\r\n');
    }
    socket.once('error',failed);socket.once('end',ended);socket.once('close',ended);
    socket.on('data',receive);socket.once('connect',connected);
  });
}

export function socks5ConnectSocket(server,proxy,timeoutMs=7000){
  if(server.hostname!=='turn.cloudflare.com'||!Number.isInteger(server.port)||server.port<1||server.port>65535
      ||!proxy||proxy.type!=='socks5'||typeof proxy.host!=='string'||!proxy.host||proxy.host.length>253
      ||/[\s/\\\x00-\x1f\x7f]/.test(proxy.host)||!Number.isInteger(proxy.port)||proxy.port<1||proxy.port>65535
      ||['username','password'].some(key=>proxy[key]!==undefined&&(typeof proxy[key]!=='string'||Buffer.byteLength(proxy[key])>255)))
    return Promise.reject(Error('Invalid TURN SOCKS5 proxy'));
  return new Promise((resolve,reject)=>{
    const socket=net.connect({host:proxy.host,port:proxy.port});
    let buffer=Buffer.alloc(0),phase='greeting',settled=false;
    const auth=!!(proxy.username||proxy.password),timer=setTimeout(()=>finish(Error('TURN SOCKS5 proxy timeout')),timeoutMs);
    function finish(error){
      if(settled)return;settled=true;clearTimeout(timer);
      socket.removeListener('data',receive);socket.removeListener('error',failed);socket.removeListener('end',ended);
      socket.removeListener('close',ended);socket.removeListener('connect',connected);
      if(error){socket.destroy();reject(error);}else resolve(socket);
    }
    function failed(){finish(Error('TURN SOCKS5 proxy connection failed'));}
    function ended(){finish(Error('TURN SOCKS5 proxy connection closed'));}
    function connected(){socket.write(auth?Buffer.from([5,2,0,2]):Buffer.from([5,1,0]));}
    function connect(){
      phase='connect';const host=Buffer.from(server.hostname,'ascii'),port=Buffer.alloc(2);port.writeUInt16BE(server.port);
      socket.write(Buffer.concat([Buffer.from([5,1,0,3,host.length]),host,port]));
    }
    function receive(data){
      if(buffer.length+data.length>1024){finish(Error('TURN SOCKS5 proxy response exceeded'));return;}
      buffer=Buffer.concat([buffer,data]);
      if(phase==='greeting'&&buffer.length>=2){
        const version=buffer[0],method=buffer[1];buffer=buffer.subarray(2);
        if(version!==5||![0,2].includes(method)||method===2&&!auth){finish(Error('TURN SOCKS5 proxy authentication refused'));return;}
        if(method===0)connect();
        else{
          phase='auth';const user=Buffer.from(proxy.username||''),password=Buffer.from(proxy.password||'');
          socket.write(Buffer.concat([Buffer.from([1,user.length]),user,Buffer.from([password.length]),password]));
        }
      }
      if(phase==='auth'&&buffer.length>=2){
        if(buffer[0]!==1||buffer[1]!==0){finish(Error('TURN SOCKS5 proxy authentication refused'));return;}
        buffer=buffer.subarray(2);connect();
      }
      if(phase==='connect'&&buffer.length>=5){
        if(buffer[0]!==5||buffer[1]!==0||buffer[2]!==0||![1,3,4].includes(buffer[3])){finish(Error('TURN SOCKS5 proxy refused tunnel'));return;}
        const length=buffer[3]===1?10:buffer[3]===4?22:7+buffer[4];
        if(buffer.length<length)return;
        if(buffer.length!==length){finish(Error('TURN SOCKS5 proxy unsolicited bytes'));return;}
        socket.pause();finish();
      }
    }
    socket.once('error',failed);socket.once('end',ended);socket.once('close',ended);
    socket.on('data',receive);socket.once('connect',connected);
  });
}

export async function tlsTurnTunnel(server,onError=()=>{},proxy=null){
  if(server.hostname!=='turn.cloudflare.com'||server.relayType!=='TurnTls'||!Number.isInteger(server.port)||server.port<1||server.port>65535
      ||typeof server.username!=='string'||typeof server.password!=='string')throw Error('Invalid TURN TLS endpoint');
  const deadline=Date.now()+7000;
  const transport=proxy?await (proxy.type==='socks5'?socks5ConnectSocket:httpConnectSocket)(server,proxy):null;
  let socket;
  try{socket=tls.connect({...(transport?{socket:transport}:{host:server.hostname,port:server.port}),servername:server.hostname,rejectUnauthorized:true});}
  catch(error){transport?.destroy();throw error;}
  transport?.resume();
  socket.setNoDelay(true);
  let udp,peer,closed=false,ready=false,pendingUDP=0,rejectClosed;
  const ended=new Promise((_,reject)=>{rejectClosed=reject;});
  const close=()=>{
    if(closed)return;closed=true;
    rejectClosed(Error('TURN TLS connection closed'));
    try{udp?.close();}catch{}
    socket.destroy();
    transport?.destroy();
  };
  const fail=()=>{if(closed)return;close();if(ready)onError();};
  socket.on('error',fail);socket.on('close',fail);socket.on('end',fail);
  try{
    let timer;
    const connect=new Promise((resolve,reject)=>{
      timer=setTimeout(()=>reject(Error('TURN TLS connect timeout')),Math.max(1,deadline-Date.now()));
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

export async function adaptTurnServers(servers,onError,preferTls=true,proxies={}){
  const relays=servers.filter(row=>row&&typeof row==='object'&&row.relayType==='TurnTls').sort((a,b)=>(a.port===443?0:1)-(b.port===443?0:1));
  // Direct sessions contain STUN only: do not introduce any TLS connection.
  if(!relays.length)return {iceServers:servers,close:()=>{},relayTransport:servers.some(row=>row?.relayType==='TurnUdp')?'udp':'direct'};
  for(const relay of preferTls?relays:[]){
    try{
      const proxy=proxies?.[relay.port]||null;
      const tunnel=await tlsTurnTunnel(relay,onError,proxy);
      return {iceServers:[...servers.filter(row=>typeof row==='string'&&row.startsWith('stun:')),tunnel.server],close:tunnel.close,relayTransport:'tls',proxyUsed:!!proxy};
    }catch{}
  }
  // TLS may be blocked. Retain only real UDP endpoints from this credential set.
  const fallback=servers.filter(row=>typeof row==='string'||row?.relayType==='TurnUdp');
  if(!fallback.some(row=>row?.relayType==='TurnUdp'))throw Error('No usable TURN endpoint');
  return {iceServers:fallback,close:()=>{},relayTransport:'udp',tlsFallback:preferTls};
}
