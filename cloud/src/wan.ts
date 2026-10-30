import {DurableObject} from 'cloudflare:workers';
import {relayUsage,relayBudget,type RelayBudgetEnv} from './relay-budget';
type RelayGate={epoch:number;blocked:boolean};
type Credential={username:string;expires:number;revoke_pending?:boolean};
type Device={id:string;name:string;platform:string;online?:boolean;last?:number;lan_id?:string;account_device?:boolean};
type Seat=Device&{ip:string;connected:number;window:number;messages:number;last_seen?:number;watching?:string[];account?:string;auth_nonce?:string;account_active?:boolean};
type Binding={account:string;device:string;session:string;lan_id:string;nonce:string};
type CustomRef={code:string;nonce:string};
type CodeRow={device:string;expires:number;epoch?:number;custom?:boolean;nonce?:string;status?:'available'|'used'|'cancelled';lease?:string};
type OwnCustom=CustomRef&{expires:number;status:'available'|'used'|'cancelled'};
type Pairing={sender:string;receiver:string;token:string;expires:number;custom?:CustomRef};
type Session={id:string;sender:Device;receiver:Device;token:string;digest:string;total:string;count:number;names:string[];accepted:boolean;generation:number;expires:number;done:string[];connected?:string[];sender_code?:string;custom?:CustomRef;custom_consumed?:boolean};
export interface WanEnv extends RelayBudgetEnv {WAN:DurableObjectNamespace<WanHub>;DB:D1Database;TURN_KEY_ID?:string;TURN_KEY_TOKEN?:string;TURN_ENABLED?:string;}
const json=(value:object,status=200)=>Response.json({schema_version:1,...value},{status,headers:{'Cache-Control':'no-store'}});
const hash=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))),b=>b.toString(16).padStart(2,'0')).join('');
const check=(value:unknown,pattern:RegExp):value is string=>typeof value==='string'&&pattern.test(value);
const ID=/^[a-f0-9]{64}$/,TRANSFER=/^[a-f0-9]{32}$/;
const stun=[{urls:['stun:stun.cloudflare.com:3478']}];
function publicDevice(value:Device):Device{return {id:value.id,name:value.name,platform:value.platform,...(value.lan_id?{lan_id:value.lan_id}:{})};}

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
      rows.push({id:peerId||await hash('account-device:'+self.account+':'+device.id),name:device.name,platform:device.platform,
        account_device:true,online:!!(binding?.account===self.account&&binding.device===device.id&&this.socket(peerId)),lan_id:binding?.lan_id||''});
    }
    return rows;
  }
  private async accountPresence(accounts:string[]){
    for(const socket of this.ctx.getWebSockets()){
      const seat=socket.deserializeAttachment() as Seat;
      if(seat.account&&accounts.includes(seat.account))this.send(socket,{type:'peers',peers:await this.recent(seat.id)});
    }
  }
  private async unbind(id:string){
    const old=await this.ctx.storage.get<Binding>('account-binding:'+id);await this.ctx.storage.delete('account-binding:'+id);
    const live=this.socket(id);if(live){const seat=live.deserializeAttachment() as Seat;delete seat.account;live.serializeAttachment(seat);}
    if(old)await this.accountPresence([old.account]);
    if(live)this.send(live,{type:'peers',peers:await this.recent(id)});
  }
  async fetch(request:Request):Promise<Response>{
    const id=request.headers.get('X-Wan-Device'),ip=request.headers.get('X-Wan-IP');
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
    const alphabet='23456789ABCDEFGHJKLMNPQRSTUVWXYZ';let code='';
    const serverNow=Date.now(),expires=serverNow+300000,refreshAt=serverNow+240000;
    // Refresh leaves the previous code valid for its original remaining minute.
    // Redeeming either alias invalidates all aliases in the same epoch.
    for(let attempt=0;attempt<16;attempt++){
      code='';for(const byte of crypto.getRandomValues(new Uint8Array(8)))code+=alphabet[byte%alphabet.length];
      const saved=await this.ctx.storage.transaction(async tx=>{
        const old=await tx.get<CodeRow>('code:'+code);if(old&&old.expires>serverNow)return false;
        const epoch=await tx.get<number>('code-epoch:'+seat.id)||0;
        await tx.put({['code:'+code]:{device:seat.id,expires,epoch},['own-code:'+seat.id]:code});return true;
      });
      if(saved)break;if(attempt===15)throw new Error('接收码暂不可用，请重试。');
    }
    this.send(socket,{type:'identity',device:publicDevice(seat),code,expires,refreshAt,serverNow,recent:await this.recent(seat.id)});
  }
  private async customStatus(socket:WebSocket,operation?:string){
    const seat=socket.deserializeAttachment() as Seat,own=await this.ctx.storage.get<OwnCustom>('own-custom:'+seat.id);
    const row=own?await this.ctx.storage.get<CodeRow>('code:'+own.code):undefined;
    const status=own?.status==='available'?(own.expires<=Date.now()?'expired':row?.nonce===own.nonce&&row.lease?'connecting':'available'):own?.status||'empty';
    this.send(socket,{type:'custom_code',operation,code:own?.code||'',nonce:own?.nonce||'',expires:own?.expires||0,
      status,serverNow:Date.now()});
  }
  private async setCustom(socket:WebSocket,request:{id:unknown;code:unknown}){
    const seat=socket.deserializeAttachment() as Seat;
    await this.limited('custom:'+seat.id,8,60000);await this.limited('custom-ip:'+seat.ip,24,60000);
    if(!check(request.id,TRANSFER)||!check(request.code,/^[0-9]{4}$/))throw new Error('一次性接收码需为 4 位数字。');
    const code=request.code,nonce=crypto.randomUUID().replaceAll('-',''),now=Date.now();
    await this.ctx.storage.transaction(async tx=>{
      if(this.socket(seat.id)!==socket)throw new Error('连接已更新，请重试。');
      const old=await tx.get<CodeRow>('code:'+code),own=await tx.get<OwnCustom>('own-custom:'+seat.id);
      if(old&&old.expires>now){
        if(old.custom&&old.device===seat.id&&old.status==='available'&&own?.code===code&&own.nonce===old.nonce)return;
        throw new Error('该接收码已被使用，请更换');
      }
      if(own&&own.code!==code){
        const previous=await tx.get<CodeRow>('code:'+own.code);
        if(previous?.custom&&previous.device===seat.id&&previous.nonce===own.nonce&&previous.status==='available'){
          previous.status='cancelled';await tx.put('code:'+own.code,previous);
        }
      }
      const value:OwnCustom={code,nonce,expires:now+600000,status:'available'};
      await tx.put({['code:'+code]:{device:seat.id,expires:value.expires,custom:true,nonce,status:'available'} satisfies CodeRow,
        ['own-custom:'+seat.id]:value});
    });
    await this.customStatus(socket,request.id);
  }
  private async cancelCustom(socket:WebSocket,request:{id:unknown;nonce:unknown}){
    const seat=socket.deserializeAttachment() as Seat;
    await this.limited('custom:'+seat.id,8,60000);
    if(!check(request.id,TRANSFER)||!check(request.nonce,TRANSFER))throw new Error('接收码状态无效。');
    await this.ctx.storage.transaction(async tx=>{
      const own=await tx.get<OwnCustom>('own-custom:'+seat.id);
      if(!own||own.nonce!==request.nonce)throw new Error('接收码状态已更新，请重试。');
      const row=await tx.get<CodeRow>('code:'+own.code);
      if(row?.custom&&row.device===seat.id&&row.nonce===own.nonce&&row.status==='available'){
        row.status='cancelled';await tx.put('code:'+own.code,row);
      }
      if(own.status!=='used'){own.status='cancelled';await tx.put('own-custom:'+seat.id,own);}
    });
    await this.customStatus(socket,request.id);
  }
  private async claimCode(code:unknown,seat:Seat,prepare?:{id:string;token:string}){
    if(!check(code,/^(?:[0-9]{4}|[23456789A-HJ-NP-Z]{8})$/))throw new Error('接收码无效或已过期，请复制对方当前的接收码。');
    return this.ctx.storage.transaction(async tx=>{
      if(prepare){
        const existing=await tx.get<Pairing>('pairing:'+prepare.id);
        if(existing){
          if(existing.sender!==seat.id||existing.token!==prepare.token||existing.expires<=Date.now())throw new Error('接收码验证已失效，请重新发起。');
          return {receiver:existing.receiver,custom:existing.custom};
        }
        if(await tx.get('pairing-cancel:'+seat.id+':'+prepare.id))throw new Error('已取消传输。');
      }
      const key='code:'+code,row=await tx.get<CodeRow>(key);
      if(!row||row.expires<=Date.now()||row.device===seat.id||(!row.custom&&(row.epoch||0)!==(await tx.get<number>('code-epoch:'+row.device)||0)))throw new Error('接收码无效或已过期，请复制对方当前的接收码。');
      if(row.custom&&(row.status!=='available'||row.lease))throw new Error(row.status==='used'?'接收码已使用，请获取新的接收码。':'接收码不可用或正在建立连接，请稍后重试。');
      if(!this.socket(row.device))throw new Error('对方设备不在线。');
      const custom=row.custom?{code,nonce:row.nonce!}:undefined;
      if(!custom){await tx.put('code-epoch:'+row.device,(row.epoch||0)+1);await tx.delete(key);}
      if(prepare)await tx.put('pairing:'+prepare.id,{sender:seat.id,receiver:row.device,token:prepare.token,expires:Date.now()+600000,...(custom?{custom}:{})} satisfies Pairing);
      return {receiver:row.device,custom};
    });
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
      this.send(socket,{type:'request',id:value.id,peer:value.sender,deviceCode:value.sender_code||'',token:value.token,digest:value.digest,total:value.total,count:value.count,names:value.names});
    }else this.send(socket,{type:'waiting',id:value.id,peer:value.receiver});
  }
  private async removeSession(value:Session){
    await this.ctx.storage.transaction(async tx=>{
      if(value.custom){
        const row=await tx.get<CodeRow>('code:'+value.custom.code);
        if(row?.custom&&row.nonce===value.custom.nonce&&row.lease===value.id&&row.status==='available'){
          delete row.lease;await tx.put('code:'+value.custom.code,row);
        }
      }
      await tx.delete(['session:'+value.id,'pending:'+value.sender.id+':'+value.id,'pending:'+value.receiver.id+':'+value.id]);
    });
    await this.revokeSession(value.id);
    const receiver=this.socket(value.receiver.id);if(value.custom&&receiver)await this.customStatus(receiver);
  }
  private async consumeCustom(tx:DurableObjectTransaction,value:Session){
    if(!value.custom||value.custom_consumed)return false;
    value.custom_consumed=true;
    const row=await tx.get<CodeRow>('code:'+value.custom.code);
    if(!row?.custom||row.nonce!==value.custom.nonce||row.lease!==value.id||row.status!=='available')return false;
    row.status='used';await tx.put('code:'+value.custom.code,row);
    const own=await tx.get<OwnCustom>('own-custom:'+value.receiver.id);
    if(own?.nonce===value.custom.nonce){own.status='used';await tx.put('own-custom:'+value.receiver.id,own);return true;}
    return false;
  }
  async webSocketMessage(socket:WebSocket,message:string|ArrayBuffer){
    let request:any;
    try{
      if(typeof message!=='string'||message.length>100000)throw new Error('信令内容无效。');
      const seat=socket.deserializeAttachment() as Seat;
      if(Date.now()-seat.connected>86400000){socket.close(4001,'Reconnect');return;}
      if(Date.now()-seat.window>60000){seat.window=Date.now();seat.messages=0;}
      if(++seat.messages>240)throw new Error('信令请求过于频繁。');
      seat.last_seen=Date.now();socket.serializeAttachment(seat);request=JSON.parse(message);
      if(request.type==='ping'){this.send(socket,{type:'pong'});return;}
      if(request.type==='account_logout'){await this.unbind(seat.id);return;}
      if(request.type==='account_state'){
        if(!check(request.nonce,TRANSFER)||typeof request.active!=='boolean')throw new Error('账号状态无效。');
        seat.auth_nonce=request.nonce;seat.account_active=request.active;delete seat.account;socket.serializeAttachment(seat);
        await this.unbind(seat.id);this.send(socket,{type:'account_state',nonce:request.nonce});return;
      }
      if(request.type==='hello'){
        seat.name=String(request.name||'未命名设备').replace(/[\x00-\x1f]/g,'').slice(0,64);
        seat.platform=['windows','macos','linux'].includes(request.platform)?request.platform:'other';socket.serializeAttachment(seat);
        if(check(request.lan_id,/^[a-f0-9]{32}$/)){seat.lan_id=request.lan_id;socket.serializeAttachment(seat);}
        await this.ctx.storage.put('device:'+seat.id,publicDevice(seat));await this.code(socket);await this.customStatus(socket);await this.presence(seat.id);
        const pending=await this.ctx.storage.list<{expires:number}>({prefix:'pending:'+seat.id+':',limit:64});
        for(const [key,row] of pending){
          if(row.expires<Date.now())continue;
          const value=await this.ctx.storage.get<Session>('session:'+key.split(':').pop());
          if(value&&value.expires>Date.now())await this.replay(socket,value);
        }
        return;
      }
      if(request.type==='refresh'){await this.code(socket);return;}
      if(request.type==='custom_set'){await this.setCustom(socket,request);return;}
      if(request.type==='custom_cancel'){await this.cancelCustom(socket,request);return;}
      if(request.type==='custom_status'){await this.customStatus(socket);return;}
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
          pairing={sender:seat.id,receiver:resolved.receiver,token:request.token,expires:Date.now()+600000,...(resolved.custom?{custom:resolved.custom}:{})};
          const target=this.socket(resolved.receiver);if(target&&!resolved.custom)await this.code(target);
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
        let target:string,custom:CustomRef|undefined;
        if(request.prepared){
          const pairing=await this.ctx.storage.get<Pairing>('pairing:'+request.id);
          if(!pairing||pairing.sender!==seat.id||pairing.token!==request.token||pairing.expires<=Date.now())throw new Error('接收码验证已失效，请重新发起。');
          target=pairing.receiver;custom=pairing.custom;
        }else if(request.code){
          const resolved=await this.claimCode(request.code,seat);target=resolved.receiver;custom=resolved.custom;
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
        const value:Session={id:request.id,sender:publicDevice(seat),receiver,token:request.token,digest:request.digest,total:request.total,count:request.count,names:request.names,accepted:false,generation:0,expires:Date.now()+86400000,done:[],sender_code:await this.ctx.storage.get<string>('own-code:'+seat.id)||'',...(custom?{custom}:{})};
        const already=await this.ctx.storage.transaction(async tx=>{
          if(await tx.get('pairing-cancel:'+seat.id+':'+request.id))throw new Error('已取消传输。');
          const previous=await tx.get<Session>('session:'+value.id);
          if(previous){
            if(previous.sender.id!==seat.id||previous.token!==value.token||previous.digest!==value.digest||previous.total!==value.total||previous.count!==value.count||JSON.stringify(previous.names)!==JSON.stringify(value.names))throw new Error('传输请求冲突。');
            return previous;
          }
          if(custom){
            const row=await tx.get<CodeRow>('code:'+custom.code);
            if(!row?.custom||row.nonce!==custom.nonce||row.device!==target||row.status!=='available'||row.expires<=Date.now()||row.lease)throw new Error('接收码已失效或正在建立连接，请重试。');
          }
          await tx.put({['session:'+value.id]:value,['pending:'+value.sender.id+':'+value.id]:{expires:value.expires},['pending:'+value.receiver.id+':'+value.id]:{expires:value.expires}});
          await tx.delete('pairing:'+request.id);return null;
        });
        if(already){await this.replay(socket,already);return;}
        if(!this.send(recipient,{type:'request',id:value.id,peer:value.sender,deviceCode:value.sender_code,token:value.token,digest:value.digest,total:value.total,count:value.count,names:value.names}))throw new Error('对方设备不在线，无法发送。');
        this.send(socket,{type:'waiting',id:value.id,peer:receiver});if(request.code&&!request.prepared&&!custom)await this.code(recipient);return;
      }
      const value=await this.session(request.id,seat),other=value.sender.id===seat.id?value.receiver.id:value.sender.id;
      if(request.type==='accept'){
        if(seat.id!==value.receiver.id)throw new Error('无法接受此请求。');
        if(value.accepted){await this.replay(socket,value);return;}
        const accepted=await this.ctx.storage.transaction(async tx=>{
          const latest=await tx.get<Session>('session:'+value.id);if(!latest)throw new Error('传输请求已失效。');
          if(latest.accepted)return latest;
          if(latest.custom){
            const row=await tx.get<CodeRow>('code:'+latest.custom.code);
            if(!row?.custom||row.device!==seat.id||row.nonce!==latest.custom.nonce||row.status!=='available'||row.expires<=Date.now()||row.lease&&row.lease!==latest.id)throw new Error('接收码已失效或正在建立另一会话。');
            row.lease=latest.id;await tx.put('code:'+latest.custom.code,row);
          }
          latest.accepted=true;latest.generation++;await tx.put('session:'+latest.id,latest);return latest;
        });Object.assign(value,accepted);
        await this.remember(value);
        if(value.custom)await this.customStatus(socket);
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
        const consumed=await this.ctx.storage.transaction(async tx=>{
          const latest=await tx.get<Session>('session:'+value.id);
          if(!latest?.accepted||request.generation!==latest.generation)throw new Error('连接信令已过期。');
          latest.connected=Array.from(new Set([...(latest.connected||[]),seat.id]));
          const used=latest.connected.length===2?await this.consumeCustom(tx,latest):false;
          await tx.put('session:'+latest.id,latest);return used;
        });
        const receiver=this.socket(value.receiver.id);if(consumed&&receiver)await this.customStatus(receiver);return;
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
          // Completion also proves an established session if an earlier
          // connected acknowledgement was lost during signaling reconnect.
          const consumed=seat.id===latest.receiver.id?await this.consumeCustom(tx,latest):false;
          await tx.put('session:'+latest.id,latest);return {latest,consumed};
        });Object.assign(value,result.latest);
        const receiver=this.socket(value.receiver.id);if(result.consumed&&receiver)await this.customStatus(receiver);
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
    for(const prefix of ['code:','pairing:','pairing-cancel:','session:','pending:','ip:','connect:','request:','guess:','retry:','relay:','custom:','custom-ip:']){
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
