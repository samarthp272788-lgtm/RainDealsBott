import { page, forcedKillPage } from './ui.js';
import { loginPage } from './login_v2.js';

const enc = new TextEncoder();
const tables = {
  products: {fields:'name,icon,description,category,price,delivery_instruction,delivery_type,manual_fallback,preorder,low_stock_threshold,bulk_pricing,is_active,api_enabled,source,provider_id,reseller_service_id,reseller_cost,reseller_name', required:['name','price'], defaults:{icon:'📦',category:'General',stock:0,is_active:1,api_enabled:1,source:'own',delivery_type:'automatic',manual_fallback:0,preorder:0,low_stock_threshold:3}},
  providers: {fields:'name,provider_key,base_url,api_type,auth_type,api_key,api_secret,configuration,is_active',required:['name','provider_key','base_url'],defaults:{api_type:'generic',auth_type:'header',is_active:1}},
  promocodes: {fields:'code,amount,max_uses,is_active',required:['code','amount'],defaults:{max_uses:1,used_count:0,is_active:1}},
  custom_rates: {fields:'telegram_id,product_id,price,is_active',required:['telegram_id','price'],read:true},
  users: {fields:'is_banned',read:true}, orders:{fields:'',read:true}, deposits:{fields:'',read:true},
  tickets:{fields:'status,priority,admin_response',read:true}, api_keys:{fields:'is_active',read:true},
  api_orders:{fields:'',read:true}, referrals:{fields:'',read:true},
  manager_audit:{table:'raindeals_web_manager_audit',fields:'',read:true},
  product_rate_controls:{fields:'is_active',read:true}, rate_control_assignments:{fields:'is_active',read:true}
};
const responseFields={
 products:'id,name,icon,description,category,price,stock,delivery_instruction,delivery_type,manual_fallback,preorder,low_stock_threshold,bulk_pricing,is_active,api_enabled,source,provider_id,reseller_service_id,reseller_cost,reseller_name,created_at,updated_at',
 providers:'id,name,provider_key,base_url,api_type,auth_type,configuration,is_active,created_at,updated_at',
 promocodes:'id,code,amount,max_uses,used_count,is_active,expires_at,created_at',
  custom_rates:'id,telegram_id,product_id,price,is_active,created_at,stopped_at',
 users:'id,telegram_id,username,full_name,balance,total_spent,membership,is_banned,created_at',
 orders:'id,telegram_id,user_id,product_id,product_name,service,quantity,amount,status,delivery_type,automatic_quantity,manual_pending_quantity,created_at,updated_at,delivered_at',
 deposits:'id,telegram_id,user_id,amount,currency,network,status,txid,payment_method,created_at,updated_at,verified_at',
 tickets:'id,telegram_id,user_id,subject,status,priority,admin_response,created_at,updated_at',
 api_keys:'id,telegram_id,name,key_prefix,is_active,created_at,last_used_at',
 api_orders:'id,telegram_id,user_id,product_id,product_name,quantity,amount,status,created_at,updated_at',
 referrals:'id,referrer_id,referred_id,commission,created_at',
 product_rate_controls:'id,product_id,telegram_id,rate,is_active,created_at',
 rate_control_assignments:'id,control_id,telegram_id,is_active,created_at',
 manager_audit:'id,admin_id,action,target_type,target_id,details,created_at'
};
const secretFields = /password|token|secret|key_hash|encrypted|api_key/i;
const numeric = new Set('price,amount,max_uses,stock,manual_fallback,preorder,low_stock_threshold,is_active,api_enabled,provider_id,reseller_cost,telegram_id,product_id,is_banned'.split(','));
const bools = new Set('manual_fallback,preorder,is_active,api_enabled,is_banned'.split(','));
const sensitive = new Set(['api_key','api_secret']);
const reply=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'content-type':'application/json','cache-control':'no-store'}});
function error(message,status=400){const e=new Error(message);e.status=status;throw e;}
const equal=(a,b)=>{a=String(a||'');b=String(b||'');let n=a.length^b.length;for(let i=0;i<Math.max(a.length,b.length);i++)n|=(a.charCodeAt(i)||0)^(b.charCodeAt(i)||0);return n===0;};
const random=()=>Array.from(crypto.getRandomValues(new Uint8Array(32)),x=>x.toString(16).padStart(2,'0')).join('');
async function hash(s){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',enc.encode(s))),x=>x.toString(16).padStart(2,'0')).join('');}
const db=(env,sql,args=[])=>env.TIDB.execute(sql,args);
const rows=async(env,sql,args=[])=>((await db(env,sql,args)).rows||[]);
async function tx(env,fn){const t=await env.TIDB.begin();try{const r=await fn(t);await t.commit();return r;}catch(e){await t.rollback();throw e;}}
let schemaInitPromise;
let forceKillSchemaPromise;
const managerSchemaTables=['raindeals_web_manager_sessions','raindeals_web_manager_settings','raindeals_web_manager_attempts','raindeals_web_manager_audit','raindeals_web_manager_login_preferences','raindeals_web_manager_login_lockouts','raindeals_web_manager_force_kill','raindeals_web_manager_admins'];
const tableColumnCache=new Map();
async function columnsFor(env,table){
  const cached=tableColumnCache.get(table);
  if(cached&&cached.expires>Date.now())return cached.columns;
  const columns=(await rows(env,`SHOW COLUMNS FROM \`${table}\``)).map(c=>c.Field);
  tableColumnCache.set(table,{columns,expires:Date.now()+300000});
  return columns;
}
async function initForceKillTable(env){
  if(!forceKillSchemaPromise)forceKillSchemaPromise=(async()=>{
    await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_force_kill (lock_id TINYINT PRIMARY KEY,is_active TINYINT NOT NULL,updated_by BIGINT NOT NULL,updated_at DATETIME NOT NULL)");
    await db(env,"INSERT IGNORE INTO raindeals_web_manager_force_kill VALUES (1,0,0,UTC_TIMESTAMP())");
  })().catch(e=>{forceKillSchemaPromise=null;throw e});
  await forceKillSchemaPromise;
}
async function init(env){
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_sessions (token_hash CHAR(64) PRIMARY KEY,csrf CHAR(64) NOT NULL,admin_id BIGINT NOT NULL,expires_at DATETIME NOT NULL,created_at DATETIME NOT NULL)");
  await db(env,"DELETE FROM raindeals_web_manager_sessions WHERE expires_at>DATE_ADD(created_at,INTERVAL 120 MINUTE)");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_settings (setting_key VARCHAR(64) PRIMARY KEY,setting_value TEXT NOT NULL,updated_by BIGINT NOT NULL,updated_at DATETIME(6) NOT NULL)");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_attempts (bucket CHAR(64) PRIMARY KEY,attempts INT NOT NULL,expires_at DATETIME NOT NULL)");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_audit (id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,admin_id BIGINT NOT NULL,action VARCHAR(32) NOT NULL,target_type VARCHAR(64) NOT NULL,target_id VARCHAR(64) NULL,details TEXT NOT NULL,created_at DATETIME(6) NOT NULL,KEY ix_manager_audit_created(created_at),KEY ix_manager_audit_admin(admin_id))");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_login_preferences (token_hash CHAR(64) PRIMARY KEY,session_minutes SMALLINT NOT NULL,expires_at DATETIME NOT NULL)");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_login_lockouts (lock_id TINYINT PRIMARY KEY,locked_until DATETIME NOT NULL,updated_by BIGINT NOT NULL,updated_at DATETIME NOT NULL)");
  await db(env,"INSERT IGNORE INTO raindeals_web_manager_login_lockouts VALUES (1,'1970-01-01 00:00:00',0,UTC_TIMESTAMP())");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_force_kill (lock_id TINYINT PRIMARY KEY,is_active TINYINT NOT NULL,updated_by BIGINT NOT NULL,updated_at DATETIME NOT NULL)");
  await db(env,"INSERT IGNORE INTO raindeals_web_manager_force_kill VALUES (1,0,0,UTC_TIMESTAMP())");
  await db(env,"CREATE TABLE IF NOT EXISTS raindeals_web_manager_admins (telegram_id BIGINT NOT NULL PRIMARY KEY,display_name VARCHAR(120) NOT NULL DEFAULT '',role_key VARCHAR(32) NOT NULL,permissions_json TEXT NOT NULL,is_active TINYINT NOT NULL DEFAULT 1,is_owner TINYINT NOT NULL DEFAULT 0,added_by BIGINT NOT NULL,created_at DATETIME(6) NOT NULL,updated_at DATETIME(6) NOT NULL,KEY ix_manager_admin_active(is_active))");
  for(const id of String(env.MANAGER_ADMIN_IDS||'').split(',').map(x=>x.trim()).filter(x=>/^\d{1,20}$/.test(x))){
    await db(env,"INSERT IGNORE INTO raindeals_web_manager_admins (telegram_id,display_name,role_key,permissions_json,is_active,is_owner,added_by,created_at,updated_at) VALUES (?,'Owner','owner','[\"*\"]',1,1,?,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",[id,id]);
  }
}
async function initCached(env){if(!schemaInitPromise)schemaInitPromise=(async()=>{let complete=false;try{const found=(await rows(env,`SELECT COUNT(*) AS total FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN (${managerSchemaTables.map(()=>'?').join(',')})`,managerSchemaTables))[0];complete=Number(found?.total)===managerSchemaTables.length}catch{}if(!complete)await init(env)})().catch(e=>{schemaInitPromise=null;throw e});await schemaInitPromise}
async function forceKillState(env){const query='SELECT is_active FROM raindeals_web_manager_force_kill WHERE lock_id=1 LIMIT 1';try{return(await rows(env,query))[0]}catch(e){if(!/1146|doesn't exist/i.test(String(e?.message||e)))throw e;await initForceKillTable(env);return(await rows(env,query))[0]}}
async function session(request,env){const raw=/rd_manager=([a-f0-9]{64})/.exec(request.headers.get('cookie')||'')?.[1];if(!raw)return null;return(await rows(env,'SELECT token_hash,csrf,admin_id FROM raindeals_web_manager_sessions WHERE token_hash=? AND expires_at>UTC_TIMESTAMP()',[await hash(raw)]))[0]||null;}
async function body(request){if(Number(request.headers.get('content-length')||0)>1000000)error('Upload is limited to 1 MB.',413);const raw=await request.text();if(raw.length>1000000)error('Upload is limited to 1 MB.',413);try{return JSON.parse(raw);}catch{error('Invalid JSON request.');}}
async function throttleLogin(request,env){const bucket=await hash(request.headers.get('cf-connecting-ip')||'unknown');await db(env,"INSERT INTO raindeals_web_manager_attempts VALUES (?,1,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 1 MINUTE)) ON DUPLICATE KEY UPDATE attempts=IF(expires_at<UTC_TIMESTAMP(),1,attempts+1),expires_at=IF(expires_at<UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(),INTERVAL 1 MINUTE),expires_at)",[bucket]);const row=(await rows(env,'SELECT attempts FROM raindeals_web_manager_attempts WHERE bucket=?',[bucket]))[0];if(Number(row?.attempts)>5)error('Too many login attempts. Wait one minute.',429)}
async function audit(env,adminId,action,targetType,targetId,details={}){await db(env,'INSERT INTO raindeals_web_manager_audit (admin_id,action,target_type,target_id,details,created_at) VALUES (?,?,?,?,?,UTC_TIMESTAMP(6))',[adminId,action,targetType,targetId==null?null:String(targetId),JSON.stringify(details).slice(0,4000)])}
async function assertLoginUnlocked(env){const row=(await rows(env,'SELECT locked_until FROM raindeals_web_manager_login_lockouts WHERE lock_id=1 AND locked_until>UTC_TIMESTAMP()'))[0];if(row)error('Manager login is locked by /kill. Try again after the five-minute lock expires.',423)}
async function assertLoginUnlockedTx(t){const row=(await t.execute('SELECT lock_id FROM raindeals_web_manager_login_lockouts WHERE lock_id=1 AND locked_until>UTC_TIMESTAMP() FOR UPDATE')).rows?.[0];if(row)error('Manager login is locked by /kill. Try again after the five-minute lock expires.',423)}
const ownerIds=env=>new Set(String(env.MANAGER_ADMIN_IDS||'').split(',').map(x=>x.trim()).filter(x=>/^\d{1,20}$/.test(x)));
const managerSections=['dashboard','products','orders','deposits','users','tickets','promocodes','custom_rates','api_keys','api_orders','referrals','product_rate_controls','rate_control_assignments','manager_audit','settings'];
const writableSections=['products','orders','tickets','promocodes','custom_rates','api_keys','product_rate_controls','rate_control_assignments','settings'];
const managerCapabilities=[...writableSections.map(x=>x+'_write'),'orders_export','orders_alerts','delivery_ids','delivery_secrets'];
function jsonValue(value,fallback){if(typeof value==='string'){try{return JSON.parse(value)}catch{return fallback}}return value??fallback}
async function optionalRows(env,sql,args=[]){try{return await rows(env,sql,args)}catch(e){if(/1146|doesn't exist/i.test(String(e?.message||e)))return[];throw e}}
async function managerAdmin(env,id){
  if(ownerIds(env).has(String(id)))return{telegram_id:String(id),display_name:'Owner',role_key:'owner',permissions:['*'],is_owner:1,is_active:1};
  const r=(await rows(env,'SELECT telegram_id,display_name,role_key,permissions_json,is_owner,is_active FROM raindeals_web_manager_admins WHERE telegram_id=? AND is_active=1 LIMIT 1',[id]))[0];
  if(!r)return null;let permissions=[];try{permissions=JSON.parse(r.permissions_json||'[]')}catch{};
  return{...r,is_owner:0,permissions:Array.isArray(permissions)?permissions.filter(p=>p!=='*'&&[...managerSections,...managerCapabilities].includes(p)):[]};
}
function hasPermission(admin,permission){return Number(admin?.is_owner)===1||admin?.permissions?.includes('*')||admin?.permissions?.includes(permission)}
function requiredPermission(path){
  if(path==='/manage/me')return null;
  if(path==='/manage/admins'||path.startsWith('/manage/admins/'))return'admins';
  if(path.startsWith('/manage/orders/export'))return'orders_export';
  if(path.startsWith('/manage/orders/alerts'))return'orders_alerts';
  if(path.startsWith('/manage/orders/'))return'orders';
  if(path==='/manage/orders')return'orders';
  if(path.startsWith('/manage/providers'))return'api_keys';
  if(path.startsWith('/manage/api-settings')||path.startsWith('/manage/settings')||path.startsWith('/manage/delivery/'))return'settings';
  const match=path.match(/^\/manage\/([a-z_]+)/);return match&&managerSections.includes(match[1])?match[1]:null;
}
async function login(request,env){
  if(!env.MANAGER_USERNAME||!env.MANAGER_PASSWORD)error('Manager credentials are not configured.',503);
  await initCached(env);
  await throttleLogin(request,env);
  await assertLoginUnlocked(env);
  const input=await body(request);
  if(!equal(input.username,env.MANAGER_USERNAME)||!equal(input.password,env.MANAGER_PASSWORD))error('Invalid manager credentials.',401);
  if(!/^[A-Za-z0-9_-]{32}$/.test(String(input.code||'')))error('Enter the one-time code from the admin verification bot.',401);
  const sessionMinutes=Number(input.session_minutes||60);if(![60,120].includes(sessionMinutes))error('Choose a 60- or 120-minute session.');
  const raw=random(),digest=await hash(raw),csrf=random(),codeHash=await hash(input.code);
  await tx(env,async t=>{
    await assertLoginUnlockedTx(t);
    const found=(await t.execute('SELECT id,approved_by FROM manager_login_challenges WHERE token_hash=? AND approved_at IS NOT NULL AND consumed_at IS NULL AND expires_at>UTC_TIMESTAMP() FOR UPDATE',[codeHash])).rows?.[0];
    if(!found||!(await managerAdmin(env,found.approved_by)))error('Verification code expired or invalid.',401);
    await t.execute('UPDATE manager_login_challenges SET consumed_at=UTC_TIMESTAMP() WHERE id=?',[found.id]);
    await t.execute('DELETE FROM raindeals_web_manager_sessions WHERE admin_id=? OR expires_at<UTC_TIMESTAMP()',[found.approved_by]);
    const interval=sessionMinutes===120?'120':'60';await t.execute(`INSERT INTO raindeals_web_manager_sessions VALUES (?,?,?,DATE_ADD(UTC_TIMESTAMP(),INTERVAL ${interval} MINUTE),UTC_TIMESTAMP())`,[digest,csrf,found.approved_by]);
  });
  return new Response(JSON.stringify({success:true,session_minutes:sessionMinutes}),{headers:{'content-type':'application/json','set-cookie':`rd_manager=${raw}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=${sessionMinutes*60}`,'cache-control':'no-store'}});
}
function botLoginPage(){
  const html='<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Opening Rain Deals Manager</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1110;color:#e8f2ec;font:16px system-ui,sans-serif}.card{max-width:420px;margin:20px;padding:28px;border:1px solid #263631;border-radius:18px;background:#121a18;text-align:center}p{color:#9bada5}</style><main class="card"><h1>Rain Deals Manager</h1><p id="status">Checking your private sign-in link…</p></main><script>const status=document.getElementById("status");const token=location.hash.slice(1);history.replaceState(null,"",location.pathname);if(!token){status.textContent="The sign-in link is missing or expired. Create a new one from the bot."}else{fetch("/login/bot/exchange",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({token})}).then(async r=>{const data=await r.json();if(!r.ok)throw new Error(data.error||"This link is invalid or expired.");status.textContent="Signed in. Opening manager…";location.replace("/")}).catch(e=>{status.textContent=e.message})}</script></html>';
  return new Response(html,{headers:{'content-type':'text/html; charset=utf-8','cache-control':'no-store','content-security-policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}});
}
async function cleanManagerLinkCard(env,challenge,status){
  if(!env.MANAGER_TELEGRAM_BOT_TOKEN||!challenge?.telegram_chat_id||!challenge?.telegram_message_id)return false;
  const text=status==='used'
    ?'✅ Manager Web sign-in link used. This card no longer contains an active link.'
    :'⌛ Manager Web sign-in link expired. Open the Admin Panel to create a new one.';
  try{
    const response=await fetch(`https://api.telegram.org/bot${env.MANAGER_TELEGRAM_BOT_TOKEN}/editMessageText`,{
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({chat_id:challenge.telegram_chat_id,message_id:challenge.telegram_message_id,text,reply_markup:{inline_keyboard:[]},disable_web_page_preview:true})
    });
    const result=await response.json();
    if(!response.ok||!result?.ok){
      // Telegram returns this when a user already removed/edited the card.
      const description=String(result?.description||'').toLowerCase();
      if(!description.includes('message is not modified')&&!description.includes('message to edit not found')&&!description.includes("message can't be edited")){
        console.warn('manager_link_card_cleanup_failed',Number(response.status)||0);
        return false;
      }
    }
    await db(env,'UPDATE manager_login_challenges SET card_cleaned_at=UTC_TIMESTAMP() WHERE id=? AND card_cleaned_at IS NULL',[challenge.id]);
    return true;
  }catch{
    console.warn('manager_link_card_cleanup_network_failed');
    return false;
  }
}
async function cleanExpiredManagerLinkCards(env){
  if(!env.MANAGER_TELEGRAM_BOT_TOKEN)return;
  const pending=await rows(env,
    'SELECT id,telegram_chat_id,telegram_message_id FROM manager_login_challenges WHERE expires_at<=UTC_TIMESTAMP() AND consumed_at IS NULL AND card_cleaned_at IS NULL AND telegram_chat_id IS NOT NULL AND telegram_message_id IS NOT NULL ORDER BY expires_at LIMIT 100');
  for(const challenge of pending)await cleanManagerLinkCard(env,challenge,'expired');
}
async function exchangeBotLogin(request,env,url){
  if(request.headers.get('origin')!==url.origin)error('Invalid request origin.',403);
  const input=await body(request),token=String(input.token||'');
  if(!/^[A-Za-z0-9_-]{40,50}$/.test(token))error('Manager link is invalid or expired.',401);
  await initCached(env);
  await throttleLogin(request,env);
  await assertLoginUnlocked(env);
  const tokenHash=await hash(token),raw=random(),digest=await hash(raw),csrf=random();
  let adminId,usedChallenge;
  await tx(env,async t=>{
    await assertLoginUnlockedTx(t);
    const challenge=(await t.execute('SELECT id,approved_by,telegram_chat_id,telegram_message_id FROM manager_login_challenges WHERE token_hash=? AND approved_at IS NOT NULL AND consumed_at IS NULL AND expires_at>UTC_TIMESTAMP() LIMIT 1 FOR UPDATE',[tokenHash])).rows?.[0];
    if(!challenge)error('Manager link is invalid, expired, or already used.',401);
    adminId=challenge.approved_by;
    if(!(await managerAdmin(env,adminId)))error('This Telegram admin is not authorized for the manager.',403);
    await t.execute('UPDATE manager_login_challenges SET consumed_at=UTC_TIMESTAMP() WHERE id=? AND consumed_at IS NULL',[challenge.id]);
    usedChallenge=challenge;
    await t.execute('DELETE FROM raindeals_web_manager_sessions WHERE admin_id=? OR expires_at<UTC_TIMESTAMP()',[adminId]);
    await t.execute('INSERT INTO raindeals_web_manager_sessions VALUES (?,?,?,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 60 MINUTE),UTC_TIMESTAMP())',[digest,csrf,adminId]);
  });
  await cleanManagerLinkCard(env,usedChallenge,'used');
  return new Response(JSON.stringify({success:true,session_minutes:60}),{headers:{'content-type':'application/json','set-cookie':`rd_manager=${raw}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600`,'cache-control':'no-store'} });
}
function validate(fields,input,create=false){const out={};for(const key of fields.split(',').filter(Boolean)){if(!(key in input))continue;let value=input[key];if(sensitive.has(key)&&!value)continue;if(numeric.has(key)&&value!==null&&value!==''){if(!/^\d+(?:\.\d{1,8})?$/.test(String(value)))error(`Invalid ${key}.`);if(bools.has(key)&&!['0','1'].includes(String(value)))error(`Invalid ${key}.`);if(!['price','amount','reseller_cost'].includes(key)&&!/^\d+$/.test(String(value)))error(`${key} must be a whole number.`);}if(value===' '||value==='')value=['provider_id','product_id','reseller_cost'].includes(key)?null:value;
    if(key==='bulk_pricing'&&value){try{JSON.parse(value);}catch{error('Bulk pricing must be valid JSON.');}}
    if(key==='base_url'){let u;try{u=new URL(value);}catch{error('Invalid provider URL.');}if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash)error('Provider URL must use HTTPS without embedded credentials or query parameters.');}
    if(key==='delivery_type'&&!['automatic','manual','hybrid'].includes(value))error('Invalid delivery type.');
    if(key==='source'&&!['own','reseller'].includes(value))error('Invalid product source.');
    if(key==='api_type'&&!['generic','rest','excalibur'].includes(value))error('Invalid provider API format.');
    if(key==='auth_type'&&!['header','custom_header','api_key','bearer','query','wmemail_open','wmemail','open_api','hmac','hmac_sha256','dujiao_hmac','dujiao-next','dujiao_next','signature'].includes(value))error('Invalid provider authentication type.');
    if(key==='configuration'&&value){let c;try{c=typeof value==='string'?JSON.parse(value):value}catch{error('Provider configuration must be valid JSON.')}if(!c||typeof c!=='object'||Array.isArray(c))error('Provider configuration must be a JSON object.');value=JSON.stringify(c)}
    if(String(value||'').length>200000)error('Field is too long.');out[key]=value;
  }return out;}
function providerConfig(provider){let c=provider.configuration||{};if(typeof c==='string'){try{c=c?JSON.parse(c):{}}catch{error('Provider configuration must be valid JSON.')}}if(!c||typeof c!=='object'||Array.isArray(c))error('Provider configuration must be a JSON object.');return c}
function nested(value,path){if(!path)return undefined;return String(path).split('.').reduce((v,k)=>v&&typeof v==='object'?v[k]:undefined,value)}
async function providerRequest(provider,feature,timeoutMs=15000){
  const cfg=providerConfig(provider),fcfg=cfg[feature]&&typeof cfg[feature]==='object'?cfg[feature]:{},auth=String(provider.auth_type||'header').toLowerCase(),baseCheck=new URL(String(provider.base_url||''));
  if(baseCheck.protocol!=='https:'||baseCheck.username||baseCheck.password)error('Provider URL must use HTTPS without embedded credentials.');
  const hmac=['hmac','hmac_sha256','dujiao_hmac','dujiao-next','dujiao_next','signature'].includes(auth),wm=['wmemail_open','wmemail','open_api'].includes(auth),canboso=baseCheck.hostname.toLowerCase().includes('canboso.com'),emStore=String(provider.provider_key||'').toLowerCase().includes('em_store')||baseCheck.hostname.toLowerCase().includes('ssondigitalworks');
  const defaults=feature==='products'?(hmac?((baseCheck.pathname.includes('/api/v1/upstream')?'':'/api/v1/upstream')+'/products'):wm?'/api/v1/open/products':canboso?'/api/v2/telegram-buyer/products':emStore?'?action=products':'/api/v1/products'):(hmac?((baseCheck.pathname.includes('/api/v1/upstream')?'':'/api/v1/upstream')+'/ping'):wm?'/api/v1/open/balance':canboso?'/api/v2/telegram-buyer/balance':emStore?'?action=balance':'/api/v1/me');
  let endpoint=String(cfg[`${feature}_endpoint`]||fcfg.endpoint||defaults),method=String(cfg[`${feature}_method`]||fcfg.method||'GET').toUpperCase();
  if(!['GET'].includes(method)||!(endpoint.startsWith('/')||endpoint.startsWith('?'))||endpoint.startsWith('//')||endpoint.includes('..'))error(`Unsafe ${feature} endpoint configuration.`);
  const base=String(provider.base_url||'').replace(/\/+$/,'');let url=base+endpoint;const headers={accept:'application/json'};
  const key=String(provider.api_key||'');
  if(['header','custom_header','api_key'].includes(auth)){const hn=String(cfg.auth_header_name||'X-API-Key');if(!/^[A-Za-z0-9-]{1,64}$/.test(hn))error('Invalid provider auth header name.');if(key)headers[hn]=key}
  else if(auth==='bearer'){if(key)headers.authorization=`Bearer ${key}`}
  else if(auth==='query'||(wm&&feature==='balance')){const param=String(cfg.auth_query_param||(wm?'api_key':'key'));if(!/^[A-Za-z0-9_-]{1,64}$/.test(param))error('Invalid provider auth query parameter.');if(key){const u=new URL(url);u.searchParams.set(param,key);url=u.toString()}}else if(wm){headers['content-type']='application/json'}
  else if(hmac){const secret=String(provider.api_secret||'');if(!key||!secret)error('HMAC provider needs both API key and API secret.');const timestamp=String(Math.floor(Date.now()/1000)),path=new URL(url).pathname,canonical=`${method}\n${path}\n${timestamp}\nd41d8cd98f00b204e9800998ecf8427e`,cryptoKey=await crypto.subtle.importKey('raw',enc.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']),sig=new Uint8Array(await crypto.subtle.sign('HMAC',cryptoKey,enc.encode(canonical))),hex=Array.from(sig,x=>x.toString(16).padStart(2,'0')).join('');headers['Dujiao-Next-Api-Key']=key;headers['Dujiao-Next-Timestamp']=timestamp;headers['Dujiao-Next-Signature']=hex;}
  else error('Unsupported provider authentication type.');
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const response=await fetch(url,{method,headers,redirect:'manual',signal:controller.signal});
    if(response.status<200||response.status>=300)error(`Provider returned HTTP ${response.status}.`,502);
    const text=await response.text();
    if(text.length>2_000_000)error('Provider response exceeds 2 MB.',502);
    // The body is consumed by response.text(); parse that text rather than
    // calling response.json() on the already-consumed stream.
    const normalized=text.replace(/^\uFEFF/,'').trim().replace(/^\)\]\}',?\s*/, '');
    if(!normalized)error('Provider returned an empty response instead of a product catalog.',502);
    try{return JSON.parse(normalized)}catch{
      const contentType=response.headers.get('content-type')||'unknown content type';
      error(`Provider response is not valid JSON (${contentType}). Check that the configured endpoint returns JSON, not an HTML page or login screen.`,502)
    }
  }catch(e){if(e.status)throw e;error(e.name==='AbortError'?'Provider request timed out.':'Could not reach provider. Check its URL and credentials.',502)}finally{clearTimeout(timer)}
}
function providerCatalogItems(provider,payload){
  const cfg=providerConfig(provider),pcfg=cfg.products&&typeof cfg.products==='object'?cfg.products:{},auth=String(provider.auth_type||'').toLowerCase();
  let mapping=pcfg.mapping||cfg.mapping?.products||cfg.mapping||{};if(!mapping||typeof mapping!=='object')mapping={};
  if(!Object.keys(mapping).length&&String(provider.base_url||'').toLowerCase().includes('canboso.com'))mapping={service_id:'productId',name:'name',price:'price.amount',stock:'availability.available',description:'description',category:'productType'};
  const listKey=mapping.products_list_key||pcfg.products_list_key;let items=listKey?nested(payload,listKey):undefined;
  if(!Array.isArray(items)){if(Array.isArray(payload))items=payload;else if(payload&&typeof payload==='object')for(const k of ['products','services','data','items','catalog','result'])if(Array.isArray(payload[k])){items=payload[k];break}}
  if(!Array.isArray(items))error('Provider response has no product list.',502);
  if(['hmac','hmac_sha256','dujiao_hmac','dujiao-next','dujiao_next','signature','wmemail_open','wmemail','open_api'].includes(auth)){
    const expanded=[];for(const raw of items){const skus=raw&&typeof raw==='object'?raw.skus:null;if(!Array.isArray(skus)){expanded.push(raw);continue}let title=raw.title||raw.name||'Product';if(title&&typeof title==='object')title=title.en||title['zh-CN']||Object.values(title)[0]||'Product';for(const sku of skus)if(sku&&typeof sku==='object'&&sku.is_active!==false)expanded.push({id:sku.id,name:`${title} ${sku.sku_code||''}`.trim(),price:sku.price_amount,stock:sku.stock_quantity??sku.stock_status,description:raw.description,category:raw.category_id,emoji:sku.emoji||raw.emoji})}items=expanded;
  }
  const pick=(raw,mapped,keys)=>{for(const k of [mapped,...keys].filter(Boolean)){const v=nested(raw,k);if(v!==undefined&&v!==null)return v}return undefined};
  return items.slice(0,1000).flatMap(raw=>{
    if(!raw||typeof raw!=='object')return[];
    let id=pick(raw,mapping.service_id||mapping.product_id||mapping.id,['productId','service_id','product_id','id','service','code','item_id','ref']);
    const rawName=pick(raw,mapping.name,['name','title','product_name','service_name']);let name=String(rawName||'Unknown Product').trim();
    const tag=/\{pe:(\d+)\}/.exec(name);if(tag){if(id==null||String(id)==='None')id=tag[1];name=name.replace(/\{pe:\d+\}/g,'').replace(/^[\s\-:]+|[\s\-:]+$/g,'').trim()||String(rawName||'Unknown Product')}
    let price=pick(raw,mapping.price,['price.amount','price','rate','cost','unit_price']);if(price&&typeof price==='object')price=price.amount;
    const cost=Number(price),rawStock=pick(raw,mapping.stock,['availability.available','quantity','stock','qty','available','available_stock','stock_count','inventory','count','units','in_stock','balance','amount']);let stock=1000000;
    if(rawStock!==undefined){if(typeof rawStock==='boolean')stock=rawStock?1000000:0;else if(Number.isFinite(Number(rawStock))){const n=Number(rawStock);stock=n<0?1000000:Math.floor(n)}else{const text=String(rawStock).trim().toLowerCase();if(['unlimited','infinite','infinity','in_stock','available','active','true','yes','enabled'].includes(text))stock=1000000;else if(['out_of_stock','disabled','inactive','false','no','none','null','oos'].includes(text))stock=0;else{const digits=/\d+/.exec(text);stock=digits?Number(digits[0]):1000000;if(text==='low_stock')stock=Math.max(1,stock)}}}
    else if(['false','0','disabled','inactive','out_of_stock','oos'].includes(String(raw.status||raw.available||raw.is_available||'').trim().toLowerCase()))stock=0;
    if(id==null||!name||!Number.isFinite(cost)||cost<=0||stock<=0||raw.is_available===false)return[];
    const category=pick(raw,mapping.category,['productType','category','cat','type','group']);const description=pick(raw,mapping.description,['description','desc','details','info']);
    return[{service_id:String(id).trim().slice(0,255),name:name.slice(0,255),cost,stock:Math.min(1000000,stock),icon:String(raw.emoji||'📦').slice(0,50),category:String(category||'reseller').slice(0,255),description:String(description||`Imported from reseller: ${name}`).slice(0,4000)}];
  });
}
function parseImportBulk(value){if(!String(value||'').trim())return null;const source=String(value).trim();let incoming;if(source.startsWith('{')||source.startsWith('[')){try{incoming=JSON.parse(source)}catch{error('Bulk pricing must be valid JSON or MIN-MAX=PRICE lines.')}}else{incoming={};for(const raw of source.split(/\r?\n/)){const line=raw.trim();if(!line)continue;const parts=line.split('=');if(parts.length!==2)error('Bulk pricing lines must use MIN-MAX=PRICE or MIN+=PRICE.');const range=parts[0].trim(),price=parts[1].trim();let min,max=null;if(range.includes('-'))[min,max]=range.split('-').map(x=>Number(x.trim()));else if(range.includes('+'))min=Number(range.replace('+','').trim());else error('Bulk pricing lines must use MIN-MAX=PRICE or MIN+=PRICE.');incoming[String(min)]={min,max,price}}}if(Array.isArray(incoming))incoming=Object.fromEntries(incoming.filter(x=>x&&typeof x==='object').map(x=>[String(x.min),x]));if(!incoming||typeof incoming!=='object'||Array.isArray(incoming))error('Bulk pricing must be a JSON object or list of tiers.');const tiers=[];for(const t of Object.values(incoming)){const min=Number(t?.min),max=t?.max==null||t.max===''?null:Number(t.max),price=Number(t?.price);if(!Number.isInteger(min)||min<1||(max!==null&&(!Number.isInteger(max)||max<=min))||!Number.isFinite(price)||price<0)error('Bulk pricing has an invalid tier.');tiers.push({min,max,price})}tiers.sort((a,b)=>a.min-b.min);for(let i=0;i<tiers.length-1;i++)if(tiers[i].max===null||tiers[i].max>=tiers[i+1].min)error('Bulk pricing tiers must not overlap.');return JSON.stringify(Object.fromEntries(tiers.map(t=>[String(t.min),t])))}
async function status(env){const result={database_connected:false,render_required:false,main_bot_settings_integrated:false,delivery_configured:false,admin_verify_configured:false};const [database,api,maintenance]=await Promise.allSettled([db(env,'SELECT 1'),env.API.fetch('https://internal/healthz'),rows(env,'SELECT enabled FROM maintenance_settings WHERE id=1 LIMIT 1')]);if(database.status==='rejected')throw database.reason;result.database_connected=true;result.main_bot_settings_integrated=maintenance.status==='fulfilled';if(api.status==='fulfilled'){try{const j=await api.value.json();result.delivery_configured=!!j.delivery_webhook_configured;result.admin_verify_configured=!!j.admin_verify_webhook_configured;}catch{}}return result;}
async function notifyCompletedOrder(env,orderId,recipientId,adminId){
  if(!env.DELIVERY_BRIDGE_SECRET)return{status:'not_configured',message:'Delivery notification is not configured on the Manager Worker. Set the same DELIVERY_BRIDGE_SECRET used by the API Worker, then retry.'};
  try{
    const response=await env.API.fetch(new Request('https://internal/internal/delivery',{method:'POST',headers:{'content-type':'application/json','x-delivery-bridge-secret':env.DELIVERY_BRIDGE_SECRET},body:JSON.stringify({order_id:Number(orderId),recipient_id:String(recipientId),admin_id:String(adminId)})}));
    const payload=await response.json().catch(()=>({}));
    if(!response.ok||payload.success!==true)return{status:'failed',message:String(payload.message||payload.detail?.message||payload.error||`Delivery Worker returned HTTP ${response.status}.`)};
    return{status:String(payload.status||'queued'),message:payload.status==='waiting_for_start'?'Order is fulfilled. The recipient must start the Delivery Bot; it will send the queued delivery then.':payload.status==='already_sent'?'Delivery was already sent; no duplicate message was sent.':payload.status==='sent'?'Order fulfilled and delivery sent through Telegram.':'Order fulfilled and delivery queued.'};
  }catch(e){return{status:'failed',message:String(e?.message||'Could not reach the delivery service.')}}
}
async function route(request,env){
  const url=new URL(request.url),path=url.pathname;
  const forced=await forceKillState(env);
  if(Number(forced?.is_active)===1)return forcedKillPage();
  if(!path.startsWith('/login')&&path!=='/healthz')await initCached(env);
  if(path==='/healthz')return reply({service:'raindeals-database-manager',database_connected:!!(await db(env,'SELECT 1')),manager_link_cleanup_configured:!!env.MANAGER_TELEGRAM_BOT_TOKEN,render_required:false});
  if(path==='/login/bot'&&request.method==='GET')return botLoginPage();
  if(path==='/login/bot/exchange'&&request.method==='POST')return exchangeBotLogin(request,env,url);
  if(path==='/login'&&request.method==='POST'){if(request.headers.get('origin')!==url.origin)error('Invalid request origin.',403);return login(request,env);}
  if(path==='/login'&&request.method==='GET')return loginPage(env.ADMIN_VERIFY_BOT_USERNAME);
  if(path==='/login/start'&&request.method==='POST'){
    if(request.headers.get('origin')!==url.origin)error('Invalid request origin.',403);
    await initCached(env);await throttleLogin(request,env);await assertLoginUnlocked(env);const b=await body(request);if(!equal(b.username,env.MANAGER_USERNAME)||!equal(b.password,env.MANAGER_PASSWORD))error('Invalid manager credentials.',401);
    const sessionMinutes=Number(b.session_minutes||60);if(![60,120].includes(sessionMinutes))error('Choose a 60- or 120-minute session.');
    if(!env.ADMIN_VERIFY_BOT_USERNAME)error('Telegram verification bot is not configured.',503);
    const challenge=random(),id=crypto.randomUUID(),tokenHash=await hash(challenge);
    await tx(env,async t=>{await t.execute('INSERT INTO manager_login_challenges (id,token_hash,created_at,expires_at,approved_at,approved_by,consumed_at) VALUES (?,?,UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(),INTERVAL 5 MINUTE),NULL,NULL,NULL)',[id,tokenHash]);await t.execute('INSERT INTO raindeals_web_manager_login_preferences VALUES (?,?,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 5 MINUTE)) ON DUPLICATE KEY UPDATE session_minutes=VALUES(session_minutes),expires_at=VALUES(expires_at)',[tokenHash,sessionMinutes]);});
    return reply({challenge,session_minutes:sessionMinutes,telegram_url:`https://t.me/${String(env.ADMIN_VERIFY_BOT_USERNAME).replace(/^@/,'')}?start=${challenge}`,expires_in:300});
  }
  if(path==='/login/poll'&&request.method==='POST'){
    const b=await body(request);const row=(await rows(env,'SELECT approved_at,approved_by,expires_at FROM manager_login_challenges WHERE token_hash=? AND consumed_at IS NULL LIMIT 1',[await hash(b.challenge||'')]))[0];
    if(!row)return reply({approved:false,expired:true});
    if(!row.approved_at)return reply({approved:false});
    if(!(await managerAdmin(env,row.approved_by)))error('Telegram account is not an active manager admin.',403);
    return reply({approved:true});
  }
  if(path==='/login/finish'&&request.method==='POST'){
    if(request.headers.get('origin')!==url.origin)error('Invalid request origin.',403);
    const b=await body(request);if(!equal(b.username,env.MANAGER_USERNAME)||!equal(b.password,env.MANAGER_PASSWORD))error('Manager credentials changed. Sign in again.',401);
    await assertLoginUnlocked(env);const raw=random(),csrf=random(),digest=await hash(raw),challengeHash=await hash(b.challenge||'');let sessionMinutes=0;
    await tx(env,async t=>{await assertLoginUnlockedTx(t);const row=(await t.execute('SELECT id,approved_at,approved_by FROM manager_login_challenges WHERE token_hash=? AND consumed_at IS NULL AND expires_at>UTC_TIMESTAMP() FOR UPDATE',[challengeHash])).rows?.[0];if(!row?.approved_at)error('Telegram approval is not complete or has expired.',401);if(!(await managerAdmin(env,row.approved_by)))error('Telegram account is not an active manager admin.',403);const pref=(await t.execute('SELECT session_minutes FROM raindeals_web_manager_login_preferences WHERE token_hash=? AND expires_at>UTC_TIMESTAMP() FOR UPDATE',[challengeHash])).rows?.[0];sessionMinutes=Number(pref?.session_minutes);if(![60,120].includes(sessionMinutes))error('Session choice expired. Start login again.',401);await t.execute('UPDATE manager_login_challenges SET consumed_at=UTC_TIMESTAMP() WHERE id=?',[row.id]);await t.execute('DELETE FROM raindeals_web_manager_login_preferences WHERE token_hash=?',[challengeHash]);await t.execute('DELETE FROM raindeals_web_manager_sessions WHERE admin_id=? OR expires_at<UTC_TIMESTAMP()',[row.approved_by]);const interval=sessionMinutes===120?'120':'60';await t.execute(`INSERT INTO raindeals_web_manager_sessions VALUES (?,?,?,DATE_ADD(UTC_TIMESTAMP(),INTERVAL ${interval} MINUTE),UTC_TIMESTAMP())`,[digest,csrf,row.approved_by]);});
    return new Response(JSON.stringify({success:true,session_minutes:sessionMinutes}),{headers:{'content-type':'application/json','set-cookie':`rd_manager=${raw}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=${sessionMinutes*60}`,'cache-control':'no-store'}});
  }
  const auth=await session(request,env);
  if(!auth)return path.startsWith('/manage/')?reply({error:'Sign in required.'},401):Response.redirect(url.origin+'/login',303);
  const currentAdmin=await managerAdmin(env,auth.admin_id);if(!currentAdmin){await db(env,'DELETE FROM raindeals_web_manager_sessions WHERE token_hash=?',[auth.token_hash]);error('This Telegram admin is no longer authorized.',403);}const required=requiredPermission(path);if(required&&!hasPermission(currentAdmin,required))error('Your assigned role does not allow access to '+required+'.',403);
  if(request.method==='POST'&&writableSections.includes(required)&&!hasPermission(currentAdmin,required+'_write'))error('Your assigned role cannot change '+required+'.',403);
  if(!['GET','HEAD'].includes(request.method)){if(request.headers.get('origin')!==url.origin||!equal(request.headers.get('x-csrf-token'),auth.csrf))error('Invalid request origin or expired form.',403);}
  await initCached(env);await assertLoginUnlocked(env);
  if(path==='/logout'&&request.method==='POST'){await db(env,'DELETE FROM raindeals_web_manager_sessions WHERE token_hash=?',[auth.token_hash]);return new Response('{}',{headers:{'set-cookie':'rd_manager=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'}});}
  if(path==='/'&&request.method==='GET')return page(auth.csrf);
  if(path==='/manage/status'&&request.method==='GET')return reply(await status(env));
  if(path==='/manage/me'&&request.method==='GET')return reply({telegram_id:String(currentAdmin.telegram_id),display_name:currentAdmin.display_name,role_key:currentAdmin.role_key,is_owner:Number(currentAdmin.is_owner)===1,permissions:currentAdmin.permissions});
  if(path==='/manage/admins'&&request.method==='GET'){
    if(Number(currentAdmin.is_owner)!==1)error('Only an owner can manage admin roles.',403);
    const found=await rows(env,'SELECT telegram_id,display_name,role_key,permissions_json,is_active,is_owner,created_at,updated_at FROM raindeals_web_manager_admins ORDER BY is_owner DESC,created_at ASC');
    const output=found.map(r=>{let p=[];try{p=JSON.parse(r.permissions_json||'[]')}catch{};return{telegram_id:String(r.telegram_id),display_name:r.display_name,role_key:r.role_key,permissions:Array.isArray(p)?p:[],is_active:Number(r.is_active),is_owner:Number(r.is_owner),created_at:r.created_at,updated_at:r.updated_at}});
    return reply({rows:output,sections:managerSections,capabilities:managerCapabilities});
  }
  const adminMutation=path.match(/^\/manage\/admins(?:\/(\d{1,20}))?$/);
  if(adminMutation&&request.method==='POST'){
    if(Number(currentAdmin.is_owner)!==1)error('Only an owner can manage admin roles.',403);
    const target=adminMutation[1]||null,b=await body(request),telegramId=String(target||b.telegram_id||'').trim();
    if(!/^\d{1,20}$/.test(telegramId)||BigInt(telegramId)<=0n)error('Enter a valid numeric Telegram ID.');
    if(ownerIds(env).has(telegramId))error('Owner accounts are managed through the Worker allowlist and cannot be changed here.',403);
    if(target&&telegramId===String(auth.admin_id))error('You cannot change your own admin role.',403);
    if(b.is_active===0||b.is_active==='0'){
      await db(env,'UPDATE raindeals_web_manager_admins SET is_active=0,updated_at=UTC_TIMESTAMP(6) WHERE telegram_id=? AND is_owner=0',[telegramId]);
      await db(env,'DELETE FROM raindeals_web_manager_sessions WHERE admin_id=?',[telegramId]);
      await audit(env,auth.admin_id,'deactivate','manager_admin',telegramId,{});return reply({success:true,message:'Admin access disabled. Existing sessions were revoked.'});
    }
    const displayName=String(b.display_name||'').trim().slice(0,120),role=String(b.role_key||'custom').trim().toLowerCase();
    if(!/^[a-z][a-z0-9_-]{1,31}$/.test(role))error('Role name must be 2–32 letters, numbers, _ or -.');
    if(!Array.isArray(b.permissions)||!b.permissions.length)error('Select at least one permission.');
    const permissions=[...new Set(b.permissions.map(String))];for(const p of permissions)if(!managerSections.includes(p)&&!managerCapabilities.includes(p))error('Unknown permission: '+p);
    for(const p of permissions){if(p.endsWith('_write')&&!permissions.includes(p.slice(0,-6)))error('Grant the section before its edit permission.');if(['orders_export','orders_alerts','delivery_ids','delivery_secrets'].includes(p)&&!permissions.includes('orders'))error('Grant Orders before its special actions.');}
    await db(env,'INSERT INTO raindeals_web_manager_admins (telegram_id,display_name,role_key,permissions_json,is_active,is_owner,added_by,created_at,updated_at) VALUES (?,?,?,?,1,0,?,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE display_name=VALUES(display_name),role_key=VALUES(role_key),permissions_json=VALUES(permissions_json),is_active=1,updated_at=UTC_TIMESTAMP(6)',[telegramId,displayName,role,JSON.stringify(permissions),auth.admin_id]);
    await audit(env,auth.admin_id,target?'update':'create','manager_admin',telegramId,{role_key:role,permissions});return reply({success:true,message:'Admin role saved. The Telegram ID can now verify sign-ins.'});
  }
  if(path==='/manage/orders/export'&&request.method==='GET'){
    let after=url.searchParams.get('after_id')||'0';if(!/^\d{1,20}$/.test(after))error('Invalid export cursor.');
    const available=(await rows(env,'SHOW COLUMNS FROM orders')).map(c=>c.Field),visible=responseFields.orders.split(',').filter(c=>available.includes(c));
    const found=await rows(env,'SELECT '+visible.map(c=>'`'+c+'`').join(',')+' FROM orders WHERE id>? ORDER BY id ASC LIMIT 501',[after]);
    return reply({rows:found.slice(0,500),has_more:found.length>500,next_after_id:found.length>500?String(found[499].id):null});
  }
  if(path==='/manage/orders/alerts'&&request.method==='GET'){
    const after=url.searchParams.get('after_id');
    if(url.searchParams.get('baseline')==='1'){const latest=(await rows(env,'SELECT COALESCE(MAX(id),0) AS cursor FROM orders'))[0];return reply({rows:[],cursor:String(latest?.cursor||0)});}
    if(!after||!/^\d{1,20}$/.test(after))error('Invalid order alert cursor.');
    const available=(await rows(env,'SHOW COLUMNS FROM orders')).map(c=>c.Field),visible=['id','telegram_id','product_name','quantity','amount','status','created_at'].filter(c=>available.includes(c));
    const found=await rows(env,'SELECT '+visible.map(c=>'`'+c+'`').join(',')+' FROM orders WHERE id>? ORDER BY id ASC LIMIT 201',[after]);
    return reply({rows:found.slice(0,200),has_more:found.length>200,next_after_id:found.length>200?String(found[199].id):after});
  }
  const orderDetails=path.match(/^\/manage\/orders\/(\d+)\/details$/);
  if(orderDetails&&request.method==='GET'){
    const id=orderDetails[1],available=await columnsFor(env,'orders');
    const visible=[...responseFields.orders.split(','),'delivery_telegram_id','refunded','automatic_quantity','manual_pending_quantity'].filter(c=>available.includes(c));
    const order=(await rows(env,'SELECT '+visible.map(c=>'`'+c+'`').join(',')+",IF(delivered_account IS NOT NULL AND delivered_account<>'',1,0) AS has_delivery FROM orders WHERE id=?",[id]))[0];
    if(!order)error('Order not found.',404);
    const [submissions,jobs,webHistory,botHistory,sessionInfo]=await Promise.all([
      optionalRows(env,'SELECT telegram_id,`values`,submitted_at FROM order_requirement_submissions WHERE order_id=? LIMIT 1',[id]),
      optionalRows(env,'SELECT recipient_id,admin_id,status,attempts,created_at,updated_at,completed_at,last_error FROM raindeals_delivery_bot_jobs WHERE order_id=? LIMIT 1',[id]),
      rows(env,"SELECT a.admin_id,a.action,a.details,a.created_at,m.display_name FROM raindeals_web_manager_audit a LEFT JOIN raindeals_web_manager_admins m ON m.telegram_id=a.admin_id WHERE a.target_type='order' AND a.target_id=? ORDER BY a.id DESC LIMIT 50",[id]),
      optionalRows(env,"SELECT admin_telegram_id,action,summary,created_at FROM admin_audit_logs WHERE entity_type='order' AND entity_id=? ORDER BY id DESC LIMIT 50",[id]),
      rows(env,'SELECT admin_id,created_at,expires_at FROM raindeals_web_manager_sessions WHERE token_hash=?',[auth.token_hash])
    ]);
    let requirements=[];
    if(order.product_id&&(await columnsFor(env,'products')).includes('required_info')){const p=(await rows(env,'SELECT required_info FROM products WHERE id=?',[order.product_id]))[0];requirements=jsonValue(p?.required_info,[])}
    const submission=submissions[0],job=jobs[0]||null;
    let deliveredIdentifiers=[];
    if(hasPermission(currentAdmin,'delivery_ids')){const content=(await rows(env,'SELECT delivered_account FROM orders WHERE id=?',[id]))[0]?.delivered_account;deliveredIdentifiers=String(content||'').split(/\r?\n/).map(x=>x.trim()).filter(x=>/^[^:\s]{1,256}:.+$/.test(x)).map(x=>x.slice(0,x.indexOf(':')))}
    const history=webHistory.map(r=>{const detail=jsonValue(r.details,{});return{admin_id:String(r.admin_id),admin_name:r.display_name||'',action:r.action,at:r.created_at,source:'Manager Web',session_started_at:detail.session_started_at||null,summary:r.action==='fulfill'?'Order fulfilled using '+(detail.mode==='inventory'?'uploaded stock':'entered delivery details'):r.action==='edit_delivery'?'Saved delivery details corrected; no Telegram resend':r.action==='retry_delivery'?'Delivery checked / retried':''}}).concat(botHistory.map(r=>({admin_id:String(r.admin_telegram_id),action:r.action,at:r.created_at,source:'Telegram bot',summary:r.summary}))).sort((a,b)=>String(b.at).localeCompare(String(a.at)));
    const canWrite=hasPermission(currentAdmin,'orders_write'),completed=String(order.status).toLowerCase()==='completed';
    return reply({order,submitted_details:{status:submission?'submitted':Array.isArray(requirements)&&requirements.length?'awaiting_customer':'not_required',submitted_by:submission?String(submission.telegram_id):null,submitted_at:submission?.submitted_at||null,fields:jsonValue(submission?.values,{}),requirements},delivery:job,delivered_identifiers:deliveredIdentifiers,history,operator_session:sessionInfo[0]||null,can_edit_delivery:canWrite&&hasPermission(currentAdmin,'delivery_secrets')&&completed&&!Number(order.refunded)&&!['queued','sending','uncertain'].includes(job?.status),can_retry_delivery:canWrite&&completed&&Number(order.has_delivery)===1&&!Number(order.refunded)&&['manual','hybrid'].includes(order.delivery_type)&&(!job||['failed','queued'].includes(job.status))});
  }
  const orderContent=path.match(/^\/manage\/orders\/(\d+)\/delivery-content$/);
  if(orderContent){
    if(!hasPermission(currentAdmin,'orders_write')||!hasPermission(currentAdmin,'delivery_secrets'))error('Your role cannot view or edit delivery credentials.',403);
    const id=orderContent[1];
    if(request.method==='GET'){const order=(await rows(env,"SELECT delivered_account FROM orders WHERE id=? AND status='completed' AND refunded=0",[id]))[0];if(!order)error('Completed order not found.',404);await audit(env,auth.admin_id,'view_delivery','order',id,{});return reply({delivery_text:order.delivered_account||''})}
    if(request.method==='POST'){
      const b=await body(request),text=String(b.delivery_text||'').trim();if(!text||text.length>3000||b.confirm!==true)error('Confirm the correction and enter delivery details (maximum 3,000 characters).');
      const hasJobs=(await rows(env,"SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='raindeals_delivery_bot_jobs' LIMIT 1")).length;
      const timestamp=(await columnsFor(env,'orders')).includes('updated_at')?',updated_at=UTC_TIMESTAMP()':'';
      await tx(env,async t=>{const order=(await t.execute('SELECT status,refunded FROM orders WHERE id=? FOR UPDATE',[id])).rows?.[0];if(!order||order.status!=='completed'||Number(order.refunded))error('Only completed, non-refunded orders may be corrected.',409);const job=hasJobs?(await t.execute('SELECT status FROM raindeals_delivery_bot_jobs WHERE order_id=? FOR UPDATE',[id])).rows?.[0]:null;if(['queued','sending','uncertain'].includes(job?.status))error('Delivery is queued, in progress, or uncertain. Its content cannot be changed.',409);await t.execute('UPDATE orders SET delivered_account=?'+timestamp+' WHERE id=?',[text,id]);await t.execute("INSERT INTO raindeals_web_manager_audit (admin_id,action,target_type,target_id,details,created_at) VALUES (?,'edit_delivery','order',?,'{}',UTC_TIMESTAMP(6))",[auth.admin_id,id])});
      return reply({success:true,message:'Saved delivery details corrected. Previously sent Telegram messages were not changed or resent.'});
    }
    error('Method not allowed.',405);
  }
  const fulfillOrder=path.match(/^\/manage\/orders\/(\d+)\/fulfill$/);
  if(fulfillOrder&&request.method==='POST'){
    if(!env.DELIVERY_BRIDGE_SECRET)error('Delivery is not configured on the Manager Worker. Set the same DELIVERY_BRIDGE_SECRET used by the API Worker before fulfilling orders.',503);
    const orderId=fulfillOrder[1],b=await body(request),mode=String(b.mode||'manual');
    if(!['manual','inventory'].includes(mode))error('Choose manual delivery details or uploaded account inventory.');
    const suppliedText=String(b.delivery_text||'').trim();
    if(mode==='manual'&&(!suppliedText||suppliedText.length>3000))error('Enter delivery details (maximum 3,000 characters).');
    const suppliedRecipient=String(b.customer_telegram_id||'').trim();
    if(suppliedRecipient&&!/^\d{1,20}$/.test(suppliedRecipient))error('Customer Telegram ID must be a positive numeric ID.');
    const orderColumns=await columnsFor(env,'orders');
    const splitSelect=['automatic_quantity','manual_pending_quantity','stock_deducted_quantity','assigned_admin_id'].filter(c=>orderColumns.includes(c)).map(c=>','+c).join('');
    const timelineEnabled=(await rows(env,"SELECT 1 AS present FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='order_events' LIMIT 1")).length>0;
    const orderTimestamp=orderColumns.includes('updated_at')?',updated_at=UTC_TIMESTAMP()':'';
    const warrantyUpdate=(orderColumns.includes('warranty_days')&&orderColumns.includes('warranty_expires_at')?",warranty_expires_at=CASE WHEN warranty_days>0 AND warranty_expires_at IS NULL THEN DATE_ADD(UTC_TIMESTAMP(),INTERVAL warranty_days DAY) ELSE warranty_expires_at END":'')+(orderColumns.includes('coverage_started_at')?',coverage_started_at=COALESCE(coverage_started_at,UTC_TIMESTAMP())':'');
    const completed=await tx(env,async t=>{
      const order=(await t.execute('SELECT id,telegram_id,product_id,product_name,amount,quantity,status,delivery_type,is_preorder,refunded,delivery_telegram_id,delivered_account'+splitSelect+' FROM orders WHERE id=? FOR UPDATE',[orderId])).rows?.[0];
      if(!order)error('Order not found.',404);
      const state=String(order.status||'').toLowerCase(),deliveryType=String(order.delivery_type||'').toLowerCase();
      const canFulfill=!Number(order.refunded)&&['manual','hybrid'].includes(deliveryType)&&(state==='preorder'||['pending_manual','processing'].includes(state));
      if(!canFulfill)error('This order is not waiting for manual fulfillment, or it has been refunded.',409);
      if(order.assigned_admin_id&&String(order.assigned_admin_id)!==String(auth.admin_id))error('This order is assigned to another admin. Reassign it in the bot fulfillment queue first.',409);
      const apiOrder=Boolean((await t.execute('SELECT order_id FROM api_orders WHERE order_id=? LIMIT 1',[orderId])).rows?.length);
      let recipient=String(order.delivery_telegram_id||'');
      if(apiOrder){
        if(!recipient)recipient=suppliedRecipient;
        else if(suppliedRecipient&&suppliedRecipient!==recipient)error('Customer Telegram ID does not match the API order destination.',409);
        if(!recipient)error('Enter the customer Telegram ID for this API order before fulfillment.');
      }else{
        recipient=recipient||String(order.telegram_id||'');
        if(suppliedRecipient&&suppliedRecipient!==recipient)error('The recipient must match the order customer.',409);
      }
      if(!/^\d{1,20}$/.test(recipient)||BigInt(recipient)<=0n)error('This order does not have a valid Telegram delivery destination.',409);
      let deliveryText=suppliedText,stockUsed=0,newlyDeducted=0;
      if(mode==='inventory'){
        if(!order.product_id)error('This order has no linked product inventory.',409);
        const product=(await t.execute('SELECT source,file_content,stock,delivery_type,manual_fallback FROM products WHERE id=? FOR UPDATE',[order.product_id])).rows?.[0];
        if(!product)error('Linked product was not found.',404);
        if(String(product.source||'own')!=='own')error('Supplier stock is managed by its provider; use manual delivery details here.',409);
        const available=String(product.file_content||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean),quantity=Math.max(1,Number(order.manual_pending_quantity)||Number(order.quantity)-Number(order.automatic_quantity||0)||1);
        if(available.length<quantity)error(`Not enough uploaded accounts: ${available.length} available for quantity ${quantity}.`,409);
        deliveryText=available.slice(0,quantity).join('\n');stockUsed=quantity;
        const remaining=available.slice(quantity);
        const capacityManaged=Number(product.manual_fallback)||product.delivery_type==='manual';
        const reserved=Number(order.stock_deducted_quantity)>0||(order.stock_deducted_quantity==null&&!Number(order.is_preorder)&&capacityManaged);
        const stockAfter=reserved?Number(product.stock||0):capacityManaged?Math.max(Number(product.stock||0),available.length)-quantity:remaining.length;
        if(!reserved)newlyDeducted=quantity;
        await t.execute('UPDATE products SET file_content=?,stock=?,updated_at=UTC_TIMESTAMP() WHERE id=?',[remaining.join('\n'),stockAfter,order.product_id]);
      }
      deliveryText=[order.delivered_account,deliveryText].filter(Boolean).join('\n');
      const splitUpdate=orderColumns.includes('manual_pending_quantity')?',manual_pending_quantity=0,automatic_quantity=COALESCE(automatic_quantity,0)+'+stockUsed+((order.stock_deducted_quantity!=null||newlyDeducted)?',stock_deducted_quantity=COALESCE(stock_deducted_quantity,0)+'+newlyDeducted:''):'';
      let preorderCommission=null;
      if(Number(order.is_preorder)){
        const buyer=(await t.execute('SELECT referred_by FROM users WHERE telegram_id=? FOR UPDATE',[order.telegram_id])).rows?.[0];
        const referrerId=String(buyer?.referred_by||'');
        const rateText=String(env.REFERRAL_COMMISSION_RATE||'0.05');
        const rate=Number(rateText);
        const referrer=referrerId?(await t.execute('SELECT telegram_id FROM users WHERE telegram_id=? FOR UPDATE',[referrerId])).rows?.[0]:null;
        if(referrer&&Number.isFinite(rate)&&rate>0&&rate<=1){
          const creditToBalance=!['0','false','no','off'].includes(String(env.REFERRAL_CREDIT_TO_BALANCE??'true').toLowerCase());
          const commission=(await t.execute('SELECT CAST(ROUND(? * ?,8) AS CHAR) AS amount',[String(order.amount||'0'),rateText])).rows?.[0]?.amount||'0';
          if(creditToBalance){
            await t.execute('UPDATE users SET referral_earnings=ROUND(COALESCE(referral_earnings,0)+?,8),balance=ROUND(COALESCE(balance,0)+?,8) WHERE telegram_id=?',[commission,commission,referrerId]);
          }else{
            await t.execute('UPDATE users SET referral_earnings=ROUND(COALESCE(referral_earnings,0)+?,8) WHERE telegram_id=?',[commission,referrerId]);
          }
          preorderCommission={referrer_telegram_id:referrerId,amount:String(commission),credited_to_balance:creditToBalance};
        }
      }
      const assignmentUpdate=orderColumns.includes('assigned_admin_id')?',assigned_admin_id=COALESCE(assigned_admin_id,?)':'';
      const assignmentTime=orderColumns.includes('assigned_at')?',assigned_at=COALESCE(assigned_at,UTC_TIMESTAMP())':'';
      const completionParams=[deliveryText,recipient];
      if(assignmentUpdate)completionParams.push(auth.admin_id);
      completionParams.push(orderId);
      await t.execute("UPDATE orders SET delivered_account=?,status='completed',is_preorder=0,delivery_telegram_id=?"+splitUpdate+orderTimestamp+warrantyUpdate+assignmentUpdate+assignmentTime+' WHERE id=?',completionParams);
      if(timelineEnabled){
        if(!order.assigned_admin_id)await t.execute("INSERT INTO order_events (order_id,kind,summary,customer_visible,created_at) VALUES (?,'fulfillment_team','Assigned to the fulfillment team.',1,UTC_TIMESTAMP())",[orderId]);
        await t.execute("INSERT INTO order_events (order_id,kind,actor_id,summary,customer_visible,created_at) VALUES (?,'completed',?,'Team delivery completed.',1,UTC_TIMESTAMP())",[orderId,auth.admin_id]);
      }
      const sessionInfo=(await t.execute('SELECT created_at FROM raindeals_web_manager_sessions WHERE token_hash=?',[auth.token_hash])).rows?.[0];
      await t.execute('INSERT INTO raindeals_web_manager_audit (admin_id,action,target_type,target_id,details,created_at) VALUES (?,?,?,?,?,UTC_TIMESTAMP(6))',[auth.admin_id,'fulfill','order',String(orderId),JSON.stringify({mode,stock_used:stockUsed,recipient_id:recipient,preorder_commission:preorderCommission,session_started_at:sessionInfo?.created_at||null})]);
      return{recipient,stock_used:stockUsed,product_id:order.product_id,preorder_commission:preorderCommission};
    });
    const notification=await notifyCompletedOrder(env,orderId,completed.recipient,auth.admin_id);
    if(completed.stock_used)await audit(env,auth.admin_id,'consume','product_inventory',completed.product_id,{order_id:String(orderId),count:completed.stock_used});
    return reply({success:true,order_id:String(orderId),stock_used:completed.stock_used,notification,message:notification.message});
  }
  const retryOrderDelivery=path.match(/^\/manage\/orders\/(\d+)\/notify$/);
  if(retryOrderDelivery&&request.method==='POST'){
    const orderId=retryOrderDelivery[1],order=(await rows(env,"SELECT id,telegram_id,delivery_telegram_id,status,delivery_type,refunded,delivered_account FROM orders WHERE id=? LIMIT 1",[orderId]))[0];
    if(!order)error('Order not found.',404);
    if(String(order.status||'').toLowerCase()!=='completed'||Number(order.refunded)||!String(order.delivered_account||'').trim()||!['manual','hybrid'].includes(String(order.delivery_type||'').toLowerCase()))error('Only completed, non-refunded manual or hybrid deliveries can be retried.',409);
    const recipient=String(order.delivery_telegram_id||order.telegram_id||'');if(!/^\d{1,20}$/.test(recipient))error('This order has no valid Telegram delivery destination.',409);
    const notification=await notifyCompletedOrder(env,orderId,recipient,auth.admin_id);
    await audit(env,auth.admin_id,'retry_delivery','order',orderId,{notification_status:notification.status});
    return reply({success:notification.status!=='failed',order_id:String(orderId),notification,message:notification.message},notification.status==='failed'?502:200);
  }
  const orderIds=path.match(/^\/manage\/orders\/(\d+)\/delivery-ids$/);
  if(orderIds&&request.method==='GET'){
    if(!hasPermission(currentAdmin,'delivery_ids'))error('Your role cannot view delivered account identifiers.',403);
    const order=(await rows(env,'SELECT id,delivered_account FROM orders WHERE id=? LIMIT 1',[orderIds[1]]))[0];if(!order)error('Order not found.',404);
    const identifiers=String(order.delivered_account||'').split(/\r?\n/).map(x=>x.trim()).filter(x=>/^[^:\s]{1,256}:.+$/.test(x)).map(x=>x.slice(0,x.indexOf(':')));
    await audit(env,auth.admin_id,'view','delivered_identifiers',order.id,{count:identifiers.length});return reply({order_id:String(order.id),identifiers,format:'Only text before the first colon is returned; passwords and full delivery strings are never returned.'});
  }
  if(path==='/manage/settings'&&request.method==='GET'){
    const [saved,maintenance]=await Promise.all([
      rows(env,'SELECT * FROM raindeals_web_manager_settings'),
      rows(env,'SELECT enabled FROM maintenance_settings WHERE id=1 LIMIT 1')
    ]);
    const merged=saved.filter(row=>row.setting_key!=='maintenance_requested');
    merged.push({setting_key:'maintenance_requested',setting_value:String(Number(maintenance[0]?.enabled||0))});
    return reply({rows:merged,main_bot_integrated:true});
  }
  if(path==='/manage/settings'&&request.method==='POST'){
    const b=await body(request);
    if(!['maintenance_requested','maintenance_message'].includes(b.key))error('Unknown setting.');
    if(b.key==='maintenance_requested'){
      if(!['0','1'].includes(String(b.value)))error('Maintenance must be 0 or 1.');
      const enabled=Number(b.value);
      await db(env,'INSERT INTO maintenance_settings (id,enabled) VALUES (1,?) ON DUPLICATE KEY UPDATE enabled=VALUES(enabled)',[enabled]);
      await audit(env,auth.admin_id,'update','maintenance_mode','1',{enabled});
      return reply({success:true,enabled,message:`Maintenance mode is ${enabled?'ON':'OFF'} in the shared database. The main bot reads this setting on each update.`});
    }
    if(String(b.value).length>2000)error('Message too long.');
    await db(env,'INSERT INTO raindeals_web_manager_settings VALUES (?,?,?,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value),updated_by=VALUES(updated_by),updated_at=VALUES(updated_at)',[b.key,String(b.value),auth.admin_id]);
    await audit(env,auth.admin_id,'update','manager_setting',b.key,{key:b.key});
    return reply({success:true,message:'Manager note saved. The main bot continues to use its configured maintenance notice.'});
  }
  if(path==='/manage/api-settings'&&request.method==='GET'){
    const defaults={api_enabled:'1',api_requests_per_second:'3',api_requests_per_minute:'120',api_orders_per_minute:'10',api_invalid_auth_per_minute:'20',api_ip_requests_per_minute:'180'};
    const saved=await rows(env,"SELECT setting_key,setting_value FROM raindeals_web_manager_settings WHERE setting_key LIKE 'api_%'");
    for(const row of saved)if(row.setting_key in defaults)defaults[row.setting_key]=String(row.setting_value);
    return reply({settings:defaults,base_url:env.API_BASE_URL||null,secret_configuration:'Secrets must be managed as Worker secrets; they are not editable or displayed here.'});
  }
  if(path==='/manage/api-settings'&&request.method==='POST'){
    const b=await body(request),allowed={api_enabled:{min:0,max:1},api_requests_per_second:{min:1,max:1000},api_requests_per_minute:{min:1,max:100000},api_orders_per_minute:{min:1,max:10000},api_invalid_auth_per_minute:{min:1,max:10000},api_ip_requests_per_minute:{min:1,max:100000}};
    const entries=Object.entries(b.settings||{});if(!entries.length||entries.length!==Object.keys(allowed).length)error('Submit every API setting field.');
    const normalized={};for(const [key,value] of entries){const rule=allowed[key];if(!rule||!/^\d+$/.test(String(value))||Number(value)<rule.min||Number(value)>rule.max)error(`Invalid value for ${key}.`);normalized[key]=String(Number(value));}
    await tx(env,async t=>{for(const [key,value] of Object.entries(normalized))await t.execute('INSERT INTO raindeals_web_manager_settings (setting_key,setting_value,updated_by,updated_at) VALUES (?,?,?,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value),updated_by=VALUES(updated_by),updated_at=VALUES(updated_at)',[key,value,auth.admin_id]);});
    await audit(env,auth.admin_id,'update','api_settings','global',{keys:Object.keys(normalized)});return reply({success:true,message:'API settings saved. The API Worker applies changes within a few seconds.'});
  }
  if(path==='/manage/delivery/test'&&request.method==='POST'){const b=await body(request);const r=await env.API.fetch('https://internal/healthz');const j=await r.json();return reply({configured:!!j.delivery_webhook_configured,message:j.delivery_webhook_configured?'Delivery service is configured. Real order sends use the existing delivery webhook. Standalone test-message sending is not configured.':'Delivery bot is not configured.'});}
  if(path==='/manage/api_orders'&&request.method==='GET'){
    const pageNumber=Math.max(1,Math.min(100000,Number(url.searchParams.get('page'))||1)),offset=(pageNumber-1)*30,search=(url.searchParams.get('search')||'').trim().slice(0,100);
    const filter=search?"WHERE CAST(ao.id AS CHAR) LIKE ? OR CAST(ao.client_order_id AS CHAR) LIKE ? OR CAST(ak.telegram_id AS CHAR) LIKE ? OR CAST(ao.order_id AS CHAR) LIKE ?":"";
    const args=search?Array(4).fill(`%${search}%`):[];
    const found=await rows(env,`SELECT ao.id,ao.api_key_id,ao.client_order_id,ao.order_id,ao.status,ao.error_message,ao.created_at,ao.updated_at,ak.telegram_id,ak.key_prefix,o.product_id,o.product_name,o.quantity,o.amount,o.delivery_type FROM api_orders ao LEFT JOIN api_keys ak ON ak.id=ao.api_key_id LEFT JOIN orders o ON o.id=ao.order_id ${filter} ORDER BY ao.id DESC LIMIT 31 OFFSET ${offset}`,args);
    return reply({rows:found.slice(0,30),has_more:found.length>30,page:pageNumber,create:false});
  }
  if(path==='/manage/custom_rates/create'&&request.method==='POST'){
    const b=await body(request),telegramId=Number(b.telegram_id),productId=b.product_id===''||b.product_id==null?null:Number(b.product_id),price=String(b.price??'').trim();
    if(!Number.isSafeInteger(telegramId)||telegramId<=0||!/^\d+(?:\.\d{1,8})?$/.test(price)||Number(price)>999999999999||(productId!==null&&(!Number.isSafeInteger(productId)||productId<=0)))error('Enter a valid Telegram ID, product scope, and nonnegative price.');
    await tx(env,async t=>{const user=(await t.execute('SELECT id FROM users WHERE telegram_id=? LIMIT 1 FOR UPDATE',[telegramId])).rows?.[0];if(!user)error('Telegram user was not found.',404);if(productId!==null&&!(await t.execute('SELECT id FROM products WHERE id=? LIMIT 1',[productId])).rows?.[0])error('Product was not found.',404);const scope=productId===null?'product_id IS NULL':'product_id=?',params=productId===null?[telegramId]:[telegramId,productId];await t.execute(`UPDATE custom_rates SET is_active=0,stopped_at=UTC_TIMESTAMP() WHERE telegram_id=? AND ${scope} AND is_active=1`,params);if(productId===null)await t.execute('UPDATE rate_control_assignments SET is_active=0,stopped_at=UTC_TIMESTAMP() WHERE telegram_id=? AND is_active=1',[telegramId]);await t.execute('INSERT INTO custom_rates (telegram_id,product_id,price,is_active,created_at) VALUES (?,?,?,1,UTC_TIMESTAMP())',[telegramId,productId,price]);});
    await audit(env,auth.admin_id,'create','custom_rate',null,{telegram_id:telegramId,product_id:productId,price});return reply({success:true,message:'Custom rate saved.'});
  }
  const customRateStop=path.match(/^\/manage\/custom_rates\/(\d+)\/stop$/);
  if(customRateStop&&request.method==='POST'){
    const id=customRateStop[1];await db(env,'UPDATE custom_rates SET is_active=0,stopped_at=UTC_TIMESTAMP() WHERE id=? AND is_active=1',[id]);await audit(env,auth.admin_id,'stop','custom_rate',id,{});return reply({success:true,message:'Custom rate stopped.'});
  }
  if(path==='/manage/product_rate_controls/save'&&request.method==='POST'){
    const b=await body(request),productId=Number(b.product_id),rate=String(b.rate??'').trim();if(!Number.isSafeInteger(productId)||productId<=0||!/^\d+(?:\.\d{1,8})?$/.test(rate)||Number(rate)>999999999999)error('Enter a product and a valid nonnegative rate.');
    await tx(env,async t=>{if(!(await t.execute('SELECT id FROM products WHERE id=? LIMIT 1',[productId])).rows?.[0])error('Product was not found.',404);await t.execute('INSERT INTO product_rate_controls (product_id,rate,is_active,updated_at) VALUES (?,?,1,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE rate=VALUES(rate),is_active=1,updated_at=UTC_TIMESTAMP()',[productId,rate]);});await audit(env,auth.admin_id,'save','product_rate_control',productId,{rate});return reply({success:true,message:'Product rate saved.'});
  }
  const productRateToggle=path.match(/^\/manage\/product_rate_controls\/(\d+)\/toggle$/);
  if(productRateToggle&&request.method==='POST'){
    const id=productRateToggle[1];await db(env,'UPDATE product_rate_controls SET is_active=IF(is_active=1,0,1),updated_at=UTC_TIMESTAMP() WHERE id=?',[id]);await audit(env,auth.admin_id,'toggle','product_rate_control',id,{});return reply({success:true,message:'Product rate status changed.'});
  }
  if(path==='/manage/rate_control_assignments/create'&&request.method==='POST'){
    const b=await body(request),telegramId=Number(b.telegram_id);if(!Number.isSafeInteger(telegramId)||telegramId<=0)error('Enter a valid Telegram user ID.');
    await tx(env,async t=>{if(!(await t.execute('SELECT id FROM users WHERE telegram_id=? LIMIT 1',[telegramId])).rows?.[0])error('Telegram user was not found.',404);if(!(await t.execute('SELECT id FROM product_rate_controls WHERE is_active=1 LIMIT 1',[])).rows?.[0])error('Save at least one active product rate first.');await t.execute('UPDATE rate_control_assignments SET is_active=0,stopped_at=UTC_TIMESTAMP() WHERE telegram_id=? AND is_active=1',[telegramId]);await t.execute('UPDATE custom_rates SET is_active=0,stopped_at=UTC_TIMESTAMP() WHERE telegram_id=? AND product_id IS NULL AND is_active=1',[telegramId]);await t.execute('INSERT INTO rate_control_assignments (telegram_id,is_active,created_at) VALUES (?,1,UTC_TIMESTAMP())',[telegramId]);});await audit(env,auth.admin_id,'assign','rate_control',telegramId,{});return reply({success:true,message:'Rate Control assigned.'});
  }
  const assignmentStop=path.match(/^\/manage\/rate_control_assignments\/(\d+)\/stop$/);
  if(assignmentStop&&request.method==='POST'){
    const id=assignmentStop[1];await db(env,'UPDATE rate_control_assignments SET is_active=0,stopped_at=UTC_TIMESTAMP() WHERE id=? AND is_active=1',[id]);await audit(env,auth.admin_id,'stop','rate_control_assignment',id,{});return reply({success:true,message:'Rate Control assignment stopped.'});
  }
  const apiKeyRevoke=path.match(/^\/manage\/api_keys\/(\d+)\/revoke$/);
  if(apiKeyRevoke&&request.method==='POST'){
    const id=apiKeyRevoke[1];await db(env,'UPDATE api_keys SET is_active=0,revoked_at=UTC_TIMESTAMP() WHERE id=? AND is_active=1',[id]);await audit(env,auth.admin_id,'revoke','api_key',id,{});return reply({success:true,message:'API key revoked. Existing API requests using it will be rejected.'});
  }
  const match=path.match(/^\/manage\/([a-z_]+)(?:\/(\d+)(?:\/(accounts|link|disable|stock|catalog|test|import|delete|fulfill|notify))?)?$/);if(!match||!tables[match[1]])error('Endpoint not found.',404);
  const [,name,id,action]=match,meta=tables[name],dbTable=meta.table||name;
  if(name==='providers'&&id&&action==='delete'&&request.method==='POST'){
    const b=await body(request);if(b.confirm!==true)error('Explicit confirmation is required to remove a provider.',403);
    const provider=(await rows(env,'SELECT id,name,is_active FROM providers WHERE id=? LIMIT 1',[id]))[0];if(!provider)error('Provider not found.',404);if(Number(provider.is_active))error('Disable the provider before removing it.');
    const linked=(await rows(env,'SELECT COUNT(*) AS total FROM products WHERE provider_id=?',[id]))[0];if(Number(linked?.total||0)>0)error('This provider still has linked products. Disable or reassign those products before removing the provider.');
    await audit(env,auth.admin_id,'remove','provider',id,{name:provider.name});await db(env,'DELETE FROM providers WHERE id=? AND is_active=0',[id]);return reply({success:true,message:'Reseller provider removed.'});
  }
  if(name==='providers'&&id&&['catalog','test'].includes(action)&&request.method==='GET'){
    const provider=(await rows(env,'SELECT id,name,base_url,auth_type,api_key,api_secret,configuration,is_active FROM providers WHERE id=? LIMIT 1',[id]))[0];
    if(!provider||!Number(provider.is_active))error('Provider not found or disabled.',404);
    if(!provider.api_key)error('Provider API key is not configured.',400);
    const catalogPayload=await providerRequest(provider,'products');
    let balance=0;
    try{const balancePayload=await providerRequest(provider,'balance');const bc=providerConfig(provider).balance||{};const path=bc.balance||bc.path||providerConfig(provider).balance_path;let found=path?nested(balancePayload,path):undefined;if(found===undefined&&balancePayload&&typeof balancePayload==='object')for(const key of ['balance','funds','credits','wallet','amount','user_balance','walletBalance','data.balance']){found=nested(balancePayload,key);if(found!==undefined)break}balance=Number(found??0);if(!Number.isFinite(balance)||balance<0)balance=0}catch{balance=0}
    const products=providerCatalogItems(provider,catalogPayload);
    await audit(env,auth.admin_id,'fetch_catalog','provider',id,{item_count:products.length});
    return reply({provider_id:Number(id),provider:provider.name,balance,has_balance:balance>0,products});
  }
  if(name==='providers'&&id&&action==='import'&&request.method==='POST'){
    const b=await body(request),provider=(await rows(env,'SELECT id,name,base_url,auth_type,api_key,api_secret,configuration,is_active FROM providers WHERE id=? LIMIT 1',[id]))[0];
    if(!provider||!Number(provider.is_active))error('Provider not found or disabled.',404);
    if(!provider.api_key)error('Provider API key is not configured.',400);
    const serviceId=String(b.service_id||'').trim(),quantity=Number(b.quantity),listingPrice=Number(b.price);
    if(!serviceId||!Number.isInteger(quantity)||quantity<1||quantity>1000000||!Number.isFinite(listingPrice)||listingPrice<=0||listingPrice>999999999999)error('Choose an item, positive quantity, and valid selling price.');
    const [catalogPayload,balancePayload]=await Promise.all([providerRequest(provider,'products'),providerRequest(provider,'balance')]);
    const items=providerCatalogItems(provider,catalogPayload),item=items.find(x=>x.service_id===serviceId);if(!item)error('Provider item is no longer available. Refresh the catalog.');
    const bc=providerConfig(provider).balance||{},balancePath=bc.balance||bc.path||providerConfig(provider).balance_path;let balanceValue=balancePath?nested(balancePayload,balancePath):undefined;if(balanceValue===undefined&&balancePayload&&typeof balancePayload==='object')for(const key of ['balance','funds','credits','wallet','amount','user_balance','walletBalance','data.balance']){balanceValue=nested(balancePayload,key);if(balanceValue!==undefined)break}
    const balance=Number(balanceValue??0);if(!Number.isFinite(balance)||balance<0)error('Provider balance response could not be read. Configure balance_path and retry.',502);
    const maxAffordable=Math.floor(balance/item.cost),maxQuantity=Math.min(item.stock,maxAffordable);if(quantity>maxQuantity)error(`Quantity exceeds the live stock or wallet limit (${maxQuantity}).`);
    const name=String(b.name||item.name).trim().slice(0,255),category=String(b.category||item.category).trim().slice(0,255),description=String(b.description||item.description).trim().slice(0,4000),icon=String(b.icon||item.icon||'📦').slice(0,50),instruction=String(b.delivery_instruction||'').trim().slice(0,5000);if(name.length<2||!category)error('Product name and category are required.');
    const delivery=['automatic','manual','hybrid'].includes(b.delivery_type)?b.delivery_type:'automatic',preorder=String(b.preorder??'0')==='1'?1:0,isActive=String(b.is_active??'1')==='0'?0:1,apiEnabled=String(b.api_enabled??'1')==='0'?0:1,low=Number(b.low_stock_threshold??3);if(!Number.isInteger(low)||low<0||low>1000000)error('Low-stock threshold must be a nonnegative whole number.');const bulk=parseImportBulk(b.bulk_pricing);
    const result=await db(env,"INSERT INTO products (name,icon,description,category,price,stock,delivery_instruction,is_active,api_enabled,delivery_type,manual_fallback,preorder,low_stock_threshold,bulk_pricing,source,provider_id,reseller_service_id,reseller_cost,reseller_name,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,'reseller',?,?,?,?,UTC_TIMESTAMP(),UTC_TIMESTAMP())",[name,icon,description,category,listingPrice,quantity,instruction||null,isActive,apiEnabled,delivery,preorder,low,bulk,provider.id,serviceId,item.cost,provider.name]);
    const productId=result?.insertId||result?.lastInsertId||null;await audit(env,auth.admin_id,'import','reseller_product',productId,{provider_id:Number(id),service_id:serviceId,quantity,provider_cost:item.cost,listing_price:listingPrice});return reply({success:true,product_id:productId,message:`Imported ${name} as a live reseller product.`});
  }
  if(action==='stock'&&name==='products'&&request.method==='POST'){
    const b=await body(request);if(!/^\d{1,9}$/.test(String(b.stock)))error('Stock must be a nonnegative whole number.');
    await tx(env,async t=>{const p=(await t.execute('SELECT source,delivery_type,manual_fallback FROM products WHERE id=? FOR UPDATE',[id])).rows?.[0];if(!p)error('Product not found.',404);if(p.source!=='own'||(p.delivery_type==='automatic'&&!Number(p.manual_fallback)))error('Use account upload for automatic stock; supplier stock comes from the provider.');await t.execute('UPDATE products SET stock=?,updated_at=UTC_TIMESTAMP() WHERE id=?',[Number(b.stock),id]);});await audit(env,auth.admin_id,'update','product_stock',id,{stock:Number(b.stock)});return reply({success:true,message:'Manual stock updated.'});
  }
  if(action==='link'&&name==='products'&&request.method==='GET')return reply({url:env.BOT_USERNAME?`https://t.me/${env.BOT_USERNAME}?start=product_${id}`:null,message:env.BOT_USERNAME?'Product link':'Main bot username is not configured.'});
  if(action==='accounts'&&name==='products'){
    if(request.method==='GET'){const p=(await rows(env,'SELECT id,source,stock,file_content FROM products WHERE id=?',[id]))[0];if(!p)error('Product not found.',404);return reply({id:p.id,source:p.source,stock:p.stock,account_count:String(p.file_content||'').split(/\r?\n/).filter(x=>x.trim()).length});}
    if(request.method==='POST'){const b=await body(request);if(!['append','replace','clear'].includes(b.mode))error('Invalid accounts action.');if(b.mode!=='append'&&b.confirm!==true)error('Explicit confirmation is required for replace or clear.',403);const content=String(b.content||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean).join('\n');await tx(env,async t=>{const p=(await t.execute('SELECT source,file_content,stock,delivery_type,manual_fallback FROM products WHERE id=? FOR UPDATE',[id])).rows?.[0];if(!p)error('Product not found.',404);if(p.source!=='own')error('Supplier inventory is managed by the provider.');const value=b.mode==='clear'?'':b.mode==='replace'?content:[p.file_content,content].filter(Boolean).join('\n');await t.execute('UPDATE products SET file_content=?,stock=?,updated_at=UTC_TIMESTAMP() WHERE id=?',[value,(Number(p.manual_fallback)||p.delivery_type==='manual')?Math.max(Number(p.stock||0),value?value.split(/\r?\n/).filter(x=>x.trim()).length:0):(value?value.split(/\r?\n/).filter(x=>x.trim()).length:0),id]);});await audit(env,auth.admin_id,b.mode,'product_inventory',id,{mode:b.mode,line_count:b.mode==='clear'?0:content.split(/\r?\n/).filter(Boolean).length});return reply({success:true});}
  }
  if(action==='disable'&&name==='products'&&request.method==='POST'){const b=await body(request);if(b.confirm!==true)error('Explicit confirmation is required to disable this product.',403);await db(env,'UPDATE products SET is_active=0,api_enabled=0 WHERE id=?',[id]);await audit(env,auth.admin_id,'disable','product',id,{});return reply({success:true,message:'Product archived; order history is preserved.'});}
  if(request.method==='GET'){
    const columns=await columnsFor(env,dbTable);
    const allowed=(responseFields[name]||[]).split(',').filter(Boolean);const visible=columns.filter(c=>allowed.includes(c)&&!secretFields.test(c)&&c!=='file_content');
    const pageNumber=Math.max(1,Math.min(100000,Number(url.searchParams.get('page'))||1)),offset=(pageNumber-1)*30;
    const search=(url.searchParams.get('search')||'').slice(0,100);const searchable=visible.filter(c=>['id','telegram_id','name','code','status','product_name','username','full_name','key_prefix'].includes(c));
    const where=id?' WHERE id=?':search&&searchable.length?' WHERE '+searchable.map(c=>`CAST(\`${c}\` AS CHAR) LIKE ?`).join(' OR '):'';
    const params=id?[id]:where?searchable.map(()=>`%${search}%`):[];
    const extra=name==='orders'?",IF(COALESCE(refunded,0)=0 AND LOWER(COALESCE(delivery_type,'')) IN ('manual','hybrid') AND (LOWER(COALESCE(status,''))='preorder' OR LOWER(COALESCE(status,'')) IN ('pending_manual','processing')),1,0) AS can_fulfill,IF(LOWER(COALESCE(status,''))='completed' AND COALESCE(refunded,0)=0 AND LOWER(COALESCE(delivery_type,'')) IN ('manual','hybrid') AND delivered_account IS NOT NULL AND delivered_account<>'',1,0) AS has_delivery":'';
    const pageRows=await rows(env,`SELECT ${visible.map(c=>'`'+c+'`').join(',')}${extra} FROM \`${dbTable}\`${where} ORDER BY id DESC LIMIT 31 OFFSET ${offset}`,params);
    let outputRows=pageRows.slice(0,30);
    if(name==='providers'&&outputRows.length){
      const ids=outputRows.map(row=>row.id),placeholders=ids.map(()=>'?').join(',');
      const [flags,counts]=await Promise.all([
        rows(env,`SELECT id,(api_key IS NOT NULL AND api_key<>'') AS has_api_key,(api_secret IS NOT NULL AND api_secret<>'') AS has_api_secret FROM providers WHERE id IN (${placeholders})`,ids),
        rows(env,`SELECT provider_id,COUNT(*) AS product_count FROM products WHERE provider_id IN (${placeholders}) GROUP BY provider_id`,ids)
      ]);
      const flagMap=new Map(flags.map(row=>[String(row.id),row]));
      const countMap=new Map(counts.map(row=>[String(row.provider_id),Number(row.product_count)]));
      for(const row of outputRows)Object.assign(row,flagMap.get(String(row.id))||{},{product_count:countMap.get(String(row.id))||0});
    }
    return reply({rows:outputRows,has_more:pageRows.length>30,fields:meta.fields.split(',').filter(c=>c&&columns.includes(c)&&!secretFields.test(c)),required:meta.required||[],create:!meta.read,defaults:meta.defaults||{},page:pageNumber});
  }
  if(request.method==='POST'&&!action){const b=await body(request),values=validate(meta.fields,b,!id);if(!id){if(meta.read)error('Creation is unavailable for this section.');Object.assign(values,{...meta.defaults,...values});for(const f of meta.required||[])if(values[f]==null||values[f]==='')error(`${f} is required.`);if(name==='promocodes')values.created_by=auth.admin_id;}
    const keys=Object.keys(values);if(!keys.length)error('No editable values supplied.');
    if(id&&name==='products'&&'is_active'in values){const old=(await rows(env,'SELECT is_active FROM products WHERE id=?',[id]))[0];if(old&&Number(old.is_active)!==Number(values.is_active)&&b.confirm_sensitive!==true)error('Explicit confirmation is required to change product visibility.',403);}
    if(id)await db(env,`UPDATE \`${dbTable}\` SET ${keys.map(k=>'`'+k+'`=?').join(',')} WHERE id=?`,[...Object.values(values),id]);else await db(env,`INSERT INTO \`${dbTable}\` (${keys.map(k=>'`'+k+'`').join(',')}) VALUES (${keys.map(()=>'?').join(',')})`,Object.values(values));
    await audit(env,auth.admin_id,id?'update':'create',name,id||null,{fields:keys});return reply({success:true,message:'Saved to the shared database. The main bot must read this data to apply changes.'});}
  error('Action is not configured.',405);
}
export default {async fetch(request,env){try{const response=await route(request,env);const headers=new Headers(response.headers);headers.set('cache-control','no-store');headers.set('x-content-type-options','nosniff');headers.set('x-frame-options','DENY');headers.set('referrer-policy','no-referrer');headers.set('content-security-policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");return new Response(response.body,{status:response.status,headers});}catch(e){if(!e.status)console.error('manager_request_failed',String(e.message).replace(/(password|token|secret)[^ ]*/gi,'[redacted]').slice(0,120));return reply({error:e.status?e.message:'Database operation failed. Check the schema and Worker service bindings.'},e.status||503);}},async scheduled(_event,env){try{await cleanExpiredManagerLinkCards(env)}catch(e){console.error('manager_link_expiry_cleanup_failed',String(e?.message||'unknown').slice(0,120))}}};
