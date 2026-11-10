export const SHELF_QUOTA_BYTES=1024**3;
const DAY_MS=86400000;
const BEIJING_OFFSET_MS=8*3600000;
export function shelfResetAt(now=Date.now()){
  return (Math.floor((now+BEIJING_OFFSET_MS)/DAY_MS)+1)*DAY_MS-BEIJING_OFFSET_MS;
}
export async function shelfStorage(env:{DB:D1Database},account:string){
  const row=await env.DB.prepare('SELECT COALESCE(SUM(size),0) used_bytes FROM shelf_usage WHERE account_id=?').bind(account).first<{used_bytes:number}>();
  if(!row||!Number.isSafeInteger(row.used_bytes)||row.used_bytes<0)throw new Error('Invalid shelf allocation total');
  return {used_bytes:row.used_bytes,quota_bytes:SHELF_QUOTA_BYTES,remaining_bytes:Math.max(0,SHELF_QUOTA_BYTES-row.used_bytes),reset_at:new Date(shelfResetAt()).toISOString(),server_time:new Date().toISOString()};
}
