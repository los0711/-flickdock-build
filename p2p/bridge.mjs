// One file-transfer stream using the shared native peer implementation.
import {createPeer,cleanup} from './rtc-peer.mjs';
let closed=false;
function frame(kind,body,done) {
  if(closed)return;
  const data=Buffer.isBuffer(body)?body:Buffer.from(JSON.stringify(body));
  const header=Buffer.allocUnsafe(5);header.writeUInt32BE(data.length+1);header[4]=kind.charCodeAt(0);
  process.stdout.write(Buffer.concat([header,data]),done);
}
const peer=createPeer(value=>frame('J',value),(body,done)=>frame('B',body,done),stop);
function stop() {
  if(closed)return;closed=true;peer.stop();cleanup();process.stdout.end(()=>process.exit(0));
}
process.on('uncaughtException',()=>{frame('J',{type:'error',message:'WebRTC 传输协议异常'});stop();});
process.on('SIGTERM',stop);
try {
  let input=Buffer.alloc(0);
  for await(const chunk of process.stdin) {
    input=Buffer.concat([input,chunk]);
    while(input.length>=4) {
      const length=input.readUInt32BE();
      if(length<1||length>131073)throw new Error('Invalid IPC frame');
      if(input.length<length+4)break;
      const kind=input[4],body=input.subarray(5,4+length);input=input.subarray(4+length);
      if(kind===74)await peer.control(JSON.parse(body.toString('utf8')));
      else if(kind===66)await peer.send(body);
      else throw new Error('Invalid IPC type');
    }
  }
}finally{stop();}
