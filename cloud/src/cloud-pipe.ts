/** Short-lived encrypted transport blocks, independent of shelf/clipboard state. */
import {storageBudget,StorageBudgetError,type StorageBudgetEnv} from './storage-budget';
type Binding={account:string;device:string;session:string};
type Block={key:string;sha:string;size:number;ready:boolean};
type Pipe={account:string;devices:string[];sessions:string[];expires:number;written:number[];read:number[];blocks:Record<string,Block>};
interface Env extends StorageBudgetEnv{FILES:R2Bucket;}
const MAX_BYTES=1024*1024+16,MAX_UNREAD=4,TTL_MS=120000;
const json=(value:object,status=200)=>Response.json({schema_version:1,...value},{status,headers:{'Cache-Control':'no-store'}});
const error=(message:string,status=400)=>json({error:{message}},status);

export class CloudPipes{
  private waiters=new Map<string,Set<()=>void>>();
  private ctx:DurableObjectState;
  private env:Env;
  private binding:(id:string)=>Promise<Binding|null>;
  constructor(ctx:DurableObjectState,env:Env,binding:(id:string)=>Promise<Binding|null>){this.ctx=ctx;this.env=env;this.binding=binding;}
  private wake(key:string){for(const done of this.waiters.get(key)||[])done();this.waiters.delete(key);}
  private async wait(key:string){
    await new Promise<void>(resolve=>{
      const done=()=>{clearTimeout(timer);const rows=this.waiters.get(key);rows?.delete(done);if(!rows?.size)this.waiters.delete(key);resolve();};
      const timer=setTimeout(done,8000),rows=this.waiters.get(key)||new Set();rows.add(done);this.waiters.set(key,rows);
    });
  }
  private async remove(id:string,value:Pipe){
    await this.ctx.storage.transaction(async tx=>{await tx.delete('cloud-pipe:'+id);await tx.put('cloud-pipe-closed:'+id,Date.now()+TTL_MS);const live=await tx.get<string[]>('cloud-pipe-active')||[];await tx.put('cloud-pipe-active',live.filter(key=>key!==id));});
    const keys=Object.values(value.blocks).map(row=>row.key);if(keys.length)await this.env.FILES.delete(keys);
    for(const key of [...this.waiters.keys()])if(key.startsWith(id+':'))this.wake(key);
  }
  async fetch(request:Request):Promise<Response>{
    const match=new URL(request.url).pathname.match(/^\/cloud\/([a-f0-9]{32})(?:\/([01])\/(\d{1,9}))?$/);
    if(!match)return error('Invalid cloud transport endpoint');
    const [,id,dirText,seqText]=match,key='cloud-pipe:'+id;
    const account=request.headers.get('X-Wan-Account')||'',device=request.headers.get('X-Wan-Account-Device')||'',session=request.headers.get('X-Wan-Session')||'';
    try{
      if(request.method==='POST'&&!dirText){
        const raw=await request.text();if(raw.length>1024)return error('Cloud transport request too large',413);
        const body=JSON.parse(raw);
        if(!body||typeof body!=='object'||Object.keys(body).join(',')!=='peer'||!/^([a-f0-9]{64})$/.test(body.peer))return error('Invalid cloud transport request');
        const first=await this.binding(request.headers.get('X-Wan-Device')||''),second=await this.binding(body.peer);
        if(!first||!second||first.account!==account||first.device!==device||first.session!==session||second.account!==account||second.device===device)return error('Account devices are not authorized',401);
        const value:Pipe={account,devices:[device,second.device],sessions:[session,second.session],expires:Date.now()+TTL_MS,written:[0,0],read:[0,0],blocks:{}};
        const created=await this.ctx.storage.transaction(async tx=>{
          if(await tx.get(key)||await tx.get('cloud-pipe-closed:'+id))return 'exists';
          const active=[];
          for(const old of await tx.get<string[]>('cloud-pipe-active')||[]){const row=await tx.get<Pipe>('cloud-pipe:'+old);if(row&&row.expires>Date.now())active.push(old);}
          if(active.length>=16)return 'limit';
          const owners=[];
          for(const member of value.devices){
            const ownerKey='cloud-pipe-owner:'+member,live=[];
            for(const row of await tx.get<{id:string;expires:number}[]>(ownerKey)||[]){const pipe=await tx.get<Pipe>('cloud-pipe:'+row.id);if(pipe&&pipe.expires>Date.now())live.push(row);}
            if(live.length>=4)return 'limit';
            owners.push({key:ownerKey,rows:[...live,{id,expires:value.expires}]});
          }
          for(const owner of owners)await tx.put(owner.key,owner.rows);
          await tx.put(key,value);await tx.put('cloud-pipe-active',[...active,id]);return 'created';
        });
        if(created!=='created')return error('Cloud transport '+created,created==='limit'?429:409);
        await this.ctx.storage.setAlarm(Date.now()+30000);return json({created:true},201);
      }
      const value=await this.ctx.storage.get<Pipe>(key);
      if(!value||value.expires<Date.now()||value.account!==account)return error('Cloud transport expired',410);
      const side=value.devices.indexOf(device);
      if(side<0||value.sessions[side]!==session)return error('Cloud transport ownership mismatch',401);
      const other=1-side;
      const authorized=await this.env.DB.prepare('SELECT 1 valid FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.account_id=? AND s.device_id=? AND s.expires_at>? AND d.revoked_at IS NULL').bind(value.sessions[other],value.account,value.devices[other],Date.now()).first();
      if(!authorized){await this.remove(id,value);return error('Peer account session expired',401);}
      if(request.method==='DELETE'&&!dirText){await this.remove(id,value);return json({closed:true});}
      if(!dirText||seqText===undefined)return error('Missing cloud block');
      const dir=Number(dirText),sequence=Number(seqText),blockKey=dir+':'+sequence;
      if(request.method==='PUT'){
        if(dir!==side)return error('Cannot write another device stream',403);
        const declared=Number(request.headers.get('Content-Length')),sha=request.headers.get('X-Content-SHA256')||'';
        if(!Number.isSafeInteger(declared)||declared<16||declared>MAX_BYTES||!/^([a-f0-9]{64})$/.test(sha)||!request.body)return error('Invalid cloud block',413);
        const reader=request.body.getReader(),chunks:Uint8Array[]=[];let size=0;
        try{while(true){const {value,done}=await reader.read();if(done)break;size+=value.byteLength;if(size>declared){await reader.cancel();return error('Cloud block size mismatch',413);}chunks.push(value);}}finally{reader.releaseLock();}
        if(size!==declared)return error('Cloud block size mismatch');
        const body=new Uint8Array(size);let offset=0;for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
        const objectKey='transport/'+account+'/'+id+'/'+dir+'/'+sequence;
        const reservation=await this.ctx.storage.transaction(async tx=>{
          const current=await tx.get<Pipe>(key);if(!current||current.expires<Date.now())return 'expired';
          const old=current.blocks[blockKey];if(old)return old.sha===sha&&old.size===declared?(old.ready?'ready':'retry'):'conflict';
          if(sequence!==current.written[dir]||current.written[dir]-current.read[dir]>=MAX_UNREAD)return 'window';
          current.blocks[blockKey]={key:objectKey,sha,size:declared,ready:false};await tx.put(key,current);return 'retry';
        });
        if(reservation==='ready')return json({accepted:true});
        if(reservation==='window')return json({error:{code:'CLOUD_WINDOW',message:'Cloud transport waiting for receiver'}},409);
        if(reservation!=='retry')return error('Cloud block '+reservation,reservation==='expired'?410:409);
        await storageBudget(this.env,'A',1);
        await this.env.FILES.put(objectKey,body,{sha256:sha});
        const stored=await this.ctx.storage.transaction(async tx=>{
          const current=await tx.get<Pipe>(key);if(!current||current.expires<Date.now()||current.blocks[blockKey]?.sha!==sha)return false;
          current.blocks[blockKey].ready=true;current.written[dir]=Math.max(current.written[dir],sequence+1);current.expires=Date.now()+TTL_MS;await tx.put(key,current);return true;
        });
        if(!stored){await this.env.FILES.delete(objectKey);return error('Cloud transport expired',410);}
        this.wake(id+':'+blockKey);return json({accepted:true});
      }
      if(dir===side)return error('Cannot read own cloud stream',403);
      if(request.method==='DELETE'){
        if(sequence<value.read[dir])return json({acknowledged:true});
        const block=value.blocks[blockKey];if(sequence!==value.read[dir]||!block?.ready)return error('Invalid cloud acknowledgement',409);
        await this.env.FILES.delete(block.key);
        await this.ctx.storage.transaction(async tx=>{const current=await tx.get<Pipe>(key);if(current&&current.read[dir]===sequence){delete current.blocks[blockKey];current.read[dir]++;current.expires=Date.now()+TTL_MS;await tx.put(key,current);}});
        return json({acknowledged:true});
      }
      if(request.method!=='GET')return error('Invalid cloud transport method',405);
      if(sequence<value.read[dir]||sequence>value.read[dir])return error('Cloud read position invalid',409);
      let block=value.blocks[blockKey];
      if(!block?.ready){
        const pending=this.wait(id+':'+blockKey),check=await this.ctx.storage.get<Pipe>(key);
        if(check?.blocks[blockKey]?.ready)this.wake(id+':'+blockKey);
        await pending;const current=await this.ctx.storage.get<Pipe>(key);if(!current)return error('Cloud transport expired',410);block=current.blocks[blockKey];
      }
      if(!block?.ready)return json({ready:false});
      await storageBudget(this.env,'B',1);const object=await this.env.FILES.get(block.key);
      if(!object)return error('Cloud block unavailable',503);
      const data=new Uint8Array(await object.arrayBuffer());
      const encoded=Buffer.from(data).toString('base64');return json({ready:true,body:encoded});
    }catch(problem){
      if(problem instanceof StorageBudgetError)return json({error:{code:problem.code,message:problem.message,...problem.details}},problem.status);
      return error('Cloud transport operation failed',503);
    }
  }
  async alarm(){
    const rows=await this.ctx.storage.list<Pipe>({prefix:'cloud-pipe:',limit:100});
    for(const [key,value] of rows)if(value.expires<Date.now())await this.remove(key.slice('cloud-pipe:'.length),value);
    const owners=await this.ctx.storage.list<{id:string;expires:number}[]>({prefix:'cloud-pipe-owner:',limit:100});
    for(const [key,value] of owners){const live=[];for(const row of value){const pipe=await this.ctx.storage.get<Pipe>('cloud-pipe:'+row.id);if(pipe&&pipe.expires>Date.now())live.push({id:row.id,expires:pipe.expires});}if(live.length)await this.ctx.storage.put(key,live);else await this.ctx.storage.delete(key);}
    const closed=await this.ctx.storage.list<number>({prefix:'cloud-pipe-closed:',limit:100});
    for(const [key,expires] of closed)if(expires<Date.now())await this.ctx.storage.delete(key);
    return rows.size===100||owners.size===100||closed.size===100||[...rows.values()].some(row=>row.expires>=Date.now())||[...closed.values()].some(expires=>expires>=Date.now());
  }
  async revoke(device:string,account:string){
    const rows=await this.ctx.storage.get<{id:string;expires:number}[]>('cloud-pipe-owner:'+device)||[];
    for(const row of rows){const value=await this.ctx.storage.get<Pipe>('cloud-pipe:'+row.id);if(value&&value.account===account&&value.devices.includes(device))await this.remove(row.id,value);}
    await this.ctx.storage.delete('cloud-pipe-owner:'+device);
  }
}
