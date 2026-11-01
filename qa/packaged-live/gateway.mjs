/** Hosted-only TLS gateway to actual LOCAL workerd. Never sends to public cloud. */
import https from 'node:https';
import http from 'node:http';
import {readFile,writeFile} from 'node:fs/promises';

if(process.env.GITHUB_ACTIONS!=='true'||process.env.RUNNER_OS!=='Windows')throw Error('Ephemeral Windows CI only');
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const origin=new URL(config.origin);
if(origin.protocol!=='http:'||origin.hostname!=='127.0.0.1')throw Error('Upstream must be local workerd');
const rows=[],held=[];let hold=false;
function route(path){return path.split('?')[0].replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/gi,':id');}
function localTarget(path){
  const incoming=new URL(path,'https://flickdock-cloud.roger000416.workers.dev');
  const target=new URL(origin);target.pathname=incoming.pathname;target.search=incoming.search;return target;
}
function forward(req,res,row){
  if(req.destroyed){row.cancelled=true;return;}
  const upstream=http.request(localTarget(req.url),{method:req.method,headers:req.headers},answer=>{
    row.status=answer.statusCode;row.first_byte=performance.now();
    res.writeHead(answer.statusCode,answer.headers);answer.pipe(res);
    answer.on('end',()=>{row.finished=performance.now();});
  });
  upstream.on('error',()=>{row.error='LOCAL_UPSTREAM';if(!res.headersSent)res.writeHead(502);res.end();});
  res.on('close',()=>{if(!res.writableEnded)upstream.destroy();});
  req.pipe(upstream);req.resume();
}
const gateway=https.createServer({key:await readFile(config.key),cert:await readFile(config.cert)},(req,res)=>{
  const row={index:rows.length,method:req.method,path:route(req.url),started:performance.now()};rows.push(row);
  if(hold&&req.method==='PUT'&&req.url.startsWith('/v1/clipboard/')){
    if(held.length>=4){row.status=503;res.writeHead(503);res.end();return;}
    row.held=true;req.pause();held.push([req,res,row]);return;
  }
  forward(req,res,row);
});
gateway.on('upgrade',(req,socket,head)=>{
  const request=http.request(localTarget(req.url),{method:req.method,headers:req.headers});
  request.on('upgrade',(res,upstream,upstreamHead)=>{
    socket.write('HTTP/1.1 101 Switching Protocols\r\n'+Object.entries(res.headers).map(([k,v])=>`${k}: ${v}\r\n`).join('')+'\r\n');
    if(head.length)upstream.write(head);if(upstreamHead.length)socket.write(upstreamHead);
    socket.pipe(upstream);upstream.pipe(socket);
    socket.on('error',()=>upstream.destroy());upstream.on('error',()=>socket.destroy());
    socket.on('close',()=>upstream.destroy());upstream.on('close',()=>socket.destroy());
  });
  request.on('response',res=>{socket.end('HTTP/1.1 '+res.statusCode+' Rejected\r\nContent-Length: 0\r\n\r\n');});
  request.on('error',()=>socket.destroy());request.end();
});
const control=http.createServer(async(req,res)=>{
  if(req.url==='/stats'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({rows,held:held.length}));return;}
  if(req.url==='/hold'){hold=true;res.end('ok');return;}
  if(req.url==='/release'){
    hold=false;for(const [request,response,row] of held.splice(0))forward(request,response,row);res.end('ok');return;
  }
  res.writeHead(404);res.end();
});
await new Promise((resolve,reject)=>gateway.once('error',reject).listen(443,'127.0.0.1',resolve));
await new Promise(resolve=>control.listen(0,'127.0.0.1',resolve));
await writeFile(config.ready,JSON.stringify({control:'http://127.0.0.1:'+control.address().port}));
process.stdout.write('LOCAL_TLS_READY\n');
