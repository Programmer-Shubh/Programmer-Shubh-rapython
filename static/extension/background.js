chrome.runtime.onMessage.addListener((msg, sender, sendResponse)=>{
  if(msg.type==="TV_ALERT"){
    // Forward to RaTrade free webhook as backup
    chrome.storage.sync.get(["ratrade_webhook_url"], (r)=>{
      const url = r.ratrade_webhook_url || "https://ratrade.onrender.com/api/webhook/free";
      fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: msg.body || "{}"}).catch(()=>{});
    });
  }
});
