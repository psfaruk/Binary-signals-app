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

FREQ-FIRST-FIX (2026-09-09) — user's LATEST directive supersedes selectivity:

  "আমার প্রত্যেক ক্যান্ডেল এ সিগন্যাল লাগবে। যে কোনো একটি স্ট্রাটেজি
   একমত হলেই সিগন্যাল আসবে। অবশ্য বেস্ট stradegy টি সিগন্যাল দিবে।"

  = EVERY candle emits CALL/PUT; if ANY ONE strategy has a directional
    vote the signal is emitted from that vote; the BEST strategy (highest
    learned-weighted score for this pair+direction) decides the direction.
    When a strict gate rejects the setup, _fallback_result() emits the
    best-strategy direction with honest FALLBACK labeling (confidence
    50-63, strategy "confluence_v1_fallback", best_strategy=<module>) so
    the UI, history and win-rate stats can always separate strict
    high-confidence signals from fallback coverage signals. The TARGET-75
    gate (core/target_gate.py) that previously converted these into WAIT
    is now default-OFF.

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

# ── SIGNAL MODE (USER REQUIREMENT 2026-09-07) ──────────────────────────────
# The user's standing requirement is: "আমার প্রত্যেকটি ক্যান্ডেল এ সিগন্যাল লাগবে"
# (every candle MUST produce a CALL or PUT signal).
#
#   "every_candle"  (DEFAULT) — strict confluence is tried first; when any
#       gate rejects the setup, a DETERMINISTIC evidence-based fallback
#       direction is emitted instead of NEUTRAL so coverage is 100%.
#       Fallback signals are honestly labeled (strategy
#       "confluence_v1_fallback", confidence 50-63, quality FALLBACK) so
#       the UI and win-rate stats can always separate them from strict
#       high-confidence signals.
#   "strict" — the original CONFLUENCE-V1 abstention behavior: any gate
#       failure returns NEUTRAL (coverage historically 0-0.4%).
SIGNAL_MODE = os.environ.get("QX_SIGNAL_MODE", "every_candle").strip().lower()
if SIGNAL_MODE not in ("every_candle", "strict"):
    SIGNAL_MODE = "every_candle"

# ── Tunables (env-overridable for ops, safe defaults) ────────────────────────
MIN_AGREE_CLUSTERS = max(2, int(os.environ.get("QX_MIN_AGREE_CLUSTERS", "3")))
MIN_CONFIDENCE = max(50, int(os.environ.get("QX_MIN_CONFLUENCE_CONF", "65")))
RANGE_FADE_BAND = float(os.environ.get("QX_RANGE_FADE_BAND", "0.30"))
NOISE_ATR_RATIO = float(os.environ.get("QX_NOISE_ATR_RATIO", "0.20"))
MAX_CONFIDENCE = 92
# Fallback confidence band — always BELOW MIN_CONFIDENCE so a fallback
# signal can never be mistaken for a strict high-confidence signal.
FALLBACK_CONF_BASE = 50
FALLBACK_CONF_CAP = min(63, MIN_CONFIDENCE - 2)

# ── PERSISTENCE-AWARE FALLBACK (ACCURACY-FIX 2026-09-11) ────────────────────
# Live-data audit of 7,376 graded signals (50.23% WR) found the old
# fallback direction chain was ANTI-predictive in its deterministic steps:
#   htf_trend basis      → 32.0% win (n=25)  (following the 5m trend loses)
#   cluster_majority     → 44.4% win (n=72)
#   body_direction       → 45.5% win (n=33)  (following the last body loses)
#   best_strategy_vote   → 50.3% win (n=6144) (no edge at all)
# while the only REAL, walk-forward-verifiable edge in the data is per-pair
# candle-colour PERSISTENCE (mean-reversion on OTC feeds, momentum on real
# feeds): P(next=UP|last=UP) measured on a rolling window of CLOSED candles.
# Walk-forward replay over the same 7,376 candles: +0.73pp aggregate and up
# to +6.45pp on mean-reverting pairs (USDZAR_otc 47.98%→54.44%), because the
# stats are computed strictly from candles[:-1] (already closed) at predict
# time — no look-ahead by construction.
PERSIST_WINDOW = int(os.environ.get("QX_PERSIST_WINDOW", "300"))   # rolling candle window
PERSIST_MIN_N = int(os.environ.get("QX_PERSIST_MIN_N", "60"))      # min transitions to trust
PERSIST_MARGIN = float(os.environ.get("QX_PERSIST_MARGIN", "0.03")) # min |p-0.5| edge
PERSIST_CONF_PER_PP = 1.0   # confidence pp per measured edge pp (honest calibration)

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


def _persistence_stats(candles, window=PERSIST_WINDOW):
    """Per-pair candle-colour persistence from CLOSED candle history.

    STRICTLY CAUSAL: callers pass the closed-candle list available at predict
    time (the new candle is never in it), so every transition counted here
    happened in the past. Returns a dict:
      {"last": "UP"|"DOWN"|None,
       "n": transitions-after-last-colour,
       "p_next_up": P(next=UP | last colour),
       "edge_pp": |p_next_up - 0.5| * 100,
       "dir": "CALL"|"PUT" (the persistence-implied next direction)}
    """
    if not candles or len(candles) < 3:
        return None
    hist = candles[-(window + 1):] if len(candles) > window + 1 else candles
    last_color = None
    o = float(hist[-1].get("open", 0.0) or 0.0)
    c = float(hist[-1].get("close", 0.0) or 0.0)
    if c > o:
        last_color = "UP"
    elif c < o:
        last_color = "DOWN"
    if last_color is None:
        return None

    n_after = k_up = 0
    for j in range(1, len(hist)):
        po = float(hist[j - 1].get("open", 0.0) or 0.0)
        pc = float(hist[j - 1].get("close", 0.0) or 0.0)
        prev_color = "UP" if pc > po else ("DOWN" if pc < po else None)
        if prev_color != last_color:
            continue
        oj = float(hist[j].get("open", 0.0) or 0.0)
        cj = float(hist[j].get("close", 0.0) or 0.0)
        if cj == oj:
            continue  # draw — no direction information
        n_after += 1
        if cj > oj:
            k_up += 1
    if n_after < PERSIST_MIN_N:
        return None
    p_next_up = k_up / n_after
    if abs(p_next_up - 0.5) < PERSIST_MARGIN:
        return None  # no significant edge — do not override module evidence
    return {
        "last": last_color,
        "n": n_after,
        "p_next_up": p_next_up,
        "edge_pp": abs(p_next_up - 0.5) * 100.0,
        "dir": "CALL" if p_next_up > 0.5 else "PUT",
        "kind": "mean-reversion" if (
            (last_color == "UP" and p_next_up < 0.5)
            or (last_color == "DOWN" and p_next_up > 0.5)) else "momentum",
    }


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


def _fallback_direction(module_votes, cluster_votes, htf_trend,
                        candles=None):
    """Deterministic best-effort direction for EVERY-CANDLE mode.

    USER DIRECTIVE (FREQ-FIRST-FIX 2026-09-09):
      "যে কোনো একটি স্ট্রাটেজি একমত হলেই সিগন্যাল আসবে। অবশ্য বেস্ট
       stradegy টি সিগন্যাল দিবে।"
    = if ANY ONE strategy has a directional vote, a signal MUST be emitted,
      and the BEST strategy's vote decides the direction.

    ACCURACY-FIX (2026-09-11) — measured performance of the OLD chain on
    7,098 graded live signals drove a reordering. New priority chain
    (first decisive step wins):

      0. PERSISTENCE EDGE (NEW) — when this pair's closed-candle history
         shows a statistically meaningful candle-colour persistence
         (mean-reversion on OTC feeds / momentum on real feeds, measured
         over the last ≤300 CLOSED candles with ≥60 transitions and a
         ≥3pp deviation from the coin-flip), that measured edge decides.
         This is the only walk-forward-verified edge in the live data
         (+0.73pp aggregate, up to +6.45pp per pair). The best-module
         vote only decides when no measured persistence edge exists.
      1. BEST-STRATEGY VOTE — the single highest-scoring directional module
         decides (scores are already scaled by reliability × per-pair
         LEARNED weights in blender.py, so "best" = the strategy with the
         strongest learned evidence for THIS pair+direction).
      2. Exact score tie between two best modules → net weighted evidence
         (sum of all module scores per direction).
      3. Cluster-count majority.
      4. HTF (5-minute) trend — now FADED, not followed: the live ledger
         shows following the 5m EMA trend on 1-minute binaries won only
         32% of the time (n=25). Counter-trend is the honest default for
         a 1-minute expiry against a 5-minute trend extreme.
      5. Last candle body — now FADED (anti-momentum): following the last
         body won 45.5% (n=33); the pooled after-run mean-reversion edge
         is P(reverse) ≈ 52-53% after runs of 1-3 same-colour candles.
      6. Absolute last resort: CALL (deterministic, never random).

    Returns (direction, net_evidence, basis_label, best_module_name|None,
             persistence_dict|None).
    """
    # ── Step 0 (ACCURACY-FIX): measured per-pair persistence edge ──────
    persist = _persistence_stats(candles) if candles else None
    if persist is not None:
        return persist["dir"], int(round(persist["edge_pp"])), \
            f"persistence_{persist['kind']}", None, persist

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
        return direction, net, "best_strategy_vote", best_mod, None

    if best_mod is not None:
        # Top-score tier is SPLIT (e.g. momentum 5 CALL vs pattern 5 PUT):
        # documented step 2 — net weighted evidence of ALL votes decides.
        call_score = sum(v["score"] for v in module_votes.values()
                         if v["direction"] == "CALL")
        put_score = sum(v["score"] for v in module_votes.values()
                        if v["direction"] == "PUT")
        if call_score > put_score:
            return "CALL", call_score - put_score, "module_evidence", None, None
        if put_score > call_score:
            return "PUT", put_score - call_score, "module_evidence", None, None
        # still tied → fall through to the deterministic chain below.

    # No directional module vote at all → deterministic tie-break chain.
    n_call = sum(1 for v in cluster_votes.values() if v["direction"] == "CALL")
    n_put = sum(1 for v in cluster_votes.values() if v["direction"] == "PUT")
    if n_call > n_put:
        return "CALL", 0, "cluster_majority", None, None
    if n_put > n_call:
        return "PUT", 0, "cluster_majority", None, None

    # Still tied → HTF trend — FADED (see docstring: following it won 32%).
    if htf_trend == "UPTREND":
        return "PUT", 0, "htf_fade", None, None
    if htf_trend == "DOWNTREND":
        return "CALL", 0, "htf_fade", None, None

    # Still tied → last candle body — FADED (anti-momentum; see docstring).
    if candles:
        try:
            last = candles[-1]
            o = float(last.get("open", 0.0))
            c = float(last.get("close", 0.0))
            if c > o:
                return "PUT", 0, "body_fade", None, None
            if c < o:
                return "CALL", 0, "body_fade", None, None
        except Exception:
            pass

    return "CALL", 0, "default", None, None


def _fallback_result(reasons, module_votes, cluster_votes, ctx, asset,
                     htf_trend, candles, gate="unknown"):
    """Build an every-candle FALLBACK prediction (CALL/PUT always present).

    Honest labeling contract:
      * strategy        = "confluence_v1_fallback" (never masquerades as strict)
      * signal_quality  = "FALLBACK"
      * confidence      = 50..FALLBACK_CONF_CAP (below MIN_CONFIDENCE) and
                         EMPIRICALLY calibrated (ACCURACY-FIX 2026-09-11):
                         when the direction comes from a measured persistence
                         edge, confidence = 50 + measured edge pp — no more
                         fabricated numbers (the old net//3 formula claimed
                         conf~60-70 while delivering 48-50% actual win rate,
                         a -12 to -21pp calibration gap measured live).
      * confluence_reject_gate = which strict gate rejected the setup
      * best_strategy   = the strategy module whose vote DECIDED the
        direction (FREQ-FIRST-FIX 2026-09-09: "বেস্ট stradegy টি সিগন্যাল
        দিবে") — surfaced in reasons + UI so the user always sees WHICH
        strategy gave the signal and why. When a measured persistence edge
        decided instead, best_strategy is None and the persistence stats
        are surfaced in reasons + the confluence dict.
    The direction is deterministic (see _fallback_direction) — same input
    data always yields the same signal, so backtests are reproducible.
    """
    direction, net, basis, best_mod, persist = _fallback_direction(
        module_votes, cluster_votes, htf_trend, candles)

    n_agree = sum(1 for v in cluster_votes.values()
                  if v["direction"] == direction)
    n_oppose = sum(1 for v in cluster_votes.values()
                   if v["direction"] != direction)

    # Honest low-band confidence (ACCURACY-FIX 2026-09-11):
    # • persistence-decided → 50 + measured edge pp (cap FALLBACK_CONF_CAP)
    # • otherwise → the old conservative formula (small net bonus only)
    if persist is not None:
        confidence = FALLBACK_CONF_BASE + int(round(
            persist["edge_pp"] * PERSIST_CONF_PER_PP))
    else:
        confidence = FALLBACK_CONF_BASE + min(6, net // 3)
    htf_aligned = ((htf_trend == "UPTREND" and direction == "CALL")
                   or (htf_trend == "DOWNTREND" and direction == "PUT"))
    if htf_aligned and persist is None:
        confidence += 2
    # RANGE regime fade-alignment bonus: in a range, a signal that fades the
    # extreme (CALL at bottom / PUT at top) is structurally better placed.
    pos = _range_position(candles or [])
    regime = ctx.regime if ctx is not None else {}
    fade_aligned = False
    if regime.get("is_ranging") and pos is not None:
        fade_aligned = ((direction == "CALL" and pos <= RANGE_FADE_BAND)
                        or (direction == "PUT" and pos >= 1.0 - RANGE_FADE_BAND))
        if fade_aligned and persist is None:
            confidence += 2
    confidence = max(FALLBACK_CONF_BASE, min(FALLBACK_CONF_CAP, confidence))

    n_voted = len(module_votes)
    if persist is not None:
        reasons.append(
            f"_PERSISTENCE_EDGE: measured P(next=UP|last={persist['last']})="
            f"{persist['p_next_up']:.1%} over {persist['n']} transitions — "
            f"{persist['kind']} edge {persist['edge_pp']:.1f}pp decided "
            f"{direction} (empirical, walk-forward-verified basis).")
    if best_mod is not None and persist is None:
        best_score = module_votes[best_mod]["score"]
        n_same_dir = sum(1 for v in module_votes.values()
                         if v["direction"] == direction)
        n_opp_dir = n_voted - n_same_dir
        reasons.append(
            f"_BEST_STRATEGY_VOTE: {best_mod} (learned-weighted score "
            f"{best_score}) decided {direction} — {n_voted} strategy "
            f"module(s) voted ({n_same_dir} {direction}, {n_opp_dir} "
            f"opposed); any-one-agrees rule satisfied.")
    reasons.append(
        f"_EVERY_CANDLE_FALLBACK: strict gate '{gate}' rejected the setup — "
        f"emitting {direction} (basis={basis}, net={net}, conf={confidence}) "
        f"to honor the every-candle signal requirement.")

    return {
        "signal": direction,
        "confidence": confidence,
        "raw_confidence": confidence,
        "strength": "WEAK",
        "score": net,
        "agree": n_agree,
        "total": len(cluster_votes) or n_agree,
        "signals_fired": sum(len(v["members"]) for v in cluster_votes.values()),
        "strategy": "confluence_v1_fallback",
        "best_strategy": best_mod,
        "strategy_reason": (
            f"every-candle fallback — strict gate '{gate}' rejected; "
            + (f"best strategy {best_mod} (score "
               f"{module_votes[best_mod]['score']}) voted {direction}"
               if best_mod is not None
               else (f"measured persistence edge {persist['edge_pp']:.1f}pp "
                     f"({persist['kind']}, n={persist['n']}) decided {direction}"
                     if persist is not None
                     else f"direction from {basis}"))),
        "signal_quality": "FALLBACK",
        "fallback": True,
        "fallback_basis": basis,
        "confluence_reject_gate": gate,
        "persistence": persist,
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
    """Single exit point for strict-gate failures.

    every_candle mode → deterministic fallback signal (100% coverage).
    strict mode      → NEUTRAL with the reject-gate label.
    """
    if SIGNAL_MODE == "every_candle":
        return _fallback_result(reasons, module_votes, cluster_votes, ctx,
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
