"""Module: TICKRUN — Real-time tick-level theories (TICKSWEEP, ABSORBWALL, LATEFLIP)."""
from engines.base.types import ModuleResult, MarketContext

__all__ = ["analyze"]

import os
ENABLE_TICKSWEEP = os.environ.get("ENABLE_TICKSWEEP", "1") == "1"
ENABLE_ABSORBWALL = os.environ.get("ENABLE_ABSORBWALL", "1") == "1"
ENABLE_LATEFLIP = os.environ.get("ENABLE_LATEFLIP", "1") == "1"

MIN_TICKS_TICKSWEEP = 20
MIN_TICKS_ABSORBWALL = 25
MIN_TICKS_LATEFLIP = 20


def _ticksweep(ticks):
    """Detect stop-hunt pattern in tick sequence; returns (dir, mag, reason) or None."""
    if not ticks or len(ticks) < MIN_TICKS_TICKSWEEP:
        return None

    n = len(ticks)
    hi_idx = ticks.index(max(ticks))
    lo_idx = ticks.index(min(ticks))
    in_middle = lambda idx: n * 0.20 <= idx <= n * 0.80

    # Upper sweep: high in middle, then retraced >=50%
    if in_middle(hi_idx):
        peak = ticks[hi_idx]
        after = ticks[hi_idx:hi_idx + 8]
        retrace = (peak - min(after)) if after else 0
        excursion = peak - ticks[0]
        if (excursion > 0
                and retrace >= 0.50 * excursion
                and excursion >= (peak - ticks[lo_idx]) * 0.30):
            return (-1, 3,
                    f"TICKSWEEP Upper stop-hunt at tick {hi_idx}"
                    f" (retraced {retrace/excursion:.0%}) -> PUT (x3)")

    # Lower sweep: low in middle, then retraced >=50%
    if in_middle(lo_idx):
        trough = ticks[lo_idx]
        after = ticks[lo_idx:lo_idx + 8]
        retrace = (max(after) - trough) if after else 0
        excursion = ticks[0] - trough
        if (excursion > 0
                and retrace >= 0.50 * excursion
                and excursion >= (ticks[hi_idx] - trough) * 0.30):
            return (+1, 3,
                    f"TICKSWEEP Lower stop-hunt at tick {lo_idx}"
                    f" (retraced {retrace/excursion:.0%}) -> CALL (x3)")

    return None


def _absorbwall(ticks):
    """Detect price-band absorption; returns (dir, mag, reason) or None."""
    if not ticks or len(ticks) < MIN_TICKS_ABSORBWALL:
        return None

    hi = max(ticks)
    lo = min(ticks)
    rng = hi - lo
    if rng == 0:
        return None

    band_size = rng * 0.10
    hi_band = hi - band_size
    lo_band = lo + band_size

    upper_sells = sum(1 for i in range(1, len(ticks))
                      if ticks[i] > hi_band and ticks[i] < ticks[i - 1])
    upper_total = sum(1 for t in ticks if t > hi_band)

    lower_buys = sum(1 for i in range(1, len(ticks))
                     if ticks[i] < lo_band and ticks[i] > ticks[i - 1])
    lower_total = sum(1 for t in ticks if t < lo_band)

    threshold = 0.35

    # Upper absorption wall: sellers rejected at top -> PUT
    if (upper_total >= 8
            and upper_sells / upper_total >= threshold
            and ticks[-1] < hi_band):
        return (-1, 3,
                f"ABSORBWALL {upper_sells} sell-ticks absorbed at upper band"
                f" ({upper_sells/upper_total:.0%} of {upper_total}) -> PUT (x3)")

    # Lower absorption wall: buyers rejected at bottom -> CALL
    if (lower_total >= 8
            and lower_buys / lower_total >= threshold
            and ticks[-1] > lo_band):
        return (+1, 3,
                f"ABSORBWALL {lower_buys} buy-ticks absorbed at lower band"
                f" ({lower_buys/lower_total:.0%} of {lower_total}) -> CALL (x3)")

    return None


def _lateflip(ticks):
    """Detect 70/30 control transfer; returns (dir, mag, reason) or None."""
    if not ticks or len(ticks) < MIN_TICKS_LATEFLIP:
        return None

    n = len(ticks)
    split = int(n * 0.70)
    seg_a = ticks[:split]
    seg_b = ticks[split:]

    def bpct(seg):
        if len(seg) < 2:
            return 0.5
        u = sum(1 for i in range(1, len(seg)) if seg[i] > seg[i - 1])
        d = sum(1 for i in range(1, len(seg)) if seg[i] < seg[i - 1])
        t = u + d
        return u / t if t else 0.5

    a = bpct(seg_a)
    b = bpct(seg_b)
    a_dom = abs(a - 0.5) >= 0.15
    b_dom = abs(b - 0.5) >= 0.15
    opposite = ((a - 0.5) * (b - 0.5)) < 0

    if a_dom and b_dom and opposite:
        # Direction flipped: late-candle flip is mean-reverting in 1-min binary.
        direction = -1 if b > 0.5 else +1
        a_lbl = f"{a:.0%} buy" if a > 0.5 else f"{1 - a:.0%} sell"
        b_lbl = f"{b:.0%} buy" if b > 0.5 else f"{1 - b:.0%} sell"
        return (direction, 3,
                f"LATEFLIP Control transfer: first 70% {a_lbl}, last 30% {b_lbl}"
                f" -> mean-reversion {'PUT' if direction < 0 else 'CALL'} (x3)")

    return None


def analyze(candles, ticks, ctx: MarketContext) -> list:
    """Run TICKSWEEP, ABSORBWALL, LATEFLIP on raw ticks."""
    if not ticks or len(ticks) < 15:
        return []

    results = []

    if ENABLE_TICKSWEEP:
        r = _ticksweep(ticks)
        if r:
            direction, mag, reason = r
            results.append(ModuleResult(
                module_name="tickrun",
                direction="CALL" if direction > 0 else "PUT",
                score=mag,
                confidence=60,
                signal_type="REVERSAL",
                reliability="MICRO",
                group="TICKRUN_TICKSWEEP",
                reasons=[reason],
            ))

    if ENABLE_ABSORBWALL:
        r = _absorbwall(ticks)
        if r:
            direction, mag, reason = r
            results.append(ModuleResult(
                module_name="tickrun",
                direction="CALL" if direction > 0 else "PUT",
                score=mag,
                confidence=60,
                signal_type="REVERSAL",
                reliability="MICRO",
                group="TICKRUN_ABSORBWALL",
                reasons=[reason],
            ))

    if ENABLE_LATEFLIP:
        r = _lateflip(ticks)
        if r:
            direction, mag, reason = r
            results.append(ModuleResult(
                module_name="tickrun",
                direction="CALL" if direction > 0 else "PUT",
                score=mag,
                confidence=60,
                signal_type="REVERSAL",
                reliability="MICRO",
                group="TICKRUN_LATEFLIP",
                reasons=[reason],
            ))

    return results
