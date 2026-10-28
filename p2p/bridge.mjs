// Native libdatachannel worker. Stdio is framed binary; secrets never use argv.
import rtc from 'node-datachannel';
const WINDOW=4*1024*1024, MESSAGE=32*1024;
rtc.initLogger('Fatal',()=>{});
rtc.setSctpSettings({recvBufferSize:16*1024*1024,sendBufferSize:8*1024*1024,maxChunksOnQueue:16384});
let pc,channel,closed=false,unacked=0,pendingOutput=0,wake=()=>{};
function frame(kind,body,done) {
  if(closed)return;
  const data=Buffer.isBuffer(body)?body:Buffer.from(JSON.stringify(body));
  const header=Buffer.allocUnsafe(5);header.writeUInt32BE(data.length+1);header[4]=kind.charCodeAt(0);
  process.stdout.write(Buffer.concat([header,data]),done);
}
const event=value=>frame('J',value);
function stop() {
  if(closed)return;closed=true;wake();
  try{channel?.close();pc?.close();rtc.cleanup();}catch{}
  process.exit(0);
}
function bind(dc) {
  if(channel)throw new Error('Unexpected extra data channel');
  channel=dc;
  dc.setBufferedAmountLowThreshold(WINDOW/2);
  dc.onBufferedAmountLow(()=>wake());
  dc.onOpen(()=>{
    const pair=pc.getSelectedCandidatePair();
    event({type:'open',route:pair?.local?.type==='relay'||pair?.remote?.type==='relay'?'relay':'direct',maxMessageSize:dc.maxMessageSize()});wake();
  });
  dc.onClosed(()=>{event({type:'closed'});stop();});
  dc.onError(()=>{event({type:'error',message:'WebRTC 数据通道中断'});stop();});
  dc.onMessage(message=>{
    if(typeof message==='string')throw new Error('Unexpected text channel message');
    const data=Buffer.from(message);
    if(data.length===5&&data[0]===2) {
      const count=data.readUInt32BE(1);
      if(count>unacked)throw new Error('Invalid credit');
      unacked-=count;wake();return;
    }
    if(data[0]!==1||data.length<2||data.length>MESSAGE+1)throw new Error('Invalid channel frame');
    const payload=data.subarray(1);
    pendingOutput+=payload.length;
    if(pendingOutput>WINDOW+MESSAGE)throw new Error('Peer exceeded receive credit');
    frame('B',payload,()=>{
      pendingOutput-=payload.length;
      if(!channel?.isOpen())return;
      const ack=Buffer.allocUnsafe(5);ack[0]=2;ack.writeUInt32BE(payload.length,1);channel.sendMessageBinary(ack);
    });
  });
}
async function control(value) {
  if(value.type==='init') {
    if(pc)throw new Error('Already initialized');
    pc=new rtc.PeerConnection('FlickDock',{iceServers:value.iceServers||[],maxMessageSize:65536,iceTransportPolicy:value.forceRelay?'relay':'all',enableIceTcp:true});
    pc.onLocalDescription((sdp,type)=>event({type:'description',sdp,descriptionType:type}));
    pc.onLocalCandidate((candidate,mid)=>event({type:'candidate',candidate,mid}));
    pc.onStateChange(state=>{event({type:'state',state});if(state==='failed')stop();});
    pc.onDataChannel(bind);
    if(value.initiator)bind(pc.createDataChannel('flickdock-file-v1',{protocol:'flickdock-file-v1'}));
  } else if(value.type==='description')pc.setRemoteDescription(value.sdp,value.descriptionType);
  else if(value.type==='candidate')pc.addRemoteCandidate(value.candidate,value.mid);
  else if(value.type==='close') {
    const deadline=Date.now()+3000;
    while(!closed&&unacked>0&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,5));
    stop();
  }
  else throw new Error('Unknown bridge control');
}
async function send(data) {
  for(let position=0;position<data.length;) {
    const size=Math.min(MESSAGE,data.length-position,Math.max(1,(channel?.maxMessageSize()||MESSAGE+1)-1));
    while(!closed&&(!channel?.isOpen()||unacked+size>WINDOW||channel.bufferedAmount()>WINDOW)) {
      await new Promise(resolve=>{wake=resolve;});
    }
    if(closed)return;
    const packet=Buffer.allocUnsafe(size+1);packet[0]=1;data.copy(packet,1,position,position+size);
    unacked+=size;channel.sendMessageBinary(packet);position+=size;
  }
}
process.on('uncaughtException',()=>{event({type:'error',message:'WebRTC 传输协议异常'});stop();});
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
      if(kind===74)await control(JSON.parse(body.toString('utf8')));
      else if(kind===66)await send(body);
      else throw new Error('Invalid IPC type');
    }
  }
}finally{stop();}
