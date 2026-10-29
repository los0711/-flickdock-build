// Verified alternative cloud entrance. Transfer payloads remain peer to peer.
const backend='https://flickdock-cloud.roger000416.workers.dev';
export default {
  async fetch(request,env){
    const path=new URL(request.url).pathname;
    if(path.startsWith('/v1/')){
      if(!['GET','POST','PUT','DELETE','OPTIONS'].includes(request.method))return new Response('Method not allowed',{status:405});
      if(path==='/v1/wan/connect'&&request.headers.get('Upgrade')?.toLowerCase()!=='websocket')return new Response('WebSocket required',{status:400});
      // No arbitrary upstream URL, cookie forwarding or public proxy route.
      const headers=new Headers(request.headers);headers.delete('Cookie');headers.delete('Referer');
      headers.set('X-FlickDock-Client-IP',request.headers.get('CF-Connecting-IP')||'');
      return env.WAN_SERVICE.fetch(new Request(backend+path+new URL(request.url).search,{method:request.method,headers,body:['GET','HEAD'].includes(request.method)?undefined:request.body,duplex:'half'}));
    }
    return env.ASSETS.fetch(request);
  },
};
