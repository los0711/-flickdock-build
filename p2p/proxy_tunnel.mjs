import http from 'node:http';
import {SocksClient} from 'socks';

const CONNECT_TIMEOUT_MS=7000;

export async function proxyTunnel(proxy,port){
  if(!proxy||!['http','socks5'].includes(proxy.kind)||typeof proxy.host!=='string'||!proxy.host||proxy.host.length>255||/[\s\0]/.test(proxy.host)
      ||!Number.isInteger(proxy.port)||proxy.port<1||proxy.port>65535||![443,5349].includes(port)
      ||!['username','password'].every(key=>typeof proxy[key]==='string'&&proxy[key].length<=2048))throw Error('Invalid relay proxy');
  if(proxy.kind==='socks5'){
    const {socket}=await SocksClient.createConnection({command:'connect',timeout:CONNECT_TIMEOUT_MS,
      proxy:{host:proxy.host,port:proxy.port,type:5,userId:proxy.username,password:proxy.password},
      destination:{host:'turn.cloudflare.com',port}});
    socket.setNoDelay(true);return socket;
  }
  return new Promise((resolve,reject)=>{
    let finished=false;
    const headers={Host:`turn.cloudflare.com:${port}`};
    if(proxy.username||proxy.password)headers['Proxy-Authorization']='Basic '+Buffer.from(proxy.username+':'+proxy.password).toString('base64');
    const request=http.request({host:proxy.host,port:proxy.port,method:'CONNECT',path:`turn.cloudflare.com:${port}`,headers,agent:false,maxHeaderSize:16384});
    const timer=setTimeout(()=>{request.destroy(Error('Relay proxy timeout'));},CONNECT_TIMEOUT_MS);
    request.once('error',()=>{if(!finished){finished=true;clearTimeout(timer);reject(Error('Relay proxy connection failed'));}});
    request.once('connect',(response,socket,head)=>{
      if(finished){socket.destroy();return;}finished=true;clearTimeout(timer);
      if(response.statusCode!==200||head.length){socket.destroy();reject(Error('Relay proxy rejected connection'));return;}
      socket.setNoDelay(true);resolve(socket);
    });
    request.end();
  });
}
