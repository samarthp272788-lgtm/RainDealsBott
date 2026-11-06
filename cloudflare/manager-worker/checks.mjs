import assert from 'node:assert/strict';
import worker from './src/index.js';
import {page} from './src/ui.js';
import {loginPage} from './src/login_v2.js';

const calls=[];
const csrf='c'.repeat(64),token='a'.repeat(64);
let forceKill=false;
const env={MAIN_BOT_SETTINGS_INTEGRATED:'false',MANAGER_ADMIN_IDS:'123',TIDB:{
  async execute(sql,args=[]){calls.push({sql,args});if(sql.includes('FROM raindeals_web_manager_sessions'))return {rows:[{csrf,admin_id:123,token_hash:'hash'}]};if(sql.includes('FROM raindeals_web_manager_stepup_grants'))return {rows:[{expires_at:'2099-01-01 00:00:00'}]};if(sql.includes('FROM raindeals_web_manager_force_kill'))return {rows:[{is_active:forceKill?1:0}]};return {rows:[]};},
  async begin(){return {async execute(sql,args){calls.push({sql,args});if(sql.startsWith('SELECT source,file_content'))return {rows:[{source:'own',file_content:'first'}]};return {rows:[]};},async commit(){calls.push({sql:'COMMIT'})},async rollback(){calls.push({sql:'ROLLBACK'})}}}
}};
const request=(path,body,extra={})=>new Request('https://manager.example'+path,{method:body===undefined?'GET':'POST',headers:{cookie:'rd_manager='+token,origin:'https://manager.example','x-csrf-token':csrf,...extra},...(body===undefined?{}:{body:JSON.stringify(body)})});
assert.equal((await worker.fetch(new Request('https://manager.example/manage/products'),env)).status,401);
assert.equal((await worker.fetch(request('/manage/settings',{key:'maintenance_requested',value:'1'},{'x-csrf-token':'bad'}),env)).status,403);
assert.equal((await worker.fetch(request('/manage/settings',{key:'maintenance_requested',value:'1'},{origin:'https://attacker.example'}),env)).status,403);
let r=await worker.fetch(request('/manage/settings',{key:'maintenance_requested',value:'1'}),env);
assert.equal(r.status,200);assert.match((await r.json()).message,/shared database/);
assert.deepEqual(calls.find(x=>x.sql.startsWith('INSERT INTO maintenance_settings')).args,[1]);
assert.equal((await worker.fetch(request('/manage/settings',{key:'arbitrary_sql',value:'DROP TABLE users'}),env)).status,400);
r=await worker.fetch(request('/manage/products/12/accounts',{mode:'append',content:'second\nthird'}),env);assert.equal(r.status,200);
const inventory=calls.find(x=>x.sql.startsWith('UPDATE products SET file_content'));assert.deepEqual(inventory.args,['first\nsecond\nthird',3,'12']);assert(calls.some(x=>x.sql==='COMMIT'));
assert.equal((await worker.fetch(request('/manage/deposits/1',{status:'completed'}),env)).status,400);
assert.equal((await worker.fetch(request('/manage/unknown_table'),env)).status,404);
forceKill=true;
const locked=await worker.fetch(new Request('https://manager.example/login'),env);
const lockedPage=await locked.text();assert.match(lockedPage,/force-locked/);assert.doesNotMatch(lockedPage,/<form|<button|<input/i);
for(const response of [page(csrf),loginPage('verificationoctabot')]){const html=await response.text();const script=/<script>([\s\S]*)<\/script>/.exec(html)[1];new Function(script);}
console.log('Manager checks passed: authentication, CSRF, settings isolation, inventory transaction, financial write restrictions, force-lock page, generated scripts.');
