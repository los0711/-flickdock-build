import rtc from 'node-datachannel';

const PROBE_TIMEOUT_MS=1000;
const CLOUD_STUN='stun:stun.cloudflare.com:3478';
const SECONDARY_STUN='stun:stun.l.google.com:19302';

export async function selectStun(servers,preferred=null){
  if(!servers.some(row=>typeof row==='string'&&row.startsWith('stun:')))return {servers,selected:null};
  const replace=url=>({servers:[url,...servers.filter(row=>typeof row!=='string'||!row.startsWith('stun:'))],selected:url});
  if(preferred===CLOUD_STUN||preferred===SECONDARY_STUN)return replace(preferred);
  const peers=[];
  let timer;
  try{
    const selected=await new Promise((resolve,reject)=>{
      let finished=false,completed=0;
      const finish=url=>{if(!finished){finished=true;clearTimeout(timer);resolve(url);}};
      timer=setTimeout(()=>finish(CLOUD_STUN),PROBE_TIMEOUT_MS);
      for(const server of [CLOUD_STUN,SECONDARY_STUN]){
        try{
          const peer=new rtc.PeerConnection('FlickDock-STUN',{iceServers:[server],maxMessageSize:131072});peers.push(peer);
          peer.onLocalCandidate(candidate=>{if(/\btyp srflx\b/.test(candidate))finish(server);});
          peer.onGatheringStateChange(state=>{if(state==='complete'&&++completed===2)finish(CLOUD_STUN);});
          peer.createDataChannel('stun-probe');
        }catch(error){clearTimeout(timer);reject(error);return;}
      }
    });
    return replace(selected);
  }finally{
    clearTimeout(timer);
    for(const peer of peers)peer.close();
  }
}
