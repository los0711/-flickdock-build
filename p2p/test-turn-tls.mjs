import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {randomBytes} from 'node:crypto';
import test from 'node:test';
import dgram from 'node:dgram';
import tls from 'node:tls';
import {udpToStream,TurnStreamDecoder,tlsTurnTunnel,adaptTurnServers} from './turn_tls.mjs';

const relay=(port=443)=>({hostname:'turn.cloudflare.com',port,relayType:'TurnTls',username:'test-user',password:'test-credential'});
const udpRelay={...relay(3478),relayType:'TurnUdp'},stun='stun:stun.cloudflare.com:3478';
const packet=()=>{const b=Buffer.alloc(23);b.writeUInt16BE(0x4001);b.writeUInt16BE(19,2);return b;};
class TLS extends EventEmitter{
  writableLength=0;writes=[];destroyed=false;
  setNoDelay(value){assert.equal(value,true);}
  write(data){this.writes.push(Buffer.from(data));return true;}
  destroy(){if(!this.destroyed){this.destroyed=true;this.emit('close');}}
}
class UDP extends EventEmitter{
  sent=[];closed=false;
  bind(port,host,done){assert.equal(port,0);assert.equal(host,'127.0.0.1');queueMicrotask(done);}
  address(){return {port:31888};}
  setRecvBufferSize(size){assert.equal(size,4*1024*1024);this.receiveBuffer=size;}
  setSendBufferSize(size){assert.equal(size,4*1024*1024);this.sendBuffer=size;}
  close(){this.closed=true;}
  send(data,port,host,done){this.sent.push({data:Buffer.from(data),port,host});queueMicrotask(()=>done(null));}
}
function setup(t,{failPorts=[],connect=true}={}){
  const sockets=[],datagrams=[],options=[];
  t.mock.method(tls,'connect',value=>{
    options.push(value);const socket=new TLS();sockets.push(socket);
    if(connect)queueMicrotask(()=>socket.emit(failPorts.includes(value.port)?'error':'secureConnect',Error('test failure')));
    return socket;
  });
  t.mock.method(dgram,'createSocket',kind=>{assert.equal(kind,'udp4');const udp=new UDP();datagrams.push(udp);return udp;});
  return {sockets,datagrams,options};
}

test('TURN retains original STUN and channel bytes for arbitrary TLS fragmentation',()=>{
  const packets=[];
  for(const size of [0,1,2,3,4,999,1000,32768,65503]){
    const p=Buffer.alloc(4+size);p.writeUInt16BE(0x4001);p.writeUInt16BE(size,2);randomBytes(size).copy(p,4);packets.push(p);
  }
  for(const size of [0,4,8,1024]){
    const p=Buffer.alloc(20+size);p.writeUInt16BE(0x0103);p.writeUInt16BE(size,2);p.writeUInt32BE(0x2112a442,4);randomBytes(12+size).copy(p,8);packets.push(p);
  }
  const wire=Buffer.concat(packets.map(udpToStream));
  for(const step of [1,2,3,4,19,20,117,4096,65536,wire.length]){
    const result=[],decoder=new TurnStreamDecoder(p=>result.push(Buffer.from(p)));
    for(let offset=0;offset<wire.length;offset+=step)decoder.push(wire.subarray(offset,offset+step));
    assert.deepEqual(result,packets);assert.equal(decoder.buffer.length,0);
  }
  assert.throws(()=>udpToStream(Buffer.from([0x40,0,0,7])));
  assert.throws(()=>new TurnStreamDecoder(()=>{}).push(Buffer.from([0x80,0,0,0])));
  assert.throws(()=>new TurnStreamDecoder(()=>{}).push(Buffer.alloc(2*1024*1024+1)));
  const bad=Buffer.alloc(20);assert.throws(()=>udpToStream(bad));
});
test('strict supplied TLS endpoint, pinned loopback peer and idempotent cleanup',async t=>{
  const fake=setup(t);let errors=0;
  await assert.rejects(()=>tlsTurnTunnel({...relay(),hostname:'127.0.0.1'}));assert.equal(fake.sockets.length,0);
  const tunnel=await tlsTurnTunnel(relay(),()=>errors++),socket=fake.sockets[0],udp=fake.datagrams[0];
  assert.deepEqual(fake.options[0],{host:'turn.cloudflare.com',port:443,servername:'turn.cloudflare.com',rejectUnauthorized:true});
  assert.equal(tunnel.server.hostname,'127.0.0.1');assert.equal(tunnel.server.port,31888);
  assert.equal(udp.receiveBuffer,4*1024*1024);assert.equal(udp.sendBuffer,4*1024*1024);
  udp.emit('message',packet(),{address:'127.0.0.1',port:41000});
  udp.emit('message',packet(),{address:'127.0.0.1',port:41001});
  udp.emit('message',packet(),{address:'10.0.0.2',port:41000});assert.equal(socket.writes.length,1);
  socket.emit('data',udpToStream(packet()));await Promise.resolve();
  assert.deepEqual(udp.sent,[{data:packet(),port:41000,host:'127.0.0.1'}]);
  socket.emit('error',Error('connection lost'));socket.emit('close');tunnel.close();
  assert.equal(errors,1);assert.equal(socket.destroyed,true);assert.equal(udp.closed,true);
});
test('direct sessions make no TLS call; TLS failure uses only supplied UDP fallback',async t=>{
  const fake=setup(t,{failPorts:[443,5349]});
  const direct=await adaptTurnServers([stun]);assert.deepEqual(direct.iceServers,[stun]);assert.equal(fake.sockets.length,0);
  const result=await adaptTurnServers([relay(5349),stun,udpRelay,relay(443)]);
  assert.deepEqual(fake.options.map(row=>row.port),[443,5349]);assert.deepEqual(result.iceServers,[stun,udpRelay]);
  assert(fake.sockets.every(socket=>socket.destroyed));assert.equal(fake.datagrams.length,0);result.close();
});
test('a remembered TLS failure uses supplied UDP without another TLS attempt',async t=>{
  const fake=setup(t);const result=await adaptTurnServers([stun,udpRelay,relay()],()=>{},false);
  assert.equal(result.relayTransport,'udp');assert.deepEqual(result.iceServers,[stun,udpRelay]);assert.equal(fake.sockets.length,0);result.close();
});
test('TLS 443 failure can use supplied 5349; cancellation closes owned sockets',async t=>{
  const fake=setup(t,{failPorts:[443]});const result=await adaptTurnServers([relay(5349),stun,udpRelay,relay(443)]);
  assert.deepEqual(fake.options.map(row=>row.port),[443,5349]);assert.equal(result.iceServers.length,2);
  assert.equal(result.iceServers[1].hostname,'127.0.0.1');assert(fake.sockets[0].destroyed);
  result.close();result.close();assert(fake.sockets[1].destroyed);assert(fake.datagrams[0].closed);
});
test('handshake timeout closes pending TLS before any local UDP allocation',async t=>{
  const fake=setup(t,{connect:false});t.mock.timers.enable({apis:['setTimeout']});
  const pending=tlsTurnTunnel(relay());const rejected=assert.rejects(pending,/timeout/);
  t.mock.timers.tick(7001);await rejected;assert(fake.sockets[0].destroyed);assert.equal(fake.datagrams.length,0);
});
test('a clean EOF during handshake rejects without resurrecting a UDP socket',async t=>{
  const fake=setup(t,{connect:false});const pending=tlsTurnTunnel(relay());
  const rejected=assert.rejects(pending,/closed/);fake.sockets[0].emit('end');await rejected;assert.equal(fake.datagrams.length,0);
});
test('bounded TLS send queue and malformed receive frame fail explicitly',async t=>{
  const fake=setup(t);let errors=0;const tunnel=await tlsTurnTunnel(relay(),()=>errors++);
  fake.sockets[0].writableLength=32*1024*1024;
  fake.datagrams[0].emit('message',packet(),{address:'127.0.0.1',port:41000});
  assert.equal(errors,1);assert(fake.datagrams[0].closed);tunnel.close();
  const second=await tlsTurnTunnel(relay(),()=>errors++);
  fake.sockets[1].emit('data',Buffer.from([0x80,0,0,0]));assert.equal(errors,2);assert(fake.datagrams[1].closed);second.close();
});
