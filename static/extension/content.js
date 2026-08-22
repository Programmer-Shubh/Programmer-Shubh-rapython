// RaTrade - Unlock TradingView webhook in FREE plan (Stoxo/NextLevel style)
(function(){
  const RURL_KEY = "ratrade_webhook_url";
  let webhookUrl = "";

  chrome.storage.sync.get([RURL_KEY], (r)=>{ webhookUrl = r[RURL_KEY] || "https://ratrade.onrender.com/api/webhook/free"; });

  function unlockWebhook(){
    // Enable webhook checkbox & URL input even on Free plan
    const checks = document.querySelectorAll('input[type="checkbox"]');
    checks.forEach(ch=>{
      const label = ch.closest('label') || ch.parentElement;
      const txt = (label?.innerText || ch.getAttribute('aria-label') || "").toLowerCase();
      if(txt.includes("webhook")){
        ch.disabled = false;
        ch.removeAttribute("disabled");
        label && (label.style.opacity="1");
        label && (label.style.pointerEvents="auto");
      }
    });
    // Find webhook URL inputs (placeholder contains webhook)
    document.querySelectorAll('input[placeholder*="webhook" i], input[name*="webhook" i]').forEach(inp=>{
      inp.disabled=false;
      inp.removeAttribute("disabled");
      inp.placeholder = webhookUrl || inp.placeholder;
      inp.style.opacity="1";
      if(!inp.value && webhookUrl) inp.value = webhookUrl;
      // Auto-fill RaTrade URL
      inp.addEventListener('focus', ()=>{ if(!inp.value && webhookUrl) inp.value = webhookUrl; });
    });
    // Also enable by data-name
    document.querySelectorAll('[data-name*="webhook" i]').forEach(el=>{
      el.style.display=""; el.style.opacity="1";
    });
  }

  // Watch TradingView alert dialog
  const obs = new MutationObserver(()=> unlockWebhook());
  obs.observe(document.body, {childList:true, subtree:true});
  setInterval(unlockWebhook, 1200);
  unlockWebhook();

  // Intercept TradingView alert creation to also POST to RaTrade (fallback)
  const origFetch = window.fetch;
  window.fetch = async function(url, opts){
    const res = await origFetch.apply(this, arguments);
    try{
      const u = String(url);
      if(u.includes("tradingview.com") && u.includes("alert") && opts && opts.method==="POST"){
        const body = opts.body ? String(opts.body) : "";
        if(body.includes("webhook") || body.includes("ratrade")){
          // Also notify background
          chrome.runtime.sendMessage({type:"TV_ALERT", url:u, body});
        }
      }
    }catch(e){}
    return res;
  };
  console.log("[RaTrade] Free webhook unlocked - paste your RaTrade URL: " + webhookUrl);
})();
