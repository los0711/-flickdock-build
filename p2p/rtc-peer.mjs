// Native libdatachannel worker. Stdio is framed binary; secrets never use argv.
import rtc from 'node-datachannel';
import {randomBytes} from 'node:crypto';
import {adaptTurnServers} from './turn_tls.mjs';
const LEGACY_WINDOW=4*1024*1024, LEGACY_MESSAGE=32*1024;
const WINDOW=16*1024*1024, MESSAGE=64*1024;
const MAX_PENDING_CANDIDATES=64;
rtc.initLogger('Fatal',()=>{});
rtc.setSctpSettings({recvBufferSize:32*1024*1024,sendBufferSize:16*1024*1024,maxChunksOnQueue:16384});

export const cleanup=()=>rtc.cleanup();
export function createPeer(outputEvent,outputBytes,onClose) {
let pc,channel,turnTransport,closed=false,unacked=0,acknowledged=0,pendingOutput=0,wake=()=>{},statsTimer;
let sendWindow=LEGACY_WINDOW,sendMessage=LEGACY_MESSAGE,credit=0,creditTimer;
const flowNonce=randomBytes(16).toString('hex');
const nearbyNonce=randomBytes(16).toString('hex');
const routesNonce=randomBytes(16).toString('hex');
let nearbyCapable=false,adaptiveCapable=false,lastRoute='';
let remoteReady=false;
const pendingCandidates=[];
function frame(kind,body,done) {
  if(closed)return;
  if(kind==='B')outputBytes(body,done);else outputEvent(body);
}
const event=value=>frame('J',value);
function stats() {
  if(!pc||closed)return;
  const pair=pc.getSelectedCandidatePair();
  if(pair?.local&&pair?.remote){
    const route=pair.local.type==='relay'||pair.remote.type==='relay'?'relay':'direct';
    if(lastRoute&&lastRoute!==route)event({type:'path',route,localType:pair.local.type,remoteType:pair.remote.type});
    lastRoute=route;
  }
  event({type:'stats',sent:pc.bytesSent(),received:pc.bytesReceived(),delivered:acknowledged});
}
function stop() {
  if(closed)return;stats();closed=true;wake();clearTimeout(creditTimer);clearInterval(statsTimer);
  pendingCandidates.length=0;
  turnTransport?.close();
  try{channel?.close();pc?.close();}catch{}
  onClose();
}
function acknowledge() {
  clearTimeout(creditTimer);creditTimer=undefined;
  if(!credit||!channel?.isOpen())return;
  const ack=Buffer.allocUnsafe(5);ack[0]=2;ack.writeUInt32BE(credit,1);
  channel.sendMessageBinary(ack);credit=0;
}
function guarded(callback) {
  return (...args)=>{
    if(closed)return;
    try{return callback(...args);}
    catch(error){event({type:'error',message:String(error.message||error).slice(0,160)});stop();}
  };
}
function bind(dc) {
  if(channel)throw new Error('Unexpected extra data channel');
  channel=dc;
  dc.setBufferedAmountLowThreshold(LEGACY_WINDOW/2);
  dc.onBufferedAmountLow(()=>wake());
  dc.onOpen(()=>{
    const pair=pc.getSelectedCandidatePair();
    lastRoute=pair?.local?.type==='relay'||pair?.remote?.type==='relay'?'relay':'direct';
    event({type:'open',route:pair?.local?.type==='relay'||pair?.remote?.type==='relay'?'relay':'direct',maxMessageSize:dc.maxMessageSize(),adaptive:adaptiveCapable,
      nearby:{capable:nearbyCapable&&!!pair?.local&&!!pair?.remote&&pair.local.type!=='relay'&&pair.remote.type!=='relay',local:pair?.local?.address,remote:pair?.remote?.address}});wake();
    statsTimer=setInterval(stats,250);
  });
  dc.onClosed(()=>{event({type:'closed'});stop();});
  dc.onError(()=>{event({type:'error',message:'WebRTC 数据通道中断'});stop();});
  dc.onMessage(guarded(message=>{
    if(typeof message==='string')throw new Error('Unexpected text channel message');
    const data=Buffer.isBuffer(message)?message:Buffer.from(message);
    if(data[0]===3&&nearbyCapable&&data.length>1&&data.length<=1025){event({type:'nearby',data:JSON.parse(data.subarray(1).toString('utf8'))});return;}
    if(data.length===5&&data[0]===2) {
      const count=data.readUInt32BE(1);
      if(count>unacked)throw new Error('Invalid credit');
      unacked-=count;acknowledged+=count;wake();return;
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
  }));
}
async function control(value) {
  if(value.type==='init') {
    if(pc)throw new Error('Already initialized');
    turnTransport=await adaptTurnServers(value.iceServers||[],()=>{
      if(closed)return;
      const pair=pc?.getSelectedCandidatePair();
      if(pair?.local?.type==='relay'||pair?.remote?.type==='relay'){
        event({type:'error',message:'中继连接中断，正在尝试恢复。'});stop();
      }else event({type:'diagnostic',code:'unused-relay-unavailable'});
    },value.preferTls!==false,value.proxies||{});
    if(closed){turnTransport.close();return;}
    event({type:'transport',relayTransport:turnTransport.relayTransport,tlsFallback:!!turnTransport.tlsFallback,proxyUsed:!!turnTransport.proxyUsed});
    pc=new rtc.PeerConnection('FlickDock',{iceServers:turnTransport.iceServers,maxMessageSize:131072,iceTransportPolicy:value.forceRelay?'relay':'all',enableIceTcp:true});
    // Unknown SDP attributes are ignored by older clients. Start conservatively
    // and enlarge the bounded credit/message window only for an agreeing peer.
    pc.onLocalDescription((sdp,type)=>event({type:'description',sdp:sdp.replace(/^a=x-flickdock-(?:flow|nearby|routes):[^\r\n]*\r?\n?/gm,'').trimEnd()+`\r\na=x-flickdock-flow:2 ${flowNonce} ${WINDOW} ${MESSAGE}\r\na=x-flickdock-nearby:1 ${nearbyNonce}\r\na=x-flickdock-routes:1 ${routesNonce}\r\n`,descriptionType:type}));
    pc.onLocalCandidate((candidate,mid)=>event({type:'candidate',candidate,mid}));
    pc.onStateChange(state=>{event({type:'state',state});if(state==='failed')stop();});
    pc.onDataChannel(guarded(bind));
    if(value.initiator)bind(pc.createDataChannel('flickdock-file-v1',{protocol:'flickdock-file-v1'}));
  } else if(value.type==='description') {
    // libdatachannel v1 peers echo unknown offer attributes in their answer.
    // Each endpoint must advertise its own nonce; an echo cannot opt in.
    const flow=/^a=x-flickdock-flow:2 ([a-f0-9]{32}) ([0-9]+) ([0-9]+)\r?$/m.exec(value.sdp);
    const nearby=/^a=x-flickdock-nearby:1 ([a-f0-9]{32})\r?$/m.exec(value.sdp);
    const routes=/^a=x-flickdock-routes:1 ([a-f0-9]{32})\r?$/m.exec(value.sdp);
    nearbyCapable=!!nearby&&nearby[1]!==nearbyNonce;
    adaptiveCapable=!!routes&&routes[1]!==routesNonce;
    event({type:'capabilities',adaptive:adaptiveCapable});
    if(flow&&flow[1]!==flowNonce) {
      const window=Number(flow[2]),message=Number(flow[3]);
      if(Number.isSafeInteger(window)&&window>=LEGACY_WINDOW&&window<=WINDOW&&Number.isSafeInteger(message)&&message>=LEGACY_MESSAGE&&message<=MESSAGE) {
        sendWindow=window;sendMessage=message;
      }
    }
    pc.setRemoteDescription(value.sdp,value.descriptionType);
    remoteReady=true;
    // Trickle ICE may overtake the SDP through asynchronous signaling. The
    // native API requires a remote description before accepting candidates.
    for(const candidate of pendingCandidates)pc.addRemoteCandidate(candidate.candidate,candidate.mid);
    pendingCandidates.length=0;
  }
  else if(value.type==='candidate'){
    if(remoteReady)pc.addRemoteCandidate(value.candidate,value.mid);
    else{
      if(pendingCandidates.length>=MAX_PENDING_CANDIDATES)throw new Error('Too many pending ICE candidates');
      pendingCandidates.push({candidate:value.candidate,mid:value.mid});
    }
  }
  else if(value.type==='stats')stats();
  else if(value.type==='nearby'){
    if(!nearbyCapable||!channel?.isOpen())throw new Error('Nearby capability unavailable');
    const body=Buffer.from(JSON.stringify(value.data));if(body.length>1024)throw new Error('Nearby control too large');
    channel.sendMessageBinary(Buffer.concat([Buffer.from([3]),body]));
  }
  else if(value.type==='abort')stop();
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

return {control,send,stop};
}
