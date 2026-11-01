import { doc } from './ui.js';

export function loginPage(bot) {
  const name = /^[A-Za-z0-9_]{5,32}$/.test(String(bot || '').replace(/^@/, ''))
    ? String(bot).replace(/^@/, '') : '';
  const html = `<body class="login-body"><main class="login-card">
    <section class="intro"><div><div class="mark">✳ RAIN DEALS · ADMIN</div>
      <h1>Good to have you back.</h1><p>Your control room for orders, products, customers, and system status.</p>
      </div><div class="orb"></div></section>
    <section class="login"><h2>Sign in</h2>
      <p class="muted">Enter your manager credentials, then approve with your assigned Telegram account.</p>
      <form id="login"><label>Admin username</label><input name="username" autocomplete="username" required>
        <label>Password</label><input name="password" type="password" autocomplete="current-password" required>
        <label>Session duration</label><select name="session_minutes">
          <option value="60">60 minutes</option><option value="120">120 minutes</option>
        </select><button id="continue">Continue to Telegram verification →</button></form>
      <div id="verify" hidden><a class="telegram" id="telegram" target="_blank" rel="noopener">
        Open Telegram verification bot</a>
        <p class="muted">Tap <b>Start</b> in Telegram, then return here. This page checks approval automatically.</p>
        <p id="status" class="status" role="status">Waiting for Telegram approval…</p></div>
      <p id="message" class="error" role="alert"></p>
      <p class="muted">Verification bot: ${name ? `<a href="https://t.me/${name}">@${name}</a>` : 'not configured'}
        · fallback: send /generate privately.</p></section></main></body>`;
  const script = `
    const form=document.getElementById('login');
    const button=document.getElementById('continue');
    const verify=document.getElementById('verify');
    const link=document.getElementById('telegram');
    const status=document.getElementById('status');
    const message=document.getElementById('message');
    let challenge='',credentials,pollTimer;
    async function call(path,data,timeoutMs=25000){
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);
      try{
        const response=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify(data),signal:controller.signal});
        const result=await response.json();
        if(!response.ok)throw Error(result.error||'Request failed');
        return result;
      }finally{clearTimeout(timer)}
    }
    function stop(text){
      challenge='';clearTimeout(pollTimer);button.disabled=false;
      if(text)message.textContent=text;
    }
    async function poll(){
      if(!challenge)return;
      try{
        const result=await call('/login/poll',{challenge},15000);
        if(result.expired){stop('Verification expired. Press Continue to try again.');return}
        if(result.approved){
          status.textContent='Verified. Signing in…';
          const finished=challenge;challenge='';
          await call('/login/finish',{...credentials,challenge:finished});
          location.href='/';return;
        }
        pollTimer=setTimeout(poll,2000);
      }catch(error){
        stop(error.name==='AbortError'?'Verification check timed out. Press Continue to try again.':error.message);
      }
    }
    form.onsubmit=async event=>{
      event.preventDefault();stop();button.disabled=true;
      button.textContent='Preparing Telegram link…';message.textContent='';
      credentials=Object.fromEntries(new FormData(form));
      let popup=null;
      try{
        popup=window.open('about:blank','_blank');
        if(popup)popup.document.body.textContent='Preparing your Telegram verification link…';
        status.textContent='Creating verification request…';
        const result=await call('/login/start',credentials,30000);
        challenge=result.challenge;link.href=result.telegram_url;verify.hidden=false;
        status.textContent='Open Telegram, tap Start, then return here. Waiting for approval…';
        if(popup)popup.location.href=result.telegram_url;
        else message.textContent='Your browser blocked the new tab. Tap the Telegram link above.';
        pollTimer=setTimeout(poll,2000);
      }catch(error){
        if(popup)popup.close();
        stop(error.name==='AbortError'?'The manager took too long to respond. Try again.':error.message);
      }finally{
        button.textContent='Continue to Telegram verification →';
        if(challenge)button.disabled=false;
      }
    };
  `;
  return doc(html, script);
}
