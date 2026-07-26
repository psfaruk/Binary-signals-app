"""
Engines package — category-aware prediction router.

Two completely separate engines live side-by-side:

    engines.otc  — for OTC pairs (broker-generated price feed)
        6 modules + Smart Blender tuned for mean-reversion behavior.
        Heavier weight on candle_reaction, otc_pattern, key_level.
        Payout floor: 85%.

    engines.real — for real-market pairs (live exchange prices)
        Same 6 modules + Smart Blender, but retuned for trend-following.
        Module 6 is replaced with `trend_follow` (instead of `otc_pattern`)
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

from engines import otc as _otc_engine
from engines import real as _real_engine

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
    # "Otc", "ALLTIME_OTC" etc. are accepted. Previously a mixed-case category
    # raised ValueError because `detected` is always lowercase.
    if isinstance(category, str):
        category = category.lower()

    # FIX (DEEP-AUDIT-2026-07-26 / F-03-06): previously `detected = category_of(asset)`
    # was always computed even when the caller explicitly passed category. Now
    # we only call category_of() when needed: for auto-detect (category is None)
    # or for the mismatch check (explicit category passed).
    if category is None:
        category = category_of(asset)
    elif category not in ("otc", "real", "alltime_otc"):
        # Unknown category — let dispatcher raise a clear error below.
        pass
    else:
        detected = category_of(asset)
        if category != detected and not (
            category == "alltime_otc" and detected == "otc"
        ):
            # FIX (AUDIT-DEEP #05, 2026-07-23): the previous hard-mismatch check
            # rejected `category="alltime_otc"` even when the asset ended with
            # "_otc" (which is the correct pairing). `category_of()` returns
            # "otc" or "real" — never "alltime_otc" — so passing
            # `category="alltime_otc"` always triggered the ValueError.
            # `alltime_otc` is a presentation-layer flag (the 6 exotic pairs
            # get a dedicated UI tab) but the engine logic is identical to
            # regular OTC, so it should be accepted and routed to the OTC
            # engine.
            #
            # FIX (DEEP-AUDIT-2026-07-26 / F-03-01): previously this branch
            # normalized `category = "otc"` for routing, which (a) made the
            # `or category == "alltime_otc"` check at the dispatcher dead
            # (PROBLEM 12), and (b) lost the alltime_otc flag on echo-back so
            # the UI could not distinguish the 6 exotic pairs (PROBLEM 13).
            # Now we keep the original category and let the dispatcher handle
            # both "otc" and "alltime_otc".
            #
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
    if category == "otc" or category == "alltime_otc":
        # FIX (P1-ISSUE-004, 2026-07-22): alltime_otc routes to the OTC engine
        # (mean-reversion tuned). The 'alltime_otc' category is a presentation-
        # layer flag for the 6 exotic pairs; the engine logic is identical to
        # regular OTC.
        engine = _otc_engine
    elif category == "real":
        engine = _real_engine
    else:
        raise ValueError(
            f"unknown category {category!r}; expected 'otc', 'alltime_otc' "
            f"or 'real'")

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
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-03-02): preserve the ORIGINAL category
    # ("alltime_otc" is no longer silently overwritten with "otc") so the UI
    # can route signals to the dedicated exotic-pairs tab. Also moved
    # `import copy` to module top per PEP 8 (was inlined per PROBLEM 68).
    result = copy.deepcopy(result)
    result["category"] = category
    return result


# Convenience submodules — callers can also import the engine directly:
#   from engines.otc import predict as predict_otc
#   from engines.real import predict as predict_real
otc = _otc_engine
real = _real_engine
