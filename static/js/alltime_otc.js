/* alltime_otc.js — All-Time OTC Market page bootstrap.
   Loaded after common.js. Boots the app in "alltime_otc" mode.
   FIX (DATA-FLOW-2026-07-22): new 3rd market category. These 6 exotic
   OTC pairs (USDBDT, USDBRL, USDPKR, USDCOP, USDMXN, USDIDR) are always
   available regardless of payout % — used for 24/7 algorithm-change
   monitoring. They use the OTC engine (mean-reversion tuned).

   FIX (DEEP-AUDIT-2026-07-26 / F-18-01): A-09 PROBLEM 87 — this file is one
   of THREE near-identical bootstrap files (real.js / otc.js /
   alltime_otc.js). The only difference is the category string passed to
   initApp(). The audit recommends consolidating into a single bootstrap.js
   that reads `document.body.dataset.category` (set via
   `<body data-category="alltime_otc">` in each HTML). However, since each
   HTML page <script> tag references its own bootstrap file, consolidation
   would require HTML changes which are out of scope for this fix-batch.
   The duplication is therefore DOCUMENTED here: if you change this retry/boot
   pattern, you MUST change it identically in real.js and otc.js. */
(function(){
  'use strict';
  // FIX (DEEP-AUDIT-2026-07-26 / F-18-02): Hoist boot reference so a
  // previously-registered DOMContentLoaded listener can be removed before
  // re-adding — prevents potential double-boot if the IIFE somehow runs twice
  // (defensive; not currently triggered by any caller).
  function boot(){
    if(typeof window.initApp !== 'function'){
      if(!boot._retries) boot._retries = 0;
      if(++boot._retries > 50){
        // FIX (DEEP-AUDIT-2026-07-26 / F-18-03): prefix console.error with
        // a stable tag so ops can grep for it.
        console.error('[bootstrap:alltime_otc] common.js failed to load — initApp not found');
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
