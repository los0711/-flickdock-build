import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';
import {SocksClient} from 'socks';
import {proxyTunnel} from './proxy_tunnel.mjs';

async function server(t,status=200){
  const sockets=new Set(),requests=[];
  const proxy=http.createServer();
  proxy.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
  proxy.on('connect',(request,socket,head)=>{
    requests.push({path:request.url,headers:request.headers});assert.equal(head.length,0);
    socket.write(`HTTP/1.1 ${status} Result\r\n\r\n`);
    if(status===200)socket.on('data',data=>socket.write(data));else socket.end();
  });
  await new Promise(resolve=>proxy.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{for(const socket of sockets)socket.destroy();await new Promise(resolve=>proxy.close(resolve));});
  return {proxy,requests,config:{kind:'http',host:'127.0.0.1',port:proxy.address().port,username:'fixture',password:'secret'}};
}

test('actual HTTP CONNECT tunnel carries original binary bytes and scopes proxy auth',async t=>{
  const s=await server(t);const socket=await proxyTunnel(s.config,443);
  const original=Buffer.alloc(256*1024,0x6b),chunks=[];let received=0;
  try{
    await new Promise((resolve,reject)=>{
      socket.on('error',reject);socket.on('data',data=>{chunks.push(data);received+=data.length;if(received===original.length)resolve();});
      socket.write(original);
    });
    assert.deepEqual(Buffer.concat(chunks),original);assert.equal(s.requests[0].path,'turn.cloudflare.com:443');
    assert.equal(s.requests[0].headers['proxy-authorization'],'Basic '+Buffer.from('fixture:secret').toString('base64'));
  }finally{socket.destroy();}
});

test('proxy refusal never becomes an accepted TURN socket',async t=>{
  const s=await server(t,407);await assert.rejects(proxyTunnel(s.config,443),/rejected/);
});

test('SOCKS delegates remote DNS and bounded authenticated CONNECT to the library',async t=>{
  const socket={setNoDelay:()=>{}};
  t.mock.method(SocksClient,'createConnection',async options=>{
    assert.equal(options.command,'connect');assert.equal(options.timeout,7000);
    assert.deepEqual(options.destination,{host:'turn.cloudflare.com',port:5349});
    assert.equal(options.proxy.type,5);assert.equal(options.proxy.userId,'fixture');return {socket};
  });
  assert.equal(await proxyTunnel({kind:'socks5',host:'127.0.0.1',port:7890,username:'fixture',password:'secret'},5349),socket);
});

test('invalid proxy and destination are rejected before I/O',async()=>{
  for(const config of [null,{kind:'http',host:'127.0.0.1',port:0,username:'',password:''}])
    await assert.rejects(proxyTunnel(config,443),/Invalid/);
  await assert.rejects(proxyTunnel({kind:'http',host:'127.0.0.1',port:7890,username:'',password:''},80),/Invalid/);
});
