#!/usr/bin/env node
/* PHASE-1 TRUTH LAYER — pure signal-state mapping tests (2026-10, rebuild).
 *
 * Run:  node scripts/test_phase1_signal_states.cjs
 *
 * Tests static/js/signal_states.js — the single source of truth for
 * contract state → UI state:
 *   - the 13 reason codes (cross-checked against the Python contract
 *     source so JS/Python can never drift silently),
 *   - NO_SIGNAL rendering (label + reason, never a fake confidence),
 *   - honest confidence formatting (null → "—", never 0/50),
 *   - research-only handling for rejected engine preferences,
 *   - live-pred research labeling,
 *   - broker-anchored countdown math (edge cases).
 *
 * Pure functions only: no DOM, no network, no clock reads inside the
 * module under test.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const REPO = path.dirname(path.dirname(__filename));
const SS = require(path.join(REPO, 'static', 'js', 'signal_states.js'));

let pass = 0, fail = 0;
const failures = [];

function check(cond, label, detail) {
  if (cond) { pass++; console.log('  ok   ' + label); }
  else {
    fail++; failures.push(label + (detail ? ' ' + detail : ''));
    console.log('  FAIL ' + label + (detail ? ' ' + detail : ''));
  }
}

console.log('='.repeat(72));
console.log('PHASE-1 SIGNAL-STATE MAPPING TESTS');
console.log('='.repeat(72));

/* ── A. reason-code parity with the Python contract ─────────────────── */
console.log('\n[A] Reason codes (JS ↔ Python parity)');

check(Array.isArray(SS.ALL_REASON_CODES) && SS.ALL_REASON_CODES.length === 13,
  'A0 exactly 13 reason codes in JS', String(SS.ALL_REASON_CODES.length));

const pySrc = fs.readFileSync(
  path.join(REPO, 'core', 'signal_contract.py'), 'utf8');
const pyBlock = pySrc.match(/ALL_REASON_CODES = \(([\s\S]*?)\)/);
const pyCodes = pyBlock
  ? pyBlock[1].split(',').map(s => s.trim()).filter(Boolean)
  : [];
check(pyCodes.length === 13
  && JSON.stringify(pyCodes) === JSON.stringify(SS.ALL_REASON_CODES),
  'A1 JS reason codes match core/signal_contract.py exactly',
  `py=${pyCodes.length} js=${SS.ALL_REASON_CODES.length}`);

for (const code of SS.ALL_REASON_CODES) {
  const label = SS.reasonLabel(code);
  check(typeof label === 'string' && label.length > 0
    && label.indexOf('অজানা') === -1,
    `A2 label exists for ${code}`);
}

check(SS.reasonLabel('NOT_A_REAL_CODE').indexOf('অজানা') === 0,
  'A3 unknown code gets an explicit unknown-label (no silent blank)');
check(SS.reasonLabel(null) === '' && SS.reasonLabel(undefined) === '',
  'A4 null/undefined code → empty label');

/* ── B. honest confidence formatting ─────────────────────────────────── */
console.log('\n[B] Honest confidence formatting');

check(SS.formatConfidence(null) === '—', 'B1 null → "—"');
check(SS.formatConfidence(undefined) === '—', 'B2 undefined → "—"');
check(SS.formatConfidence(NaN) === '—', 'B3 NaN → "—"');
check(SS.formatConfidence('abc') === '—', 'B4 garbage → "—"');
check(SS.formatConfidence(62.4) === '62%', 'B5 62.4 → "62%"');
check(SS.formatConfidence(0) === '0%', 'B6 genuine 0 → "0%"');
check(SS.confidenceBarWidth(null) === '0%', 'B7 null bar width 0%');
check(SS.confidenceBarWidth(150) === '100%', 'B8 >100 clamps to 100%');
check(SS.confidenceBarWidth(-5) === '0%', 'B9 negative clamps to 0%');

/* ── C. mapSignalState ───────────────────────────────────────────────── */
console.log('\n[C] mapSignalState (contract → UI)');

const gated = {
  signal: 'NO_SIGNAL', confidence: null, strength: 'NO_SIGNAL',
  reason_code: 'DATA_SOURCE_UNAUTHORIZED',
  truth_status: 'rejected', research_only: true,
  research_direction: 'CALL', research_confidence: 71,
  research_strategy: 'next_candle_v1',
  eligibility_policy_version: '1.0.0',
  contract_schema_version: 'phase1_truth_v1',
  created_at_utc: '2026-10-18T10:00:00+00:00',
  signal_id: 'abc123'
};
let s = SS.mapSignalState(gated);
check(s.label === '⛔ NO SIGNAL', 'C1 NO_SIGNAL label', s.label);
check(s.cssClass === 'neutral', 'C2 NO_SIGNAL neutral class');
check(s.actionable === false, 'C3 NO_SIGNAL not actionable');
check(s.confidenceText === '—', 'C4 NO_SIGNAL confidence "—"');
check(s.confidenceWidth === '0%', 'C5 NO_SIGNAL bar width 0%');
check(s.reasonCode === 'DATA_SOURCE_UNAUTHORIZED'
  && s.reasonText.length > 0, 'C6 reason code + visible reason text');
check(s.researchDirection === 'CALL'
  && s.researchConfidenceText === '71%'
  && s.researchStrategy === 'next_candle_v1',
  'C7 research diagnostics preserved');
check(s.signalId === 'abc123' && s.truthStatus === 'rejected',
  'C8 identity + truth status preserved');

// The TODAY-impossible case: an eligible CALL (only reachable with an
// authorized source + validated model — none exist today).
const eligible = {
  signal: 'CALL', confidence: 68, research_only: false,
  reason_code: null, truth_status: 'eligible'
};
s = SS.mapSignalState(eligible);
check(s.label === '🟢 CALL' && s.cssClass === 'call' && s.actionable === true,
  'C9 eligible CALL renders as CALL');
check(s.confidenceText === '68%', 'C10 eligible measured confidence shows');

// Rejected engine preference (research_only CALL) — must NOT display as
// a tradeable CALL.
const rejected = { signal: 'CALL', confidence: 50, research_only: true,
                   reason_code: 'TIMING_UNVERIFIED' };
s = SS.mapSignalState(rejected);
check(s.label === '⛔ NO SIGNAL' && s.actionable === false
  && s.researchOnly === true,
  'C11 research-only CALL/PUT renders NO SIGNAL (never tradeable)');

// Legacy NEUTRAL + WAIT
s = SS.mapSignalState({ signal: 'NEUTRAL' });
check(s.label === '➖ NEUTRAL' && s.actionable === false,
  'C12 legacy NEUTRAL stays non-directional');
s = SS.mapSignalState({ signal: 'NEUTRAL', target_gate: true });
check(s.label === '⏸ WAIT', 'C13 legacy WAIT preserved');

// PENDING (REST rows before any prediction)
s = SS.mapSignalState({ signal: 'PENDING' });
check(s.label === 'PENDING' && s.actionable === false,
  'C14 PENDING passthrough (non-directional)');

// Robustness: garbage inputs must map safely, never throw
let threw = false;
try {
  SS.mapSignalState(null);
  SS.mapSignalState(undefined);
  SS.mapSignalState('string');
  SS.mapSignalState(42);
  SS.mapSignalState({});
  SS.mapSignalState({ signal: null, confidence: undefined });
} catch (e) { threw = true; }
check(!threw, 'C15 garbage inputs never throw');

s = SS.mapSignalState(null);
check(s.label === '➖ NEUTRAL' && s.actionable === false,
  'C16 null pred → safe NEUTRAL (no fabrication)');

/* ── D. live-pred research labeling ─────────────────────────────────── */
console.log('\n[D] mapLivePred (research-only diagnostic)');

let lp = SS.mapLivePred({ ready: true, direction: 'CALL', confidence: 72 });
check(lp.researchOnly === true && lp.actionable === false,
  'D1 live pred is research-only, never actionable');
check(typeof lp.researchTag === 'string' && lp.researchTag.length > 0,
  'D2 research tag text present');
check(lp.confidenceText === '72%', 'D3 live confidence shows (diagnostic)');

lp = SS.mapLivePred({ ready: false });
check(lp.confidenceText === '—' && lp.direction === null,
  'D4 not-ready live pred → honest "—"');

lp = SS.mapLivePred(null);
check(lp.researchOnly === true && !lp.ready, 'D5 null live pred safe');

/* ── E. broker-anchored countdown math ──────────────────────────────── */
console.log('\n[E] candleSecondsLeft (broker-anchored)');

const open = 1700000640;      // a candle open (broker epoch, 60s grid)
check(SS.candleSecondsLeft(open, 60, (open + 30) * 1000) === 30,
  'E1 30s into the candle → 30s left');
check(SS.candleSecondsLeft(open, 60, (open + 59.6) * 1000) === 0,
  'E2 59.6s → rounds to 0 (boundary honest)');
check(SS.candleSecondsLeft(open, 60, (open + 61) * 1000) === 0,
  'E3 past close → 0 (never negative)');
check(SS.candleSecondsLeft(open, 60, open * 1000) === 60,
  'E4 at open → full period');
check(SS.candleSecondsLeft(0, 60, Date.now()) === null,
  'E5 no candle yet → null (UI falls back honestly)');
check(SS.candleSecondsLeft(null, 60, Date.now()) === null,
  'E6 null open → null');
check(SS.candleSecondsLeft(open, 0, open * 1000) === null,
  'E7 invalid period → null');
check(SS.candleSecondsLeft(open, 60, NaN) === null,
  'E8 invalid now → null');
// Exact minute edge: a tick at exactly the boundary belongs to the NEW candle
check(SS.candleSecondsLeft(open, 60, (open + 60) * 1000) === 0,
  'E9 boundary timestamp → 0 left of the OLD candle');

/* ── F. honest clock/countdown labels exist ─────────────────────────── */
console.log('\n[F] Honest label constants');

check(typeof SS.HONEST_LABELS.countdownPrefix === 'string'
  && SS.HONEST_LABELS.countdownPrefix.indexOf('≈') !== -1,
  'F1 countdown labeled as estimate (≈)');
check(typeof SS.HONEST_LABELS.clockTitle === 'string'
  && SS.HONEST_LABELS.clockTitle.indexOf('ব্রোকার') !== -1,
  'F2 clock title states the broker-sync caveat');
check(SS.CALL === 'CALL' && SS.PUT === 'PUT'
  && SS.NO_SIGNAL === 'NO_SIGNAL', 'F3 label constants exported');

/* ── summary ─────────────────────────────────────────────────────────── */
console.log('\n' + '='.repeat(72));
console.log(`PHASE-1 SIGNAL STATES: ${pass} passed, ${fail} failed, ` +
            `${pass + fail} total`);
if (failures.length) {
  console.log('FAILURES:');
  for (const f of failures) console.log('  - ' + f);
}
console.log('='.repeat(72));
process.exit(fail ? 1 : 0);
