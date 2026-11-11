export interface StorageBudgetEnv{DB:D1Database;R2_CLASS_A_BUDGET?:string;R2_CLASS_B_BUDGET?:string;}
export class StorageBudgetError extends Error{
  status=503;code='STORAGE_REQUEST_BUDGET';details:Record<string,number>;
  constructor(){super('Storage request budget exhausted');const now=new Date();this.details={retry_after:Math.ceil((Date.UTC(now.getUTCFullYear(),now.getUTCMonth()+1,1)-now.getTime())/1000)};}
}
export async function storageBudget(env:StorageBudgetEnv,kind:'A'|'B',count:number,operations:D1PreparedStatement[]=[]){
  if(count<=0){if(operations.length)throw new Error('A storage transaction requires a positive budget');return;}
  const cap=Number((kind==='A'?env.R2_CLASS_A_BUDGET:env.R2_CLASS_B_BUDGET)??(kind==='A'?1000000:10000000));
  if(!Number.isSafeInteger(cap)||cap<1)throw new Error('Invalid storage budget configuration');
  const statement=env.DB.prepare('INSERT INTO budgets(period,kind,n) VALUES(?,?,?) ON CONFLICT(period,kind) DO UPDATE SET n=n+excluded.n RETURNING n').bind(new Date().toISOString().slice(0,7),kind,count);
  // Allocation and its request allowance share one primary-database round trip.
  // D1 rolls back every statement if an allocation/quota trigger rejects it.
  const row=operations.length?(await env.DB.batch<{n:number}>([...operations,statement])).at(-1)!.results[0]:await statement.first<{n:number}>();
  if(!row)throw new Error('Missing storage budget result');
  if(row.n>cap)throw new StorageBudgetError();
}
