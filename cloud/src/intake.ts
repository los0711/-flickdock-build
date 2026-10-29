/** Short-lived metadata only: announce shelf items before disk staging/hash work. */
type Intake={upload_id:string;item_id:string;name:string;kind:string;size:number;device_id:string;device_name:string;expires:number};
export async function intakeRequest(request:Request,ctx:DurableObjectState,publish:()=>Promise<unknown>){
  const now=Date.now(),key='shelf-intakes';
  if(request.method==='GET'){
    const rows=await ctx.storage.get<Record<string,Intake>>(key)||{};
    return Response.json({transfers:Object.values(rows).filter(row=>row.expires>now).map(row=>({...row,
      state:'preparing',received_bytes:0,expires_at:new Date(row.expires).toISOString()}))});
  }
  const value=await request.json() as {items?:Intake[];end?:string};
  await ctx.storage.transaction(async tx=>{
    const rows=await tx.get<Record<string,Intake>>(key)||{};
    const ended=await tx.get<Record<string,number>>('shelf-intakes-ended')||{};
    for(const id of Object.keys(rows))if(rows[id].expires<=now)delete rows[id];
    for(const id of Object.keys(ended))if(ended[id]<=now)delete ended[id];
    if(value.end){delete rows[value.end];ended[value.end]=now+86400000;}
    for(const row of value.items||[]){
      // An older, delayed announcement must not resurrect an initialized or cancelled upload.
      if(ended[row.upload_id])continue;
      if(!rows[row.upload_id]&&Object.keys(rows).length>=100)continue;
      rows[row.upload_id]={...row,expires:now+600000};
    }
    await tx.put(key,rows);await tx.put('shelf-intakes-ended',ended);
  });
  await publish();return Response.json({ok:true});
}
