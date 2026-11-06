import {DurableObject} from 'cloudflare:workers';
import {relayUsage,relayBudget,type RelayBudgetEnv} from './relay-budget';
import {CloudPipes} from './cloud-pipe';
import {manualSignal,type NativeState} from './manual-signal';
type RelayGate={epoch:number;blocked:boolean};
type Credential={username:string;expires:number;revoke_pending?:boolean};
type SyncIce={id:string;account:string;device:string;session:string;expires:number;iceServers:unknown[]};
type Device={id:string;name:string;platform:string;online?:boolean;last?:number;lan_id?:string;account_device?:boolean;account_device_id?:string;features?:string[]};
type Seat=Device&{ip:string;connected:number;window:number;messages:number;last_seen?:number;watching?:string[];account?:string;auth_nonce?:string;account_active?:boolean;sync_links?:Record<string,string>;sync_window?:number;sync_messages?:number};
type Binding={account:string;device:string;session:string;lan_id:string;nonce:string};
type CodeRow={device:string;expires:number;epoch:number};
type Pairing={sender:string;receiver:string;token:string;expires:number};
type Session={id:string;sender:Device;receiver:Device;token:string;digest:string;total:string;count:number;names:string[];accepted:boolean;generation:number;expires:number;done:string[];connected?:string[];sender_code?:string;native?:NativeState};
export interface WanEnv extends RelayBudgetEnv {WAN:DurableObjectNamespace<WanHub>;DB:D1Database;FILES:R2Bucket;R2_CLASS_A_BUDGET?:string;R2_CLASS_B_BUDGET?:string;TURN_KEY_ID?:string;TURN_KEY_TOKEN?:string;TURN_ENABLED?:string;}
const json=(value:object,status=200)=>Response.json({schema_version:1,...value},{status,headers:{'Cache-Control':'no-store'}});
const hash=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))),b=>b.toString(16).padStart(2,'0')).join('');
const check=(value:unknown,pattern:RegExp):value is string=>typeof value==='string'&&pattern.test(value);
const ID=/^[a-f0-9]{64}$/,TRANSFER=/^[a-f0-9]{32}$/;
const CODE_ALPHABET='23456789ABCDEFGHJKLMNPQRSTUVWXYZ',CODE_LENGTH=4,CODE_ATTEMPTS=64;
const RECEIVE_CODE=/^(?=.*[2-9])(?=.*[A-HJ-NP-Z])[23456789A-HJ-NP-Z]{4}$/;
const stun=[{urls:['stun:stun.cloudflare.com:3478']}];
function routeSignal(input:any):object|null{
  if(!input||!check(input.id,TRANSFER))return null;
  if(input.kind==='start'&&typeof input.directOnly==='boolean')return {kind:'start',id:input.id,directOnly:input.directOnly};
  if(input.kind==='cloud'&&check(input.key,ID))return {kind:'cloud',id:input.id,key:input.key};
  const value=input.payload;
  if(input.kind!=='candidate'||!value||typeof value!=='object')return null;
  if(value.type==='description'&&['offer','answer'].includes(value.descriptionType)&&typeof value.sdp==='string'&&value.sdp.length<=65000)
    return {kind:'candidate',id:input.id,payload:{type:'description',descriptionType:value.descriptionType,sdp:value.sdp}};
  if(value.type==='candidate'&&typeof value.candidate==='string'&&value.candidate.length<=4096&&typeof value.mid==='string'&&value.mid.length<=256)
    return {kind:'candidate',id:input.id,payload:{type:'candidate',candidate:value.candidate,mid:value.mid}};
  return null;
}
function publicDevice(value:Device):Device{return {id:value.id,name:value.name,platform:value.platform,...(value.lan_id?{lan_id:value.lan_id}:{}),...(value.features?{features:value.features}:{})};}

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
  private syncIceJobs=new Map<string,Promise<object>>();
  private cloudPipes=new CloudPipes(this.ctx,this.env,id=>this.binding(id),(signal,account,id)=>{const socket=this.socket(signal);if(socket&&(socket.deserializeAttachment() as Seat).account===account)this.send(socket,{type:'cloud_data',id});});
  private async scheduleCleanup(){
    const next=Date.now()+30000,current=await this.ctx.storage.getAlarm();
    if(current===null||current>next)await this.ctx.storage.setAlarm(next);
  }
  private send(socket:WebSocket,value:object){
    if(socket.readyState!==1)return false;
    try{socket.send(JSON.stringify({schema_version:1,...value}));return true;}catch{return false;}
  }
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
  private async binding(id:string){
    const value=await this.ctx.storage.get<Binding>('account-binding:'+id);
    if(!value)return null;
    const seat=this.socket(id)?.deserializeAttachment() as Seat|undefined;
    if(!seat?.account_active||seat.auth_nonce!==value.nonce)return null;
    const valid=await this.env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL')
      .bind(value.session,value.account,value.device,Date.now()).first();
    return valid?value:null;
  }
  private async accountPeers(id:string):Promise<Device[]>{
    const self=await this.binding(id);if(!self)return [];
    const devices=(await this.env.DB.prepare('SELECT id,name,platform FROM devices WHERE account_id=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 20').bind(self.account).all<{id:string;name:string;platform:string}>()).results;
    const members=await this.ctx.storage.get<Record<string,string>>('account-members:'+self.account)||{};
    const rows:Device[]=[];
    for(const device of devices){
      if(device.id===self.device)continue;
      const peerId=members[device.id],binding=peerId?await this.binding(peerId):null;
      const socket=peerId?this.socket(peerId):null,live=socket?socket.deserializeAttachment() as Seat:null;
      rows.push({id:peerId||await hash('account-device:'+self.account+':'+device.id),name:device.name,platform:device.platform,
        account_device:true,account_device_id:device.id,online:!!(binding?.account===self.account&&binding.device===device.id&&socket),lan_id:binding?.lan_id||'',features:live?.features||[]});
    }
    return rows;
  }
  private async accountPresence(accounts:string[]){
    for(const socket of this.ctx.getWebSockets()){
      const seat=socket.deserializeAttachment() as Seat;
      if(seat.account&&accounts.includes(seat.account))this.send(socket,{type:'peers',peers:await this.recent(seat.id)});
    }
  }
  private async unbind(id:string,keepIce=false){
    const grant=await this.ctx.storage.get<SyncIce>('sync-ice:'+id);
    if(grant&&!keepIce){await this.ctx.storage.delete('sync-ice:'+id);await this.revokeSession(grant.id);}
    const active=this.socket(id);
    if(active){
      const seat=active.deserializeAttachment() as Seat;
      for(const [link,peer] of Object.entries(keepIce?{}:seat.sync_links||{})){
        const target=this.socket(peer);if(target)this.send(target,{type:'sync_closed',id:link});
      }
      seat.sync_links={};active.serializeAttachment(seat);
    }
    const old=await this.ctx.storage.get<Binding>('account-binding:'+id);await this.ctx.storage.delete('account-binding:'+id);
    if(old&&!keepIce)await this.cloudPipes.revoke(old.device,old.account);
    const live=this.socket(id);if(live){const seat=live.deserializeAttachment() as Seat;delete seat.account;live.serializeAttachment(seat);}
    if(old)await this.accountPresence([old.account]);
    if(live)this.send(live,{type:'peers',peers:await this.recent(id)});
  }
  async fetch(request:Request):Promise<Response>{
    if(new URL(request.url).pathname.startsWith('/cloud/'))return this.cloudPipes.fetch(request);
    const id=request.headers.get('X-Wan-Device'),ip=request.headers.get('X-Wan-IP');
    if(new URL(request.url).pathname==='/ice'&&request.method==='POST'){
      if(!check(id,ID))return json({error:{message:'Invalid device'}},401);
      const owner=await this.binding(id),seat=this.socket(id)?.deserializeAttachment() as Seat|undefined;
      const account=request.headers.get('X-Wan-Account'),device=request.headers.get('X-Wan-Account-Device'),session=request.headers.get('X-Wan-Session');
      if(!owner||owner.account!==account||owner.device!==device||owner.session!==session||!seat?.account_active||seat.auth_nonce!==owner.nonce)
        return json({error:{message:'Account device authorization expired'}},401);
      const jobKey=id+':'+owner.nonce;
      let job=this.syncIceJobs.get(jobKey);
      if(!job){
        job=(async()=>{
        const cached=await this.ctx.storage.get<SyncIce>('sync-ice:'+id);
        if(cached&&cached.account===account&&cached.device===device&&cached.session===session&&cached.expires>Date.now()+300000)
          return {iceServers:cached.iceServers,expires:cached.expires,serverNow:Date.now()};
        await this.limited('sync-ice:'+id,2,60000);
        const grantId=crypto.randomUUID().replaceAll('-','');
        const {iceServers}=await this.turn({id:grantId,total:'0'});
        const current=await this.binding(id),live=this.socket(id)?.deserializeAttachment() as Seat|undefined;
        if(!current||current.account!==account||current.device!==device||current.session!==session||!live?.account_active||live.auth_nonce!==owner.nonce){
          await this.revokeSession(grantId);throw new Error('Account intent changed');
        }
        if(cached)await this.revokeSession(cached.id);
        const expires=Date.now()+21600000;
        await this.ctx.storage.put('sync-ice:'+id,{id:grantId,account,device,session,expires,iceServers} satisfies SyncIce);
        return {iceServers,expires,serverNow:Date.now()};
        })().finally(()=>this.syncIceJobs.delete(jobKey));
        this.syncIceJobs.set(jobKey,job);
      }
      try{
        const result=await job;
        const live=this.socket(id)?.deserializeAttachment() as Seat|undefined;
        if(!live?.account_active||live.auth_nonce!==owner.nonce)return json({error:{message:'Account intent changed'}},409);
        return json(result);
      }catch(error){return json({error:{message:error instanceof Error?error.message:'Relay unavailable'}},503);}
    }
    if(new URL(request.url).pathname==='/account'&&request.method==='POST'){
      const account=request.headers.get('X-Wan-Account')||'',device=request.headers.get('X-Wan-Account-Device')||'',session=request.headers.get('X-Wan-Session')||'',lan_id=request.headers.get('X-Wan-Lan')||'',nonce=request.headers.get('X-Wan-Nonce')||'';
      if(!check(id,ID)||!check(session,ID)||!/^[a-f0-9-]{36}$/.test(account)||!/^[a-f0-9-]{36}$/.test(device))return json({error:{message:'Invalid account device'}},401);
      const valid=await this.env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL').bind(session,account,device,Date.now()).first();
      if(!valid)return json({error:{message:'Session expired'}},401);
      const intent=()=>{const seat=this.socket(id)?.deserializeAttachment() as Seat|undefined;return seat?.account_active&&seat.auth_nonce===nonce;};
      if(!check(nonce,TRANSFER)||!intent())return json({error:{message:'Account intent changed'}},409);
      const old=await this.ctx.storage.get<Binding>('account-binding:'+id);
      await this.ctx.storage.transaction(async tx=>{
        if(!intent())throw new Error('Account intent changed');
        const members=await tx.get<Record<string,string>>('account-members:'+account)||{};members[device]=id;
        if(!intent())throw new Error('Account intent changed');
        await tx.put('account-members:'+account,members);await tx.put('account-binding:'+id,{account,device,session,lan_id,nonce});
      });
      const live=this.socket(id);if(live&&intent()){const seat=live.deserializeAttachment() as Seat;seat.account=account;seat.lan_id=lan_id;live.serializeAttachment(seat);}
      await this.accountPresence([account,...(old?[old.account]:[])]);return json({bound:true});
    }
    if(!check(id,ID)||!check(ip,ID))return json({error:{message:'Invalid device'}},401);
    try{
      await this.limited('connect:'+id,30,60000);
      // Reconnecting a proven anonymous device must not consume the shared
      // network's new-device allowance (VMs/NAT and transient outages).
      if(!await this.ctx.storage.get('device:'+id))await this.limited('ip:'+ip,40,3600000);
    }catch{return json({error:{message:'Too many connections'}},429);}
    for(const existing of this.ctx.getWebSockets(id))try{existing.close(4000,'Reconnected');}catch{}
    const pair=new WebSocketPair();const [client,server]=Object.values(pair);
    this.ctx.acceptWebSocket(server,[id]);
    const binding=await this.binding(id);
    server.serializeAttachment({id,ip,connected:Date.now(),name:'未命名设备',platform:'other',window:Date.now(),messages:0,...(binding?{account:binding.account,lan_id:binding.lan_id}:{})} satisfies Seat);
    await this.scheduleCleanup();
    return new Response(null,{status:101,webSocket:client});
  }
  private async recent(id:string){
    const history=await this.ctx.storage.get<Device[]>('recent:'+id)||[];
    const hidden=new Set(await this.ctx.storage.get<string[]>('recent-hidden:'+id)||[]);
    const account=await this.accountPeers(id),ids=new Set(account.map(row=>row.id));
    const rows=[...account,...history.filter(row=>!ids.has(row.id))].filter(row=>!hidden.has(row.id));
    const socket=this.socket(id);
    if(socket){const seat=socket.deserializeAttachment() as Seat;seat.watching=rows.slice(0,50).map(row=>row.id);socket.serializeAttachment(seat);}
    return rows.slice(0,50).map(row=>row.account_device?row:({...row,...this.peer(row.id),online:!!this.socket(row.id)}));
  }
  private async presence(id:string){
    // Recent lists can be asymmetric after their independent 30-device limit.
    // Watch the list actually displayed by each connected observer.
    for(const target of this.ctx.getWebSockets()){
      const seat=target.deserializeAttachment() as Seat;
      const changed=this.socket(id)?.deserializeAttachment() as Seat|undefined;
      if(target.readyState===1&&(seat.watching?.includes(id)||changed?.account&&seat.account===changed.account))this.send(target,{type:'peers',peers:await this.recent(seat.id)});
    }
  }
  async webSocketClose(socket:WebSocket){
    const seat=socket.deserializeAttachment() as Seat;
    try{socket.close(1000,'Disconnected');}catch{}
    await this.presence(seat.id);
    if(seat.account)await this.accountPresence([seat.account]);
  }
  async webSocketError(socket:WebSocket){
    const seat=socket.deserializeAttachment() as Seat;
    try{socket.close(1011,'Connection lost');}catch{}
    await this.presence(seat.id);
    if(seat.account)await this.accountPresence([seat.account]);
  }
  private async remember(value:Session){
    await this.ctx.storage.transaction(async tx=>{
      for(const [self,peer] of [[value.sender,value.receiver],[value.receiver,value.sender]]){
        const rows=await tx.get<Device[]>('recent:'+self.id)||[];
        await tx.put('trusted:'+self.id+':'+peer.id,{accepted:Date.now()});
        await tx.put('recent:'+self.id,[{...peer,last:Date.now()},...rows.filter(row=>row.id!==peer.id)].slice(0,30));
        const hidden=await tx.get<string[]>('recent-hidden:'+self.id)||[];
        if(hidden.includes(peer.id))await tx.put('recent-hidden:'+self.id,hidden.filter(id=>id!==peer.id));
      }
    });
    for(const device of [value.sender,value.receiver]){
      const target=this.socket(device.id);if(target)this.send(target,{type:'peers',peers:await this.recent(device.id)});
    }
  }
  private async code(socket:WebSocket){
    const seat=socket.deserializeAttachment() as Seat;
    let code='';
    const serverNow=Date.now(),expires=serverNow+300000,refreshAt=serverNow+240000;
    // Refresh leaves the previous code valid for its original remaining minute.
    // Redeeming either alias invalidates all aliases in the same epoch.
    for(let attempt=0;attempt<CODE_ATTEMPTS;attempt++){
      code='';for(const byte of crypto.getRandomValues(new Uint8Array(CODE_LENGTH)))code+=CODE_ALPHABET[byte%CODE_ALPHABET.length];
      if(!RECEIVE_CODE.test(code)){if(attempt===CODE_ATTEMPTS-1)throw new Error('接收码暂不可用，请重试。');continue;}
      const saved=await this.ctx.storage.transaction(async tx=>{
        const old=await tx.get<CodeRow>('code:'+code);if(old&&old.expires>serverNow)return false;
        const epoch=await tx.get<number>('code-epoch:'+seat.id)||0;
        await tx.put({['code:'+code]:{device:seat.id,expires,epoch},['own-code:'+seat.id]:code});return true;
      });
      if(saved)break;if(attempt===CODE_ATTEMPTS-1)throw new Error('接收码暂不可用，请重试。');
    }
    this.send(socket,{type:'identity',device:publicDevice(seat),code,expires,refreshAt,serverNow,recent:await this.recent(seat.id)});
  }
  private async claimCode(code:unknown,seat:Seat,prepare?:{id:string;token:string}){
    if(!check(code,RECEIVE_CODE))throw new Error('接收码无效或已过期，请复制对方当前的接收码。');
    return this.ctx.storage.transaction(async tx=>{
      if(prepare){
        const existing=await tx.get<Pairing>('pairing:'+prepare.id);
        if(existing){
          if(existing.sender!==seat.id||existing.token!==prepare.token||existing.expires<=Date.now())throw new Error('接收码验证已失效，请重新发起。');
          return {receiver:existing.receiver};
        }
        if(await tx.get('pairing-cancel:'+seat.id+':'+prepare.id))throw new Error('已取消传输。');
      }
      const key='code:'+code,row=await tx.get<CodeRow>(key);
      if(!row||row.expires<=Date.now()||row.device===seat.id||row.epoch!==(await tx.get<number>('code-epoch:'+row.device)||0))throw new Error('接收码无效或已过期，请复制对方当前的接收码。');
      if(!this.socket(row.device))throw new Error('对方设备不在线。');
      await tx.put('code-epoch:'+row.device,row.epoch+1);await tx.delete(key);
      if(prepare)await tx.put('pairing:'+prepare.id,{sender:seat.id,receiver:row.device,token:prepare.token,expires:Date.now()+600000} satisfies Pairing);
      return {receiver:row.device};
    });
  }
  private async session(id:unknown,seat:Seat){
    if(!check(id,TRANSFER))throw new Error('传输标识无效。');
    const value=await this.ctx.storage.get<Session>('session:'+id);
    if(!value||value.expires<Date.now()||![value.sender.id,value.receiver.id].includes(seat.id))throw new Error('传输请求已失效，请重新发起。');
    return value;
  }
  private async publish(value:Session,message:object){
    const capabilities=(message as {type?:string}).type==='begin'
      ?{adaptive:value.sender.features?.includes('routes-v1')===true&&value.receiver.features?.includes('routes-v1')===true}:{};
    for(const id of [value.sender.id,value.receiver.id]){
      const socket=this.socket(id);if(socket)this.send(socket,{id:value.id,generation:value.generation,...message,...capabilities});
    }
  }
  private async replay(socket:WebSocket,value:Session){
    const seat=socket.deserializeAttachment() as Seat;
    if(value.accepted){
      const from=value.native?await this.binding(value.sender.id):null,to=value.native?await this.binding(value.receiver.id):null;
      this.send(socket,{type:'resumed',id:value.id,accepted:true,generation:value.generation,settled:value.done.length===2,
        ...(value.native?{account_device:!!(from&&to&&from.account===to.account&&from.device!==to.device)}:{})});
      if(value.native){
        const source=seat.id===value.sender.id?value.native.receiver:value.native.sender;
        const peer=seat.id===value.sender.id?value.native.receiverPeer:value.native.senderPeer;
        for(const metadata of [peer,source,...(seat.id===value.receiver.id?[value.native.cloud]:[]),
            ...(value.native.fallback?[{kind:'fallback'}]:[]),...(value.native.saved?[{kind:'saved'}]:[])])
          if(metadata)this.send(socket,{type:'native_signal',id:value.id,generation:value.generation,value:metadata});
      }
    }else if(seat.id===value.receiver.id){
      const from=await this.binding(value.sender.id),to=await this.binding(value.receiver.id);
      const same=!!(from&&to&&from.account===to.account&&from.device!==to.device);
      this.send(socket,{type:'request',id:value.id,peer:value.sender,account_device:same,...(same?{account_id:from!.account}:{}),deviceCode:value.sender_code||'',token:value.token,digest:value.digest,total:value.total,count:value.count,names:value.names});
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
      request=JSON.parse(message);
      if(seat.account_active&&seat.account&&typeof request?.type==='string'&&request.type.startsWith('sync_')){
        if(!seat.sync_window||Date.now()-seat.sync_window>60000){seat.sync_window=Date.now();seat.sync_messages=0;}
        seat.sync_messages=(seat.sync_messages||0)+1;
        if(seat.sync_messages>1200)throw new Error('同账号信令请求过于频繁。');
      }else if(++seat.messages>240)throw new Error('信令请求过于频繁。');
      seat.last_seen=Date.now();socket.serializeAttachment(seat);
      if(typeof request?.type==='string'&&request.type.startsWith('sync_')){
        try{
          if(!check(request.id,TRANSFER))throw new Error('同账号连接标识无效。');
          const self=await this.binding(seat.id);
          if(!self)throw new Error('账号授权已失效。');
          const peerId=request.type==='sync_open'?request.peer:seat.sync_links?.[request.id];
          if(!check(peerId,ID)||peerId===seat.id)throw new Error('同账号设备无效。');
          const other=await this.binding(peerId),target=this.socket(peerId);
          if(!other||other.account!==self.account||other.device===self.device||!target)throw new Error('同账号设备已离线或授权已撤销。');
          if(this.socket(seat.id)!==socket)throw new Error('连接已更新。');
          const current=socket.deserializeAttachment() as Seat,remote=target.deserializeAttachment() as Seat;
          if(!current.account_active||current.auth_nonce!==self.nonce||current.account!==self.account
              ||!remote.account_active||remote.auth_nonce!==other.nonce||remote.account!==other.account)throw new Error('账号授权已改变。');
          Object.assign(seat,current);
          if(request.type==='sync_open'){
            const opposite=target.deserializeAttachment() as Seat;
            const links=(rows:Record<string,string>|undefined,peer:string)=>Object.fromEntries(Object.entries(rows||{}).filter(([,value])=>value!==peer));
            seat.sync_links={...links(seat.sync_links,peerId),[request.id]:peerId};
            opposite.sync_links={...links(opposite.sync_links,seat.id),[request.id]:seat.id};
            if(Object.keys(seat.sync_links).length>19||Object.keys(opposite.sync_links).length>19)throw new Error('同账号设备连接数量超限。');
            socket.serializeAttachment(seat);target.serializeAttachment(opposite);
            const iceServers=[{urls:['stun:stun.cloudflare.com:3478']}];
            this.send(target,{type:'sync_begin',id:request.id,peer:publicDevice(seat),account:self.account,initiator:false,iceServers,serverNow:Date.now()});
            this.send(socket,{type:'sync_begin',id:request.id,peer:publicDevice(opposite),account:self.account,initiator:true,iceServers,serverNow:Date.now()});return;
          }
          const opposite=target.deserializeAttachment() as Seat;
          if(opposite.sync_links?.[request.id]!==seat.id)throw new Error('同账号连接已过期。');
          if(request.type==='sync_signal'){
            const payload=request.payload;
            if(!payload||typeof payload!=='object')throw new Error('同账号连接信令无效。');
            let value;
            if(payload.type==='description'&&['offer','answer'].includes(payload.descriptionType)&&typeof payload.sdp==='string'&&payload.sdp.length<=65000)
              value={type:'description',descriptionType:payload.descriptionType,sdp:payload.sdp};
            else if(payload.type==='candidate'&&typeof payload.candidate==='string'&&payload.candidate.length<=4096&&typeof payload.mid==='string'&&payload.mid.length<=256)
              value={type:'candidate',candidate:payload.candidate,mid:payload.mid};
            else if(payload.type==='cloud'&&check(payload.id,TRANSFER)&&check(payload.key,ID))
              value={type:'cloud',id:payload.id,key:payload.key};
            else if(payload.type==='route'&&routeSignal(payload.value))value={type:'route',value:routeSignal(payload.value)};
            else throw new Error('同账号连接信令无效。');
            this.send(target,{type:'sync_signal',id:request.id,payload:value});return;
          }
          if(request.type==='sync_lease'){this.send(socket,{type:'sync_lease',id:request.id});return;}
          if(request.type==='sync_close'){
            delete seat.sync_links?.[request.id];delete opposite.sync_links?.[request.id];
            socket.serializeAttachment(seat);target.serializeAttachment(opposite);
            this.send(target,{type:'sync_closed',id:request.id});return;
          }
          throw new Error('未知同账号连接指令。');
        }catch(error){this.send(socket,{type:'sync_error',id:request.id,message:error instanceof Error?error.message:'同账号连接失败。'});return;}
      }
      if(request.type==='lan_authorization'){
        if(!check(request.peer,ID)||request.peer===seat.id)throw new Error('局域网接收设备无效。');
        const self=await this.binding(seat.id),other=await this.binding(request.peer),target=this.socket(request.peer);
        if(!self||!other||self.account!==other.account||self.device===other.device||!target)
          throw new Error('局域网免确认仅允许已认证的同账号在线设备。');
        const source=socket.deserializeAttachment() as Seat,recipient=target.deserializeAttachment() as Seat;
        if(this.socket(seat.id)!==socket||source.auth_nonce!==self.nonce||recipient.auth_nonce!==other.nonce
            ||!source.account_active||!recipient.account_active)throw new Error('账号授权已改变。');
        const value=request.value;
        if(!value||value.type!=='lan-transfer'||!check(value.id,TRANSFER))throw new Error('局域网授权标识无效。');
        let metadata:object;
        if(value.action==='prepare'&&check(value.token,ID)&&check(value.digest,ID)&&check(value.fingerprint,ID))
          metadata={type:'lan-transfer',action:'prepare',id:value.id,token:value.token,digest:value.digest,fingerprint:value.fingerprint};
        else if(value.action==='accepted')metadata={type:'lan-transfer',action:'accepted',id:value.id};
        else throw new Error('局域网授权内容无效。');
        // Consent metadata only: never SDP, file content or a preconnected stream.
        this.send(target,{type:'lan_authorization',peer:seat.id,value:metadata});return;
      }
      if(request.type==='ping'){this.send(socket,{type:'pong'});return;}
      if(request.type==='account_logout'){await this.unbind(seat.id);return;}
      if(request.type==='account_state'){
        if(!check(request.nonce,TRANSFER)||typeof request.active!=='boolean')throw new Error('账号状态无效。');
        seat.auth_nonce=request.nonce;seat.account_active=request.active;delete seat.account;socket.serializeAttachment(seat);
        await this.unbind(seat.id,request.active);this.send(socket,{type:'account_state',nonce:request.nonce});return;
      }
      if(request.type==='hello'){
        if(request.features!==undefined&&(!Array.isArray(request.features)||request.features.length>4||request.features.some((value:unknown)=>!['routes-v1','mesh-v1','syncthing-v2'].includes(String(value)))))
          throw new Error('传输能力声明无效。');
        seat.features=request.features===undefined?[]:[...new Set<string>(request.features)];
        seat.name=String(request.name||'未命名设备').replace(/[\x00-\x1f]/g,'').slice(0,64);
        seat.platform=['windows','macos','linux'].includes(request.platform)?request.platform:'other';socket.serializeAttachment(seat);
        if(check(request.lan_id,/^[a-f0-9]{32}$/)){seat.lan_id=request.lan_id;socket.serializeAttachment(seat);}
        await this.ctx.storage.put('device:'+seat.id,publicDevice(seat));await this.code(socket);await this.presence(seat.id);
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
        await this.ctx.storage.transaction(async tx=>{
          const rows=await tx.get<Device[]>('recent:'+seat.id)||[];
          if(rows.some(row=>row.id===request.peer))await tx.put('trusted:'+seat.id+':'+request.peer,{accepted:Date.now()});
          await tx.put('recent:'+seat.id,rows.filter(row=>row.id!==request.peer));
          const hidden=await tx.get<string[]>('recent-hidden:'+seat.id)||[];
          await tx.put('recent-hidden:'+seat.id,Array.from(new Set([...hidden,request.peer])).slice(-100));
        });
        this.send(socket,{type:'peers',peers:await this.recent(seat.id)});return;
      }
      if(request.type==='prepare'){
        if(!check(request.id,TRANSFER)||!check(request.token,ID))throw new Error('传输请求无效。');
        let pairing=await this.ctx.storage.get<Pairing>('pairing:'+request.id);
        if(pairing){
          if(pairing.sender!==seat.id||pairing.token!==request.token||pairing.expires<=Date.now())throw new Error('接收码验证已失效，请重新发起。');
        }else{
          await this.limited('request:'+seat.id,15,60000);await this.limited('guess:'+seat.ip,30,60000);
          const resolved=await this.claimCode(request.code,seat,{id:request.id,token:request.token});
          pairing={sender:seat.id,receiver:resolved.receiver,token:request.token,expires:Date.now()+600000};
          const target=this.socket(resolved.receiver);if(target)await this.code(target);
        }
        const peer=this.peer(pairing.receiver);if(!peer)throw new Error('对方设备不在线。');
        this.send(socket,{type:'prepared',id:request.id,peer});return;
      }
      if(request.type==='cancel'&&check(request.id,TRANSFER)){
        const preparedOnly=await this.ctx.storage.transaction(async tx=>{
          if(await tx.get('session:'+request.id))return false;
          const pairing=await tx.get<Pairing>('pairing:'+request.id);
          if(pairing&&pairing.sender!==seat.id)throw new Error('无法取消此请求。');
          await tx.delete('pairing:'+request.id);
          await tx.put('pairing-cancel:'+seat.id+':'+request.id,{expires:Date.now()+600000});return true;
        });
        if(preparedOnly){this.send(socket,{type:'cancelled',id:request.id});return;}
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
        if(!request.prepared){await this.limited('request:'+seat.id,15,60000);await this.limited('guess:'+seat.ip,30,60000);}
        let target:string;
        if(request.prepared){
          const pairing=await this.ctx.storage.get<Pairing>('pairing:'+request.id);
          if(!pairing||pairing.sender!==seat.id||pairing.token!==request.token||pairing.expires<=Date.now())throw new Error('接收码验证已失效，请重新发起。');
          target=pairing.receiver;
        }else if(request.code){
          const resolved=await this.claimCode(request.code,seat);target=resolved.receiver;
        }else{
          const self=await this.binding(seat.id),other=check(request.peer,ID)?await this.binding(request.peer):null;
          const sameAccount=!!(self&&other&&self.account===other.account&&self.device!==other.device);
          const history=await this.ctx.storage.get<Device[]>('recent:'+seat.id)||[];
          if(!check(request.peer,ID))throw new Error('首次传输请使用对方的接收码。');
          const trusted=await this.ctx.storage.get('trusted:'+seat.id+':'+request.peer);
          if(!sameAccount&&!trusted&&history.every(row=>row.id!==request.peer)){
            const knownAccount=(await this.accountPeers(seat.id)).some(row=>row.id===request.peer);
            if(knownAccount)throw new Error('对方设备不在线。');
            throw new Error('首次传输请使用对方的接收码。');
          }
          target=request.peer;
        }
        const receiver=this.peer(target),recipient=this.socket(target);
        if(!receiver||!recipient)throw new Error('对方设备不在线。');
        if(await this.ctx.storage.get('session:'+request.id))throw new Error('传输请求已存在。');
        const native=seat.features?.includes('syncthing-v2')&&receiver.features?.includes('syncthing-v2');
        const value:Session={id:request.id,sender:publicDevice(seat),receiver,token:request.token,digest:request.digest,total:request.total,count:request.count,names:request.names,accepted:false,generation:0,expires:Date.now()+86400000,done:[],sender_code:await this.ctx.storage.get<string>('own-code:'+seat.id)||'',...(native?{native:{}}:{})};
        const already=await this.ctx.storage.transaction(async tx=>{
          if(await tx.get('pairing-cancel:'+seat.id+':'+request.id))throw new Error('已取消传输。');
          const previous=await tx.get<Session>('session:'+value.id);
          if(previous){
            if(previous.sender.id!==seat.id||previous.token!==value.token||previous.digest!==value.digest||previous.total!==value.total||previous.count!==value.count||JSON.stringify(previous.names)!==JSON.stringify(value.names))throw new Error('传输请求冲突。');
            return previous;
          }
          await tx.put({['session:'+value.id]:value,['pending:'+value.sender.id+':'+value.id]:{expires:value.expires},['pending:'+value.receiver.id+':'+value.id]:{expires:value.expires}});
          await tx.delete('pairing:'+request.id);return null;
        });
        if(already){await this.replay(socket,already);return;}
        const from=await this.binding(seat.id),to=await this.binding(target);
        const sameAccount=!!(from&&to&&from.account===to.account&&from.device!==to.device);
        if(!this.send(recipient,{type:'request',id:value.id,peer:value.sender,account_device:sameAccount,...(sameAccount?{account_id:from!.account}:{}),deviceCode:value.sender_code,token:value.token,digest:value.digest,total:value.total,count:value.count,names:value.names}))throw new Error('对方设备不在线，无法发送。');
        this.send(socket,{type:'waiting',id:value.id,peer:receiver});if(request.code&&!request.prepared)await this.code(recipient);return;
      }
      const value=await this.session(request.id,seat),other=value.sender.id===seat.id?value.receiver.id:value.sender.id;
      if(request.type==='accept'){
        if(seat.id!==value.receiver.id)throw new Error('无法接受此请求。');
        if(value.accepted){await this.replay(socket,value);return;}
        const accepted=await this.ctx.storage.transaction(async tx=>{
          const latest=await tx.get<Session>('session:'+value.id);if(!latest)throw new Error('传输请求已失效。');
          if(latest.accepted)return latest;
          latest.accepted=true;latest.generation++;await tx.put('session:'+latest.id,latest);return latest;
        });Object.assign(value,accepted);
        await this.remember(value);
        const from=value.native?await this.binding(value.sender.id):null,to=value.native?await this.binding(value.receiver.id):null;
        await this.publish(value,value.native?{type:'begin',initiator:value.sender.id,transport:'syncthing-v2',account_device:!!(from&&to&&from.account===to.account&&from.device!==to.device)}:{type:'begin',initiator:value.sender.id,iceServers:stun,relayAllowed:this.env.TURN_ENABLED==='true'});return;
      }
      if(request.type==='reject'||request.type==='cancel'){
        await this.publish(value,{type:'cancelled'});await this.removeSession(value);return;
      }
      if(!value.accepted)throw new Error('接收方尚未确认。');
      if(request.type==='native_signal'){
        if(!value.native||request.generation!==value.generation)throw Error('传输内核信令已失效。');
        const sender=seat.id===value.sender.id,metadata=manualSignal(request.value,sender);
        if(metadata.kind==='progress'&&metadata.total!==value.native.sender?.content?.size)throw Error('接收进度与发送内容不一致。');
        if(metadata.kind==='saved'&&(!value.native.sender?.content||!value.native.receiver&&!value.native.cloud))throw Error('传输内容尚未建立。');
        if(metadata.kind==='r2'){
          const from=await this.binding(value.sender.id),to=await this.binding(value.receiver.id);
          if(!from||!to||from.account!==to.account||from.device===to.device)throw Error('云端兜底需要两端登录同一账号。');
          const file=await this.env.DB.prepare('SELECT version,size,sha256,kind,deleted FROM items WHERE account_id=? AND id=?').bind(from.account,metadata.id).first<any>();
          if(!file||file.deleted||file.kind!=='transfer'||file.version!==metadata.version||file.size!==metadata.size||file.sha256!==metadata.sha256)
            throw Error('云端文件未保存或已改变。');
        }
        if(metadata.kind!=='progress')await this.ctx.storage.transaction(async tx=>{
          const latest=await tx.get<Session>('session:'+value.id);
          if(!latest?.native||!latest.accepted||latest.generation!==request.generation)throw Error('传输内核信令已失效。');
          const state=latest.native;
          if(metadata.kind==='peer'){
            const previous=sender?state.senderPeer:state.receiverPeer;
            const content=sender?state.sender:state.receiver;
            if(previous&&JSON.stringify(previous)!==JSON.stringify(metadata)||content&&content.device_id!==metadata.device_id)
              throw Error('传输设备身份已改变。');
            if(sender)state.senderPeer=metadata;else state.receiverPeer=metadata;
          }else if(metadata.kind==='native'){
            const peer=sender?state.senderPeer:state.receiverPeer;
            if(peer&&peer.device_id!==metadata.device_id)throw Error('传输设备身份已改变。');
            const previous=sender?state.sender:state.receiver;
            if(previous&&JSON.stringify(previous)!==JSON.stringify(metadata))throw Error('传输设备或文件内容已改变。');
            if(sender)state.sender=metadata;else state.receiver=metadata;
          }else if(metadata.kind==='r2'){
            if(!state.sender?.content||state.sender.content.size!==metadata.size||state.sender.content.sha256!==metadata.sha256)
              throw Error('云端文件与直连内容不一致。');
            state.cloud=metadata;
          }else if(metadata.kind==='fallback')state.fallback=true;
          else if(metadata.kind==='saved')state.saved=true;
          await tx.put('session:'+latest.id,latest);
        });
        const target=this.socket(other);if(target)this.send(target,{type:'native_signal',id:value.id,generation:value.generation,value:metadata});return;
      }
      if(request.type==='path_signal'){
        if(request.generation!==value.generation)throw new Error('连接信令已过期。');
        const metadata=routeSignal(request.value);if(!metadata)throw new Error('线路信令无效。');
        const target=this.socket(other);if(target)this.send(target,{type:'path_signal',id:value.id,generation:value.generation,value:metadata});return;
      }
      if(request.type==='cloud_path'){
        if(seat.id!==value.sender.id||request.generation!==value.generation||!check(request.pipe,TRANSFER)||!check(request.key,ID))throw new Error('云端连接信令无效。');
        const from=await this.binding(value.sender.id),to=await this.binding(value.receiver.id);
        if(!from||!to||from.account!==to.account||from.device===to.device)throw new Error('云端线路仅供已认证的同账号设备使用。');
        const target=this.socket(other);if(target)this.send(target,{type:'cloud_path',id:value.id,generation:value.generation,pipe:request.pipe,key:request.key});return;
      }
      if(request.type==='retry'){
        if(seat.id!==value.sender.id)throw new Error('仅发送方可恢复连接。');
        if(value.done.length===2){await this.publish(value,{type:'settled'});return;}
        await this.limited('retry:'+value.id,8,60000);
        value.generation++;value.connected=[];await this.ctx.storage.put('session:'+value.id,value);
        await this.publish(value,{type:'begin',initiator:value.sender.id,iceServers:stun,relayAllowed:this.env.TURN_ENABLED==='true'});return;
      }
      if(request.type==='connected'){
        await this.ctx.storage.transaction(async tx=>{
          const latest=await tx.get<Session>('session:'+value.id);
          if(!latest?.accepted||request.generation!==latest.generation)throw new Error('连接信令已过期。');
          latest.connected=Array.from(new Set([...(latest.connected||[]),seat.id]));
          await tx.put('session:'+latest.id,latest);
        });
        return;
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
        if(value.done.length===2){await this.publish(value,{type:'settled'});return;}
        const result=await this.ctx.storage.transaction(async tx=>{
          const latest=await tx.get<Session>('session:'+value.id);if(!latest?.accepted)throw new Error('传输请求已失效。');
          if(!latest.done.includes(seat.id))latest.done.push(seat.id);
          await tx.put('session:'+latest.id,latest);return {latest};
        });Object.assign(value,result.latest);
        if(value.done.length===2){
          value.expires=Date.now()+600000;await this.ctx.storage.put('session:'+value.id,value);
          await this.ctx.storage.delete(['pending:'+value.sender.id+':'+value.id,'pending:'+value.receiver.id+':'+value.id]);
          await this.revokeSession(value.id);
          await this.publish(value,{type:'settled'});
        }
        return;
      }
      throw new Error('未知的传输请求。');
    }catch(error){this.send(socket,{type:'error',id:check(request?.id,TRANSFER)?request.id:undefined,generation:Number.isSafeInteger(request?.generation)?request.generation:undefined,requestType:typeof request?.type==='string'?request.type:undefined,message:error instanceof Error?error.message:'广域网服务暂不可用。'});}
  }
  private async turn(value:Pick<Session,'id'|'total'>){
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
    const now=Date.now();let remaining=await this.cloudPipes.alarm(),paged=false,credentialCount=0;
    for(const socket of this.ctx.getWebSockets()){
      const seat=socket.deserializeAttachment() as Seat;
      if(now-(seat.last_seen||seat.connected)>90000){
        try{socket.close(4001,'Heartbeat expired');}catch{}
        await this.presence(seat.id);
      }
    }
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
    for(const prefix of ['sync-ice:','code:','pairing:','pairing-cancel:','session:','pending:','ip:','connect:','request:','guess:','retry:','relay:']){
      const cursor=await this.ctx.storage.get<string>('sweep:'+prefix);
      const rows=await this.ctx.storage.list<any>({prefix,limit:1000,...(cursor?{startAfter:cursor}:{})});
      const expired=[...rows].filter(([,row])=>(row.expires||row.until||now+1)<now).map(([key])=>key);
      if(expired.length)await this.ctx.storage.delete(expired);
      if(rows.size===1000){await this.ctx.storage.put('sweep:'+prefix,[...rows.keys()].pop()!);paged=true;}
      else await this.ctx.storage.delete('sweep:'+prefix);
      remaining=remaining||rows.size>expired.length||!!cursor;
    }
    if(paged||remaining||credentialCount||this.ctx.getWebSockets().length)await this.ctx.storage.setAlarm(now+(paged?1000:this.ctx.getWebSockets().length?30000:credentialCount?60000:300000));
  }
}
