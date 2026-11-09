/** Local-only probe: constructor identity proves the original DO was re-created. */
import application,{AccountEvents as ProductionEvents} from '../src/index';
export {WanHub,AppGateway} from '../src/index';

export class AccountEvents extends ProductionEvents {
  private readonly bootId=crypto.randomUUID();
  override async fetch(request:Request):Promise<Response>{
    if(new URL(request.url).pathname==='/__qa/boot')return Response.json({
      bootId:this.bootId,connections:this.ctx.getWebSockets().filter(ws=>ws.readyState===WebSocket.OPEN).length});
    return super.fetch(request);
  }
}

export default {
  async fetch(request:Request,env:Parameters<typeof application.fetch>[1],ctx:ExecutionContext){
    const url=new URL(request.url),account=url.searchParams.get('account')||'';
    if(url.hostname==='127.0.0.1'&&url.pathname==='/__qa/events'&&/^[a-f0-9-]{36}$/.test(account))
      return env.EVENTS.get(env.EVENTS.idFromName(account)).fetch('https://internal/__qa/boot');
    return application.fetch(request,env,ctx);
  },
};
