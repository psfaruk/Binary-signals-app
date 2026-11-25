"""
core/ensemble.py — ALL-THEORY ENSEMBLE ENGINE (ENSEMBLE-2026-11-26).

USER REQUIREMENT (verbatim):
  "Every ক্যান্ডেল এ সিগন্যাল লাগবে, 90% একরেসি সহ তুমি যাচাই করো কোড ও
  ট্রেডিং কৌশল আরও উন্নত করো, অনেক theory আছে সব গুলো সঠিক ভাবে
   ইউজ করো।"

WHAT THIS MODULE IS — the ONE honest voice that combines EVERY theory the
repository already measures, each weighted by its OWN rolling, self-graded,
real-feed accuracy:

  Voice          Source theory                                  Prior
  ────────────────────────────────────────────────────────────────────
  sigma_model    core/live_candle.py σ-model P(close>open)       1.00
  pressure       unified buyer/seller flow (audit C4 fix)        0.35
  pattern        closed-candle color-continuation statistics     0.35
  eye_factors    tick_eye anatomy factor votes (velocity,        0.30
                 close_position, wick rejection, late flip…)
  strategy       the published EOC signal (confluence/any-       0.45
                 theory/ML/fade chain) for THIS running candle
  ml_t1          the frozen ML T+1 prediction for THIS candle    0.55
                 (core/otc_predict tracker, emit-gated)

SELF-LEARNING WEIGHTS (the "সবগুলো সঠিকভাবে ইউজ" part):
  At every candle close each voice's direction is graded against the real
  close. Rolling accuracy is shrunk toward 0.5 (Jeffreys prior, +20 pseudo
  samples) so a voice must EARN weight with evidence:

      acc_hat = (correct + 10) / (n + 20)
      weight  = prior_weight * max(0, (acc_hat - 0.5) * 2)

  A voice at ≤50% rolling accuracy gets ZERO weight — it can never drag
  the ensemble below its honest voices. No hand-tuned "proven 63.5%"
  tables (audit C2): the weights ARE the measured history.

BLENDING: logit-space weighted average (robust to extreme probabilities):

      z_ens = Σ wᵢ·logit(pᵢ) / Σ wᵢ ;  p_ens = σ(z_ens)

THE 90% QUESTION — answered honestly (the repo never fabricates an edge):
  * "90% accuracy" CAN hold for the DIRECTION CALL of the running candle
    when the ensemble is confident (tier LOCKED: p≥0.90 or lock≥0.85) —
    the calibration table of the σ-model already realized 97.9% in the
    0.9-1.0 bucket on a fair random walk (scripts/backtest_live_candle_
    report.json). The per-tier accuracy is MEASURED live per pair and
    surfaced via /api/live-prediction — never promised, only shown.
  * It can NOT hold for ENTRY win-rate on a zero-edge feed (break-even
    54.05% at 85% payout; fair-walk entry WR ≈ 49-50%). The engine
    therefore separates the two numbers explicitly: p_ensemble (direction
    call) vs trade_hint (entry-quality moment, requires ensemble + σ-model
    agreement). The UI/API show both, labeled.

DESIGN CONSTRAINTS (repo lessons):
  * PURE — no I/O, no clock reads, no DB: safe on every tick (~50µs).
  * NO look-ahead — only the running candle's ticks (via lp) and already
    CLOSED candles.
  * Never throws — returns None on insufficient input; the feed wraps the
    call in try/except anyway (never break the pipeline).
  * Honest abstention — below MIN closed-candle history the pattern voice
    stays silent instead of guessing.

Public API:
  get_state(stream)                       — per-stream state (creates)
  ensemble_running_candle(lp, closed_candles, eoc_signal=None,
                          ml_t1=None, state=None) -> dict | None
  grade_ensemble(state, ens, open_px, close_px) — pure grading update
  grade_closed(stream, open_px, close_px)        — stream wrapper
  public_stats(stream)                           — API-facing stats
"""

from __future__ import annotations

import math

__all__ = [
    "get_state",
    "ensemble_running_candle",
    "grade_ensemble",
    "grade_closed",
    "public_stats",
    "VOICE_PRIORS",
]

# ── Tunables (env-free — battery-checked constants, repo convention) ─────────
SHRINK_PSEUDO_N = 20        # Jeffreys shrinkage: acc_hat=(c+10)/(n+20)
P_FLOOR, P_CEIL = 0.02, 0.98
# Prior weights — the maximum a voice can weigh before it has evidence.
VOICE_PRIORS = {
    "sigma_model": 1.00,    # the strongest measured engine in the repo
    "ml_t1":       0.55,    # frozen model, emit-gated, honestly calibrated
    "strategy":    0.45,    # the published EOC chain (50.5% measured WR)
    "pressure":    0.35,
    "pattern":     0.35,
    "eye_factors": 0.30,
}
# Pattern voice needs this many closed candles before it speaks.
PATTERN_MIN_HISTORY = 8
PATTERN_LOOKBACK = 30
PATTERN_DAMP = 0.60         # measured continuation edge × damp → probability
# Tier bands (percent) — the honest "90%" lives in the LOCKED tier.
TIER_LOCKED_P, TIER_STRONG_P, TIER_MEDIUM_P = 0.90, 0.75, 0.62
TIER_LOCKED_LOCKFRAC = 0.85

_TIER_BN = {
    "LOCKED": "লকড — দিক প্রায় নিশ্চিত",
    "STRONG": "শক্তিশালী",
    "MEDIUM": "মাঝারি",
    "WEAK":   "দুর্বল",
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _logit(p: float) -> float:
    p = _clamp(p, 0.02, 0.98)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


# ── Per-stream state ─────────────────────────────────────────────────────────

def get_state(stream) -> dict:
    """The stream's ensemble state (created on first use).

    Structure:
      voice_stats : {voice: {"n": int, "correct": int}}
      n / correct : the ENSEMBLE's own direction-call accuracy
      tiers       : {tier: {"n": int, "correct": int}}
    """
    st = getattr(stream, "_ensemble_state", None)
    if not isinstance(st, dict):
        st = {"voice_stats": {}, "n": 0, "correct": 0, "tiers": {}}
        try:
            stream._ensemble_state = st
        except Exception:
            pass
    return st


def _voice_weight(name: str, state: dict | None) -> float:
    """Evidence-earned weight for a voice (self-learning, honest floor 0)."""
    prior = VOICE_PRIORS.get(name, 0.30)
    if not state:
        return prior * 0.20          # no evidence → small neutral-say weight
    vs = (state.get("voice_stats") or {}).get(name) or {}
    n, c = int(vs.get("n", 0)), int(vs.get("correct", 0))
    acc_hat = (c + 0.5 * SHRINK_PSEUDO_N) / (n + SHRINK_PSEUDO_N)
    return prior * max(0.0, (acc_hat - 0.5) * 2.0)


# ── Individual voices (each ≤ O(40), pure) ───────────────────────────────────

def _voice_sigma(lp: dict) -> float | None:
    p = lp.get("p_close_green")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def _voice_pressure(lp: dict) -> float | None:
    b = lp.get("buyer_pct")
    if b is None:
        return None
    try:
        b = float(b)
    except (TypeError, ValueError):
        return None
    # ±20 pressure points → ±0.10 probability edge (damped, honest smallness)
    return _clamp(0.5 + (b - 50.0) / 100.0 * 0.5, 0.30, 0.70)


def _voice_pattern(closed_candles) -> float | None:
    """Color-continuation statistics of recent CLOSED candles.

    Measures the empirical P(next candle same color as previous) over the
    lookback window and bets (damped) with the measured tendency. When the
    measured continuation is < 0.5 the market is reverting — the voice
    bets AGAINST the last color. No hand-written pair tables (audit C2).
    """
    if not closed_candles:
        return None
    cols = []
    for c in closed_candles[-(PATTERN_LOOKBACK + 1):]:
        try:
            o, cl = float(c.get("open", 0.0)), float(c.get("close", 0.0))
        except (TypeError, ValueError):
            continue
        if cl > o:
            cols.append(1)
        elif cl < o:
            cols.append(-1)
        else:
            cols.append(0)
    pairs = [(cols[i], cols[i + 1])
             for i in range(len(cols) - 1) if cols[i] != 0]
    if len(pairs) < PATTERN_MIN_HISTORY or cols[-1] == 0:
        return None
    cont = sum(1 for a, b in pairs if a == b) / float(len(pairs))
    edge = (cont - 0.5) * PATTERN_DAMP          # signed: <0 ⇒ reversion
    last = cols[-1]
    return _clamp(0.5 + edge * last, 0.35, 0.65)


def _voice_eye_factors(lp: dict) -> float | None:
    """Majority vote of the tick-eye anatomy factors already in the lp."""
    factors = lp.get("factors") or []
    up = dn = 0
    for f in factors:
        d = (f or {}).get("dir")
        if d == "CALL":
            up += 1
        elif d == "PUT":
            dn += 1
    if up + dn < 2 or up == dn:
        return None
    return _clamp(0.5 + 0.04 * (up - dn), 0.30, 0.70)


def _voice_strategy(eoc_signal) -> float | None:
    if eoc_signal not in ("CALL", "PUT"):
        return None
    # The EOC chain's honest measured edge is tiny (50.5% WR, audit A1);
    # probability reflects that until rolling grading says otherwise.
    return 0.505 if eoc_signal == "CALL" else 0.495


def _voice_ml(ml_t1) -> float | None:
    """ml_t1: the frozen ML T+1 probability of UP for THIS candle."""
    if ml_t1 is None:
        return None
    try:
        p = float(ml_t1)
    except (TypeError, ValueError):
        return None
    return _clamp(p, 0.05, 0.95)


# ── The ensemble ─────────────────────────────────────────────────────────────

def ensemble_running_candle(lp: dict | None, closed_candles=None,
                            eoc_signal=None, ml_t1=None,
                            state: dict | None = None) -> dict | None:
    """Combine every theory voice into ONE calibrated probability.

    Parameters
      lp             — the live_candle result dict for the running candle
                       (must be ready; supplies σ-model, pressure, factors)
      closed_candles — recent CLOSED candle dicts (pattern voice)
      eoc_signal     — the published EOC CALL/PUT for this running candle
      ml_t1          — frozen ML T+1 P(up) for this candle (optional)
      state          — the per-stream ensemble state (weights + grading)

    Returns a dict (or None when the σ-model is not ready — the ensemble
    refuses to speak without its strongest voice):
      p_ensemble, direction, tier, tier_bn, agreement_pct,
      trade_hint, voices[], note_bn
    """
    if not isinstance(lp, dict) or not lp.get("ready"):
        return None
    p_sigma = _voice_sigma(lp)
    if p_sigma is None:
        return None

    candidates = [
        ("sigma_model", p_sigma),
        ("pressure", _voice_pressure(lp)),
        ("pattern", _voice_pattern(closed_candles)),
        ("eye_factors", _voice_eye_factors(lp)),
        ("strategy", _voice_strategy(eoc_signal)),
        ("ml_t1", _voice_ml(ml_t1)),
    ]

    voices = []
    z_sum = 0.0
    w_sum = 0.0
    for name, p in candidates:
        if p is None:
            continue
        w = _voice_weight(name, state)
        # CONFIDENCE-SCALED WEIGHT: a voice that is unsure (p≈0.5) speaks
        # softly; a voice that is sure (p→0/1) speaks loudly. Multiplier
        # 0.5..1.0 — prevents a chorus of weakly-opinionated voices from
        # diluting one strongly-evidenced call (battery check 3).
        w_eff = w * (0.5 + abs(p - 0.5))
        if w_eff <= 0.0:
            # Voice measured at/below coin-flip — it may still be LISTED
            # (transparency) but contributes zero weight.
            voices.append({"name": name, "p": round(p, 4),
                           "weight": 0.0, "note": "measured ≤50% — muted"})
            continue
        z_sum += w_eff * _logit(p)
        w_sum += w_eff
        voices.append({"name": name, "p": round(p, 4),
                       "weight": round(w_eff, 4)})

    if w_sum <= 0.0:
        # Every speaking voice is muted (all measured ≤50%): fall back to
        # the σ-model alone, honestly labeled.
        z_sum, w_sum = _logit(p_sigma), 1.0
        voices.append({"name": "fallback_sigma",
                       "p": round(p_sigma, 4), "weight": 1.0,
                       "note": "সব voice নীরব — σ-মডেল একাই"})

    p_ens = _clamp(_sigmoid(z_sum / w_sum), P_FLOOR, P_CEIL)
    direction = "CALL" if p_ens > 0.5 else "PUT"

    # Tier — the honest home of the "90%" question.
    lock = float(lp.get("lock_fraction") or 0.0)
    if p_ens >= TIER_LOCKED_P or lock >= TIER_LOCKED_LOCKFRAC:
        tier = "LOCKED"
    elif p_ens >= TIER_STRONG_P:
        tier = "STRONG"
    elif p_ens >= TIER_MEDIUM_P:
        tier = "MEDIUM"
    else:
        tier = "WEAK"

    # Agreement: weighted share of voices pointing the ensemble's way.
    agree_w = disagree_w = 0.0
    for v in voices:
        if v.get("weight", 0.0) <= 0.0:
            continue
        if (v["p"] > 0.5) == (p_ens > 0.5):
            agree_w += v["weight"]
        else:
            disagree_w += v["weight"]
    agreement_pct = int(round(100.0 * agree_w
                              / max(agree_w + disagree_w, 1e-9)))

    # Trade hint — the money question, kept SEPARATE from the direction
    # call: requires the σ-model's tradable probability AND ensemble
    # agreement, at a high tier or in the LAST10 window.
    hint = lp.get("entry_hint")
    trade_hint = hint if (hint in ("CALL", "PUT")
                          and hint == direction
                          and (tier in ("LOCKED", "STRONG")
                               or lp.get("phase") == "LAST10")) else None

    spoken = sum(1 for v in voices if v.get("weight", 0.0) > 0.0)
    note_bn = (f"{spoken}টি theory-voice মিলিয়ে {direction} "
               f"({p_ens:.0%}), ঐকমত্য {agreement_pct}% — "
               f"{_TIER_BN.get(tier, tier)}")

    return {
        "engine": "ensemble_v1",
        "p_ensemble": round(p_ens, 4),
        "direction": direction,
        "tier": tier,
        "tier_bn": _TIER_BN.get(tier, tier),
        "lock_fraction": lp.get("lock_fraction"),
        "agreement_pct": agreement_pct,
        "trade_hint": trade_hint,
        "voices": voices,
        "note_bn": note_bn,
    }


# ── Grading (the self-learning loop) ────────────────────────────────────────

def grade_ensemble(state: dict, ens: dict | None,
                   open_px, close_px) -> None:
    """Grade the FINAL ensemble snapshot + every speaking voice.

    Called at candle close with the final live-pred's ensemble payload.
    Updates rolling accuracies that drive the weights — this is the loop
    that makes "সব theory সঠিকভাবে ইউজ" self-correcting on the REAL feed.
    """
    if not isinstance(state, dict) or not isinstance(ens, dict):
        return
    if open_px is None or close_px is None or close_px == open_px:
        return
    try:
        actual_green = float(close_px) > float(open_px)
    except (TypeError, ValueError):
        return

    p = ens.get("p_ensemble")
    if p is not None:
        state["n"] = int(state.get("n", 0)) + 1
        state["correct"] = int(state.get("correct", 0)) \
            + int((float(p) > 0.5) == actual_green)
    tier = ens.get("tier")
    if tier:
        t = state.setdefault("tiers", {}).setdefault(
            tier, {"n": 0, "correct": 0})
        t["n"] += 1
        if p is not None:
            t["correct"] += int((float(p) > 0.5) == actual_green)
    for v in ens.get("voices") or []:
        name, pv = (v or {}).get("name"), (v or {}).get("p")
        if not name or pv is None or name == "fallback_sigma":
            continue
        vs = state.setdefault("voice_stats", {}).setdefault(
            name, {"n": 0, "correct": 0})
        vs["n"] += 1
        vs["correct"] += int((float(pv) > 0.5) == actual_green)


def grade_closed(stream, open_px, close_px) -> None:
    """Stream wrapper: grade the final snapshot held on the stream."""
    try:
        final = getattr(stream, "_live_pred_final", None)
        ens = final.get("ensemble") if isinstance(final, dict) else None
        grade_ensemble(get_state(stream), ens, open_px, close_px)
    except Exception:
        pass


def public_stats(stream) -> dict | None:
    """API-facing rolling stats (never promises — only measurements)."""
    st = getattr(stream, "_ensemble_state", None)
    if not isinstance(st, dict):
        return None
    out = {
        "ensemble": {
            "n": st.get("n", 0),
            "correct": st.get("correct", 0),
            "accuracy": (round(st.get("correct", 0)
                               / max(1, st.get("n", 1)), 4)
                         if st.get("n") else None),
        },
        "voices": {},
        "tiers": {},
    }
    for name, vs in (st.get("voice_stats") or {}).items():
        n, c = int(vs.get("n", 0)), int(vs.get("correct", 0))
        out["voices"][name] = {
            "n": n, "correct": c,
            "accuracy": round(c / n, 4) if n else None,
            "weight_now": round(_voice_weight(name, st), 4),
        }
    for tier, t in (st.get("tiers") or {}).items():
        n, c = int(t.get("n", 0)), int(t.get("correct", 0))
        out["tiers"][tier] = {"n": n, "correct": c,
                              "accuracy": round(c / n, 4) if n else None}
    return out
