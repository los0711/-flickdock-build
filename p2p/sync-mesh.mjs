// All authenticated account peers share one native runtime and bounded IPC.
import {createPeer,cleanup} from './rtc-peer.mjs';
const peers=new Map(),MAX_PEERS=38,MAX_PENDING_BYTES=4*1024*1024;
let closed=false;
function frame(kind,body,done) {
  if(closed)return;
  const data=Buffer.isBuffer(body)?body:Buffer.from(JSON.stringify(body));
  const header=Buffer.allocUnsafe(5);header.writeUInt32BE(data.length+1);header[4]=kind.charCodeAt(0);
  process.stdout.write(Buffer.concat([header,data]),done);
}
function failure(id,error) {
  frame('J',{id,event:{type:'error',message:String(error.message||error).slice(0,160)}});
  peers.get(id)?.peer.stop();
}
function stop() {
  if(closed)return;
  for(const row of peers.values())row.peer.stop();
  peers.clear();closed=true;cleanup();process.stdout.end(()=>process.exit(0));
}
function control(value) {
  if(value.type==='shutdown'){stop();return;}
  const id=value.id;
  if(typeof id!=='string'||!/^[a-f0-9]{32}$/.test(id))throw new Error('Invalid peer identity');
  let row=peers.get(id);
  if(value.type==='init') {
    if(row||peers.size>=MAX_PEERS)throw new Error('Invalid peer initialization');
    const peer=createPeer(event=>frame('J',{id,event}),(data,done)=>frame('B',Buffer.concat([Buffer.from(id,'hex'),data]),done),()=>{
      peers.delete(id);frame('J',{id,event:{type:'closed'}});
    });
    row={peer,ready:null,writes:Promise.resolve(),pending:0};peers.set(id,row);
    row.ready=peer.control(value).catch(error=>failure(id,error));
  } else if(row)row.ready.then(()=>row.peer.control(value)).catch(error=>failure(id,error));
}
process.on('uncaughtException',error=>{process.stderr.write('Native mesh exception: '+String(error.message||error)+'\n');stop();});process.on('SIGTERM',stop);
try {
  let input=Buffer.alloc(0);
  for await(const chunk of process.stdin) {
    input=Buffer.concat([input,chunk]);
    while(input.length>=4) {
      const length=input.readUInt32BE();
      if(length<1||length>131089)throw new Error('Invalid mesh IPC frame');
      if(input.length<length+4)break;
      const kind=input[4],body=input.subarray(5,4+length);input=input.subarray(4+length);
      if(kind===74)control(JSON.parse(body.toString('utf8')));
      else if(kind===66&&body.length>16) {
        const id=body.subarray(0,16).toString('hex'),row=peers.get(id);
        if(!row)continue;
        const data=Buffer.from(body.subarray(16));row.pending+=data.length;
        if(row.pending>MAX_PENDING_BYTES){failure(id,new Error('Peer send queue exceeded'));continue;}
        row.writes=row.writes.then(()=>row.ready).then(()=>row.peer.send(data))
          .catch(error=>failure(id,error)).finally(()=>{row.pending-=data.length;});
      } else throw new Error('Invalid mesh IPC packet');
    }
  }
}finally{stop();}
