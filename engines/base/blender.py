"""engines/base/blender.py — strict confluence blender shared by OTC/Real.

CONFLUENCE-V1 REWRITE (2026-09-02) — full replacement of the previous
1,500-line patch stack. The audit found the old pipeline produced WRONG
predictions because it:

  * emitted "smart fallback" + "last-resort evidence-vote" signals with no
    statistical edge when no module fired (historically ~41.9% WR),
  * counted correlated group names as independent confluence (one hammer at
    support = 5 "agreeing groups"),
  * remapped every confidence through a hand-tuned calibration table that
    flattened all outputs into a meaningless 47-68% band,
  * allowed WEAK signals and single-group signals (~47% WR) through.

The new pipeline:
  1. run every strategy module (fixed indicator math),
  2. collapse correlated modules into six INDEPENDENT evidence clusters,
  3. hand the cluster votes to engines.base.confluence.evaluate() which
     applies the strict high-confidence gates (>=3 clusters agree, zero
     opposition, position-aware, HTF-aware, noise-aware, honest confidence),
  4. EVERY-CANDLE MODE (default, QX_SIGNAL_MODE=every_candle): when a gate
     fails, confluence emits a DETERMINISTIC evidence-based fallback signal
     (labeled "confluence_v1_fallback", confidence 50-63) instead of
     NEUTRAL — 100% candle coverage per the user requirement
     "প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে". Set QX_SIGNAL_MODE=strict to
     restore pure abstention.

Output dict keeps the exact key set the frontend and feed pipeline expect.
"""
from dataclasses import dataclass

from engines.base.context import compute_context
from engines.base.types import ModuleResult
from engines.base import confluence as _cf

from engines.base.modules import (
    candle_reaction as mod_candle,
    pattern as mod_pattern,
    key_level as mod_keylevel,
)
from engines.base.modules import (
    market_state as mod_market_state,
    wickwall as mod_wickwall,
    divergence as mod_divergence,
    tickrun as mod_tickrun,
)
from engines.base.modules import (
    multi_tf as mod_multi_tf,
    momentum as mod_momentum,
)
from engines.base.modules import (
    bollinger_rsi as mod_bollinger_rsi,
    stochastic as mod_stochastic,
    ema_ribbon as mod_ema_ribbon,
    sr_bounce as mod_sr_bounce,
)
from engines.base.per_pair import PairWeightAdapter

MIN_CANDLES_FOR_PREDICTION = 30  # honest indicator warmup (RSI14, MACD26+9)

__all__ = ["predict", "BlenderConfig", "MIN_CANDLES_FOR_PREDICTION",
           "MODULE_ORDER", "_module_breakdown", "_neutral"]

MODULE_ORDER = (
    "candle_reaction", "pattern", "key_level", "market_state", "wickwall",
    "divergence", "tickrun", "multi_tf", "momentum", "bollinger_rsi",
    "stochastic", "ema_ribbon", "sr_bounce",
)


@dataclass
class BlenderConfig:
    """Engine-specific configuration for the shared blender."""
    reliability: dict
    weight_adapter: PairWeightAdapter
    module_names: tuple
    engine_name: str = "base"


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, config=None, recent_accuracy=None) -> dict:
    """Run all strategy modules + the strict confluence engine.

    NOTE (CONFLUENCE-V1): `recent_accuracy` is accepted for API
    compatibility but is deliberately UNUSED — adaptive confidence
    inflation/deflation from small samples (n<150) was one of the audit's
    top confidence-corruption sources. Honest confidence is computed only
    from current-candle evidence.
    """
    if config is None:
        raise ValueError("BlenderConfig is required — pass engines.{otc,real}.config.CONFIG")

    reliability = config.reliability
    weight_adapter = config.weight_adapter
    module_names = config.module_names

    if candles is None or len(candles) < MIN_CANDLES_FOR_PREDICTION:
        # EVERY-CANDLE MODE: even below the indicator warmup floor the user
        # still requires a direction. Use the deterministic body-direction /
        # HTF tie-break chain (no indicator math — the data is too thin for
        # the modules) with the honest fallback labeling.
        if _cf.SIGNAL_MODE == "every_candle" and candles:
            n = len(candles)
            direction = "CALL"
            basis = "default"
            if n >= 1:
                try:
                    o = float(candles[-1].get("open", 0.0))
                    c = float(candles[-1].get("close", 0.0))
                    if c > o:
                        direction, basis = "CALL", "body_direction"
                    elif c < o:
                        direction, basis = "PUT", "body_direction"
                except Exception:
                    pass
            if basis == "default":
                if htf_trend == "UPTREND":
                    direction, basis = "CALL", "htf_trend"
                elif htf_trend == "DOWNTREND":
                    direction, basis = "PUT", "htf_trend"
            conf = _cf.FALLBACK_CONF_BASE
            if (htf_trend == "UPTREND" and direction == "CALL") or (
                    htf_trend == "DOWNTREND" and direction == "PUT"):
                conf += 2
            result = _neutral(
                [f"INSUFFICIENT_DATA: need >= {MIN_CANDLES_FOR_PREDICTION} "
                 f"closed candles (got {n}) — every-candle fallback active"],
                {}, asset, weight_adapter,
                module_names=module_names, htf_trend=htf_trend)
            result.update({
                "signal": direction,
                "confidence": conf,
                "raw_confidence": conf,
                "strength": "WEAK",
                "score": 0,
                "strategy": "confluence_v1_fallback",
                "strategy_reason": (
                    f"every-candle fallback ({basis}) — insufficient data"),
                "signal_quality": "FALLBACK",
                "fallback": True,
                "fallback_basis": basis,
                "confluence_reject_gate": "insufficient_data",
            })
            return result
        return _neutral(["INSUFFICIENT_DATA: need >= 30 closed candles"],
                        {}, asset, weight_adapter,
                        module_names=module_names, htf_trend=htf_trend)

    # ── Step 1: shared market context ────────────────────────────────────────
    ctx = compute_context(candles)

    # ── Step 2: run every module (fixed indicator math) ─────────────────────
    all_results = []
    all_results += mod_candle.analyze(candles, ctx)
    all_results += mod_pattern.analyze(candles, ctx)
    all_results += mod_keylevel.analyze(candles, ctx)
    all_results += mod_market_state.analyze(candles, ctx)
    all_results += mod_wickwall.analyze(candles, ctx)
    all_results += mod_divergence.analyze(candles, ctx)
    all_results += mod_tickrun.analyze(candles, ticks, ctx)
    all_results += mod_multi_tf.analyze(candles, ctx)
    all_results += mod_momentum.analyze(candles, ctx)
    all_results += mod_bollinger_rsi.analyze(candles, ctx)
    all_results += mod_stochastic.analyze(candles, ctx)
    all_results += mod_ema_ribbon.analyze(candles, ctx)
    all_results += mod_sr_bounce.analyze(candles, ctx)

    # ── Step 3: per-module net collapse (dedup double-counted groups) ──────
    # CONFLUENCE FIX: the old engine let one module emit several results into
    # several group names (key_level up to 4, tickrun up to 3, momentum 2 in
    # the same direction) and then counted GROUPS as independent voters.
    # Each module now contributes at most ONE net vote; pattern multi-emit
    # and MACD double votes are collapsed inside the modules themselves too.
    by_module = {}
    for r in all_results:
        by_module.setdefault(r.module_name, []).append(r)

    grouped_results = []
    for mname, results in by_module.items():
        call_score = sum(r.score for r in results if r.direction == "CALL")
        put_score = sum(r.score for r in results if r.direction == "PUT")
        if call_score == 0 and put_score == 0:
            continue
        if call_score == put_score:
            # module split 50/50 → it abstains; keep zero-weight record so
            # the UI can show "conflict" for that module.
            neutral_repr = ModuleResult(
                module_name=mname,
                direction="NEUTRAL",
                score=0,
                confidence=0,
                signal_type="CONTINUATION",
                reliability=results[0].reliability,
                group=results[0].group,
                reasons=[f"[SPLIT] internal CALL/PUT conflict ({call_score} vs {put_score}) — abstains"])
            grouped_results.append(neutral_repr)
            continue
        winning_dir = "CALL" if call_score > put_score else "PUT"
        net = abs(call_score - put_score)
        best = max((r for r in results if r.direction == winning_dir),
                   key=lambda r: r.score)
        collapsed = ModuleResult(
            module_name=mname,
            direction=winning_dir,
            score=net,
            confidence=best.confidence,
            signal_type=best.signal_type,
            reliability=best.reliability,
            group=best.group,
            reasons=[f"[NET {mname}] {winning_dir} net={net} "
                     f"({call_score} CALL vs {put_score} PUT)"] + best.reasons)
        grouped_results.append(collapsed)

    # ── Step 4: apply reliability + per-pair weights as SCORE scalers ──────
    # Weights scale evidence strength (used only for the honest-confidence
    # score-quality bonus) — they can no longer inflate the cluster count.
    pair_weights = weight_adapter.get_weights(asset, period=period)
    for r in grouped_results:
        if r.direction == "NEUTRAL":
            continue
        t_mult = reliability.get(r.reliability, 1.0)
        p_mult = pair_weights.get(r.module_name, 1.0)
        r.score = max(1, int(round(r.score * t_mult * p_mult)))

    # ── Step 5: STRICT CONFLUENCE — the single decision authority ──────────
    all_reasons = []
    result = _cf.evaluate(
        grouped_results, ctx, config, asset=asset, htf_trend=htf_trend,
        candles=candles, all_reasons=all_reasons)

    # Attach shared fields every caller expects.
    pair_profile = weight_adapter.get_profile(asset)
    result["reasons"] = all_reasons
    result["regime"] = ctx.regime
    result["modules"] = _module_breakdown(grouped_results, module_names,
                                          final_signal=result["signal"])
    result["asset"] = asset
    result["profile"] = pair_profile
    result["htf_trend"] = htf_trend
    if recent_accuracy is not None:
        result["recent_accuracy_ignored"] = True

    return result


def _module_breakdown(grouped_results, module_names, final_signal="NEUTRAL"):
    """Per-module breakdown dict for UI display (post-collapse, one entry
    per module). Cluster membership is exposed via pred.confluence."""
    breakdown = {}
    by_name = {r.module_name: r for r in grouped_results}

    for mname in module_names:
        r = by_name.get(mname)
        if r is None:
            breakdown[mname] = {
                "direction": "NEUTRAL", "score": 0, "reasons": [], "fired": False,
            }
            continue
        reasons = list(r.reasons)
        breakdown[mname] = {
            "direction": r.direction,
            "score": r.score if r.direction != "NEUTRAL" else 0,
            "reasons": reasons,
            "fired": r.direction != "NEUTRAL",
            "agree_with_final": (r.direction == final_signal
                                 if final_signal in ("CALL", "PUT") else False),
        }

    return breakdown


def _neutral(reasons, regime, asset="", weight_adapter=None, ctx=None,
             module_names: tuple = None, htf_trend="SIDEWAYS") -> dict:
    """Return a NEUTRAL prediction (no fallback — abstention by design)."""
    modules = {}
    pair_profile = "default"
    if weight_adapter is not None:
        pair_profile = weight_adapter.get_profile(asset)
        if module_names:
            modules = _module_breakdown([], module_names)
    return {
        "signal": "NEUTRAL", "confidence": 0, "raw_confidence": 0,
        "strength": "NEUTRAL",
        "score": 0, "reasons": reasons if isinstance(reasons, list) else [reasons],
        "regime": regime, "agree": 0, "total": 0, "signals_fired": 0,
        "modules": modules, "asset": asset, "profile": pair_profile,
        "htf_trend": htf_trend, "signal_quality": "NONE",
        "strategy": "confluence_v1", "strategy_reason": "insufficient data",
    }
