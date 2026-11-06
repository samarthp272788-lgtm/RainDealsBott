import assert from 'node:assert/strict';
import worker from './src/index.js';
import {quote,units} from './src/aftercare.js';
import {page} from './src/ui.js';

// In-memory SQL fixtures, with serialized transactions. No live service or payment calls.
const date=n=>new Date(Date.now()-n*86400000).toISOString().slice(0,19).replace('T',' ');
const csrf='c'.repeat(64),calls=[];
let reader=false,failNotification=false,releaseLock=Promise.resolve();
const initial={order:{id:12,telegram_id:'777',product_id:2,product_name:'Original product',amount:'30.00000000',quantity:1,status:'completed',refunded:0,delivery_type:'manual',created_at:date(6),aftercare_policy:'refund',subscription_days:30,warranty_days:0,warranty_expires_at:null,coverage_started_at:null,reseller_id:null,reseller_order_id:null,delivered_account:'original:PRIVATE_DELIVERY'},
 claim:{id:9,order_id:12,telegram_id:'777',status:'submitted',request_type:'refund',reason:'The account was deactivated.',issue_type:'deactivated',product_confirmed:1,evidence:'{"kind":"photo","file_id":"fixture"}',refund_amount:'21.60000000',used_days:3,remaining_days:27,deactivated_at:'Yesterday',created_at:date(2),updated_at:date(2),admin_id:null,resolution:null,invoice_snapshot:null,delivery_snapshot:'original:PRIVATE_DELIVERY',proof_mime:'image/jpeg',proof_image:new Uint8Array([255,216,255,1]),bot_id:12345},
 product:{id:2,name:'Current product',description:'Current description',price:'40.00',aftercare_policy:null,warranty_days:0,subscription_days:0,stock:10,required_info:null},
 buyer:{telegram_id:'777',referred_by:null,balance:'5.00000000'},events:[{kind:'completed',summary:'Original fulfillment',actor_id:'123',created_at:date(4)}],notifications:[],audit:[],messages:[]};
let state=structuredClone(initial);
function selected(sql,row){const part=sql.slice(7,sql.indexOf(' FROM '));if(part==='*')return {...row};return Object.fromEntries(part.split(',').map(s=>s.trim().replaceAll('`','')).filter(k=>k in row).map(k=>[k,row[k]]));}
const env={MANAGER_ADMIN_IDS:'123',TIDB:{
 async execute(sql,args=[]){calls.push({sql,args});
  if(sql.includes('FROM raindeals_web_manager_force_kill'))return {rows:[{is_active:0}]};
  if(sql.includes('FROM raindeals_web_manager_sessions'))return {rows:[{csrf,admin_id:reader?'900':'123',token_hash:'hash'}]};
  if(sql.includes('FROM raindeals_web_manager_admins WHERE'))return {rows:[{telegram_id:'900',permissions_json:'["orders"]',is_active:1}]};
  if(sql.includes('COUNT(*) AS total FROM information_schema.tables'))return {rows:[{total:8}]};
  if(sql.startsWith('SHOW COLUMNS')){const table=sql.match(/FROM\s+`?(\w+)/)[1],value=table==='products'?state.product:table==='orders'?state.order:state.claim;return {rows:Object.keys(value).map(Field=>({Field}))};}
  if(sql.includes('FROM bot_claim_messages'))return {rows:state.messages.map(m=>({...m}))};
  if(sql.startsWith('INSERT INTO bot_claim_messages')){state.messages.push({id:state.messages.length+1,order_id:args[0],role:'admin',text:args[2],created_at:date(0)});return {rows:[]};}
  if(sql.startsWith("UPDATE bot_warranty_requests SET status='needs_info'")){state.claim.status='needs_info';return {rows:[]};}
  if(sql.includes('TO_BASE64(proof_image)'))return {rows:[{content:state.claim.proof_image?Buffer.from(state.claim.proof_image).toString('base64'):null,proof_mime:state.claim.proof_mime}]};
  if(sql.includes('MIN(created_at) AS at FROM order_events'))return {rows:[{at:state.events.find(e=>e.kind==='completed')?.created_at}]};
  if(sql.includes('MIN('))return {rows:[]};
  if(sql.includes('FROM bot_warranty_requests WHERE'))return {rows:[selected(sql,state.claim)]};
  if(sql.includes('FROM bot_warranty_requests c JOIN orders'))return {rows:[{...selected('SELECT order_id,status,request_type,refund_amount,created_at,issue_type,telegram_id FROM x',state.claim),product_name:state.order.product_name}]};
  if(sql.includes('FROM order_requirement_submissions'))return {rows:[{telegram_id:'777',values:'{"Email":{"kind":"text","value":"buyer@example.com"}}',submitted_at:date(5)}]};
  if(sql.includes('FROM order_events'))return {rows:structuredClone(state.events)};
  if(/FROM `?products`? WHERE/.test(sql))return {rows:[selected(sql,state.product)]};
  if(sql.includes('FROM orders WHERE'))return {rows:[selected(sql,state.order)]};
  if(sql.startsWith('SELECT telegram_id,referred_by FROM users'))return {rows:[{...state.buyer}]};
  if(sql.startsWith('UPDATE orders SET coverage_started_at=?,warranty_expires_at=CASE'))state.order.coverage_started_at=args[0];
  else if(sql.startsWith('UPDATE orders SET coverage_started_at=?,subscription_days='))Object.assign(state.order,{coverage_started_at:args[0],subscription_days:args[1],warranty_days:args[2],warranty_expires_at:args[3]});
  else if(sql.startsWith('UPDATE bot_warranty_requests SET invoice_snapshot='))Object.assign(state.claim,{invoice_snapshot:args[0],refund_amount:args[1],used_days:args[2],remaining_days:args[3]});
  else if(sql.startsWith('UPDATE users SET balance='))state.buyer.balance=(Number(state.buyer.balance)+Number(args[0])).toFixed(8);
  else if(sql.startsWith("UPDATE orders SET refunded=1"))Object.assign(state.order,{refunded:1,status:'refunded',refund_amount:args[0]});
  else if(sql.startsWith('UPDATE bot_warranty_requests SET status='))Object.assign(state.claim,{status:args[0],admin_id:args[1],resolution:args[2]});
  else if(sql.startsWith('UPDATE bot_warranty_requests SET proof_image='))Object.assign(state.claim,{proof_image:Buffer.from(args[0],'base64'),proof_mime:args[1]});
  else if(sql.startsWith('UPDATE `products` SET')){const fields=[...sql.matchAll(/`(\w+)`=\?/g)].map(m=>m[1]);fields.forEach((k,i)=>state.product[k]=args[i]);}
  else if(sql.startsWith('INSERT INTO bot_notification_jobs')){if(failNotification)throw Error('fixture queue failure');if(!state.notifications.some(j=>j.key===args[0]))state.notifications.push({key:args[0],bot_id:args[1],chat:args[2],text:args[3]});}
  else if(sql.startsWith('INSERT INTO order_events'))state.events.push({order_id:args[0],kind:args[1],summary:args[2],actor_id:args[3],created_at:date(0)});
  else if(sql.startsWith('INSERT INTO raindeals_web_manager_audit'))state.audit.push({args});
  return {rows:[]};
 },
 async begin(){const previous=releaseLock;let release;releaseLock=new Promise(r=>release=r);await previous;const snapshot=structuredClone(state);return {execute:(...args)=>env.TIDB.execute(...args),async commit(){release()},async rollback(){state=snapshot;release()}};}
}};
const request=(path,b,headers={})=>new Request('https://manager.example'+path,{method:b===undefined?'GET':'POST',headers:{cookie:'rd_manager='+'a'.repeat(64),origin:'https://manager.example','x-csrf-token':csrf,...headers},...(b===undefined?{}:{body:JSON.stringify(b)})});
const review={operation:'approve',proof_reviewed:true,confirm:true};
assert.equal((await worker.fetch(new Request('https://manager.example/manage/claims/12/conversation'),env)).status,401);
reader=true;
assert.equal((await worker.fetch(request('/manage/claims/12/conversation',{text:'Please add a login screenshot.'}),env)).status,403);
reader=false;
assert.equal((await worker.fetch(request('/manage/claims/12/conversation',{text:'Please add a login screenshot.'},{'x-csrf-token':'bad'}),env)).status,403);
assert.equal((await worker.fetch(request('/manage/claims/12/conversation',{text:'Please add a login screenshot.'}),env)).status,200);
assert.equal(state.claim.status,'needs_info');assert.equal(state.messages.length,1);
const conversation=await(await worker.fetch(request('/manage/claims/12/conversation'),env)).json();
assert.equal(conversation.messages[0].text,'Please add a login screenshot.');
assert(!JSON.stringify(conversation).includes('PRIVATE_DELIVERY'));
assert.equal(state.notifications[0].chat,'777');
state=structuredClone(initial);

assert.equal(units('0.00000001'),1n);
for(const [days,expected] of [[0,'30.00000000'],[1,'30.00000000'],[2,'21.60000000'],[6,'18.40000000'],[7,'16.50000000']]){
 assert.equal(quote({...state.order,coverage_started_at:'2026-11-01 00:00:00'},new Date(Date.UTC(2026,10,1+days))).refund_amount,expected);
}
assert.equal((await worker.fetch(new Request('https://manager.example/manage/claims/12'),env)).status,401);
let response=await worker.fetch(request('/manage/claims/12'),env);
assert.equal(response.status,200);
let detail=await response.json();
assert.equal(detail.order.delivered_at,initial.events[0].created_at);
assert.equal(detail.delivery_content,'original:PRIVATE_DELIVERY');
assert.equal(detail.submitted_details[0].values.Email.value,'buyer@example.com');
assert.equal(detail.proof_url,'/manage/claims/12/proof');
reader=true;
detail=await(await worker.fetch(request('/manage/claims/12'),env)).json();
assert.equal(detail.delivery_content,null);
assert(!JSON.stringify(detail).includes('PRIVATE_DELIVERY'));
assert.equal((await worker.fetch(request('/manage/claims/12/review',review),env)).status,403);
state.claim.request_type='warranty';state.claim.status='completed';state.claim.resolution='replacement:PRIVATE_REPLACEMENT';
detail=await(await worker.fetch(request('/manage/claims/12'),env)).json();
assert(!JSON.stringify(detail).includes('PRIVATE_REPLACEMENT'));
reader=false;state=structuredClone(initial);
response=await worker.fetch(request('/manage/claims/12/proof'),env);
assert.equal(response.headers.get('content-type'),'image/jpeg');
assert.deepEqual([...new Uint8Array(await response.arrayBuffer())],[255,216,255,1]);
assert.equal((await worker.fetch(request('/manage/claims/12/review',review,{'x-csrf-token':'invalid'}),env)).status,403);
assert.equal((await worker.fetch(request('/manage/claims/12/review',{...review,proof_reviewed:false}),env)).status,400);
assert.equal(state.buyer.balance,'5.00000000');
failNotification=true;
assert.equal((await worker.fetch(request('/manage/claims/12/review',review),env)).status,503);
assert.equal(state.buyer.balance,'5.00000000');assert.equal(state.claim.status,'submitted');assert.equal(state.order.refunded,0);
failNotification=false;
const approvals=await Promise.all([worker.fetch(request('/manage/claims/12/review',review),env),worker.fetch(request('/manage/claims/12/review',review),env)]);
assert.deepEqual(approvals.map(r=>r.status).sort(),[200,409]);
assert.equal(state.buyer.balance,'26.60000000');assert.equal(state.notifications.length,1);assert.equal(state.claim.status,'completed');
assert(!calls.some(c=>c.sql.startsWith('UPDATE products SET file_content')));
state=structuredClone(initial);state.claim.refund_amount=null;state.order.subscription_days=0;state.order.coverage_started_at=null;
state.claim.invoice_snapshot=JSON.stringify({product:'Original invoice name',paid:'30.00000000'});state.order.product_name='Edited later';
assert.equal((await worker.fetch(request('/manage/claims/12/review',review),env)).status,409);
response=await worker.fetch(request('/manage/claims/12/metadata',{days:30,delivered_at:date(4),note:'Checked fulfillment history',confirm:true}),env);
assert.equal(response.status,200);assert(state.claim.refund_amount);assert.equal(state.buyer.balance,'5.00000000');
assert.equal(JSON.parse(state.claim.invoice_snapshot).product,'Original invoice name');
assert.equal((await worker.fetch(request('/manage/products/2',{aftercare_policy:'refund',subscription_days:30}),env)).status,200);
assert.equal(state.product.aftercare_policy,'refund');assert.equal(state.product.subscription_days,30);assert.equal(state.product.warranty_days,0);
assert.equal((await worker.fetch(request('/manage/products/2',{aftercare_policy:'refund',subscription_days:0}),env)).status,400);
state=structuredClone(initial);state.order.aftercare_policy='warranty';state.order.warranty_days=7;state.order.subscription_days=0;
state.claim.request_type='warranty';state.claim.refund_amount=null;
assert.equal((await worker.fetch(request('/manage/claims/12/review',review),env)).status,200);
assert.equal(state.claim.status,'approved');assert.equal(state.buyer.balance,'5.00000000');
assert.equal((await worker.fetch(request('/manage/claims/12/review',{operation:'complete',resolution:'replacement:PRIVATE_NEW',confirm:true}),env)).status,200);
assert.equal(state.claim.status,'completed');assert.equal(state.buyer.balance,'5.00000000');assert.equal(state.notifications.length,2);
assert.equal((await worker.fetch(request('/manage/claims/12/review',{operation:'complete',resolution:'repeat',confirm:true}),env)).status,409);
state=structuredClone(initial);state.claim.proof_image=null;state.claim.proof_mime=null;
const previousFetch=globalThis.fetch;let telegramReads=0;
env.MANAGER_TELEGRAM_BOT_TOKEN='fixture-token-never-real';
globalThis.fetch=async url=>{telegramReads++;assert(String(url).startsWith('https://api.telegram.org/'));return String(url).endsWith('/getFile')?Response.json({ok:true,result:{file_path:'photos/fixture.jpg'}}):new Response(new Uint8Array([255,216,255,1]),{headers:{'content-length':'4'}})};
try{
 response=await worker.fetch(request('/manage/claims/12/proof'),env);assert.equal(response.status,200);assert.equal(response.headers.get('content-type'),'image/jpeg');assert.equal(telegramReads,2);
 assert.equal((await worker.fetch(request('/manage/claims/12/proof'),env)).status,200);assert.equal(telegramReads,2);
 assert(!JSON.stringify(await(await worker.fetch(request('/manage/claims/12'),env)).json()).includes(env.MANAGER_TELEGRAM_BOT_TOKEN));
}finally{globalThis.fetch=previousFetch;delete env.MANAGER_TELEGRAM_BOT_TOKEN;}
const html=await page(csrf).text(),script=html.match(/<script>([\s\S]*)<\/script>/)[1];new Function(script);
assert(script.includes('function openClaim('));assert(script.includes('data-policy-days'));assert(!script.includes('proof_image'));
console.log('Aftercare checks passed: policy saving, historical delivery recovery, evidence, permissions, CSRF, atomic refunds/retries/rollback, metadata corrections and browser script.');
