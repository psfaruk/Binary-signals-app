/* real.js — Real Market page bootstrap.
   Loaded after common.js. Boots the app in "real" mode. */
(function(){
  'use strict';
  function boot(){
    if(typeof window.initApp !== 'function'){
      // common.js not loaded yet — retry up to 50×100ms.
      if(!boot._retries) boot._retries = 0;
      if(++boot._retries > 50){
        console.error('common.js failed to load — initApp not found');
        return;
      }
      return setTimeout(boot, 100);
    }
    window.initApp('real');
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
