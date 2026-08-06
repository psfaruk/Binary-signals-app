/* ═══════════════════════════════════════════════════════════════════════════
   agent-panel.js — Agent tab accordion + AI activity strip on signal card
   Loaded after common.js. Wires up the new collapsible dropdown sections
   and keeps the AI Activity Strip on the signal card in sync with the
   agent's real-time status.

   Functions exposed (all no-op safe if elements missing):
     - initAgentAccordion()           — wire accordion toggle behavior
     - updateAIActivityStrip(state)   — update signal-card AI badge
     - updateAgentHeroMetrics(data)   — update hero card inline metrics
     - updateAgentAccordionMeta(data) — update per-section meta counts
   ═══════════════════════════════════════════════════════════════════════════ */
(function(){
  'use strict';

  // ─── Local helpers ──────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // Track agent state — defaults to "loading"
  let _agentEnabled = null;      // null = unknown, true/false = known
  let _agentLastPWin = null;     // last P(win) seen for current asset
  let _agentLastTickCount = 0;   // last tick count seen
  let _agentLastDecisionCount = 0;
  let _agentModelCount = 0;
  let _agentThoughtCount = 0;

  // ─── 1. ACCORDION TOGGLE ────────────────────────────────────────────
  function initAgentAccordion(){
    const accordion = $('agent-accordion');
    if(!accordion) return;
    if(accordion._wired) return;
    accordion._wired = true;

    $$('.acc-header', accordion).forEach(header => {
      header.addEventListener('click', () => toggleAccordion(header));
      header.addEventListener('keydown', (e) => {
        if(e.key === 'Enter' || e.key === ' '){
          e.preventDefault();
          toggleAccordion(header);
        }
      });
    });
    console.log('[agent-panel] accordion wired —', $$('.acc-header', accordion).length, 'sections');
  }

  function toggleAccordion(header){
    const item = header.closest('.acc-item');
    if(!item) return;
    const bodyId = header.getAttribute('aria-controls');
    const body = bodyId ? $(bodyId) : null;
    const isOpen = item.classList.contains('acc-item--open');

    if(isOpen){
      // Close
      item.classList.remove('acc-item--open');
      header.setAttribute('aria-expanded', 'false');
      if(body) body.hidden = true;
    } else {
      // Open
      item.classList.add('acc-item--open');
      header.setAttribute('aria-expanded', 'true');
      if(body) body.hidden = false;
      // Trigger lazy-load callback if defined
      const accName = item.dataset.acc;
      if(accName && typeof window._onAgentAccordionOpen === 'function'){
        try{ window._onAgentAccordionOpen(accName); }catch(_){}
      }
    }
  }

  // Expose for manual triggering if needed
  window.toggleAgentAccordion = function(name, open){
    const item = document.querySelector('.acc-item[data-acc="' + name + '"]');
    if(!item) return;
    const header = item.querySelector('.acc-header');
    if(!header) return;
    const isOpen = item.classList.contains('acc-item--open');
    if(open === true && !isOpen) toggleAccordion(header);
    else if(open === false && isOpen) toggleAccordion(header);
    else if(open === undefined) toggleAccordion(header);
  };

  // ─── 2. AI ACTIVITY STRIP (on signal card) ──────────────────────────
  // state: 'loading' | 'active' | 'learning' | 'disabled'
  function updateAIActivityStrip(state, opts){
    opts = opts || {};
    const strip = $('ai-activity-strip');
    if(!strip) return;
    strip.setAttribute('data-state', state);

    const statusEl = $('ai-activity-status');
    const labelEl = $('ai-activity-label');
    const pairEl = $('ai-activity-pair');
    const pwinWrap = $('ai-activity-pwin');
    const pwinVal = $('ai-activity-pwin-val');

    // Update pair name
    if(pairEl){
      const pairName = opts.pair || (typeof window.currentAsset === 'string' ? window.currentAsset : '');
      pairEl.textContent = pairName ? formatPair(pairName) : '—';
    }

    // Update status text + label
    if(statusEl){
      switch(state){
        case 'active':
          statusEl.textContent = opts.status || 'Actively evaluating ticks…';
          break;
        case 'learning':
          statusEl.textContent = opts.status || 'Learning from outcome…';
          break;
        case 'disabled':
          statusEl.textContent = 'Agent disabled (QX_AGENT_BRAIN=0)';
          break;
        case 'loading':
        default:
          statusEl.textContent = opts.status || 'Initializing…';
          break;
      }
    }
    if(labelEl){
      labelEl.textContent = state === 'disabled' ? 'AI Agent (off)' : 'AI Agent';
    }

    // Update P(win) badge
    if(pwinWrap && pwinVal){
      if(opts.pwin != null && state !== 'disabled'){
        pwinWrap.hidden = false;
        const pct = (opts.pwin * 100).toFixed(1);
        pwinVal.textContent = pct + '%';
        // Color: green if >0.55, yellow if 0.45-0.55, red if <0.45
        if(opts.pwin >= 0.55){
          pwinVal.style.color = 'var(--green)';
        } else if(opts.pwin >= 0.45){
          pwinVal.style.color = 'var(--yellow)';
        } else {
          pwinVal.style.color = 'var(--red)';
        }
      } else {
        pwinWrap.hidden = true;
      }
    }
  }

  function formatPair(asset){
    if(!asset) return '—';
    // EURUSD_otc -> EUR/USD OTC
    let s = asset.replace(/_otc$/i, '');
    if(s.length === 6 && /^[A-Z]{6}$/.test(s)){
      s = s.slice(0,3) + '/' + s.slice(3);
    }
    if(/_otc$/i.test(asset)) s += ' OTC';
    return s;
  }

  // ─── 3. HERO METRICS ────────────────────────────────────────────────
  function updateAgentHeroMetrics(data){
    if(!data) return;
    const ticksEl = $('ahm-ticks');
    const pwinEl = $('ahm-pwin');
    const uptimeEl = $('ahm-uptime');
    const pairEl = $('ahm-pair');

    if(ticksEl && typeof data.total_ticks_processed === 'number'){
      ticksEl.textContent = formatNumber(data.total_ticks_processed);
    }
    if(uptimeEl && data.uptime_human){
      uptimeEl.textContent = data.uptime_human;
    }
    if(pairEl){
      const asset = (typeof window.currentAsset === 'string' ? window.currentAsset : '');
      pairEl.textContent = asset ? formatPair(asset) : '—';
    }
    // P(win) comes from features endpoint, not status — handled separately
  }

  function formatNumber(n){
    if(n < 1000) return String(n);
    if(n < 1_000_000) return (n / 1000).toFixed(1) + 'K';
    return (n / 1_000_000).toFixed(2) + 'M';
  }

  // ─── 4. ACCORDION META COUNTS ──────────────────────────────────────
  function updateAgentAccordionMeta(data){
    if(!data) return;

    // Live stream meta
    const liveMeta = $('acc-live-meta');
    if(liveMeta && typeof data.total_ticks_processed === 'number'){
      if(data.total_ticks_processed > 0){
        liveMeta.textContent = formatNumber(data.total_ticks_processed) + ' ticks';
      } else {
        liveMeta.textContent = 'waiting…';
      }
    }

    // Models meta
    const modelsMeta = $('acc-models-meta');
    if(modelsMeta && typeof data.models_count === 'number'){
      modelsMeta.textContent = data.models_count + (data.models_count === 1 ? ' model' : ' models');
    }

    // Recent decisions meta
    const recentMeta = $('acc-recent-meta');
    if(recentMeta && typeof data.decisions_count === 'number'){
      recentMeta.textContent = data.decisions_count + (data.decisions_count === 1 ? ' decision' : ' decisions');
    }
  }

  // ─── 5. AUTO-WIRE ON DOM READY ─────────────────────────────────────
  function autoWire(){
    initAgentAccordion();

    // Initialize the AI Activity Strip with a loading state
    updateAIActivityStrip('loading');

    // Initialize hero metrics placeholders
    const pairEl = $('ahm-pair');
    if(pairEl && typeof window.currentAsset === 'string'){
      pairEl.textContent = formatPair(window.currentAsset);
    }

    // Listen for currentAsset changes (poll every 1s — cheap)
    let _lastAsset = window.currentAsset || null;
    setInterval(() => {
      const cur = window.currentAsset || null;
      if(cur !== _lastAsset){
        _lastAsset = cur;
        // Refresh pair display in both places
        const pairEl1 = $('ai-activity-pair');
        const pairEl2 = $('ahm-pair');
        const pairTxt = cur ? formatPair(cur) : '—';
        if(pairEl1) pairEl1.textContent = pairTxt;
        if(pairEl2) pairEl2.textContent = pairTxt;
        // Update features accordion meta
        const featMeta = $('acc-features-meta');
        if(featMeta) featMeta.textContent = cur ? formatPair(cur) : 'no pair selected';
      }
    }, 1000);
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', autoWire);
  } else {
    // Defer to next tick so common.js can initialize first
    setTimeout(autoWire, 50);
  }

  // ─── 6. HOOK INTO EXISTING AGENT POLLING ────────────────────────────
  // We wrap the existing updateAgentStatus + renderAgentFeatures functions
  // (defined in common.js) so we can mirror data into the new UI elements.
  function hookAgentStatus(){
    if(typeof window.updateAgentStatus !== 'function') return false;
    if(window._agentStatusHooked) return true;
    const original = window.updateAgentStatus;
    window.updateAgentStatus = function(data){
      try{ original(data); }catch(_){}
      try{
        // Mirror to hero metrics
        updateAgentHeroMetrics(data);
        // Update accordion meta counts (live stream, models, decisions)
        updateAgentAccordionMeta({
          total_ticks_processed: data.total_ticks_processed,
          models_count: data.models_count,
          decisions_count: data.total_decisions,
        });
        // Update AI activity strip state
        const enabled = data.enabled === true;
        _agentEnabled = enabled;
        if(enabled){
          if(data.total_ticks_processed > 0){
            // Check if recently learned (decision_count increased)
            updateAIActivityStrip('active', {
              pair: window.currentAsset,
              status: data.total_ticks_processed + ' ticks processed · agent running',
            });
          } else {
            updateAIActivityStrip('active', {
              pair: window.currentAsset,
              status: 'Agent active · waiting for first tick…',
            });
          }
        } else {
          updateAIActivityStrip('disabled');
        }
      }catch(_){}
    };
    window._agentStatusHooked = true;
    console.log('[agent-panel] hooked updateAgentStatus');
    return true;
  }

  function hookAgentFeatures(){
    if(typeof window.renderAgentFeatures !== 'function') return false;
    if(window._agentFeaturesHooked) return true;
    const original = window.renderAgentFeatures;
    window.renderAgentFeatures = function(data){
      try{ original(data); }catch(_){}
      try{
        // Update P(win) on AI Activity Strip + hero metrics
        if(data && data.last_p_win != null){
          updateAIActivityStrip(_agentEnabled === false ? 'disabled' : 'active', {
            pair: data.asset || window.currentAsset,
            pwin: data.last_p_win,
            status: 'P(win) = ' + (data.last_p_win * 100).toFixed(1) + '% · ' + (data.samples || 0) + ' samples',
          });
          // Also update hero P(win)
          const pwinEl = $('ahm-pwin');
          if(pwinEl){
            pwinEl.textContent = (data.last_p_win * 100).toFixed(1) + '%';
            pwinEl.style.color = data.last_p_win >= 0.55 ? 'var(--green)'
                               : data.last_p_win >= 0.45 ? 'var(--yellow)'
                               : 'var(--red)';
          }
        }
      }catch(_){}
    };
    window._agentFeaturesHooked = true;
    console.log('[agent-panel] hooked renderAgentFeatures');
    return true;
  }

  function hookAgentLive(){
    if(typeof window.renderAgentLive !== 'function') return false;
    if(window._agentLiveHooked) return true;
    const original = window.renderAgentLive;
    window.renderAgentLive = function(data){
      try{ original(data); }catch(_){}
      try{
        if(data && data.thoughts){
          const count = data.thoughts.length;
          const liveMeta = $('acc-live-meta');
          if(liveMeta){
            liveMeta.textContent = count + (count === 1 ? ' event' : ' events');
          }
        }
      }catch(_){}
    };
    window._agentLiveHooked = true;
    console.log('[agent-panel] hooked renderAgentLive');
    return true;
  }

  function hookAgentModels(){
    if(typeof window.renderAgentModels !== 'function') return false;
    if(window._agentModelsHooked) return true;
    const original = window.renderAgentModels;
    window.renderAgentModels = function(data){
      try{ original(data); }catch(_){}
      try{
        if(data && data.models){
          const count = data.models.length;
          const modelsMeta = $('acc-models-meta');
          if(modelsMeta){
            modelsMeta.textContent = count + (count === 1 ? ' model' : ' models');
          }
        }
      }catch(_){}
    };
    window._agentModelsHooked = true;
    console.log('[agent-panel] hooked renderAgentModels');
    return true;
  }

  // Try to hook on a timer (common.js may not be loaded yet)
  let _hookRetries = 0;
  function tryHook(){
    _hookRetries++;
    if(_hookRetries > 60) return; // give up after ~6s
    let hooked = true;
    hooked = hookAgentStatus() && hooked;
    hooked = hookAgentFeatures() && hooked;
    hooked = hookAgentLive() && hooked;
    hooked = hookAgentModels() && hooked;
    if(!hooked) setTimeout(tryHook, 100);
  }
  setTimeout(tryHook, 200);

  // Expose for debugging
  window._agentPanel = {
    updateAIActivityStrip,
    updateAgentHeroMetrics,
    updateAgentAccordionMeta,
    initAgentAccordion,
    toggleAgentAccordion: (name) => window.toggleAgentAccordion(name),
  };

  console.log('[agent-panel] loaded');
})();
