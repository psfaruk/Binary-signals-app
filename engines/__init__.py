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
from engines import otc as _otc_engine
from engines import real as _real_engine

__all__ = ["predict", "otc", "real", "category_of"]


def category_of(asset: str) -> str:
    """Return the category for an asset name.

    "EURUSD_otc" → "otc"
    "EURUSD"     → "real"
    """
    return "otc" if (asset or "").endswith("_otc") else "real"


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
    # Auto-detect category from asset name when not specified.
    detected = category_of(asset)
    if category is None:
        category = detected
    elif category != detected:
        # Hard mismatch — refuse to route. This was previously silent,
        # allowing an OTC pair to be analyzed by the Real engine (or
        # vice versa), defeating the whole point of having two engines.
        raise ValueError(
            f"category/asset mismatch: category={category!r} but asset "
            f"{asset!r} implies category={detected!r}. Pass a consistent "
            f"pair, or omit category to auto-detect.")

    if category == "otc":
        result = _otc_engine.predict(
            candles, ticks, micro, asset=asset,
            htf_trend=htf_trend, period=period,
            recent_accuracy=recent_accuracy)
    elif category == "real":
        result = _real_engine.predict(
            candles, ticks, micro, asset=asset,
            htf_trend=htf_trend, period=period,
            recent_accuracy=recent_accuracy)
    else:
        raise ValueError(
            f"unknown category {category!r}; expected 'otc' or 'real'")

    # Echo the resolved category so the UI / signal_log can record which
    # engine produced this prediction (useful for per-engine accuracy
    # tracking in /api/stats).
    result = dict(result)
    result["category"] = category
    return result


# Convenience submodules — callers can also import the engine directly:
#   from engines.otc import predict as predict_otc
#   from engines.real import predict as predict_real
otc = _otc_engine
real = _real_engine
