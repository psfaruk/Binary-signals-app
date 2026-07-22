/* alltime_otc.js — All-Time OTC Market page bootstrap.
   Loaded after common.js. Boots the app in "alltime_otc" mode.
   FIX (DATA-FLOW-2026-07-22): new 3rd market category. These 6 exotic
   OTC pairs (USDBDT, USDBRL, USDPKR, USDCOP, USDMXN, USDIDR) are always
   available regardless of payout % — used for 24/7 algorithm-change
   monitoring. They use the OTC engine (mean-reversion tuned). */
(function(){
  'use strict';
  function boot(){
    if(typeof window.initApp !== 'function'){
      if(!boot._retries) boot._retries = 0;
      if(++boot._retries > 50){
        console.error('common.js failed to load — initApp not found');
        return;
      }
      return setTimeout(boot, 100);
    }
    window.initApp('alltime_otc');
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
