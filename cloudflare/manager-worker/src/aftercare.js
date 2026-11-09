// Shared-database claim management. No Telegram token or payment call reaches the browser.
const DAY = 86400000, SCALE = 100000000n;
let schemaPromise;
const rows = async (db, sql, args=[]) => (await db.execute(sql,args)).rows || [];
const fail = (message,status=400) => { const e=new Error(message);e.status=status;throw e; };
const json = (value,fallback={}) => {try{return typeof value==='string'?JSON.parse(value):value??fallback}catch{return fallback}};
const utc = value => value instanceof Date?value:new Date(String(value).replace(' ','T').replace(/(?<!Z)(?<![+-]\d\d:\d\d)$/,'Z'));
const stamp = value => utc(value).toISOString().slice(0,19).replace('T',' ');
const html = value => String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
export function units(value){const s=typeof value==='number'?value.toFixed(8):String(value??'');if(!/^\d+(?:\.\d{1,8})?$/.test(s))fail('Invalid saved money amount.');const [a,b='']=s.split('.');return BigInt(a)*SCALE+BigInt(b.padEnd(8,'0'));}
export const money = value => `${value/SCALE}.${String(value%SCALE).padStart(8,'0')}`;
export const policy = order => order.aftercare_policy??(Number(order.warranty_days)>0?'warranty':'skip');
export function quote(order,at=new Date()){
  const days=Number(order.subscription_days),start=utc(order.coverage_started_at),when=utc(at);
  if(policy(order)!=='refund'||!Number.isInteger(days)||days<1||!Number.isFinite(+start)||+when<+start)return null;
  const used=Math.floor((when-start)/DAY)+1,remaining=Math.max(0,days-used),paid=units(order.amount);
  const value=used<=2?paid:paid*BigInt(remaining)*BigInt(used<=7?80:75)/BigInt(days)/100n;
  return {paid:money(paid),subscription_days:days,used_days:used,remaining_days:remaining,refund_amount:money(value)};
}

export async function ensureAftercareSchema(env){
  if(!schemaPromise)schemaPromise=(async()=>{
    const db=env.TIDB;
    await db.execute("CREATE TABLE IF NOT EXISTS bot_warranty_requests (id INT AUTO_INCREMENT PRIMARY KEY,order_id INT NOT NULL UNIQUE,telegram_id BIGINT NOT NULL,status VARCHAR(20) NOT NULL DEFAULT 'submitted',reason TEXT NOT NULL,evidence JSON NOT NULL,request_type VARCHAR(16) NOT NULL DEFAULT 'warranty',refund_amount DECIMAL(20,8) NULL,used_days INT NULL,remaining_days INT NULL,deactivated_at VARCHAR(200) NULL,admin_id BIGINT NULL,resolution TEXT NULL,created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)");
    await db.execute("CREATE TABLE IF NOT EXISTS bot_claim_messages (id INT AUTO_INCREMENT PRIMARY KEY,order_id INT NOT NULL,actor_id BIGINT NOT NULL,role VARCHAR(12) NOT NULL,text TEXT NOT NULL,image MEDIUMBLOB NULL,mime VARCHAR(32) NULL,created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,KEY ix_claim_message_order(order_id))");
    await db.execute("CREATE TABLE IF NOT EXISTS bot_cancellation_requests (order_id INT PRIMARY KEY,telegram_id BIGINT NOT NULL,bot_id BIGINT NOT NULL,reason TEXT NOT NULL,status VARCHAR(20) NOT NULL DEFAULT 'submitted',admin_id BIGINT NULL,resolution TEXT NULL,created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)");
    const definitions={products:{term_days:'INT NOT NULL DEFAULT 0',aftercare_policy:'VARCHAR(16) NULL',warranty_days:'INT NOT NULL DEFAULT 0',subscription_days:'INT NOT NULL DEFAULT 0',reusable_delivery:'TEXT NULL',reusable_stock:'INT NOT NULL DEFAULT 0'},orders:{received_at:'DATETIME NULL',subscription_expires_at:'DATETIME NULL',term_days:'INT NOT NULL DEFAULT 0',aftercare_policy:'VARCHAR(16) NULL',warranty_days:'INT NOT NULL DEFAULT 0',subscription_days:'INT NOT NULL DEFAULT 0',coverage_started_at:'DATETIME NULL',warranty_expires_at:'DATETIME NULL',refund_amount:'DECIMAL(20,8) NULL',reusable_delivery_account:'TEXT NULL',reusable_delivery_quantity:'INT NOT NULL DEFAULT 0'},bot_warranty_requests:{request_type:"VARCHAR(16) NOT NULL DEFAULT 'warranty'",refund_amount:'DECIMAL(20,8) NULL',used_days:'INT NULL',remaining_days:'INT NULL',deactivated_at:'VARCHAR(200) NULL',issue_type:"VARCHAR(32) NOT NULL DEFAULT 'other'",product_confirmed:'INT NOT NULL DEFAULT 0',invoice_snapshot:'JSON NULL',delivery_snapshot:'TEXT NULL',proof_image:'MEDIUMBLOB NULL',proof_mime:'VARCHAR(32) NULL',bot_id:'BIGINT NULL'}};
    for(const [table,columns] of Object.entries(definitions)){
      const found=new Set((await rows(db,`SHOW COLUMNS FROM ${table}`)).map(r=>r.Field));
      for(const [column,type] of Object.entries(columns))if(!found.has(column)){
        try{await db.execute(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`)}catch(e){if(!/1060|duplicate column/i.test(String(e.message)))throw e;}
      }
    }
  })().catch(e=>{schemaPromise=null;throw e});
  await schemaPromise;
}

async function optional(db,sql,args){try{return await rows(db,sql,args)}catch(e){if(/1146|doesn't exist|no such table|1054|unknown column/i.test(String(e.message)))return[];throw e}}
export async function recoverCoverage(db,order){
  if(order.status!=='completed'||order.coverage_started_at||policy(order)==='skip')return false;
  let date;
  if(Number(order.warranty_days)>0&&order.warranty_expires_at)date=new Date(+utc(order.warranty_expires_at)-Number(order.warranty_days)*DAY);
  if(!date)for(const [sql,args] of [
    ["SELECT MIN(created_at) AS at FROM order_events WHERE order_id=? AND kind='completed'",[order.id]],
    ["SELECT MIN(created_at) AS at FROM raindeals_web_manager_audit WHERE target_type='order' AND target_id=? AND action='fulfill'",[String(order.id)]],
    ["SELECT MIN(created_at) AS at FROM admin_audit_logs WHERE entity_type='order' AND entity_id=? AND action='manual_deliver_order'",[String(order.id)]],
    ["SELECT MIN(completed_at) AS at FROM raindeals_delivery_bot_jobs WHERE order_id=? AND status='sent'",[order.id]],
  ]){const value=(await optional(db,sql,args))[0]?.at;if(value){date=utc(value);break}}
  if(!date&&order.delivery_type==='automatic'&&!order.reseller_id&&order.delivered_account)date=utc(order.created_at);
  if(!date||!Number.isFinite(+date)||date<utc(order.created_at)||date>new Date())return false;
  await db.execute("UPDATE orders SET coverage_started_at=?,warranty_expires_at=CASE WHEN warranty_days>0 AND warranty_expires_at IS NULL THEN DATE_ADD(?,INTERVAL warranty_days DAY) ELSE warranty_expires_at END WHERE id=? AND coverage_started_at IS NULL",[stamp(date),stamp(date),order.id]);
  order.coverage_started_at=stamp(date);
  return true;
}

const invoice = o => ({reference:'RD-'+o.id,order_id:o.id,product_id:o.product_id,product:o.product_name,quantity:o.quantity,paid:String(o.amount),unit_paid:money(units(o.amount)/BigInt(Math.max(1,Number(o.quantity)||1))),customer_id:String(o.telegram_id),purchased_at:o.created_at,delivered_at:o.coverage_started_at,policy:policy(o),warranty_days:o.warranty_days,subscription_days:o.subscription_days,delivery_type:o.delivery_type,supplier_reference:o.reseller_order_id});
const metadataInvoice=(saved,o)=>({...invoice(o),...json(saved),delivered_at:o.coverage_started_at||json(saved).delivered_at,warranty_days:o.warranty_days,subscription_days:o.subscription_days});
const claimFields='id,order_id,telegram_id,status,reason,evidence,request_type,refund_amount,used_days,remaining_days,deactivated_at,issue_type,product_confirmed,invoice_snapshot,proof_mime,bot_id,admin_id,resolution,created_at,updated_at';
function imageMime(bytes){if(bytes[0]===255&&bytes[1]===216&&bytes[2]===255)return'image/jpeg';if(bytes[0]===137&&bytes[1]===80&&bytes[2]===78&&bytes[3]===71)return'image/png';if(new TextDecoder().decode(bytes.slice(0,4))==='RIFF'&&new TextDecoder().decode(bytes.slice(8,12))==='WEBP')return'image/webp';fail('Proof must be a JPG, PNG or WebP screenshot.');}
async function importLegacyProof(env,id){
  const claim=await getClaim(env.TIDB,id),evidence=json(claim?.evidence),token=env.MANAGER_TELEGRAM_BOT_TOKEN;
  if(!token||evidence.kind!=='photo'||!evidence.file_id)return null;
  const info=await fetch(`https://api.telegram.org/bot${token}/getFile`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({file_id:evidence.file_id}),signal:AbortSignal.timeout(15000)});
  const meta=await info.json(),path=meta.result?.file_path;
  if(!info.ok||!meta.ok||!/^[A-Za-z0-9_./-]+$/.test(path||'')||path.includes('..'))return null;
  const response=await fetch(`https://api.telegram.org/file/bot${token}/${path}`,{signal:AbortSignal.timeout(15000)});
  if(!response.ok||Number(response.headers.get('content-length')||0)>2097152)return null;
  const reader=response.body.getReader(),chunks=[];let size=0;
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>2097152){await reader.cancel();fail('Proof exceeds the 2 MB limit.',413)}chunks.push(value)}
  const bytes=new Uint8Array(size);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.length}
  const mime=imageMime(bytes);let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
  await env.TIDB.execute('UPDATE bot_warranty_requests SET proof_image=FROM_BASE64(?),proof_mime=? WHERE order_id=? AND proof_image IS NULL',[btoa(binary),mime,id]);
  return {bytes,mime};
}
async function getOrder(db,id,lock=false){return(await rows(db,'SELECT * FROM orders WHERE id=?'+(lock?' FOR UPDATE':''),[id]))[0]}
async function getClaim(db,id,lock=false){return(await rows(db,'SELECT '+claimFields+' FROM bot_warranty_requests WHERE order_id=?'+(lock?' FOR UPDATE':''),[id]))[0]}
async function event(db,id,kind,summary,actor){await db.execute('INSERT INTO order_events (order_id,kind,summary,actor_id,customer_visible,created_at) VALUES (?,?,?,?,1,UTC_TIMESTAMP())',[id,kind,summary,actor]);}
async function audit(db,id,actor,action,details){await db.execute('INSERT INTO raindeals_web_manager_audit (admin_id,action,target_type,target_id,details,created_at) VALUES (?,?,\'order\',?,?,UTC_TIMESTAMP(6))',[actor,action,String(id),JSON.stringify(details)]);}
async function enqueue(db,env,claim,eventKey,text){
  const botId=claim.bot_id||String(env.MANAGER_TELEGRAM_BOT_TOKEN||'').split(':')[0];
  if(!/^\d+$/.test(String(botId||'')))fail('This older claim has no source bot recorded. Open its claim screen in the bot once, then retry.',409);
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(`${botId}:${eventKey}:${claim.telegram_id}`));
  const hash=Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');
  await db.execute("INSERT INTO bot_notification_jobs (event_key,bot_id,chat_id,body,status,attempts,next_attempt_at,created_at) VALUES (?,?,?,?,'queued',0,UTC_TIMESTAMP(),UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE event_key=event_key",[hash,botId,String(claim.telegram_id),text]);
}

export async function claimContext(env,id,canDelivery=false){
  await ensureAftercareSchema(env);
  let order=await getOrder(env.TIDB,id);if(!order)fail('Order not found.',404);
  if(await recoverCoverage(env.TIDB,order))order=await getOrder(env.TIDB,id);
  const [claim,submissions,events,product]=await Promise.all([
    getClaim(env.TIDB,id),optional(env.TIDB,'SELECT telegram_id,`values`,submitted_at FROM order_requirement_submissions WHERE order_id=?',[id]),
    optional(env.TIDB,'SELECT kind,summary,actor_id,created_at FROM order_events WHERE order_id=? AND customer_visible=1 ORDER BY id ASC LIMIT 100',[id]),
    order.product_id?rows(env.TIDB,'SELECT id,name,description,price,aftercare_policy,warranty_days,subscription_days FROM products WHERE id=?',[order.product_id]):[],
  ]);
  let delivery=null;
  if(canDelivery){const snap=claim?(await rows(env.TIDB,'SELECT delivery_snapshot FROM bot_warranty_requests WHERE order_id=?',[id]))[0]?.delivery_snapshot:null;delivery=snap||order.delivered_account||'';}
  const q=quote(order),missing=policy(order)==='refund'?(!Number(order.subscription_days)||!order.coverage_started_at):policy(order)==='warranty'&&(!Number(order.warranty_days)||!order.warranty_expires_at);
  const safeOrder={...invoice(order),term_days:order.term_days||0,subscription_expires_at:order.subscription_expires_at||null,received_at:order.received_at||null,status:order.status,refunded:!!Number(order.refunded)};
  const safeClaim=claim?{...claim,evidence:json(claim.evidence),invoice_snapshot:undefined,bot_id:undefined}:null;
  if(safeClaim&&safeClaim.request_type==='warranty'&&safeClaim.status==='completed'&&!canDelivery)safeClaim.resolution='Replacement delivered. Your role cannot view delivery credentials.';
  return {order:safeOrder,invoice:metadataInvoice(claim?.invoice_snapshot,order),product:product[0]||null,
    claim:safeClaim,
    submitted_details:submissions.map(s=>({...s,values:json(s.values)})),history:events,delivery_content:delivery,
    quote:q,metadata_missing:missing,proof_url:claim?.proof_mime||json(claim?.evidence).kind==='photo'?`/manage/claims/${id}/proof`:null};
}

export async function claimsRoute(request,env,url,auth,admin,helpers){
  const {reply,tx,hasPermission}=helpers,path=url.pathname;
  if(path!=='/manage/claims'&&!path.startsWith('/manage/claims/'))return null;
  await ensureAftercareSchema(env);
  if(path==='/manage/claims'&&request.method==='GET'){
    const page=Math.max(1,Math.min(100000,Number(url.searchParams.get('page'))||1));
    const search=(url.searchParams.get('search')||'').slice(0,100),status=url.searchParams.get('status')||'active';
    let where=status==='active'?" WHERE c.status IN ('submitted','needs_info','approved') AND o.status='completed' AND COALESCE(o.refunded,0)=0":'';const args=[];
    if(search){where+=where?' AND ':' WHERE ';where+='(CAST(c.order_id AS CHAR) LIKE ? OR o.product_name LIKE ? OR CAST(c.telegram_id AS CHAR) LIKE ?)';args.push(...Array(3).fill('%'+search+'%'));}
    const found=await rows(env.TIDB,'SELECT c.order_id,c.status,c.request_type,c.refund_amount,c.created_at,c.issue_type,o.product_name,c.telegram_id FROM bot_warranty_requests c JOIN orders o ON o.id=c.order_id'+where+' ORDER BY c.created_at DESC,c.id DESC LIMIT 31 OFFSET '+((page-1)*30),args);
    return reply({rows:found.slice(0,30),has_more:found.length>30,page});
  }
  const match=path.match(/^\/manage\/claims\/(\d+)(?:\/(proof|review|metadata))?$/);if(!match)fail('Claim endpoint not found.',404);
  const [,id,action]=match;
  if(!action&&request.method==='GET')return reply(await claimContext(env,id,hasPermission(admin,'delivery_secrets')&&hasPermission(admin,'orders_write')));
  if(action==='proof'&&request.method==='GET'){
    const file=(await rows(env.TIDB,'SELECT TO_BASE64(proof_image) AS content,proof_mime FROM bot_warranty_requests WHERE order_id=?',[id]))[0];
    if(!file?.content||!['image/jpeg','image/png','image/webp'].includes(file.proof_mime)){
      let imported;try{imported=await importLegacyProof(env,id)}catch(e){if(e.status)throw e;}
      if(imported)return new Response(imported.bytes,{headers:{'content-type':imported.mime,'cache-control':'no-store'}});
      fail('This older proof exists only in Telegram. Open View evidence in the bot once to save it for web review.',404);
    }
    const raw=atob(String(file.content).replace(/\s/g,''));return new Response(Uint8Array.from(raw,c=>c.charCodeAt(0)),{headers:{'content-type':file.proof_mime,'cache-control':'no-store','content-disposition':'inline'}});
  }
  if(request.method!=='POST'||!hasPermission(admin,'orders_write'))fail('Your role cannot change claims.',403);
  const b=await helpers.body(request);
  if(action==='metadata'){
    const days=Number(b.days),start=utc(b.delivered_at),note=String(b.note||'').trim();
    if(b.confirm!==true||!Number.isInteger(days)||days<1||days>3650||!Number.isFinite(+start)||note.length<5||note.length>500)fail('Confirm 1–3650 days, the actual UTC delivery time, and a reason.');
    await tx(env,async db=>{
      const order=await getOrder(db,id,true),claim=await getClaim(db,id,true);
      if(!order||order.status!=='completed'||Number(order.refunded)||policy(order)==='skip')fail('Only a completed, non-refunded order with an existing policy can be corrected.',409);
      if(claim&&claim.status!=='submitted')fail('The claim was already reviewed.',409);
      if(start<utc(order.created_at)||start>utc(claim?.created_at||new Date()))fail('Delivery must be between purchase and claim submission (or now).');
      const updated={...order,coverage_started_at:stamp(start),...(policy(order)==='refund'?{subscription_days:days,warranty_days:0,warranty_expires_at:null}:{subscription_days:0,warranty_days:days,warranty_expires_at:stamp(new Date(+start+days*DAY))})};
      const q=claim&&policy(order)==='refund'?quote(updated,claim.created_at):null;
      if(claim&&policy(order)==='refund'&&(!q||units(q.refund_amount)<=0n))fail('No refundable amount remains at submission. Decline with a reason instead.');
      await db.execute('UPDATE orders SET coverage_started_at=?,subscription_days=?,warranty_days=?,warranty_expires_at=? WHERE id=?',[updated.coverage_started_at,updated.subscription_days||0,updated.warranty_days||0,updated.warranty_expires_at||null,id]);
      if(claim)await db.execute('UPDATE bot_warranty_requests SET invoice_snapshot=?,refund_amount=?,used_days=?,remaining_days=?,updated_at=UTC_TIMESTAMP() WHERE order_id=?',[JSON.stringify(metadataInvoice(claim.invoice_snapshot,updated)),q?.refund_amount??claim.refund_amount,q?.used_days??claim.used_days,q?.remaining_days??claim.remaining_days,id]);
      await event(db,id,'aftercare_metadata_confirmed','Team confirmed the subscription and delivery date.',auth.admin_id);
      await audit(db,id,auth.admin_id,'aftercare_metadata',{days,delivered_at:stamp(start),note});
    });return reply({success:true,message:'Confirmed metadata saved. The claim calculation uses its original submission time.'});
  }
  if(action==='review'){
    const operation=b.operation,resolution=String(b.resolution||'').trim();
    if(!['approve','reject','complete'].includes(operation)||b.confirm!==true)fail('Confirm a valid claim action.');
    if(['reject','complete'].includes(operation)&&(!resolution||resolution.length>3000))fail('Enter the reason or replacement details (up to 3000 characters).');
    await tx(env,async db=>{
      const order=await getOrder(db,id,true),claim=await getClaim(db,id,true);
      if(!order||!claim)fail('Claim not found.',404);
      if(order.status!=='completed'||Number(order.refunded)||claim.status!==(operation==='complete'?'approved':'submitted'))fail('This claim or order was already handled. Refresh.',409);
      if(operation==='approve'&&b.proof_reviewed!==true)fail('Confirm that you reviewed the product, invoice and proof.');
      if(claim.request_type==='refund'&&operation==='complete')fail('Use Approve refund.');
      let next=operation==='approve'?'approved':operation==='reject'?'rejected':'completed',summary,resolved=resolution||null;
      if(claim.request_type==='refund'&&operation==='approve'){
        if(claim.refund_amount==null)fail('Confirm the subscription and delivery date first.',409);
        const amount=units(claim.refund_amount);if(amount<=0n||amount>units(order.amount))fail('The saved refund amount is invalid.');
        const buyer=(await rows(db,'SELECT telegram_id,referred_by FROM users WHERE telegram_id=? FOR UPDATE',[order.telegram_id]))[0];if(!buyer)fail('Buyer wallet not found.',409);
        await db.execute('UPDATE users SET balance=COALESCE(balance,0)+? WHERE telegram_id=?',[money(amount),order.telegram_id]);
        if(buyer.referred_by){const saved=json(claim.invoice_snapshot),rate=units(saved.referral_commission_rate??env.REFERRAL_COMMISSION_RATE??'0.05');if(rate>SCALE)fail('Invalid saved referral commission.');const commission=amount*rate/SCALE,credit=saved.referral_credit_to_balance??(String(env.REFERRAL_CREDIT_TO_BALANCE||'true')!=='false');if(commission>0n)await db.execute('UPDATE users SET referral_earnings=GREATEST(0,COALESCE(referral_earnings,0)-?)'+(credit?',balance=COALESCE(balance,0)-?':'')+' WHERE telegram_id=?',credit?[money(commission),money(commission),buyer.referred_by]:[money(commission),buyer.referred_by]);}
        await db.execute("UPDATE orders SET refunded=1,status='refunded',refund_amount=? WHERE id=?",[money(amount),id]);
        next='completed';resolved=`Approved refund: $${money(amount)} USDT credited to your bot wallet.`;summary=resolved;
        await enqueue(db,env,claim,`warranty:${id}:refund`,`💸 <b>Order #${id} · Refund approved</b>\n${summary}`);
      }else{
        summary=operation==='approve'?'Replacement approved; awaiting team delivery.':operation==='reject'?`${claim.request_type==='refund'?'Refund':'Warranty'} request declined.`:'Replacement delivered.';
        await enqueue(db,env,claim,`warranty:${id}:${next}`,`🛡 <b>Order #${id} · Claim</b>\n${summary}${resolved?'\n\n'+html(resolved):''}`);
      }
      await db.execute('UPDATE bot_warranty_requests SET status=?,admin_id=?,resolution=?,updated_at=UTC_TIMESTAMP() WHERE order_id=?',[next,auth.admin_id,resolved,id]);
      await event(db,id,claim.request_type==='refund'&&operation==='approve'?'refunded':claim.request_type+'_'+next,summary,auth.admin_id);
      await audit(db,id,auth.admin_id,'claim_'+operation,{request_type:claim.request_type,amount:claim.refund_amount,proof_reviewed:b.proof_reviewed===true});
    });return reply({success:true,message:'Claim updated. The customer notification is queued in the bot.'});
  }
  fail('Claim action not found.',404);
}
