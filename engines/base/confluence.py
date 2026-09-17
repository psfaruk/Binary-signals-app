"""engines/base/confluence.py — ANY-ONE-THEORY confluence engine.

CONFLUENCE-V1 (2026-09-02) — Complete replacement for the old
pile-of-patches blending pipeline. Built from a full code audit that found
the root causes of wrong predictions:

  1. FAKE CONFLUENCE — the old engine counted *group names*. One observation
     (a hammer at support) spawned 5+ "agreeing groups" (pattern, sr_bounce,
     key_level, wickwall, market_state) that are all the same wick geometry
     re-measured 5 ways. Confidence math treated them as independent voters.
  2. FALLBACK SIGNALS — when no module fired, two fallback layers
     ("smart_fallback" + "smart_evidence_vote") manufactured a CALL/PUT out
     of thin air (last candle color, wall-clock hour, range position) —
     historically ~41.9% WR or worse.
  3. NO POSITION AWARENESS — signals fired against the prevailing regime
     (reversals in trends, continuations mid-range).

ANY-THEORY REWRITE (USER-2026-09-14) — the standing directive:

  "প্রত্যেক ক্যান্ডেল এ সিগন্যাল প্রধান করতে হবে, কিন্তু fallback signals
   দেওয়া যাবে না। ... যে কোনো একটি পাস হলেই সিগন্যাল দিবে। মডিউল
   ইঞ্জিন থেকে সিগন্যাল আসলো না [তাহলে] ML model থেকে সিগন্যাল টি আসবে।"

  = every candle emits CALL/PUT, BUT the direction must be THEORY-backed:
      * ANY ONE strategy module/theory with a directional vote — the signal
        is emitted from that vote (the best learned-weighted module decides
        the direction), labeled strategy "confluence_v1_any", a REAL
        signal (never the banned "confluence_v1_fallback").
      * ZERO theories voted — this engine returns NEUTRAL and the ML model
        supplies the candle's signal (feed.py wires that source).
      * Heuristic fallbacks (persistence, HTF-fade, body-fade, default CALL)
        are BANNED — those deterministic coin-flip chains measured 32-45.5%
        WR live and were physically removed from this engine.

The strict high-confidence confluence path (>=3 clusters, zero opposition,
position/HTF/noise-aware, honest confidence) is tried FIRST — when it
passes, the signal carries the stronger "confluence_v1" label. When any
strict gate rejects, the ANY-ONE-THEORY rule takes over (any module vote
means a signal). Both paths are theory-backed; neither fabricates direction.

Cluster definitions (independence-by-design, de-duplicating the correlated
modules found in the audit):

  TREND     : ema_ribbon, multi_tf          — structural trend direction
  MOMENTUM  : momentum, stochastic          — oscillator momentum/extension
  MEANREV   : bollinger_rsi, divergence     — overextension / exhaustion
  LEVEL     : key_level, sr_bounce, wickwall — support/resistance location
  PATTERN   : pattern                       — candlestick formation
  MICRO     : tickrun, market_state, candle_reaction — flow + state

Each module is collapsed to AT MOST ONE vote (net direction by effective
score). Each cluster votes by majority of its voting members; an internal
tie (members split CALL/PUT) makes the whole cluster abstain — split
evidence is not agreement.
"""
import os

# ── SIGNAL MODE (USER REQUIREMENT 2026-09-14, ANY-THEORY) ──────────────────
# The user's standing requirement: "প্রত্যেক ক্যান্ডেল এ সিগন্যাল প্রধান করতে
# হবে, কিন্তু fallback signals দেওয়া যাবে না" — every candle MUST produce a
# CALL or PUT signal, but heuristic fallbacks are BANNED.
#
#   "any_theory"  (DEFAULT) — strict confluence is tried first; when any
#       gate rejects, ANY ONE theory/module with a directional vote emits
#       the signal (best learned-weighted module decides). Coverage is
#       ~100% (modules fire on almost every candle). When ZERO theories
#       voted the engine returns NEUTRAL and feed.py takes the ML model's
#       frozen T+1 prediction as this candle's signal (USER: "মডিউল
#       ইঞ্জিন থেকে সিগন্যাল আসলো না — ML model থেকে সিগন্যাল টি আসবে").
#   "strict" — pure abstention: any gate failure returns NEUTRAL and the
#       ML model supplies the signal (identical NEUTRAL hand-off).
#   Legacy value "every_candle" is accepted and mapped to "any_theory"
#       (the old heuristic-fallback emission was removed — it violated the
#       no-fallback directive).
_LEGACY_MODES = {"every_candle": "any_theory"}
_raw_mode = os.environ.get("QX_SIGNAL_MODE", "any_theory").strip().lower()
SIGNAL_MODE = _LEGACY_MODES.get(_raw_mode, _raw_mode)
if SIGNAL_MODE not in ("any_theory", "strict"):
    SIGNAL_MODE = "any_theory"

# ── Tunables (env-overridable for ops, safe defaults) ────────────────────────
MIN_AGREE_CLUSTERS = max(2, int(os.environ.get("QX_MIN_AGREE_CLUSTERS", "3")))
MIN_CONFIDENCE = max(50, int(os.environ.get("QX_MIN_CONFLUENCE_CONF", "65")))
RANGE_FADE_BAND = float(os.environ.get("QX_RANGE_FADE_BAND", "0.30"))
NOISE_ATR_RATIO = float(os.environ.get("QX_NOISE_ATR_RATIO", "0.20"))
MAX_CONFIDENCE = 92
# ANY-THEORY confidence band (USER-2026-09-14) — replaces the old fallback
# band. A single-theory signal is a REAL strategy signal, so it earns a
# mid band (55+), but always stays BELOW MIN_CONFIDENCE so it can never be
# mistaken for a strict multi-cluster high-confidence signal. The legacy
# constant names are kept (scripts + joint_gate reference them) with the
# new any-theory semantics.
ANY_CONF_BASE = 55
ANY_CONF_CAP = max(ANY_CONF_BASE, MIN_CONFIDENCE - 1)
FALLBACK_CONF_BASE = ANY_CONF_BASE      # legacy alias (banned fallbacks gone)
FALLBACK_CONF_CAP = ANY_CONF_CAP        # legacy alias
# Confidence bumps inside the any-theory band:
ANY_CONF_PER_AGREEING_MODULE = 3        # each extra agreeing module
ANY_CONF_PER_NET_SCORE = 4              # honest evidence-quality bonus (net//4)

# NOTE (USER-2026-09-14): the PERSISTENCE-AWARE FALLBACK block that used to
# live here (PERSIST_* constants + _persistence_stats) was REMOVED — the
# no-fallback directive bans persistence/htf_fade/body_fade/default chains
# as signal sources. A measured persistence edge no longer manufactures a
# direction; only real theory votes (or the ML model, via feed.py) do.

# Cluster → member modules. A module name may appear in exactly one cluster.
# TICK-EYE (2026-09-16): the human-eye tick-anatomy module joins the MICRO
# cluster alongside tickrun — it reads the same raw-tick evidence class
# (intra-candle microstructure), so it must NOT count as an independent
# confluence cluster (that would inflate cluster counts — the exact bug
# CONFLUENCE-V1 was written to kill).
CLUSTERS = {
    "TREND":    ("ema_ribbon", "multi_tf"),
    "MOMENTUM": ("momentum", "stochastic"),
    "MEANREV":  ("bollinger_rsi", "divergence"),
    "LEVEL":    ("key_level", "sr_bounce", "wickwall"),
    "PATTERN":  ("pattern",),
    "MICRO":    ("tickrun", "market_state", "candle_reaction", "tick_eye",
                 "micro_flow"),
}
MODULE_TO_CLUSTER = {}
for _c, _members in CLUSTERS.items():
    for _m in _members:
        MODULE_TO_CLUSTER[_m] = _c


def _collapse_module_votes(grouped_results):
    """Collapse every module to AT MOST ONE net vote.

    Returns {module_name: {"direction": "CALL"|"PUT", "score": int, "results": [...]}}
    Modules whose results split evenly across directions are DROPPED
    (they abstain — split evidence is not agreement).
    """
    by_module = {}
    for r in grouped_results:
        by_module.setdefault(r.module_name, []).append(r)

    votes = {}
    for mname, results in by_module.items():
        call_score = sum(r.score for r in results if r.direction == "CALL")
        put_score = sum(r.score for r in results if r.direction == "PUT")
        if call_score > put_score:
            votes[mname] = {"direction": "CALL", "score": call_score - put_score,
                            "results": results}
        elif put_score > call_score:
            votes[mname] = {"direction": "PUT", "score": put_score - call_score,
                            "results": results}
        # exact tie → module abstains (no entry)
    return votes


def _cluster_votes(module_votes):
    """Aggregate module votes into cluster votes.

    A cluster votes for a direction only when a MAJORITY of its voting
    members agree on that direction; its strength is the sum of the
    agreeing members' net scores. Internal splits (1 CALL vs 1 PUT) make
    the cluster abstain entirely.
    Returns {cluster_name: {"direction": ..., "score": ..., "members": [...]}}
    """
    clusters = {}
    for cname, members in CLUSTERS.items():
        voting = [(m, module_votes[m]) for m in members if m in module_votes]
        if not voting:
            continue
        call_members = [(m, v) for m, v in voting if v["direction"] == "CALL"]
        put_members = [(m, v) for m, v in voting if v["direction"] == "PUT"]
        if len(call_members) > len(put_members):
            clusters[cname] = {
                "direction": "CALL",
                "score": sum(v["score"] for _, v in call_members),
                "members": [m for m, _ in call_members],
                "split": bool(put_members),
            }
        elif len(put_members) > len(call_members):
            clusters[cname] = {
                "direction": "PUT",
                "score": sum(v["score"] for _, v in put_members),
                "members": [m for m, _ in put_members],
                "split": bool(call_members),
            }
        # equal member counts → whole cluster abstains (split evidence)
    return clusters


def _any_theory_direction(module_votes, cluster_votes):
    """THEORY-ONLY direction for the ANY-ONE-THEORY mode (USER 2026-09-14).

    USER DIRECTIVE: "যে কোনো একটি পাস হলেই সিগন্যাল দিবে" — if ANY ONE
    strategy/theory has a directional vote, the signal is emitted from that
    vote; the BEST strategy (highest learned-weighted score) decides the
    direction. NO heuristic fallbacks (persistence, HTF-fade, body-fade,
    default CALL) are consulted — those are banned.

    Priority chain (every step is theory-backed; first decisive step wins):

      1. BEST-STRATEGY VOTE — the single highest-scoring directional module
         decides (scores are already scaled by reliability × per-pair
         LEARNED weights in blender.py, so "best" = the strategy with the
         strongest learned evidence for THIS pair+direction).
      2. Exact score tie between two best modules → net weighted evidence
         (sum of all module scores per direction).
      3. Cluster-count majority (clusters are themselves derived only from
         module votes — still theory-backed).

    Returns (direction|None, net_evidence, basis_label, best_module|None).
    direction is None when ZERO theories voted — the caller then returns
    NEUTRAL and feed.py takes the ML model's frozen T+1 prediction as this
    candle's signal ("মডিউল ইঞ্জিন থেকে সিগন্যাল আসলো না — ML model থেকে
    সিগন্যাল টি আসবে").
    """
    best_mod = None
    best_score = 0
    tie_directions = set()
    for mname, v in module_votes.items():
        if v["score"] > best_score:
            best_score = v["score"]
            best_mod = mname
            tie_directions = {v["direction"]}
        elif v["score"] == best_score and best_mod is not None:
            tie_directions.add(v["direction"])

    if best_mod is not None and len(tie_directions) == 1:
        # ANY-ONE-AGREES: at least one strategy voted, and the top-score
        # tier agrees on ONE direction → its best representative decides.
        direction = module_votes[best_mod]["direction"]
        call_score = sum(v["score"] for v in module_votes.values()
                         if v["direction"] == "CALL")
        put_score = sum(v["score"] for v in module_votes.values()
                        if v["direction"] == "PUT")
        net = abs(call_score - put_score)
        return direction, net, "best_strategy_vote", best_mod

    if best_mod is not None:
        # Top-score tier is SPLIT (e.g. momentum 5 CALL vs pattern 5 PUT):
        # documented step 2 — net weighted evidence of ALL votes decides.
        call_score = sum(v["score"] for v in module_votes.values()
                         if v["direction"] == "CALL")
        put_score = sum(v["score"] for v in module_votes.values()
                        if v["direction"] == "PUT")
        if call_score > put_score:
            return "CALL", call_score - put_score, "module_evidence", None
        if put_score > call_score:
            return "PUT", put_score - call_score, "module_evidence", None
        # still tied → cluster majority below.

    # Cluster-count majority — clusters derive ONLY from module votes.
    n_call = sum(1 for v in cluster_votes.values() if v["direction"] == "CALL")
    n_put = sum(1 for v in cluster_votes.values() if v["direction"] == "PUT")
    if n_call > n_put:
        return "CALL", 0, "cluster_majority", None
    if n_put > n_call:
        return "PUT", 0, "cluster_majority", None

    # ZERO theory-backed evidence → no direction (NEUTRAL; ML takes over).
    # Deliberately NO htf_fade / body_fade / default CALL — banned.
    return None, 0, "no_theory_vote", None

def _any_theory_result(reasons, module_votes, cluster_votes, ctx, asset,
                        htf_trend, candles, gate="unknown"):
    """Build the ANY-ONE-THEORY prediction (USER 2026-09-14).

    Honest labeling contract:
      * strategy        = "confluence_v1_any" — a REAL theory-backed signal
                         (strict gates rejected the setup, but a strategy
                         module voted and its vote decided the direction).
      * signal_quality  = "MEDIUM" (>=2 modules agree) / "LOW" (single module)
      * confidence      = ANY_CONF_BASE..ANY_CONF_CAP (always below
                         MIN_CONFIDENCE so it never masquerades as a strict
                         multi-cluster signal) and evidence-scaled:
                         base 55, +3 per extra agreeing module, +net//4
                         evidence bonus, +2 HTF alignment, +2 range-fade
                         alignment.
      * confluence_reject_gate = which strict gate rejected the setup
      * best_strategy   = the strategy module whose vote DECIDED the
        direction ("বেস্ট stradegy টি সিগন্যাল দিবে") — surfaced in
        reasons + UI so the user always sees WHICH strategy gave the
        signal.
      * NO fallback key, NO persistence, NO fabricated direction — when
        zero theories voted this returns NEUTRAL (via _neutral_result in
        _gate_exit) and feed.py takes the ML model's signal instead.
    Deterministic: same input always yields the same signal, so backtests
    are reproducible.
    """
    direction, net, basis, best_mod = _any_theory_direction(
        module_votes, cluster_votes)

    if direction is None:
        # ZERO theories voted — strategy engine abstains; the ML model
        # (feed.py source hand-off) supplies this candle's signal.
        reasons.append(
            "_NO_THEORY_VOTE: no strategy module produced a directional "
            "vote on this candle — module engine abstains (NEUTRAL); "
            "the ML model supplies the signal.")
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                                asset, htf_trend, gate="no_theory_vote")

    n_agree = sum(1 for v in cluster_votes.values()
                  if v["direction"] == direction)
    n_voted = len(module_votes)
    n_same_dir = sum(1 for v in module_votes.values()
                     if v["direction"] == direction)
    n_opp_dir = n_voted - n_same_dir

    # Honest evidence-scaled confidence inside the any-theory band.
    confidence = ANY_CONF_BASE
    confidence += ANY_CONF_PER_AGREEING_MODULE * max(0, n_same_dir - 1)
    confidence += min(ANY_CONF_PER_NET_SCORE, net // ANY_CONF_PER_NET_SCORE
                      if ANY_CONF_PER_NET_SCORE else 0)
    htf_aligned = ((htf_trend == "UPTREND" and direction == "CALL")
                   or (htf_trend == "DOWNTREND" and direction == "PUT"))
    if htf_aligned:
        confidence += 2
    # RANGE regime fade-alignment bonus: in a range, a signal that fades the
    # extreme (CALL at bottom / PUT at top) is structurally better placed.
    pos = _range_position(candles or [])
    regime = ctx.regime if ctx is not None else {}
    fade_aligned = False
    if regime.get("is_ranging") and pos is not None:
        fade_aligned = ((direction == "CALL" and pos <= RANGE_FADE_BAND)
                        or (direction == "PUT" and pos >= 1.0 - RANGE_FADE_BAND))
        if fade_aligned:
            confidence += 2
    confidence = max(ANY_CONF_BASE, min(ANY_CONF_CAP, confidence))

    if best_mod is not None:
        best_score = module_votes[best_mod]["score"]
        reasons.append(
            f"_BEST_STRATEGY_VOTE: {best_mod} (learned-weighted score "
            f"{best_score}) decided {direction} — {n_voted} strategy "
            f"module(s) voted ({n_same_dir} {direction}, {n_opp_dir} "
            f"opposed); any-one-theory rule satisfied.")
    reasons.append(
        f"_ANY_THEORY_SIGNAL: strict gate '{gate}' rejected the setup — "
        f"emitting {direction} from theory evidence (basis={basis}, "
        f"net={net}, agree_modules={n_same_dir}, conf={confidence}).")

    return {
        "signal": direction,
        "confidence": confidence,
        "raw_confidence": confidence,
        "strength": "MEDIUM" if n_same_dir >= 2 else "WEAK",
        "score": net,
        "agree": n_agree,
        "total": len(cluster_votes) or n_agree,
        "signals_fired": sum(len(v["members"]) for v in cluster_votes.values()),
        "strategy": "confluence_v1_any",
        "best_strategy": best_mod,
        "strategy_reason": (
            f"any-one-theory — strict gate '{gate}' rejected; "
            + (f"best strategy {best_mod} (score "
               f"{module_votes[best_mod]['score']}) voted {direction}"
               if best_mod is not None
               else f"direction from {basis}")),
        "signal_quality": "MEDIUM" if n_same_dir >= 2 else "LOW",
        "signal_source": "strategy",
        "any_theory": True,
        "any_theory_basis": basis,
        "confluence_reject_gate": gate,
        "confluence": {
            "clusters_agree": sorted(
                c for c, v in cluster_votes.items()
                if v["direction"] == direction),
            "clusters_oppose": sorted(
                c for c, v in cluster_votes.items()
                if v["direction"] != direction),
            "cluster_detail": {
                c: {"direction": v["direction"], "score": v["score"],
                    "members": v["members"]}
                for c, v in cluster_votes.items()},
            "module_votes": {
                m: {"direction": v["direction"], "score": v["score"]}
                for m, v in module_votes.items()},
            "range_position": pos,
            "position_reason": (
                f"range fade aligned ({pos:.0%})" if fade_aligned else None),
            "htf_aligned": htf_aligned,
        },
    }

def _gate_exit(reasons, module_votes, cluster_votes, ctx, asset,
               htf_trend, candles, gate):
    """Single exit point for strict-gate failures (USER-2026-09-14).

    any_theory mode → ANY ONE module vote emits a REAL theory signal
                      (strategy "confluence_v1_any"); zero votes → NEUTRAL
                      and the ML model supplies the signal from feed.py.
    strict mode     → NEUTRAL with the reject-gate label (ML model still
                      supplies the signal from feed.py — same hand-off).
    """
    if SIGNAL_MODE == "any_theory":
        return _any_theory_result(reasons, module_votes, cluster_votes, ctx,
                                  asset, htf_trend, candles, gate=gate)
    return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                           asset, htf_trend, gate=gate)


def _range_position(candles, lookback=20):
    """Position of last close within the recent range: 0.0 = at the low,
    1.0 = at the high. Returns None when the range is degenerate."""
    if len(candles) < 5:
        return None
    window = candles[-min(lookback, len(candles)):]
    hi = max(c["high"] for c in window)
    lo = min(c["low"] for c in window)
    rng = hi - lo
    if rng <= 0:
        return None
    return (candles[-1]["close"] - lo) / rng


def evaluate(grouped_results, ctx, config, asset="", htf_trend="SIDEWAYS",
             candles=None, all_reasons=None):
    """Run the strict confluence gates, then the ANY-ONE-THEORY rule.

    Returns a prediction dict. Strict gates first (>=3 clusters, zero
    opposition, position/HTF/noise-aware, honest confidence ≥
    MIN_CONFIDENCE). When a gate rejects: any module vote ⇒ REAL
    "confluence_v1_any" signal; zero votes ⇒ NEUTRAL (feed.py then takes
    the ML model's frozen T+1 prediction as this candle's signal). NO
    heuristic fallback is ever emitted (USER-2026-09-14 directive).
    """
    candles = candles or []
    reasons = all_reasons if all_reasons is not None else []

    # ── Step 1: module-level collapse (kills multi-group double counting) ──
    module_votes = _collapse_module_votes(grouped_results)

    # ── Step 2: cluster-level aggregation (kills fake confluence) ──────────
    cluster_votes = _cluster_votes(module_votes)
    call_clusters = {c: v for c, v in cluster_votes.items() if v["direction"] == "CALL"}
    put_clusters = {c: v for c, v in cluster_votes.items() if v["direction"] == "PUT"}

    n_call = len(call_clusters)
    n_put = len(put_clusters)

    _cluster_state = (
        f"clusters CALL={sorted(call_clusters)} PUT={sorted(put_clusters)} "
        f"(need ≥{MIN_AGREE_CLUSTERS} agree, 0 oppose)")
    reasons.append(f"_CONFLUENCE_VOTE: {_cluster_state}")

    # ── Gate 1: pure one-sided agreement ───────────────────────────────────
    if n_call > 0 and n_put > 0:
        reasons.append(
            f"_CONFLUENCE_REJECT: cross-direction opposition "
            f"({n_call} CALL vs {n_put} PUT clusters) → NEUTRAL. "
            f"High-confidence requires ZERO opposing clusters.")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles, gate="opposition")

    majority = call_clusters if n_call > 0 else put_clusters
    n_agree = len(majority)
    if n_agree == 0:
        reasons.append(
            "_CONFLUENCE_REJECT: no cluster produced a net vote → NEUTRAL "
            "(no fallback signal by design).")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles, gate="no_votes")
    if n_agree < MIN_AGREE_CLUSTERS:
        reasons.append(
            f"_CONFLUENCE_REJECT: only {n_agree} cluster(s) agree "
            f"(need ≥{MIN_AGREE_CLUSTERS}) → NEUTRAL.")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles,
                          gate="insufficient_agreement")

    signal = "CALL" if n_call > 0 else "PUT"
    cluster_score = sum(v["score"] for v in majority.values())

    # ── Gate 2: POSITION AWARENESS — regime/location/type coherence ────────
    regime = ctx.regime if ctx is not None else {}
    _is_volatile = regime.get("is_volatile", False)
    _is_trending = regime.get("is_trending", False)
    _is_ranging = regime.get("is_ranging", False)
    regime_name = regime.get("regime", "UNKNOWN")
    pos = _range_position(candles)

    position_reason = None
    if _is_volatile:
        reasons.append(
            f"_POSITION_GATE: regime={regime_name} VOLATILE → trading "
            f"prohibited (historically coin-flip) → NEUTRAL.")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles, gate="position_volatile")

    if _is_trending:
        # FIX (TREND-DIR-EXACT-2026-09-07): was `"UP" in str(regime_name)` —
        # any future regime token containing "UP" (e.g. "RUPTURE") would
        # silently map to CALL. Exact-match the known trend regimes instead.
        if regime_name in ("TREND_UP", "UPTREND"):
            trend_dir = "CALL"
        elif regime_name in ("TREND_DOWN", "DOWNTREND"):
            trend_dir = "PUT"
        else:
            trend_dir = "CALL" if "UP" in str(regime_name) else "PUT"
        if signal != trend_dir:
            reasons.append(
                f"_POSITION_GATE: regime={regime_name} but {signal} is "
                f"counter-trend → NEUTRAL. Counter-trend reversals in a "
                f"trend regime are not high-confidence.")
            return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                              asset, htf_trend, candles,
                              gate="position_counter_trend")
        position_reason = f"with-trend continuation in {regime_name}"
    elif _is_ranging:
        if pos is None:
            reasons.append("_POSITION_GATE: RANGE regime but range position "
                           "unmeasurable → NEUTRAL.")
            return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                              asset, htf_trend, candles,
                              gate="position_range_unknown")
        if signal == "CALL" and pos > RANGE_FADE_BAND:
            reasons.append(
                f"_POSITION_GATE: RANGE regime, price at {pos:.0%} of range "
                f"— CALL only valid near the bottom (≤{RANGE_FADE_BAND:.0%}) → NEUTRAL.")
            return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                              asset, htf_trend, candles,
                              gate="position_range_mid")
        if signal == "PUT" and pos < (1.0 - RANGE_FADE_BAND):
            reasons.append(
                f"_POSITION_GATE: RANGE regime, price at {pos:.0%} of range "
                f"— PUT only valid near the top (≥{1.0 - RANGE_FADE_BAND:.0%}) → NEUTRAL.")
            return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                              asset, htf_trend, candles,
                              gate="position_range_mid")
        position_reason = f"range fade at {pos:.0%} of range"
    # SIDEWAYS/UNKNOWN regime: no position restriction, HTF gate still applies.

    # ── Gate 3: HTF (5-minute) trend must not oppose ───────────────────────
    if htf_trend == "UPTREND" and signal == "PUT":
        reasons.append("_HTF_GATE: 5m UPTREND opposes PUT → NEUTRAL.")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles, gate="htf_opposition")
    if htf_trend == "DOWNTREND" and signal == "CALL":
        reasons.append("_HTF_GATE: 5m DOWNTREND opposes CALL → NEUTRAL.")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles, gate="htf_opposition")

    # ── Gate 4: sub-noise candle filter ────────────────────────────────────
    atr = ctx.atr if ctx is not None else 0.0
    if candles and atr > 0:
        last = candles[-1]
        candle_range = max(0.0, last.get("high", 0.0) - last.get("low", 0.0))
        if candle_range < atr * NOISE_ATR_RATIO:
            reasons.append(
                f"_NOISE_GATE: last candle range {candle_range:.5g} < "
                f"{NOISE_ATR_RATIO:.0%}×ATR({atr:.5g}) — sub-noise candle, "
                f"direction is a coin flip → NEUTRAL.")
            return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                              asset, htf_trend, candles, gate="noise")

    # ── Step 3: HONEST confidence (no manufactured calibration) ────────────
    # base 55 for the minimum viable 3-cluster agreement; each extra cluster
    # adds +7 (real independent evidence); score quality adds up to +8;
    # regime/position coherence +5; HTF alignment +5. Hard cap 92 — never
    # claim near-certainty on a 1-minute binary bet.
    confidence = 55 + 7 * (n_agree - MIN_AGREE_CLUSTERS)
    confidence += min(8, int(cluster_score // 3))
    if position_reason:
        confidence += 5
        reasons.append(f"_POSITION_ALIGNED: {position_reason} (+5)")
    htf_aligned = ((htf_trend == "UPTREND" and signal == "CALL")
                   or (htf_trend == "DOWNTREND" and signal == "PUT"))
    if htf_aligned:
        confidence += 5
        reasons.append("_HTF_ALIGNED: 5m trend agrees with direction (+5)")
    confidence = min(MAX_CONFIDENCE, confidence)

    if confidence < MIN_CONFIDENCE:
        reasons.append(
            f"_CONFIDENCE_GATE: honest confidence {confidence} < "
            f"{MIN_CONFIDENCE} → NEUTRAL.")
        return _gate_exit(reasons, module_votes, cluster_votes, ctx,
                          asset, htf_trend, candles, gate="confidence")

    # ── Step 4: strength — WEAK does not exist in this engine ──────────────
    strength = "STRONG" if (n_agree >= MIN_AGREE_CLUSTERS + 2
                            and confidence >= 75) else "MEDIUM"

    reasons.append(
        f"_CONFLUENCE_PASS: {n_agree} clusters ({', '.join(sorted(majority))}) "
        f"agree on {signal}, cluster_score={cluster_score}, conf={confidence}")

    return {
        "signal": signal,
        "confidence": confidence,
        "raw_confidence": confidence,
        "strength": strength,
        "score": cluster_score,
        "agree": n_agree,
        "total": len(cluster_votes) or n_agree,
        "signals_fired": sum(len(v["members"]) for v in cluster_votes.values()),
        "strategy": "confluence_v1",
        "signal_source": "strategy",
        "strategy_reason": (
            f"{n_agree} independent clusters agree: "
            f"{', '.join(sorted(majority))}"),
        "signal_quality": "HIGH" if strength == "STRONG" else "MEDIUM",
        "confluence": {
            "clusters_agree": sorted(majority),
            "clusters_oppose": [],
            "cluster_detail": {
                c: {"direction": v["direction"], "score": v["score"],
                    "members": v["members"]}
                for c, v in cluster_votes.items()},
            "module_votes": {
                m: {"direction": v["direction"], "score": v["score"]}
                for m, v in module_votes.items()},
            "range_position": pos,
            "position_reason": position_reason,
            "htf_aligned": htf_aligned,
        },
    }


def _neutral_result(reasons, module_votes, cluster_votes, ctx, asset,
                    htf_trend, gate="unknown"):
    """Build a NEUTRAL prediction carrying the confluence diagnostics."""
    return {
        "signal": "NEUTRAL",
        "confidence": 0,
        "raw_confidence": 0,
        "strength": "NEUTRAL",
        "score": 0,
        "agree": 0,
        "total": len(cluster_votes),
        "signals_fired": sum(len(v["members"]) for v in cluster_votes.values()),
        "strategy": "confluence_v1",
        "strategy_reason": f"no trade — gate: {gate}",
        "signal_quality": "NONE",
        "confluence_reject_gate": gate,
        "confluence": {
            "clusters_agree": [],
            "clusters_oppose": [],
            "cluster_detail": {
                c: {"direction": v["direction"], "score": v["score"],
                    "members": v["members"]}
                for c, v in cluster_votes.items()},
            "module_votes": {
                m: {"direction": v["direction"], "score": v["score"]}
                for m, v in module_votes.items()},
            "range_position": None,
            "position_reason": None,
            "htf_aligned": False,
        },
    }
