import {DurableObject} from 'cloudflare:workers';
import {relayUsage,relayBudget,type RelayBudgetEnv} from './relay-budget';
type RelayGate={epoch:number;blocked:boolean};
type Credential={username:string;expires:number;revoke_pending?:boolean};
type Device={id:string;name:string;platform:string;online?:boolean;last?:number};
type Seat=Device&{ip:string;connected:number;window:number;messages:number};
type Session={id:string;sender:Device;receiver:Device;token:string;digest:string;total:string;count:number;names:string[];accepted:boolean;generation:number;expires:number;done:string[];connected?:string[]};
export interface WanEnv extends RelayBudgetEnv {WAN:DurableObjectNamespace<WanHub>;TURN_KEY_ID?:string;TURN_KEY_TOKEN?:string;TURN_ENABLED?:string;}
const json=(value:object,status=200)=>Response.json({schema_version:1,...value},{status,headers:{'Cache-Control':'no-store'}});
const hash=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))),b=>b.toString(16).padStart(2,'0')).join('');
const check=(value:unknown,pattern:RegExp):value is string=>typeof value==='string'&&pattern.test(value);
const ID=/^[a-f0-9]{64}$/,TRANSFER=/^[a-f0-9]{32}$/;
const stun=[{urls:['stun:stun.cloudflare.com:3478']}];
function publicDevice(value:Device):Device{return {id:value.id,name:value.name,platform:value.platform};}

export async function wanRoute(request:Request,env:WanEnv):Promise<Response>{
  const url=new URL(request.url);
  if(url.pathname==='/v1/wan/health')return json({service:'flickdock-wan',relay:env.TURN_ENABLED==='true'});
  if(url.pathname!=='/v1/wan/connect'||request.method!=='GET'||request.headers.get('Upgrade')?.toLowerCase()!=='websocket')return json({error:{message:'Invalid signaling endpoint'}},400);
  const token=request.headers.get('Authorization')?.replace(/^Bearer /,'');
  if(!check(token,ID))return json({error:{message:'Missing device credential'}},401);
  // The stable anonymous device identity is derived from its secret, never
  // accepted from a client-controlled name, IP, history entry or from field.
  const headers=new Headers(request.headers);headers.delete('Authorization');
  headers.set('X-Wan-Device',await hash(token));
  headers.set('X-Wan-IP',await hash(request.headers.get('CF-Connecting-IP')||'local'));
  return env.WAN.get(env.WAN.idFromName('flickdock-wan-v1')).fetch(new Request('https://internal/connect',{headers}));
}

export class WanHub extends DurableObject<WanEnv>{
  private async scheduleCleanup(){
    const next=Date.now()+300000,current=await this.ctx.storage.getAlarm();
    if(current===null||current>next)await this.ctx.storage.setAlarm(next);
  }
  private send(socket:WebSocket,value:object){socket.send(JSON.stringify({schema_version:1,...value}));}
  private socket(id:string){return this.ctx.getWebSockets(id).find(socket=>socket.readyState===1);}
  private peer(id:string){const socket=this.socket(id);return socket?publicDevice(socket.deserializeAttachment() as Seat):null;}
  private async limited(key:string,limit:number,period:number){
    const now=Date.now();
    return this.ctx.storage.transaction(async tx=>{
      const old=await tx.get<{until:number;n:number}>(key),row=old&&old.until>now?old:{until:now+period,n:0};
      if(++row.n>limit)throw new Error('请求过于频繁，请稍后再试。');
      await tx.put(key,row);return row;
    });
  }
  async fetch(request:Request):Promise<Response>{
    const id=request.headers.get('X-Wan-Device'),ip=request.headers.get('X-Wan-IP');
    if(!check(id,ID)||!check(ip,ID))return json({error:{message:'Invalid device'}},401);
    try{await this.limited('ip:'+ip,40,3600000);}catch{return json({error:{message:'Too many connections'}},429);}
    for(const existing of this.ctx.getWebSockets(id))try{existing.close(4000,'Reconnected');}catch{}
    const pair=new WebSocketPair();const [client,server]=Object.values(pair);
    this.ctx.acceptWebSocket(server,[id]);
    server.serializeAttachment({id,ip,connected:Date.now(),name:'未命名设备',platform:'other',window:Date.now(),messages:0} satisfies Seat);
    await this.scheduleCleanup();
    return new Response(null,{status:101,webSocket:client});
  }
  private async recent(id:string){
    const rows=await this.ctx.storage.get<Device[]>('recent:'+id)||[];
    return rows.slice(0,30).map(row=>({...row,...this.peer(row.id),online:!!this.socket(row.id)}));
  }
  private async code(socket:WebSocket){
    const seat=socket.deserializeAttachment() as Seat,previous=await this.ctx.storage.get<string>('own-code:'+seat.id);
    if(previous)await this.ctx.storage.delete('code:'+previous);
    const alphabet='23456789ABCDEFGHJKLMNPQRSTUVWXYZ';let code='';
    for(const byte of crypto.getRandomValues(new Uint8Array(8)))code+=alphabet[byte%alphabet.length];
    const expires=Date.now()+300000;
    await this.ctx.storage.put({['code:'+code]:{device:seat.id,expires},['own-code:'+seat.id]:code});
    this.send(socket,{type:'identity',device:publicDevice(seat),code,expires,recent:await this.recent(seat.id)});
  }
  private async session(id:unknown,seat:Seat){
    if(!check(id,TRANSFER))throw new Error('传输标识无效。');
    const value=await this.ctx.storage.get<Session>('session:'+id);
    if(!value||value.expires<Date.now()||![value.sender.id,value.receiver.id].includes(seat.id))throw new Error('传输请求已失效，请重新发起。');
    return value;
  }
  private async publish(value:Session,message:object){
    for(const id of [value.sender.id,value.receiver.id]){
      const socket=this.socket(id);if(socket)this.send(socket,{id:value.id,generation:value.generation,...message});
    }
  }
  private async replay(socket:WebSocket,value:Session){
    const seat=socket.deserializeAttachment() as Seat;
    if(value.accepted){
      this.send(socket,{type:'resumed',id:value.id,accepted:true,generation:value.generation,settled:value.done.length===2});
    }else if(seat.id===value.receiver.id){
      this.send(socket,{type:'request',id:value.id,peer:value.sender,token:value.token,digest:value.digest,total:value.total,count:value.count,names:value.names});
    }else this.send(socket,{type:'waiting',id:value.id,peer:value.receiver});
  }
  private async removeSession(value:Session){
    await this.ctx.storage.delete(['session:'+value.id,'pending:'+value.sender.id+':'+value.id,'pending:'+value.receiver.id+':'+value.id]);
    await this.revokeSession(value.id);
  }
  async webSocketMessage(socket:WebSocket,message:string|ArrayBuffer){
    let request:any;
    try{
      if(typeof message!=='string'||message.length>100000)throw new Error('信令内容无效。');
      const seat=socket.deserializeAttachment() as Seat;
      if(Date.now()-seat.connected>86400000){socket.close(4001,'Reconnect');return;}
      if(Date.now()-seat.window>60000){seat.window=Date.now();seat.messages=0;}
      if(++seat.messages>240)throw new Error('信令请求过于频繁。');
      socket.serializeAttachment(seat);request=JSON.parse(message);
      if(request.type==='hello'){
        seat.name=String(request.name||'未命名设备').replace(/[\x00-\x1f]/g,'').slice(0,64);
        seat.platform=['windows','macos','linux'].includes(request.platform)?request.platform:'other';socket.serializeAttachment(seat);
        await this.ctx.storage.put('device:'+seat.id,publicDevice(seat));await this.code(socket);
        const pending=await this.ctx.storage.list<{expires:number}>({prefix:'pending:'+seat.id+':',limit:64});
        for(const [key,row] of pending){
          if(row.expires<Date.now())continue;
          const value=await this.ctx.storage.get<Session>('session:'+key.split(':').pop());
          if(value&&value.expires>Date.now())await this.replay(socket,value);
        }
        return;
      }
      if(request.type==='refresh'){await this.code(socket);return;}
      if(request.type==='recent'){this.send(socket,{type:'peers',peers:await this.recent(seat.id)});return;}
      if(request.type==='remove'){
        if(!check(request.peer,ID))throw new Error('设备无效。');
        for(const [a,b] of [[seat.id,request.peer],[request.peer,seat.id]]){
          const rows=await this.ctx.storage.get<Device[]>('recent:'+a)||[];await this.ctx.storage.put('recent:'+a,rows.filter(row=>row.id!==b));
        }
        this.send(socket,{type:'peers',peers:await this.recent(seat.id)});return;
      }
      if(request.type==='request'){
        if(!check(request.id,TRANSFER)||!check(request.token,ID)||!check(request.digest,ID)
            ||!check(request.total,/^\d{1,20}$/)||BigInt(request.total)<0n||!Number.isSafeInteger(request.count)||request.count<0
            ||!Array.isArray(request.names)||request.names.length>12||request.names.some((name:unknown)=>typeof name!=='string'||name.length>255||/[\\/\x00-\x1f]/.test(name)))throw new Error('传输请求无效。');
        const previous=await this.ctx.storage.get<Session>('session:'+request.id);
        if(previous){
          if(previous.sender.id!==seat.id||previous.token!==request.token||previous.digest!==request.digest||previous.total!==request.total||previous.count!==request.count||JSON.stringify(previous.names)!==JSON.stringify(request.names))throw new Error('传输请求冲突。');
          if(previous.expires<Date.now())throw new Error('传输请求已失效，请重新发起。');
          await this.replay(socket,previous);return;
        }
        await this.limited('request:'+seat.id,15,60000);await this.limited('guess:'+seat.ip,30,60000);
        let target:string;
        if(request.code){
          if(!check(request.code,/^[23456789A-HJ-NP-Z]{8}$/))throw new Error('接收码无效或已过期。');
          target=await this.ctx.storage.transaction(async tx=>{
            const key='code:'+request.code,row=await tx.get<{device:string;expires:number}>(key);
            if(!row||row.expires<Date.now()||row.device===seat.id)throw new Error('接收码无效或已过期。');
            await tx.delete(key);return row.device;
          });
        }else{
          if(!check(request.peer,ID)||(await this.recent(seat.id)).every(row=>row.id!==request.peer))throw new Error('首次传输请使用对方的接收码。');
          target=request.peer;
        }
        const receiver=this.peer(target),recipient=this.socket(target);
        if(!receiver||!recipient)throw new Error('对方设备不在线。');
        if(await this.ctx.storage.get('session:'+request.id))throw new Error('传输请求已存在。');
        const value:Session={id:request.id,sender:publicDevice(seat),receiver,token:request.token,digest:request.digest,total:request.total,count:request.count,names:request.names,accepted:false,generation:0,expires:Date.now()+86400000,done:[]};
        await this.ctx.storage.put({['session:'+value.id]:value,['pending:'+value.sender.id+':'+value.id]:{expires:value.expires},['pending:'+value.receiver.id+':'+value.id]:{expires:value.expires}});
        this.send(recipient,{type:'request',id:value.id,peer:value.sender,token:value.token,digest:value.digest,total:value.total,count:value.count,names:value.names});
        this.send(socket,{type:'waiting',id:value.id,peer:receiver});await this.code(recipient);return;
      }
      const value=await this.session(request.id,seat),other=value.sender.id===seat.id?value.receiver.id:value.sender.id;
      if(request.type==='accept'){
        if(seat.id!==value.receiver.id)throw new Error('无法接受此请求。');
        if(value.accepted){await this.replay(socket,value);return;}
        value.accepted=true;value.generation++;await this.ctx.storage.put('session:'+value.id,value);
        await this.publish(value,{type:'begin',initiator:value.sender.id,iceServers:stun,relayAllowed:this.env.TURN_ENABLED==='true'});return;
      }
      if(request.type==='reject'||request.type==='cancel'){
        await this.publish(value,{type:'cancelled'});await this.removeSession(value);return;
      }
      if(!value.accepted)throw new Error('接收方尚未确认。');
      if(request.type==='retry'){
        if(seat.id!==value.sender.id)throw new Error('仅发送方可恢复连接。');
        if(value.done.length===2){await this.publish(value,{type:'settled'});return;}
        await this.limited('retry:'+value.id,8,60000);
        value.generation++;value.connected=[];await this.ctx.storage.put('session:'+value.id,value);
        await this.publish(value,{type:'begin',initiator:value.sender.id,iceServers:stun,relayAllowed:this.env.TURN_ENABLED==='true'});return;
      }
      if(request.type==='connected'){
        if(request.generation!==value.generation)throw new Error('连接信令已过期。');
        value.connected=Array.from(new Set([...(value.connected||[]),seat.id]));
        await this.ctx.storage.put('session:'+value.id,value);return;
      }
      if(request.type==='relay'){
        if(seat.id!==value.sender.id||request.generation!==value.generation)throw new Error('连接请求已失效。');
        if(value.done.length===2||value.connected?.length)return;
        await this.limited('relay:'+value.id,3,60000);
        const {iceServers,epoch}=await this.turn(value);
        const current=await this.ctx.storage.transaction(async tx=>{
          const gate=await tx.get<RelayGate>('relay-gate');
          if(gate?.blocked||(gate?.epoch||0)!==epoch)return null;
          const latest=await tx.get<Session>('session:'+value.id);
          if(!latest||!latest.accepted||latest.generation!==value.generation||latest.expires<Date.now()||latest.done.length===2||latest.connected?.length)return null;
          latest.generation++;await tx.put('session:'+latest.id,latest);return latest;
        });
        if(current)await this.publish(current,{type:'begin',initiator:current.sender.id,iceServers,relayAllowed:false,relayAttempt:true});
        else await this.revokeSession(value.id,new Set(iceServers.map((row:any)=>row.username)));
        return;
      }
      if(request.type==='description'||request.type==='candidate'){
        if(request.generation!==value.generation)throw new Error('连接信令已过期。');
        if(request.type==='description'&&(!check(request.sdp,/^[\s\S]{1,65536}$/)||request.descriptionType!==(seat.id===value.sender.id?'offer':'answer')))throw new Error('连接描述无效。');
        if(request.type==='candidate'&&(!check(request.candidate,/^[^\x00\r\n]{1,2048}$/)||!check(request.mid,/^[\w-]{1,32}$/)))throw new Error('网络候选无效。');
        const target=this.socket(other);if(!target)throw new Error('对方设备已离线。');
        this.send(target,{type:request.type,id:value.id,generation:value.generation,...(request.type==='description'?{sdp:request.sdp,descriptionType:request.descriptionType}:{candidate:request.candidate,mid:request.mid})});return;
      }
      if(request.type==='complete'){
        if(!value.done.includes(seat.id))value.done.push(seat.id);
        await this.ctx.storage.put('session:'+value.id,value);
        if(value.done.length===2){
          value.expires=Date.now()+600000;await this.ctx.storage.put('session:'+value.id,value);
          await this.ctx.storage.delete(['pending:'+value.sender.id+':'+value.id,'pending:'+value.receiver.id+':'+value.id]);
          await this.revokeSession(value.id);
          for(const [self,peer] of [[value.sender,value.receiver],[value.receiver,value.sender]]){
            const rows=await this.ctx.storage.get<Device[]>('recent:'+self.id)||[];
            await this.ctx.storage.put('recent:'+self.id,[{...peer,last:Date.now()},...rows.filter(row=>row.id!==peer.id)].slice(0,30));
            const target=this.socket(self.id);if(target)this.send(target,{type:'peers',peers:await this.recent(self.id)});
          }
          await this.publish(value,{type:'settled'});
        }
        return;
      }
      throw new Error('未知的传输请求。');
    }catch(error){this.send(socket,{type:'error',id:check(request?.id,TRANSFER)?request.id:undefined,generation:Number.isSafeInteger(request?.generation)?request.generation:undefined,requestType:typeof request?.type==='string'?request.type:undefined,message:error instanceof Error?error.message:'广域网服务暂不可用。'});}
  }
  private async turn(value:Session){
    if(this.env.TURN_ENABLED!=='true'||!this.env.TURN_KEY_ID||!this.env.TURN_KEY_TOKEN)throw new Error('中继当前不可用，仍可使用 P2P 直连。');
    let usage;
    try{usage=await relayUsage(this.env,this.ctx.storage);}
    catch(error){await this.setRelayGate(true);await this.revokeAll();throw error;}
    if(usage.bytes>=relayBudget(this.env)){await this.setRelayGate(true);await this.revokeAll();throw new Error('中继用量已接近免费额度，当前仅允许 P2P 直连。');}
    const {epoch}=await this.setRelayGate(false);
    // Admission protection, not a provider billing cap. The owner accepted
    // the residual overage risk; analytics and credential revocation add a
    // separate circuit breaker below the shared free tier.
    const key='relay-budget:'+new Date().toISOString().slice(0,7),budget=BigInt(this.env.TURN_MONTHLY_BUDGET_BYTES||'900000000000');
    const reserve=BigInt(value.total)*4n+64n*1024n*1024n;
    await this.ctx.storage.transaction(async tx=>{
      const used=BigInt(await tx.get<string>(key)||'0');
      if(used+reserve>budget)throw new Error('本月中继免费额度已用完，仍可使用 P2P 直连。');
      await tx.put(key,String(used+reserve));
    });
    const response=await fetch('https://rtc.live.cloudflare.com/v1/turn/keys/'+encodeURIComponent(this.env.TURN_KEY_ID)+'/credentials/generate-ice-servers',{
      method:'POST',headers:{Authorization:'Bearer '+this.env.TURN_KEY_TOKEN,'Content-Type':'application/json'},body:JSON.stringify({ttl:21600,customIdentifier:'flickdock-'+value.id}),signal:AbortSignal.timeout(12000)});
    if(!response.ok)throw new Error('中继连接暂时不可用，请稍后重试。');
    const data=await response.json() as {iceServers:unknown[]};
    if(!Array.isArray(data.iceServers)||data.iceServers.length>8)throw new Error('中继配置无效。');
    for(const row of data.iceServers as any[])if(typeof row.username==='string')await this.ctx.storage.put('credential:'+value.id+':'+row.username,{username:row.username,expires:Date.now()+21600000});
    const current=await this.ctx.storage.getAlarm();if(current===null||current>Date.now()+60000)await this.ctx.storage.setAlarm(Date.now()+60000);
    return {iceServers:data.iceServers,epoch};
  }
  private async setRelayGate(blocked:boolean){
    return this.ctx.storage.transaction(async tx=>{
      const old=await tx.get<RelayGate>('relay-gate')||{epoch:0,blocked:false};
      const gate=old.blocked===blocked?old:{epoch:old.epoch+1,blocked};
      await tx.put('relay-gate',gate);return gate;
    });
  }
  private async *credentialPages(prefix='credential:'){
    let cursor='';
    while(true){
      const rows=await this.ctx.storage.list<Credential>({prefix,limit:100,...(cursor?{startAfter:cursor}:{})});
      if(!rows.size)return;
      cursor=[...rows.keys()].pop()!;yield rows;
      if(rows.size<100)return;
    }
  }
  private async revoke(iceServers:unknown[]){
    const names=[...new Set<string>(iceServers.map((row:any)=>row?.username).filter(name=>typeof name==='string'))];
    const revoked=new Set<string>();
    for(let index=0;index<names.length;index+=8)await Promise.all(names.slice(index,index+8).map(async name=>{
      try{
        const result=await fetch('https://rtc.live.cloudflare.com/v1/turn/keys/'+encodeURIComponent(this.env.TURN_KEY_ID||'')+'/credentials/'+encodeURIComponent(name)+'/revoke',{method:'POST',headers:{Authorization:'Bearer '+this.env.TURN_KEY_TOKEN},signal:AbortSignal.timeout(10000)});
        if(result.ok||result.status===404)revoked.add(name);
      }catch{}
    }));
    return revoked;
  }
  private async revokePending(){
    // At most 32 provider requests per event, leaving room for analytics and
    // issuance even under the Free plan's 50 subrequest ceiling. Persist the
    // scan cursor across alarms, including failed revocations.
    const cursor=await this.ctx.storage.get<string>('revoke-cursor');
    const rows=await this.ctx.storage.list<Credential>({prefix:'credential:',limit:32,...(cursor?{startAfter:cursor}:{})});
    const pending=[...rows].filter(([,row])=>row.revoke_pending);
    const revoked=await this.revoke(pending.map(([,row])=>row));
    const keys=pending.filter(([,row])=>revoked.has(row.username)).map(([key])=>key);
    if(keys.length)await this.ctx.storage.delete(keys);
    if(rows.size===32)await this.ctx.storage.put('revoke-cursor',[...rows.keys()].pop()!);
    else await this.ctx.storage.delete('revoke-cursor');
    const more=rows.size===32||!!cursor;
    if(more){const next=Date.now()+1000,current=await this.ctx.storage.getAlarm();if(current===null||current>next)await this.ctx.storage.setAlarm(next);}
    return more;
  }
  private async markRevoke(prefix:string,only?:Set<string>){
    for await(const rows of this.credentialPages(prefix))for(const [key,row] of rows){
      if(!only||only.has(row.username))await this.ctx.storage.put(key,{...row,revoke_pending:true});
    }
    const next=Date.now()+1000,current=await this.ctx.storage.getAlarm();
    if(current===null||current>next)await this.ctx.storage.setAlarm(next);
  }
  private async revokeSession(id:string,only?:Set<string>){
    await this.markRevoke('credential:'+id+':',only);await this.revokePending();
  }
  private async revokeAll(){
    await this.markRevoke('credential:');return this.revokePending();
  }
  async alarm(){
    const now=Date.now();let remaining=false,paged=false,credentialCount=0;
    for await(const rows of this.credentialPages()){
      const expired=[...rows].filter(([,row])=>row.expires<=now).map(([key])=>key);
      if(expired.length)await this.ctx.storage.delete(expired);
      credentialCount+=rows.size-expired.length;
    }
    if(credentialCount){
      let blocked=true;
      try{blocked=(await relayUsage(this.env,this.ctx.storage)).bytes>=relayBudget(this.env);}catch{}
      await this.setRelayGate(blocked);
      paged=blocked?await this.revokeAll():await this.revokePending();
      credentialCount=(await this.ctx.storage.list({prefix:'credential:',limit:1})).size;
    }
    for(const prefix of ['code:','session:','pending:','ip:','request:','guess:','retry:','relay:']){
      const cursor=await this.ctx.storage.get<string>('sweep:'+prefix);
      const rows=await this.ctx.storage.list<any>({prefix,limit:1000,...(cursor?{startAfter:cursor}:{})});
      const expired=[...rows].filter(([,row])=>(row.expires||row.until||now+1)<now).map(([key])=>key);
      if(expired.length)await this.ctx.storage.delete(expired);
      if(rows.size===1000){await this.ctx.storage.put('sweep:'+prefix,[...rows.keys()].pop()!);paged=true;}
      else await this.ctx.storage.delete('sweep:'+prefix);
      remaining=remaining||rows.size>expired.length||!!cursor;
    }
    if(paged||remaining||credentialCount||this.ctx.getWebSockets().length)await this.ctx.storage.setAlarm(now+(paged?1000:credentialCount?60000:300000));
  }
}
