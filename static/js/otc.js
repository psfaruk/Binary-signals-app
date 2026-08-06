/* otc.js — OTC Market page bootstrap. */
(function(){'use strict';
function boot(){
  if(typeof window.initApp!=='function'){
    if(!boot._retries)boot._retries=0;
    if(++boot._retries>50){console.error('[bootstrap:otc] common.js failed to load — initApp not found');return;}
    return setTimeout(boot,100);
  }
  window.initApp('otc');
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);
else boot();
})();
