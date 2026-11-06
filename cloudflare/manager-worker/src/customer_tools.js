/* Shared claim conversation; all routes run behind the manager's role and CSRF gates. */
import { ensureAftercareSchema } from './aftercare.js';
const rows=async(db,sql,args=[])=>((await db.execute(sql,args))?.rows||[]);
const fail=(message,status=400)=>{throw Object.assign(new Error(message),{status})};

async function notify(db,claim,key,text){
  if(!/^\d+$/.test(String(claim.bot_id||'')))fail('Open the claim in the main bot once to record its source bot.',409);
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(`${claim.bot_id}:${key}:${claim.telegram_id}`));
  const hash=Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');
  await db.execute("INSERT INTO bot_notification_jobs (event_key,bot_id,chat_id,body,status,attempts,next_attempt_at,created_at) VALUES (?,?,?,?,'queued',0,UTC_TIMESTAMP(),UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE event_key=event_key",[hash,claim.bot_id,String(claim.telegram_id),text]);
}

export async function customerToolsRoute(request,env,url,auth,admin,{reply,tx,hasPermission,body}){
  const match=url.pathname.match(/^\/manage\/claims\/(\d+)\/(conversation|cancellation)(?:\/(\d+)\/proof)?$/);
  if(!match)return null;
  await ensureAftercareSchema(env);
  const [,id,action,messageId]=match;
  if(!hasPermission(admin,'orders'))fail('Your role cannot view orders.',403);
  if(messageId&&request.method==='GET'&&action==='conversation'){
    const image=(await rows(env.TIDB,'SELECT TO_BASE64(image) AS content,mime FROM bot_claim_messages WHERE id=? AND order_id=?',[messageId,id]))[0];
    if(!image?.content||!['image/jpeg','image/png','image/webp'].includes(image.mime))fail('Evidence not found.',404);
    return new Response(Uint8Array.from(atob(image.content.replace(/\s/g,'')),c=>c.charCodeAt(0)),{headers:{'content-type':image.mime,'cache-control':'no-store','x-content-type-options':'nosniff'}});
  }
  if(request.method==='GET'){
    if(action==='cancellation')return reply({request:(await rows(env.TIDB,'SELECT reason,status,resolution,admin_id,created_at,updated_at FROM bot_cancellation_requests WHERE order_id=?',[id]))[0]||null});
    const messages=await rows(env.TIDB,'SELECT id,role,text,mime,actor_id,created_at FROM bot_claim_messages WHERE order_id=? ORDER BY id DESC LIMIT 100',[id]);
    return reply({messages:messages.reverse()});
  }
  if(request.method!=='POST'||!hasPermission(admin,'orders_write'))fail('Your role cannot update orders.',403);
  const b=await body(request);
  if(action==='conversation'){
    const text=String(b.text||'').trim();if(text.length<5||text.length>1500)fail('Write 5–1,500 characters asking for the information you need.');
    await tx(env,async db=>{
      const order=(await rows(db,'SELECT id,status,refunded FROM orders WHERE id=? FOR UPDATE',[id]))[0];
      const claim=(await rows(db,'SELECT * FROM bot_warranty_requests WHERE order_id=? FOR UPDATE',[id]))[0];
      if(!order||order.status!=='completed'||Number(order.refunded)||!claim||!['submitted','needs_info'].includes(claim.status))fail('This claim no longer accepts follow-ups.',409);
      await db.execute("INSERT INTO bot_claim_messages (order_id,actor_id,role,text,created_at) VALUES (?,?,'admin',?,UTC_TIMESTAMP())",[id,auth.admin_id,text]);
      const saved=(await rows(db,'SELECT id FROM bot_claim_messages WHERE order_id=? ORDER BY id DESC LIMIT 1',[id]))[0];
      await db.execute("UPDATE bot_warranty_requests SET status='needs_info',updated_at=UTC_TIMESTAMP() WHERE order_id=?",[id]);
      await db.execute("INSERT INTO order_events (order_id,kind,summary,actor_id,customer_visible,created_at) VALUES (?,'claim_information_requested','Team requested more claim information.',?,1,UTC_TIMESTAMP())",[id,auth.admin_id]);
      await notify(db,claim,`claim-reply:${saved.id}`,`Order #${id}: support needs more information. Open My Orders → Claim → Conversation in the bot.`);
    });return reply({success:true});
  }
  // Cancellation approval stays in the transactional bot refund path. The web
  // manager can inspect and decline without introducing a second refund engine.
  if(b.operation!=='decline')fail('Approve this cancellation from Admin Notify → Review cancellation in the bot. This keeps stock and referral reversal in the same refund transaction.',409);
  const reason=String(b.reason||'').trim();if(reason.length<5||reason.length>1000)fail('Provide a 5–1,000 character reason.');
  await tx(env,async db=>{
    await rows(db,'SELECT id FROM orders WHERE id=? FOR UPDATE',[id]);
    const row=(await rows(db,'SELECT * FROM bot_cancellation_requests WHERE order_id=? FOR UPDATE',[id]))[0];
    if(!row||row.status!=='submitted')fail('Cancellation already handled.',409);
    await db.execute("UPDATE bot_cancellation_requests SET status='declined',admin_id=?,resolution=?,updated_at=UTC_TIMESTAMP() WHERE order_id=?",[auth.admin_id,reason,id]);
    await db.execute("INSERT INTO order_events (order_id,kind,summary,actor_id,customer_visible,created_at) VALUES (?,'cancel_declined','Cancellation declined by the team.',?,1,UTC_TIMESTAMP())",[id,auth.admin_id]);
    await notify(db,row,`cancel-result:${id}`,`Order #${id}: cancellation declined. Open support for details.`);
  });return reply({success:true});
}
