// Exercise the worker's real callbacks with only native/network boundaries injected.
import assert from 'node:assert/strict';
import {registerHooks} from 'node:module';
const state={pair:{local:{type:'host'},remote:{type:'srflx'}},closed:0,events:[]};
globalThis.rtcLifecycleFixture=state;
class Channel{
  setBufferedAmountLowThreshold(){} onBufferedAmountLow(){} onOpen(cb){this.open=cb;}
  onClosed(cb){this.closed=cb;} onError(){} onMessage(){} maxMessageSize(){return 65536;}
  isOpen(){return true;} close(){state.closed++;} sendMessageBinary(){}
}
class Peer{
  constructor(){state.channel=new Channel();}
  getSelectedCandidatePair(){return state.pair;} bytesSent(){return 0;} bytesReceived(){return 0;}
  onLocalDescription(){} onLocalCandidate(){} onStateChange(){} onDataChannel(){}
  createDataChannel(){return state.channel;} close(){state.closed++;}
}
state.rtc={initLogger(){},setSctpSettings(){},PeerConnection:Peer,cleanup(){}};
registerHooks({
  resolve(specifier,context,next){
    if(specifier==='node-datachannel')return {url:'fixture:rtc',shortCircuit:true};
    if(specifier==='./turn_tls.mjs')return {url:'fixture:turn',shortCircuit:true};
    return next(specifier,context);
  },
  load(url,context,next){
    if(url==='fixture:rtc')return {format:'module',source:'export default globalThis.rtcLifecycleFixture.rtc;',shortCircuit:true};
    if(url==='fixture:turn')return {format:'module',source:'export async function adaptTurnServers(servers,fail){globalThis.rtcLifecycleFixture.fail=fail;return {iceServers:servers,close(){}};}',shortCircuit:true};
    return next(url,context);
  }
});
const {createPeer}=await import('./rtc-peer.mjs');
const peer=createPeer(value=>state.events.push(value),()=>{},()=>{state.stopped=true;});
await peer.control({type:'init',initiator:true,iceServers:['stun:stun.cloudflare.com:3478']});
state.channel.open();state.fail();assert.equal(state.closed,0);assert.equal(state.stopped,undefined);
assert(state.events.some(event=>event.code==='unused-relay-unavailable'));
state.pair={local:{type:'relay'},remote:{type:'host'}};state.fail();
assert(state.closed>0&&state.stopped);assert(state.events.some(event=>event.type==='error'));
delete globalThis.rtcLifecycleFixture;
console.log('PASS unused TURN failure preserves a direct channel; active relay failure closes once');
