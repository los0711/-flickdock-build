// Actual bridge module with owned stdio and deterministic native callbacks.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {EventEmitter} from 'node:events';
import {PassThrough,Writable} from 'node:stream';
import test from 'node:test';
import vm from 'node:vm';

const source=await readFile(new URL('./bridge.mjs',import.meta.url),'utf8');
const wait=()=>new Promise(resolve=>setTimeout(resolve,10));
function ipc(value){
  const body=Buffer.from(JSON.stringify(value)),header=Buffer.alloc(5);
  header.writeUInt32BE(body.length+1);header[4]=74;return Buffer.concat([header,body]);
}
async function fixture(route,open=true){
  const owned=new EventEmitter(),input=new PassThrough(),events=[];
  let pending=Buffer.alloc(0),exited=false,tunnelFailed;
  let pair={local:{type:route==='relay'?'relay':'host'},remote:{type:'host'}};
  const channel={isOpen:()=>open,maxMessageSize:()=>131072,bufferedAmount:()=>0,
    close(){},setBufferedAmountLowThreshold(){},onBufferedAmountLow(){},
    onOpen(fn){if(open)queueMicrotask(fn);},onClosed(){},onError(){},onMessage(){},sendMessageBinary(){return true;}};
  class Peer{
    createDataChannel(){return channel;}
    getSelectedCandidatePair(){return pair;}
    bytesSent(){return 0;}
    bytesReceived(){return 0;}
    onLocalDescription(){} onLocalCandidate(){} onStateChange(){} onDataChannel(){} close(){}
  }
  owned.stdin=input;owned.exit=()=>{exited=true;};
  owned.stdout=new Writable({write(chunk,_encoding,done){
    pending=Buffer.concat([pending,chunk]);
    while(pending.length>=5&&pending.length>=pending.readUInt32BE()+4){
      const size=pending.readUInt32BE();
      if(pending[4]===74)events.push(JSON.parse(pending.subarray(5,size+4)));
      pending=pending.subarray(size+4);
    }
    done();
  }});
  const context=vm.createContext({Buffer,process:owned,console,setTimeout,clearTimeout,setInterval,clearInterval});
  const exports=new Map([
    ['node-datachannel',{default:{initLogger(){},setSctpSettings(){},PeerConnection:Peer,cleanup(){}}}],
    ['node:crypto',{randomBytes:size=>Buffer.alloc(size,1)}],
    ['./turn_tls.mjs',{adaptTurnServers:async(_servers,failed)=>{
      tunnelFailed=failed;return {iceServers:[],close(){},relayTransport:'tls'};
    }}]
  ]);
  const bridge=new vm.SourceTextModule(source,{context});
  await bridge.link(name=>{
    const values=exports.get(name);assert.ok(values,'Unexpected production bridge import');
    return new vm.SyntheticModule(Object.keys(values),function(){
      for(const [key,value] of Object.entries(values))this.setExport(key,value);
    },{context});
  });
  const evaluation=bridge.evaluate();
  input.write(ipc({type:'init',initiator:true}));await wait();
  return {events,exited:()=>exited,fail:()=>tunnelFailed(),
    selectRelay(){pair={local:{type:'relay'},remote:{type:'host'}};},
    async close(){owned.emit('SIGTERM');input.end();await evaluation;}};
}

test('failure of unused TURN tunnel preserves the established P2P connection',async()=>{
  const peer=await fixture('direct');
  try{
    assert.ok(peer.events.some(row=>row.type==='open'&&row.route==='direct'));
    peer.fail();await wait();
    assert.equal(peer.exited(),false,'Unused relay loss must not close direct DTLS');
    assert.equal(peer.events.filter(row=>row.type==='error').length,0);
  }finally{await peer.close();}
});
test('failed active TURN tunnel still stops and reports the connection',async()=>{
  const peer=await fixture('relay');
  try{peer.fail();await wait();assert.equal(peer.exited(),true);assert.ok(peer.events.some(row=>row.type==='error'));}
  finally{await peer.close();}
});
test('TURN failure before a channel is established remains a real failure',async()=>{
  const peer=await fixture('direct',false);
  try{peer.fail();await wait();assert.equal(peer.exited(),true);assert.ok(peer.events.some(row=>row.type==='error'));}
  finally{await peer.close();}
});
test('failure decision uses the current selected pair rather than the initial route',async()=>{
  const peer=await fixture('direct');
  try{peer.selectRelay();peer.fail();await wait();assert.equal(peer.exited(),true);}
  finally{await peer.close();}
});
