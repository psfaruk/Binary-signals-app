/* PHASE-1 TRUTH LAYER — pure signal-state mapping (2026-10, rebuild)
 * =================================================================
 * Contract state → UI state.  PURE functions only: no DOM, no network,
 * no clock reads, no lies.  The backend contract
 * (core/signal_contract.py, schema phase1_truth_v1) emits prediction
 * dicts whose `signal` is CALL / PUT / NO_SIGNAL; a NO_SIGNAL carries
 * `reason_code` (13 stable codes), `truth_status`, `research_*`
 * diagnostic fields and `confidence: null` (a NO_SIGNAL never carries a
 * measured-looking confidence — the UI must render "—", never a fake
 * 0/50).
 *
 * This module is the SINGLE source of truth for that mapping and is
 * unit-tested headlessly by scripts/test_phase1_signal_states.cjs
 * (node).  common.js consumes it via window.SignalStates; Node tests
 * consume it via module.exports (UMD-style factory).
 *
 * Governing rule (unchanged from the backend):
 *   NO AUTHORIZED DATA SOURCE + NO VALIDATED MODEL + NO VERIFIED TIMING
 *   = NO ACTIONABLE DIRECTIONAL SIGNAL.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();            // Node (headless tests)
  } else {
    root.SignalStates = factory();         // Browser (common.js)
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var CALL = 'CALL', PUT = 'PUT', NO_SIGNAL = 'NO_SIGNAL';

  /* The 13 stable reason codes — MUST mirror core.signal_contract
   * .ALL_REASON_CODES exactly (tests assert the list, so drift fails
   * loudly instead of silently rendering a wrong label). */
  var ALL_REASON_CODES = [
    'DATA_UNAVAILABLE',
    'DATA_INVALID',
    'DATA_STALE',
    'INSUFFICIENT_HISTORY',
    'FEATURE_ERROR',
    'MODEL_UNAVAILABLE',
    'MODEL_OUTPUT_INVALID',
    'MODEL_NOT_VALIDATED',
    'BELOW_ELIGIBILITY_THRESHOLD',
    'TARGET_CANDLE_INVALID',
    'TIMING_UNVERIFIED',
    'INTERNAL_ERROR',
    'DATA_SOURCE_UNAUTHORIZED'
  ];

  /* Honest labels (Bengali UI language of this app).  Every code gets
   * a human-readable reason — a NO_SIGNAL with no visible reason is a
   * silent lie of omission. */
  var REASON_LABELS = {
    DATA_UNAVAILABLE: 'কোনো প্রেডিকশন/ডেটা নেই',
    DATA_INVALID: 'ক্যান্ডেল ডেটা ভাঙা বা অসম্পূর্ণ',
    DATA_STALE: 'ফিড ডেটা পুরোনো (স্টেল) — তাজা টিক নেই',
    INSUFFICIENT_HISTORY: 'পর্যাপ্ত বন্ধ-ক্যান্ডেল হিস্ট্রি নেই (৬৪+ লাগবে)',
    FEATURE_ERROR: 'ফিচার গণনা ব্যর্থ হয়েছে',
    MODEL_UNAVAILABLE: 'মডেল আর্টিফ্যাক্ট লোড হয়নি',
    MODEL_OUTPUT_INVALID: 'মডেল আউটপুট অবৈধ (অসীম/সীমার বাইরে/স্কিমা মিসম্যাচ)',
    MODEL_NOT_VALIDATED: 'মডেল ভ্যালিডেটেড নয় — রেজিস্ট্রিতে বৈধ OOS প্রমাণ নেই',
    BELOW_ELIGIBILITY_THRESHOLD: 'যোগ্যতা-সীমার নিচে — ইঞ্জিন নীরব/এবস্টেইন',
    TARGET_CANDLE_INVALID: 'টার্গেট ক্যান্ডেলের পরিচয়/সময় অবৈধ',
    TIMING_UNVERIFIED: 'ব্রোকার সার্ভার-টাইম সিঙ্ক যাচাই হয়নি',
    INTERNAL_ERROR: 'অভ্যন্তরীণ ত্রুটি — ফেইল-ক্লোজড (কখনো সিগন্যাল নয়)',
    DATA_SOURCE_UNAUTHORIZED: 'অননুমোদিত ডেটা সোর্স — লাইভ চ্যানেল ব্যবহারের অনুমতি নেই'
  };

  function reasonLabel(code) {
    if (code == null) return '';
    return REASON_LABELS[code] || ('অজানা কারণ (' + String(code) + ')');
  }

  /* Confidence formatting — THE honesty rule: null/undefined means "no
   * measured confidence exists" and MUST render as "—".  Rendering 0
   * (or a fabricated 50) would imply a measurement that never happened
   * (Phase 0 fake-confidence finding). */
  function formatConfidence(v) {
    if (v === null || v === undefined) return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return Math.round(n) + '%';
  }

  /* Confidence bar width — 0 when unmeasured (never a fabricated fill). */
  function confidenceBarWidth(v) {
    if (v === null || v === undefined) return '0%';
    var n = Number(v);
    if (!isFinite(n)) return '0%';
    return Math.max(0, Math.min(100, n)) + '%';
  }

  /* The canonical mapping: one gated prediction dict → UI state.
   * Deterministic and total: ANY input (missing fields, legacy NEUTRAL,
   * garbage) maps to a safe, honest state.  Never throws. */
  function mapSignalState(pred) {
    var p = (pred && typeof pred === 'object') ? pred : {};
    // normalize: missing signal (undefined) and null both mean "no signal
    // state" — they must take the same honest non-directional branch.
    var s = (p.signal === undefined || p.signal === null) ? null : p.signal;
    var isCall = s === CALL, isPut = s === PUT;
    var actionable = (isCall || isPut) && !p.research_only;

    var state = {
      rawSignal: (s === undefined || s === null || s === '') ? null : String(s),
      actionable: actionable,
      researchOnly: !!(p.research_only && (isCall || isPut)),
      label: '',
      cssClass: 'neutral',
      confidenceText: '—',
      confidenceWidth: '0%',
      reasonCode: (p.reason_code === undefined || p.reason_code === null)
        ? null : String(p.reason_code),
      reasonText: reasonLabel(p.reason_code),
      researchDirection: (p.research_direction === undefined
        || p.research_direction === null)
        ? null : String(p.research_direction),
      researchConfidenceText: formatConfidence(p.research_confidence),
      researchStrategy: (p.research_strategy === undefined
        || p.research_strategy === null)
        ? null : String(p.research_strategy),
      truthStatus: (p.truth_status === undefined || p.truth_status === null)
        ? null : String(p.truth_status),
      signalId: (p.signal_id === undefined || p.signal_id === null)
        ? null : String(p.signal_id),
      createdUtc: (p.created_at_utc === undefined || p.created_at_utc === null)
        ? null : String(p.created_at_utc)
    };

    if (isCall || isPut) {
      state.label = isCall ? '🟢 CALL' : '🔴 PUT';
      state.cssClass = isCall ? 'call' : 'put';
      if (actionable) {
        // Eligible prediction: measured confidence is real and may show.
        state.confidenceText = formatConfidence(p.confidence);
        state.confidenceWidth = confidenceBarWidth(p.confidence);
      } else {
        // Rejected engine preference kept as research-only diagnostics:
        // the actionable panel must NOT display it as a tradeable signal.
        state.label = '⛔ NO SIGNAL';
        state.researchOnly = true;
      }
    } else if (s === NO_SIGNAL) {
      // The honest state under current blockers — non-directional, with
      // the machine-readable reason shown to the user.
      state.label = '⛔ NO SIGNAL';
      state.cssClass = 'neutral';
      state.confidenceText = '—';
      state.confidenceWidth = '0%';
    } else {
      // Legacy NEUTRAL / WAIT / anything else — stays non-directional.
      state.label = (s === 'NEUTRAL' || s === null)
        ? (p.target_gate ? '⏸ WAIT' : '➖ NEUTRAL') : String(s);
      state.cssClass = 'neutral';
      state.confidenceText = formatConfidence(p.confidence);
      state.confidenceWidth = confidenceBarWidth(p.confidence);
    }
    return state;
  }

  /* Running-candle live prediction (feed's live_pred payload): engine
   * diagnostic, RESEARCH-ONLY under the truth layer — can never be an
   * actionable CALL/PUT while the blockers stand. */
  function mapLivePred(lp) {
    var p = (lp && typeof lp === 'object') ? lp : {};
    return {
      ready: !!p.ready,
      direction: (p.direction === CALL || p.direction === PUT)
        ? p.direction : null,
      confidenceText: p.ready ? formatConfidence(p.confidence) : '—',
      researchOnly: true,
      actionable: false,
      researchTag: 'RESEARCH · ট্রেডযোগ্য নয়',
      note: 'রানিং-ক্যান্ডেল ইঞ্জিন ডায়াগনস্টিক — ট্রুথ-লেয়ারের অধীনে ' +
            'রিসার্চ-ওনলি (অননুমোদিত সোর্স / ভ্যালিডেশন নেই / টাইম-সিঙ্ক নেই)'
    };
  }

  /* Broker-anchored countdown: seconds until the running candle's
   * expected close, derived from the BROKER-provided candle open time
   * (feed data), not from a bare local minute-boundary guess.  Still an
   * estimate (local↔broker offset unverified until Phase B) — callers
   * must render it with the honest "≈" label below.  Pure + total. */
  function candleSecondsLeft(runningCandleOpenTime, periodSec, nowMs) {
    var open = Number(runningCandleOpenTime);
    var period = Number(periodSec);
    if (!isFinite(open) || open <= 0 || !isFinite(period) || period <= 0) {
      return null;
    }
    var nowSec = Number(nowMs) / 1000;
    if (!isFinite(nowSec)) return null;
    var left = (open + period) - nowSec;
    return left > 0 ? Math.round(left) : 0;
  }

  /* Honest clock/countdown captions (Phase 0 fake-countdown + local-
   * clock-as-Broker-UTC fixes).  Single source so every display tells
   * the same story. */
  var HONEST_LABELS = {
    countdownPrefix: '⏱ ≈',
    countdownSuffix: ' (ব্রোকার-ক্যান্ডেল-টাইম থেকে অনুমান)',
    clockPrefix: '🖥',
    clockSuffix: ' UTC',
    clockTitle: 'ডিভাইসের UTC ঘড়ি — ব্রোকার টাইম-সিঙ্ক যাচাই হয়নি ' +
                '(Phase-1: টাইমিং গেট বন্ধ থাকা পর্যন্ত এটি ব্রোকার সময় দাবি করে না)',
    nextSignalSuffix: ' (আনুমানিক)'
  };

  return {
    CALL: CALL,
    PUT: PUT,
    NO_SIGNAL: NO_SIGNAL,
    ALL_REASON_CODES: ALL_REASON_CODES,
    REASON_LABELS: REASON_LABELS,
    reasonLabel: reasonLabel,
    formatConfidence: formatConfidence,
    confidenceBarWidth: confidenceBarWidth,
    mapSignalState: mapSignalState,
    mapLivePred: mapLivePred,
    candleSecondsLeft: candleSecondsLeft,
    HONEST_LABELS: HONEST_LABELS
  };
});
