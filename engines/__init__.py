"""
Engines package — category-aware prediction router.

Two completely separate engines live side-by-side:

    engines.otc  — for OTC pairs (broker-generated price feed)
        6 modules + Smart Blender tuned for mean-reversion behavior.
        Heavier weight on candle_reaction, key_level.
        Payout floor: 85%.

    engines.real — for real-market pairs (live exchange prices)
        Same 6 modules + Smart Blender, but retuned for trend-following.
        FIX (MODULE-PRUNE-2026-08-03): engine-specific 6th module removed
        which detects momentum continuation rather than mean-reversion.
        Heavier weight on indicator (RSI/MACD/EMA), pattern (engulfing etc.).
        Payout floor: 70%.

The two engines share NO state — separate per_pair configs, separate
module weight defaults, separate reliability tier overrides. Each engine
has its own DB-adaptation cache (looked up by asset name only — OTC and
real never collide because real pairs have no "_otc" suffix).

Public API:
    from engines import predict
    result = predict(candles, ticks, micro, asset="EURUSD_otc", category="otc")
    result = predict(candles, ticks, micro, asset="EURUSD",     category="real")

If `category` is omitted, it is auto-detected from the asset name:
asset ending in "_otc" → otc, otherwise → real.

Category-asset mismatch (e.g. category="real" but asset="EURUSD_otc")
raises a ValueError — the caller MUST pass a consistent pair.
"""
import copy  # FIX (DEEP-AUDIT-2026-07-26 / F-03-03): moved out of predict() body per PEP 8 (was inlined at line 140).
import time as _time
from datetime import datetime as _datetime, timezone as _timezone

from engines import otc as _otc_engine
from engines import real as _real_engine
# USER REQUIREMENT (2026-08-03): trap-hour auto-skip. Loads a static map of
# (asset, hour_utc) pairs where the live Railway brain has detected the pair
# loses >=65% of trades with >=5 samples. Signals during these windows are
# suppressed (returned as NEUTRAL with reason="trap_hour").
from engines.base.trap_hours import is_trap_hour as _is_trap_hour, trap_reason as _trap_reason
# USER FIX #8 (2026-08-03, REVISED): pairs with chronically low win rate
# now get a CONFIDENCE PENALTY (not full block). The original full-block
# approach suppressed too many signals and the live app showed no
# direction guidance for these pairs.
from engines.base.disabled_pairs import pair_penalty as _pair_penalty, penalty_reason as _penalty_reason

__all__ = ["predict", "otc", "real", "category_of"]
# TODO (DEEP-AUDIT-2026-07-26 / F-03-08): BlenderConfig is intentionally NOT
# re-exported from this package — import directly from `engines.base` if you
# need it. Documented here so PROBLEM 121 (LOW) is resolved explicitly.


def category_of(asset: str) -> str:
    """Return the category for an asset name.

    "EURUSD_otc" → "otc"
    "EURUSD"     → "real"

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-28): also check for "OTC"
    suffix (case-insensitive) without underscore, so a pair named
    "EURUSDOTC" (typo or non-standard broker format) is still classified
    as "otc" rather than routed to the Real engine. Quotex consistently
    uses "_otc", but this is a defensive check.
    """
    asset_lower = (asset or "").lower()
    # FIX (DEEP-AUDIT-2026-07-26 / F-03-04): the prior `endswith("_otc") or
    # endswith("otc")` was redundant — every string ending in "_otc" also
    # ends in "otc", so the first condition was dead (subsumed by the second).
    # Kept the broader "otc" form to also catch non-standard broker naming
    # like "EURUSDOTC" (defensive, see docstring above).
    if asset_lower.endswith("otc"):
        return "otc"
    return "real"


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, category: str = None, recent_accuracy=None) -> dict:
    """Route to the correct engine based on `category`.

    Args:
        candles, ticks, micro, asset: passed through unchanged.
        htf_trend: passed through unchanged.
        period: candle period in seconds, passed through unchanged.
        category: "otc" | "real" | None.
            If None, auto-detected from asset name:
                ends with "_otc" → "otc"
                else             → "real"
        recent_accuracy: optional (accuracy, sample_count) tuple from
            db.recent_accuracy(). Passed through to the engine for
            accuracy-aware self-correction.

    Returns:
        The engine's prediction dict (signal, confidence, strength, etc.)
        with an extra "category" field for UI/logging.

    Raises:
        ValueError: if category is explicitly set AND conflicts with the
            asset name (e.g. category="real" but asset="EURUSD_otc").
            This is a hard error — the caller MUST fix the inconsistency
            rather than silently letting an OTC pair get analyzed by the
            Real engine (or vice versa).
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-03-05): case-normalize category so "OTC",
    # "Otc", etc. are accepted. Previously a mixed-case category raised
    # ValueError because `detected` is always lowercase.
    if isinstance(category, str):
        category = category.lower()

    # NOTE (USER REQUIREMENT 2026-08-03): the 'alltime_otc' category has
    # been REMOVED. Only 'real' and 'otc' are valid. Old callers that pass
    # 'alltime_otc' are normalized to 'otc' for backward compatibility
    # (defensive — the engine logic is identical either way, and the
    # frontend has already been updated to never send it).
    if category == "alltime_otc":
        category = "otc"

    # USER REQUIREMENT (2026-08-03): TRAP-HOUR AUTO-SKIP.
    # Before dispatching to the engine, check if (asset, current UTC hour)
    # is in the trap_hours suppression map. If yes, return NEUTRAL with a
    # reason instead of running the modules — historically these windows
    # lose >=65% of trades, so emitting any signal is a net-negative EV.
    # The map is regenerated by /home/z/my-project/scripts/generate_calibration.py
    # from the live Railway brain's /api/quotex-algo-detect output.
    try:
        _now_utc = _datetime.now(_timezone.utc)
        _hour_utc = _now_utc.hour
        if _is_trap_hour(asset, _hour_utc):
            _reason = _trap_reason(asset, _hour_utc)
            return {
                "signal": "NEUTRAL",
                "confidence": 0,
                "strength": "WEAK",
                "score": 0.0,
                "reasons": [_reason] if _reason else ["trap hour suppression"],
                "modules": {},
                "regime": "trap_hour",
                "category": category if category in ("otc", "real") else "otc",
                "trap_hour": True,
                "trap_hour_utc": _hour_utc,
                "trap_reason": _reason,
                "skipped": True,
            }
    except Exception as _trap_exc:
        # Defensive: never let the trap-hour check itself crash the prediction.
        # Log and fall through to the normal prediction path.
        print(f"[engines] trap-hour check failed for {asset}: {_trap_exc}")

    # USER FIX #8 (2026-08-03, REVISED): PAIR CONFIDENCE PENALTY.
    # Pairs with chronically low (< 45%) win rate over 200+ graded signals
    # get a confidence penalty (×0.5-0.6) instead of being fully blocked.
    # The original full-block approach suppressed too many signals.
    # Now the engine still runs and produces a signal, but the final
    # confidence is dampened so the user sees a weaker conviction.
    _pair_mult = 1.0
    _pair_pen_reason = ""
    try:
        _pair_mult = _pair_penalty(asset)
        if _pair_mult < 1.0:
            _pair_pen_reason = _penalty_reason(asset)
    except Exception as _pen_exc:
        print(f"[engines] pair-penalty check failed for {asset}: {_pen_exc}")

    # FIX (DEEP-AUDIT-2026-07-26 / F-03-06): previously `detected = category_of(asset)`
    # was always computed even when the caller explicitly passed category. Now
    # we only call category_of() when needed: for auto-detect (category is None)
    # or for the mismatch check (explicit category passed).
    if category is None:
        category = category_of(asset)
    elif category not in ("otc", "real"):
        # Unknown category — let dispatcher raise a clear error below.
        pass
    else:
        detected = category_of(asset)
        if category != detected:
            # Hard mismatch — refuse to route. This was previously silent,
            # allowing an OTC pair to be analyzed by the Real engine (or
            # vice versa), defeating the whole point of having two engines.
            raise ValueError(
                f"category/asset mismatch: category={category!r} but asset "
                f"{asset!r} implies category={detected!r}. Pass a consistent "
                f"pair, or omit category to auto-detect.")

    # FIX (DEEP-AUDIT-2026-07-26 / F-03-07): consolidate the two duplicate
    # `_otc_engine.predict(...)` / `_real_engine.predict(...)` branches into
    # a single engine variable + single call site. Both branches used the exact
    # same args, so the verbose if/elif was unnecessary. Same behavior — same
    # args, same return value, same error semantics.
    if category == "otc":
        engine = _otc_engine
    elif category == "real":
        engine = _real_engine
    else:
        raise ValueError(
            f"unknown category {category!r}; expected 'otc' or 'real'")

    result = engine.predict(
        candles, ticks, micro, asset=asset,
        htf_trend=htf_trend, period=period,
        recent_accuracy=recent_accuracy)

    # Echo the resolved category so the UI / signal_log can record which
    # engine produced this prediction (useful for per-engine accuracy
    # tracking in /api/stats).
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-3-34): use copy.deepcopy
    # instead of dict() (shallow copy). The previous code created a shallow
    # copy — nested dicts (regime, modules) were SHARED references. If a
    # downstream consumer mutated result["regime"]["foo"] = "bar", it would
    # corrupt the engine's regime dict (which is freshly computed per
    # prediction via compute_context, so unlikely to cause issues in
    # practice, but a latent footgun). deepcopy fully isolates the result.
    # FIX (DEEP-AUDIT-2026-07-26 / F-03-02): preserve the ORIGINAL category
    # so the UI can route signals to the right tab. Also moved
    # `import copy` to module top per PEP 8 (was inlined per PROBLEM 68).
    # NOTE (USER REQUIREMENT 2026-08-03): 'alltime_otc' is no longer a valid
    # category — it's normalized to 'otc' above, so the echoed category is
    # always 'otc' or 'real'.
    result = copy.deepcopy(result)
    result["category"] = category

    # USER FIX #8 (2026-08-03, REVISED): apply pair confidence penalty.
    # If the asset is in the PENALIZED_PAIRS map, multiply the final
    # confidence by the dampening factor. This happens AFTER the engine
    # has produced its signal, so the direction (CALL/PUT) is preserved
    # — only the displayed conviction is reduced.
    if _pair_mult < 1.0 and result.get("signal") in ("CALL", "PUT"):
        _orig_conf = result.get("confidence", 0)
        _new_conf = round(_orig_conf * _pair_mult)
        result["confidence"] = _new_conf
        # If the penalty drops confidence below the NEUTRAL threshold,
        # convert to NEUTRAL so the user doesn't trade on a weak signal.
        if _new_conf < 25:
            result["signal"] = "NEUTRAL"
            result["confidence"] = 0
            result["strength"] = "NEUTRAL"
        # Append the penalty reason for transparency.
        if _pair_pen_reason:
            result.setdefault("reasons", []).append(
                f"{_pair_pen_reason} (confidence {_orig_conf} -> {_new_conf})")

    return result


# Convenience submodules — callers can also import the engine directly:
#   from engines.otc import predict as predict_otc
#   from engines.real import predict as predict_real
otc = _otc_engine
real = _real_engine
