"""engines/base/confluence.py — STRICT high-confidence confluence engine.

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

THE FIX — six INDEPENDENT evidence clusters; a signal only exists when:

  * ≥ MIN_AGREE_CLUSTERS distinct clusters vote the SAME direction
  * ZERO clusters vote the opposite direction (abstention is allowed)
  * the market POSITION (regime + range location + HTF trend) agrees with
    the signal type:
      - TREND regime  → only with-trend continuation
      - RANGE regime  → only fade at range extreme (bottom/top 30%)
      - VOLATILE      → never trade
  * HTF (5-minute) trend does not oppose the direction
  * the candle is not sub-noise (range ≥ 0.20 × ATR — coin-flip territory)
  * honest confidence ≥ MIN_CONFIDENCE (no manufactured numbers)

If ANY gate fails → NEUTRAL. There is NO fallback path. Abstaining is a
first-class outcome: fewer signals, higher quality.

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
import math
import os

# ── Tunables (env-overridable for ops, safe defaults) ────────────────────────
MIN_AGREE_CLUSTERS = max(2, int(os.environ.get("QX_MIN_AGREE_CLUSTERS", "3")))
MIN_CONFIDENCE = max(50, int(os.environ.get("QX_MIN_CONFLUENCE_CONF", "65")))
RANGE_FADE_BAND = float(os.environ.get("QX_RANGE_FADE_BAND", "0.30"))
NOISE_ATR_RATIO = float(os.environ.get("QX_NOISE_ATR_RATIO", "0.20"))
MAX_CONFIDENCE = 92

# Cluster → member modules. A module name may appear in exactly one cluster.
CLUSTERS = {
    "TREND":    ("ema_ribbon", "multi_tf"),
    "MOMENTUM": ("momentum", "stochastic"),
    "MEANREV":  ("bollinger_rsi", "divergence"),
    "LEVEL":    ("key_level", "sr_bounce", "wickwall"),
    "PATTERN":  ("pattern",),
    "MICRO":    ("tickrun", "market_state", "candle_reaction"),
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
    """Run the strict confluence gates. Returns a prediction dict.

    NEVER emits a fallback signal: every gate failure returns NEUTRAL with
    the full reason trail so the UI can explain *why* no trade was taken.
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
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="opposition")

    majority = call_clusters if n_call > 0 else put_clusters
    n_agree = len(majority)
    if n_agree == 0:
        reasons.append(
            "_CONFLUENCE_REJECT: no cluster produced a net vote → NEUTRAL "
            "(no fallback signal by design).")
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="no_votes")
    if n_agree < MIN_AGREE_CLUSTERS:
        reasons.append(
            f"_CONFLUENCE_REJECT: only {n_agree} cluster(s) agree "
            f"(need ≥{MIN_AGREE_CLUSTERS}) → NEUTRAL.")
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="insufficient_agreement")

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
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="position_volatile")

    if _is_trending:
        trend_dir = "CALL" if "UP" in str(regime_name) else "PUT"
        if signal != trend_dir:
            reasons.append(
                f"_POSITION_GATE: regime={regime_name} but {signal} is "
                f"counter-trend → NEUTRAL. Counter-trend reversals in a "
                f"trend regime are not high-confidence.")
            return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                                   asset, htf_trend, gate="position_counter_trend")
        position_reason = f"with-trend continuation in {regime_name}"
    elif _is_ranging:
        if pos is None:
            reasons.append("_POSITION_GATE: RANGE regime but range position "
                           "unmeasurable → NEUTRAL.")
            return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                                   asset, htf_trend, gate="position_range_unknown")
        if signal == "CALL" and pos > RANGE_FADE_BAND:
            reasons.append(
                f"_POSITION_GATE: RANGE regime, price at {pos:.0%} of range "
                f"— CALL only valid near the bottom (≤{RANGE_FADE_BAND:.0%}) → NEUTRAL.")
            return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                                   asset, htf_trend, gate="position_range_mid")
        if signal == "PUT" and pos < (1.0 - RANGE_FADE_BAND):
            reasons.append(
                f"_POSITION_GATE: RANGE regime, price at {pos:.0%} of range "
                f"— PUT only valid near the top (≥{1.0 - RANGE_FADE_BAND:.0%}) → NEUTRAL.")
            return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                                   asset, htf_trend, gate="position_range_mid")
        position_reason = f"range fade at {pos:.0%} of range"
    # SIDEWAYS/UNKNOWN regime: no position restriction, HTF gate still applies.

    # ── Gate 3: HTF (5-minute) trend must not oppose ───────────────────────
    if htf_trend == "UPTREND" and signal == "PUT":
        reasons.append("_HTF_GATE: 5m UPTREND opposes PUT → NEUTRAL.")
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="htf_opposition")
    if htf_trend == "DOWNTREND" and signal == "CALL":
        reasons.append("_HTF_GATE: 5m DOWNTREND opposes CALL → NEUTRAL.")
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="htf_opposition")

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
            return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                                   asset, htf_trend, gate="noise")

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
        return _neutral_result(reasons, module_votes, cluster_votes, ctx,
                               asset, htf_trend, gate="confidence")

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
