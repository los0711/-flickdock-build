// Native libdatachannel worker. Stdio is framed binary; secrets never use argv.
import rtc from 'node-datachannel';
import {randomBytes} from 'node:crypto';
const LEGACY_WINDOW=4*1024*1024, LEGACY_MESSAGE=32*1024;
const WINDOW=16*1024*1024, MESSAGE=64*1024;
rtc.initLogger('Fatal',()=>{});
rtc.setSctpSettings({recvBufferSize:32*1024*1024,sendBufferSize:16*1024*1024,maxChunksOnQueue:16384});
let pc,channel,closed=false,unacked=0,pendingOutput=0,wake=()=>{},statsTimer;
let sendWindow=LEGACY_WINDOW,sendMessage=LEGACY_MESSAGE,credit=0,creditTimer;
const flowNonce=randomBytes(16).toString('hex');
function frame(kind,body,done) {
  if(closed)return;
  const data=Buffer.isBuffer(body)?body:Buffer.from(JSON.stringify(body));
  const header=Buffer.allocUnsafe(5);header.writeUInt32BE(data.length+1);header[4]=kind.charCodeAt(0);
  process.stdout.write(Buffer.concat([header,data]),done);
}
const event=value=>frame('J',value);
function stats() {
  if(!pc||closed)return;
  event({type:'stats',sent:pc.bytesSent(),received:pc.bytesReceived()});
}
function stop() {
  if(closed)return;stats();closed=true;wake();clearTimeout(creditTimer);clearInterval(statsTimer);
  try{channel?.close();pc?.close();rtc.cleanup();}catch{}
  process.stdout.end(()=>process.exit(0));
}
function acknowledge() {
  clearTimeout(creditTimer);creditTimer=undefined;
  if(!credit||!channel?.isOpen())return;
  const ack=Buffer.allocUnsafe(5);ack[0]=2;ack.writeUInt32BE(credit,1);
  channel.sendMessageBinary(ack);credit=0;
}
function bind(dc) {
  if(channel)throw new Error('Unexpected extra data channel');
  channel=dc;
  dc.setBufferedAmountLowThreshold(LEGACY_WINDOW/2);
  dc.onBufferedAmountLow(()=>wake());
  dc.onOpen(()=>{
    const pair=pc.getSelectedCandidatePair();
    event({type:'open',route:pair?.local?.type==='relay'||pair?.remote?.type==='relay'?'relay':'direct',maxMessageSize:dc.maxMessageSize()});wake();
    statsTimer=setInterval(stats,250);
  });
  dc.onClosed(()=>{event({type:'closed'});stop();});
  dc.onError(()=>{event({type:'error',message:'WebRTC 数据通道中断'});stop();});
  dc.onMessage(message=>{
    if(typeof message==='string')throw new Error('Unexpected text channel message');
    const data=Buffer.isBuffer(message)?message:Buffer.from(message);
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
      credit+=payload.length;
      if(credit>=256*1024)acknowledge();
      else if(!creditTimer)creditTimer=setTimeout(acknowledge,2);
    });
  });
}
async function control(value) {
  if(value.type==='init') {
    if(pc)throw new Error('Already initialized');
    pc=new rtc.PeerConnection('FlickDock',{iceServers:value.iceServers||[],maxMessageSize:131072,iceTransportPolicy:value.forceRelay?'relay':'all',enableIceTcp:true});
    // Unknown SDP attributes are ignored by older clients. Start conservatively
    // and enlarge the bounded credit/message window only for an agreeing peer.
    pc.onLocalDescription((sdp,type)=>event({type:'description',sdp:sdp.replace(/^a=x-flickdock-flow:[^\r\n]*\r?\n?/gm,'').trimEnd()+`\r\na=x-flickdock-flow:2 ${flowNonce} ${WINDOW} ${MESSAGE}\r\n`,descriptionType:type}));
    pc.onLocalCandidate((candidate,mid)=>event({type:'candidate',candidate,mid}));
    pc.onStateChange(state=>{event({type:'state',state});if(state==='failed')stop();});
    pc.onDataChannel(bind);
    if(value.initiator)bind(pc.createDataChannel('flickdock-file-v1',{protocol:'flickdock-file-v1'}));
  } else if(value.type==='description') {
    // libdatachannel v1 peers echo unknown offer attributes in their answer.
    // Each endpoint must advertise its own nonce; an echo cannot opt in.
    const flow=/^a=x-flickdock-flow:2 ([a-f0-9]{32}) ([0-9]+) ([0-9]+)\r?$/m.exec(value.sdp);
    if(flow&&flow[1]!==flowNonce) {
      const window=Number(flow[2]),message=Number(flow[3]);
      if(Number.isSafeInteger(window)&&window>=LEGACY_WINDOW&&window<=WINDOW&&Number.isSafeInteger(message)&&message>=LEGACY_MESSAGE&&message<=MESSAGE) {
        sendWindow=window;sendMessage=message;
      }
    }
    pc.setRemoteDescription(value.sdp,value.descriptionType);
  }
  else if(value.type==='candidate')pc.addRemoteCandidate(value.candidate,value.mid);
  else if(value.type==='stats')stats();
  else if(value.type==='close') {
    const deadline=Date.now()+3000;
    while(!closed&&unacked>0&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,5));
    stop();
  }
  else throw new Error('Unknown bridge control');
}
async function send(data) {
  for(let position=0;position<data.length;) {
    const size=Math.min(sendMessage,data.length-position,Math.max(1,(channel?.maxMessageSize()||sendMessage+1)-1));
    while(!closed&&(!channel?.isOpen()||unacked+size>sendWindow||channel.bufferedAmount()>sendWindow)) {
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
