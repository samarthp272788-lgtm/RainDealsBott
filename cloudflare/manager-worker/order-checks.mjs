import assert from 'node:assert/strict';
import worker from './src/index.js';
import {page} from './src/ui.js';

// Isolated fixtures: no live database writes or Telegram messages.
const csrf='c'.repeat(64),calls=[];
let viewer=false,jobStatus='sent';
const order={id:12,telegram_id:'777',product_id:2,product_name:'Example',quantity:1,automatic_quantity:0,manual_pending_quantity:0,stock_deducted_quantity:null,assigned_admin_id:null,assigned_at:null,warranty_days:7,warranty_expires_at:null,coverage_started_at:null,amount:'1.00000000',status:'completed',delivery_type:'manual',refunded:0,created_at:'2026-11-01 10:00:00'};
const env={MANAGER_ADMIN_IDS:'123',TIDB:{
  async execute(sql,args=[]){
    calls.push({sql,args});
    if(sql.includes('FROM raindeals_web_manager_force_kill'))return{rows:[{is_active:0}]};
    if(sql.includes('FROM raindeals_web_manager_sessions'))return{rows:[{csrf,admin_id:viewer?'900':'123',token_hash:'hash',created_at:'2026-11-05 10:00:00',expires_at:'2026-11-05 11:00:00'}]};
    if(sql.includes('FROM raindeals_web_manager_admins WHERE'))return{rows:[{telegram_id:'900',permissions_json:'["orders"]',is_active:1}]};
    if(sql.includes('COUNT(*) AS total FROM information_schema.tables'))return{rows:[{total:8}]};
    if(sql.includes('information_schema.tables'))return{rows:[{present:1}]};
    if(sql.startsWith('SHOW COLUMNS'))return{rows:(sql.includes('products')?['id','required_info']:Object.keys(order).concat('delivered_account','delivery_telegram_id','is_preorder')).map(Field=>({Field}))};
    if(sql.includes('FROM order_requirement_submissions'))return{rows:[{telegram_id:'777',values:JSON.stringify({Email:{kind:'text',value:'buyer@example.com'},Screenshot:{kind:'photo',file_id:'stored-photo-reference'}}),submitted_at:'2026-11-01 10:02:00'}]};
    if(sql.includes('FROM raindeals_delivery_bot_jobs'))return{rows:[{recipient_id:'777',admin_id:'123',status:jobStatus,attempts:1,completed_at:'2026-11-01 10:04:00'}]};
    if(sql.includes('FROM raindeals_web_manager_audit a'))return{rows:[{admin_id:'123',action:'fulfill',details:'{"mode":"manual","session_started_at":"2026-11-01 09:30:00"}',created_at:'2026-11-01 10:03:00'}]};
    if(sql.includes('FROM admin_audit_logs'))return{rows:[]};
    if(sql.includes('SELECT required_info'))return{rows:[{required_info:'[{"type":"email","label":"Email"}]'}]};
    if(sql.startsWith('SELECT source,file_content'))return{rows:[{source:'own',file_content:'c:3\nunused:4',stock:997,manual_fallback:1,delivery_type:'hybrid'}]};
    if(sql.startsWith('SELECT delivered_account'))return{rows:[{delivered_account:'account:PRIVATE_PASSWORD'}]};
    if(sql.includes('FROM orders WHERE'))return{rows:[{...order}]};
    return{rows:[]};
  },
  async begin(){return{execute:(sql,args)=>env.TIDB.execute(sql,args),async commit(){calls.push({sql:'COMMIT'})},async rollback(){calls.push({sql:'ROLLBACK'})}}}
}};
const request=(path,body,headers={})=>new Request('https://manager.example'+path,{method:body===undefined?'GET':'POST',headers:{cookie:'rd_manager='+'a'.repeat(64),origin:'https://manager.example','x-csrf-token':csrf,...headers},...(body===undefined?{}:{body:JSON.stringify(body)})});

assert.equal((await worker.fetch(new Request('https://manager.example/manage/orders/12/details'),env)).status,401);
let response=await worker.fetch(request('/manage/orders/12/details'),env);
assert.equal(response.status,200);
let detail=await response.json();
assert.equal(detail.submitted_details.fields.Email.value,'buyer@example.com');
assert.equal(detail.delivery.completed_at,'2026-11-01 10:04:00');
assert.equal(detail.history[0].admin_id,'123');
assert.equal(detail.history[0].session_started_at,'2026-11-01 09:30:00');
assert.equal(detail.can_retry_delivery,false);
assert.equal(detail.can_edit_delivery,true);
assert(!JSON.stringify(detail).includes('PRIVATE_PASSWORD'));
assert.equal(detail.submitted_details.fields.Screenshot.file_id,'stored-photo-reference');
assert.equal('photos_available' in detail.submitted_details,false);
assert.equal((await worker.fetch(request('/manage/orders/12/submission-file?field=Email'),env)).status,404);

viewer=true;
detail=await(await worker.fetch(request('/manage/orders/12/details'),env)).json();
assert.equal(detail.can_edit_delivery,false);
assert.deepEqual(detail.delivered_identifiers,[]);
assert.equal((await worker.fetch(request('/manage/orders/12/delivery-content'),env)).status,403);
assert.equal((await worker.fetch(request('/manage/orders/12/delivery-content',{delivery_text:'new',confirm:true}),env)).status,403);
viewer=false;
assert.equal((await worker.fetch(request('/manage/orders/12/delivery-content',{delivery_text:'new',confirm:true},{'x-csrf-token':'invalid'}),env)).status,403);
jobStatus='sending';
assert.equal((await worker.fetch(request('/manage/orders/12/delivery-content',{delivery_text:'new',confirm:true}),env)).status,409);
assert(!calls.some(c=>c.sql.startsWith('UPDATE orders')));
jobStatus='sent';
response=await worker.fetch(request('/manage/orders/12/delivery-content',{delivery_text:'corrected:secret',confirm:true}),env);
assert.equal(response.status,200);
assert(calls.some(c=>c.sql==='UPDATE orders SET delivered_account=? WHERE id=?'));
assert(calls.some(c=>c.sql.includes("'edit_delivery'")));
assert(calls.some(c=>c.sql==='COMMIT'));

const html=await page(csrf).text(),script=html.match(/<script>([\s\S]*)<\/script>/)[1];
new Function(script);
assert(!script.includes('/submission-file'));
assert(!script.includes('BOT_TOKEN'));
const renderer=script.slice(script.indexOf('function renderOrders('),script.indexOf('function renderAdmins('));
const render=new Function('esc',renderer+';return renderOrders')(s=>String(s??''));
const completed=render([{...order,can_fulfill:1,has_delivery:1}],{permissions:['*']});
assert.equal((completed.match(/<button/g)||[]).length,1);
assert(!/data-(notify-order|order-accounts|delivery-ids|fulfill-order)/.test(completed));
const pending=render([{...order,status:'pending_manual',can_fulfill:1}],{permissions:['*']});
assert.match(pending,/data-fulfill-order/);
Object.assign(order,{status:'pending_manual',delivery_type:'hybrid',quantity:3,automatic_quantity:2,manual_pending_quantity:1,stock_deducted_quantity:3,delivered_account:'a:1\nb:2'});
env.DELIVERY_BRIDGE_SECRET='fixture';
env.API={async fetch(){return Response.json({success:true,status:'sent'})}};
order.assigned_admin_id='456';
const beforeBlocked=calls.length;
assert.equal((await worker.fetch(request('/manage/orders/12/fulfill',{mode:'inventory'}),env)).status,409);
assert(!calls.slice(beforeBlocked).some(c=>/^UPDATE (products|orders|users)/.test(c.sql)));
order.assigned_admin_id='123';
response=await worker.fetch(request('/manage/orders/12/fulfill',{mode:'inventory'}),env);
assert.equal(response.status,200);
const allocation=calls.findLast(c=>c.sql.startsWith('UPDATE products SET file_content'));
assert.deepEqual(allocation.args,['unused:4',997,2]);
const completion=calls.findLast(c=>c.sql.startsWith('UPDATE orders SET delivered_account'));
assert.equal(completion.args[0],'a:1\nb:2\nc:3');
assert(completion.sql.includes('manual_pending_quantity=0'));
assert(completion.sql.includes('warranty_days>0 AND warranty_expires_at IS NULL'));
assert(completion.sql.includes('DATE_ADD(UTC_TIMESTAMP(),INTERVAL warranty_days DAY)'));
assert(completion.sql.includes('coverage_started_at=COALESCE(coverage_started_at,UTC_TIMESTAMP())'));
assert(completion.sql.includes('assigned_admin_id=COALESCE(assigned_admin_id,?)'));
assert.equal(String(completion.args.at(-2)),'123');
assert(calls.some(c=>c.sql.includes('INSERT INTO order_events')&&c.sql.includes("'completed'")));
console.log('Order checks passed: submitted fields, recorded delivery/admin/session times, credential masking, role checks, CSRF, in-flight edit protection, correction transaction, completed-card actions, generated browser script.');
