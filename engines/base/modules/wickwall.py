"""Module: WICKWALL — Repeated wick rejection zone detection."""
from engines.base.types import ModuleResult, MarketContext

__all__ = ["analyze"]


def _wick_wall(candles, lookback=20):
    """Detect wick cluster walls; returns (sup_walls, res_walls, avg_rng)."""
    recent = candles[-lookback:]
    if len(recent) < 4:
        return [], [], 0.0

    n = len(recent)
    avg_rng = sum(c["high"] - c["low"] for c in recent) / n
    tol = avg_rng * 0.25

    def _cluster(tips):
        s = sorted(tips, key=lambda x: x[0])
        out = []
        grp_p = [s[0][0]]
        grp_w = [s[0][1]]
        for t, w in s[1:]:
            if t - grp_p[0] <= tol:
                grp_p.append(t)
                grp_w.append(w)
            else:
                total_w = sum(grp_w)
                if total_w >= 2.5:
                    out.append((sum(p * wt for p, wt in zip(grp_p, grp_w)) / total_w,
                                total_w))
                grp_p = [t]
                grp_w = [w]
        total_w = sum(grp_w)
        if total_w >= 2.5:
            out.append((sum(p * wt for p, wt in zip(grp_p, grp_w)) / total_w,
                        total_w))
        return out

    # Recency-weighted tips.
    # FIX (PREDICTION-BUG-2026-08-07 / A-17 B18): previously used
    # `1.0 - i/n` which gives weight 1.0 to OLDEST candle (i=0) and ~0 to
    # NEWEST. That meant wick-wall detection was dominated by stale 20-candle-
    # old rejections. Now `i/n` gives weight ~0 to oldest, 1.0 to newest —
    # recent rejections dominate, which is the whole point of "wick wall".
    _low_tips = [(recent[i]["low"], i / n) for i in range(n)]
    _high_tips = [(recent[i]["high"], i / n) for i in range(n)]

    return (_cluster(_low_tips), _cluster(_high_tips), avg_rng)


def _key_touches(candles, price, lookback=40):
    """Count pivot touches at price level."""
    recent = candles[-lookback:]
    if len(recent) < 5:
        return 0
    pivots = []
    for i in range(1, len(recent) - 1):
        hi, lo = recent[i]["high"], recent[i]["low"]
        if hi >= recent[i - 1]["high"] and hi >= recent[i + 1]["high"]:
            pivots.append(hi)
        if lo <= recent[i - 1]["low"] and lo <= recent[i + 1]["low"]:
            pivots.append(lo)
    best = 0
    for p in pivots:
        if abs(p - price) <= price * 0.0006:
            best += 1
    return best


def _round_level_strength(price):
    """Return round-level strength label."""
    import math
    if price <= 0:
        return "NONE"
    mag = 10.0 ** math.floor(math.log10(abs(price)))
    for frac, label, thr in [(0.01, "BIG", 0.05), (0.005, "MID", 0.06), (0.001, "SMALL", 0.10)]:
        step = mag * frac
        level = round(round(price / step) * step, 8)
        if abs(price - level) < step * thr:
            return label
    return "NONE"


def analyze(candles, ctx: MarketContext) -> list:
    """Detect wick-wall rejections; returns 0 or 1 ModuleResult."""
    if len(candles) < 6:
        return []

    cur = candles[-1]
    o, h, l, c = cur["open"], cur["high"], cur["low"], cur["close"]
    atr = ctx.atr if ctx.atr > 0 else 0.0010

    sup_walls, res_walls, avg_rng = _wick_wall(candles)
    if avg_rng == 0:
        return []

    results = []

    if sup_walls:
        _best_sup = max(sup_walls, key=lambda x: x[1])
        _sup_lvl, _sup_tch = _best_sup
        if _sup_tch >= 3 and abs(l - _sup_lvl) <= avg_rng * 0.25:
            # Confluence bonus: key touches + round level.
            bonus = 0
            lbl = ""
            kt = _key_touches(candles, _sup_lvl)
            rnd = _round_level_strength(_sup_lvl)
            if kt >= 2 and rnd in ("BIG", "MID"):
                bonus = 1
                lbl = f", @key x{kt}+{rnd.lower()} round"
            elif kt >= 3 or rnd == "BIG":
                bonus = 1
                lbl = f", @{'KEY x'+str(kt) if kt >= 3 else 'BIG round'}"
            mag = 2 + min(bonus, 1)
            results.append(ModuleResult(
                module_name="wickwall",
                direction="CALL",
                score=mag,
                confidence=55 + bonus * 5,
                signal_type="REVERSAL",
                reliability="LEVEL",
                group="WICKWALL",
                reasons=[f"WICKWALL Lower-wick cluster x{_sup_tch:.1f} at {_sup_lvl:.5g}{lbl} -> CALL (x{mag})"],
            ))

    if res_walls:
        _best_res = max(res_walls, key=lambda x: x[1])
        _res_lvl, _res_tch = _best_res
        if _res_tch >= 3 and abs(h - _res_lvl) <= avg_rng * 0.25:
            bonus = 0
            lbl = ""
            kt = _key_touches(candles, _res_lvl)
            rnd = _round_level_strength(_res_lvl)
            if kt >= 2 and rnd in ("BIG", "MID"):
                bonus = 1
                lbl = f", @key x{kt}+{rnd.lower()} round"
            elif kt >= 3 or rnd == "BIG":
                bonus = 1
                lbl = f", @{'KEY x'+str(kt) if kt >= 3 else 'BIG round'}"
            mag = 2 + min(bonus, 1)
            results.append(ModuleResult(
                module_name="wickwall",
                direction="PUT",
                score=mag,
                confidence=55 + bonus * 5,
                signal_type="REVERSAL",
                reliability="LEVEL",
                group="WICKWALL",
                reasons=[f"WICKWALL Upper-wick cluster x{_res_tch:.1f} at {_res_lvl:.5g}{lbl} -> PUT (x{mag})"],
            ))

    return results
