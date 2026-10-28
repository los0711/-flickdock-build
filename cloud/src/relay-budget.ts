/** Provider analytics circuit breaker. This is deliberately NOT a billing cap:
 * analytics can lag and an issued credential can be reused before revocation.
 */
export interface RelayBudgetEnv {CF_ANALYTICS_TOKEN?:string;CF_ACCOUNT_ID?:string;TURN_MONTHLY_BUDGET_BYTES?:string;}
export async function relayUsage(env:RelayBudgetEnv,storage:DurableObjectStorage){
  const now=Date.now(),cached=await storage.get<{at:number;bytes:number;revision?:number}>('relay-usage');
  if(cached&&now-cached.at<30000)return cached;
  if(!env.CF_ANALYTICS_TOKEN||!/^[a-f0-9]{32}$/.test(env.CF_ACCOUNT_ID||''))throw new Error('中继用量监控暂不可用，仍可使用 P2P 直连。');
  const revision=await storage.transaction(async tx=>{
    const next=(await tx.get<number>('relay-usage-request')||0)+1;await tx.put('relay-usage-request',next);return next;
  });
  // A rolling 31 UTC-date window conservatively covers monthly billing cycles
  // without assuming that the owner's invoice resets on calendar day one.
  const start=new Date(now-30*86400000).toISOString().slice(0,10),end=new Date(now).toISOString().slice(0,10);
  const filter='limit:1,filter:{date_geq:"'+start+'",date_leq:"'+end+'"}';
  const query='{viewer{accounts(filter:{accountTag:"'+env.CF_ACCOUNT_ID+'"}){callsTurnUsageAdaptiveGroups('+filter+'){sum{egressBytes}}callsUsageAdaptiveGroups('+filter+'){sum{egressBytes}}}}}';
  const response=await fetch('https://api.cloudflare.com/client/v4/graphql',{method:'POST',headers:{Authorization:'Bearer '+env.CF_ANALYTICS_TOKEN,'Content-Type':'application/json'},body:JSON.stringify({query}),signal:AbortSignal.timeout(10000)});
  if(!response.ok)throw new Error('中继用量监控暂不可用，仍可使用 P2P 直连。');
  const data=await response.json() as any,account=data?.data?.viewer?.accounts?.[0];
  if(data.errors?.length||!Array.isArray(account?.callsTurnUsageAdaptiveGroups)||!Array.isArray(account?.callsUsageAdaptiveGroups))throw new Error('无法核实中继用量，暂时保留 P2P 直连。');
  const rows=[...account.callsTurnUsageAdaptiveGroups,...account.callsUsageAdaptiveGroups];
  const bytes=rows.reduce((sum:number,row:any)=>sum+Number(row.sum?.egressBytes),0);
  if(!Number.isFinite(bytes)||bytes<0)throw new Error('中继用量返回无效。');
  const value={at:now,bytes,revision};
  return storage.transaction(async tx=>{
    const latest=await tx.get<typeof value>('relay-usage');
    const requested=await tx.get<number>('relay-usage-request')||revision;
    if(requested>revision){
      if(latest?.revision===requested)return latest;
      throw new Error('中继用量监控正在更新，暂时保留 P2P 直连。');
    }
    await tx.put('relay-usage',value);return value;
  });
}
export function relayBudget(env:RelayBudgetEnv){
  const value=Number(env.TURN_MONTHLY_BUDGET_BYTES||'900000000000');
  if(!Number.isSafeInteger(value)||value<=0||value>900000000000)throw new Error('中继保护额度配置无效。');
  return value;
}
