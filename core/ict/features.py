"""Phase-L5 integration — ICT/SMC feature snapshot at target-candle time.

THE no-lookahead rule at integration level: a snapshot for predicting
candle ``t`` uses ONLY candles[:t]:
  * detections with confirm_index <= t-1 (each depends by contract only
    on candles[:confirm_index+1] ⊆ candles[:t]);
  * indicators / volume-delta evaluated at index t-1;
  * EX-ANTE time features legitimately known in advance: the target
    candle's session/kill-zone tag and calendar-news distance.

Two equivalent paths (pinned identical by the L5 battery):
  * ``snapshot_at(candles, t)`` — reference: context over candles[:t+1];
  * ``FeatureContext(...).snapshot(t)`` — context over any superset list
    (e.g., the full series), reading only confirm_index <= t-1 data;
  * ``build_feature_matrix`` — incremental fast path whose rows must
    equal ``snapshot(t)`` exactly (battery-pinned).

Zone status semantics match ``poi_zones.classify_zone_status`` exactly:
a zone confirmed at index c folds candles c+1 .. t-1 (untested → touched
→ broken, monotonic). All outputs are research features — nothing here
emits a direction, probability claim, or actionable signal.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Any, Optional, Sequence

from .types import Candle, Detection, validate_candles
from . import candlestick, liquidity, market_structure, momentum, orderflow, poi_zones, sessions
from .news import NewsEvent, minutes_to_event

DEFAULT_PARAMS = {
    "swing_k": 2,
    "zone_window": 300,        # only zones confirmed within this many candles
    "recent_range_window": 20,
    "pattern_window": 5,
}

_ZONE_SIDE = {
    "demand_zone": "bullish", "supply_zone": "bearish",
    "order_block_bullish": "bullish", "order_block_bearish": "bearish",
    "fvg_bullish": "bullish", "fvg_bearish": "bearish",
    "eq_highs": "bearish", "eq_lows": "bullish",
    "resistance": "bearish", "support": "bullish",
}
_SUPPLY_KINDS = ("supply_zone", "order_block_bearish", "fvg_bearish",
                 "eq_highs", "resistance")
_DEMAND_KINDS = ("demand_zone", "order_block_bullish", "fvg_bullish",
                 "eq_lows", "support")


class _ZoneTracker:
    """Incremental monotonic zone status: untested -> touched -> broken.

    Equivalent to poi_zones.classify_zone_status(zone, candles, upto) —
    pinned by the L5 battery. ``advance`` iterates only unbroken zones.
    """

    def __init__(self) -> None:
        self.zones: dict[int, Detection] = {}
        self.status: dict[int, str] = {}

    def add(self, zid: int, zone: Detection) -> None:
        self.zones[zid] = zone
        self.status[zid] = "untested"

    def advance(self, c: Candle) -> None:
        for zid in [z for z, s in self.status.items() if s != "broken"]:
            z = self.zones[zid]
            if z.level_low is None or z.level_high is None:
                continue
            side = _ZONE_SIDE.get(z.kind, "neutral")
            broken = (
                (side == "bullish" and c.close < z.level_low)
                or (side == "bearish" and c.close > z.level_high)
                or (side == "neutral" and (c.close < z.level_low
                                           or c.close > z.level_high)))
            if broken:
                self.status[zid] = "broken"
            elif c.low <= z.level_high and c.high >= z.level_low:
                self.status[zid] = "touched"

    def snapshot_statuses(self, lo: int, hi: int) -> dict[int, str]:
        """Statuses for zones [lo, hi): tracked + newly-confirmed (zones
        confirmed at t-1 fold nothing yet -> untested)."""
        out = {}
        for j in range(lo, hi):
            out[j] = self.status.get(j, "untested")
        return out


def _fold_statuses(zones: Sequence[Detection], candles: Sequence[Candle],
                   t: int) -> tuple[_ZoneTracker, list[Detection]]:
    """Reference zone-status computation for target t (fresh fold).

    Zone confirmed at c folds candles c+1 .. t-1 (classify semantics).
    """
    tr = _ZoneTracker()
    avail = [z for z in zones if z.confirm_index <= t - 1]
    ptr = 0
    for i in range(1, t + 1):
        # zones with confirm <= i-2 may be folded with candle i-1
        while ptr < len(avail) and avail[ptr].confirm_index <= i - 2:
            tr.add(ptr, avail[ptr])
            ptr += 1
        tr.advance(candles[i - 1])
    return tr, avail


class FeatureContext:
    """Precomputed detector outputs over a FIXED candle list (any superset
    of the prefix needed by the requested snapshot t)."""

    def __init__(self, candles: Sequence[Candle], params: dict | None = None):
        validate_candles(list(candles))
        self.candles = list(candles)
        self.p = dict(DEFAULT_PARAMS)
        if params:
            self.p.update(params)
        k = self.p["swing_k"]

        def _sorted(dets):
            return sorted(dets, key=lambda d: d.confirm_index)

        self.swings = _sorted(market_structure.detect_swings(self.candles, k=k))
        self.breaks = _sorted(market_structure.detect_structure_breaks(
            self.candles, k=k, swings=self.swings))
        self.trend = _sorted(market_structure.detect_trend_transitions(
            self.candles, k=k, swings=self.swings))
        self.patterns = _sorted(candlestick.detect_patterns(self.candles))
        self.indicators = momentum.compute_indicators(self.candles)
        self.divergences = _sorted(momentum.detect_divergences(self.candles))
        self.ranges = _sorted(liquidity.detect_ranges(self.candles))
        self.sweeps = _sorted(liquidity.detect_sweeps(self.candles))
        self.amd = _sorted(liquidity.detect_amd(self.candles))
        self.volume_delta = orderflow.volume_delta_series(self.candles)
        self.flow = _sorted(orderflow.detect_flow_anomalies(self.candles))
        self.activity = _sorted(orderflow.tick_intensity_flags(self.candles))
        self.zones = _sorted(
            list(poi_zones.detect_fvg(self.candles))
            + list(poi_zones.detect_order_blocks(self.candles))
            + list(poi_zones.detect_supply_demand(self.candles))
            + list(poi_zones.detect_equal_levels(self.candles))
            + list(poi_zones.detect_key_levels(self.candles)))
        # confirm-index arrays for O(log n) rightmost-<= lookups
        self._conf = {name: [d.confirm_index for d in getattr(self, name)]
                      for name in ("swings", "breaks", "trend", "patterns",
                                   "divergences", "ranges", "sweeps", "amd",
                                   "flow", "activity", "zones")}
        self._swing_high = [d for d in self.swings if d.kind == "swing_high"]
        self._swing_low = [d for d in self.swings if d.kind == "swing_low"]
        self._absorption = [d for d in self.flow if d.kind == "absorption"]
        self._effort = [d for d in self.flow if d.kind == "effort_anomaly"]
        self._pat_conf = [d.confirm_index for d in self.patterns]
        self._zone_conf = [d.confirm_index for d in self.zones]

    # -- helpers ------------------------------------------------------------

    def _last(self, dets: Sequence[Detection],
              conf: Sequence[int], t: int) -> Optional[Detection]:
        i = bisect_right(conf, t - 1)
        return dets[i - 1] if i > 0 else None

    def _recent(self, dets: Sequence[Detection],
                conf: Sequence[int], t: int, window: int) -> list[Detection]:
        hi = bisect_right(conf, t - 1)
        lo = bisect_right(conf, t - 1 - window)
        return dets[lo:hi]

    def _recent_range(self, t: int) -> Optional[float]:
        w = self.p["recent_range_window"]
        lo = max(0, t - w)
        seg = self.candles[lo:t]
        if not seg:
            return None
        return max(c.high for c in seg) - min(c.low for c in seg)

    # -- the snapshot --------------------------------------------------------

    def snapshot(self, t: int, events: Sequence[NewsEvent] | None = None,
                 _zone_state: tuple[dict, list, int] | None = None) -> dict:
        """Feature dict for predicting candle ``t`` (target ts read for
        ex-ante session features only — its OHLC is never used)."""
        if not (1 <= t < len(self.candles)):
            raise IndexError(f"t must be in [1, {len(self.candles) - 1}]")
        c_prev = self.candles[t - 1]
        f: dict[str, Any] = {}
        zw = self.p["zone_window"]

        # structure --------------------------------------------------------
        tr = self._last(self.trend, self._conf["trend"], t)
        f["structure.trend"] = tr.meta.get("new") if tr else None
        f["structure.trend_age"] = (t - 1 - tr.confirm_index) if tr else None
        sh = self._last(self._swing_high, [d.confirm_index for d in self._swing_high], t)
        sl = self._last(self._swing_low, [d.confirm_index for d in self._swing_low], t)
        f["structure.last_swing_high"] = sh.meta.get("level") if sh else None
        f["structure.swing_high_age"] = (t - 1 - sh.confirm_index) if sh else None
        f["structure.last_swing_low"] = sl.meta.get("level") if sl else None
        f["structure.swing_low_age"] = (t - 1 - sl.confirm_index) if sl else None
        br = self._last(self.breaks, self._conf["breaks"], t)
        f["structure.last_break"] = br.kind if br else None
        f["structure.break_age"] = (t - 1 - br.confirm_index) if br else None

        # zones -------------------------------------------------------------
        zw = self.p["zone_window"]
        win_lo = bisect_right(self._zone_conf, t - 1 - zw)
        if _zone_state is not None:
            statuses, avail, avail_count = _zone_state
        else:
            trk, avail = _fold_statuses(self.zones, self.candles, t)
            statuses = trk.snapshot_statuses(win_lo, len(avail))
            avail_count = len(avail)
        rr = self._recent_range(t) or 1e-9

        def _nearest(prefix: str, kinds: tuple[str, ...]) -> None:
            best = None
            for j in range(max(win_lo, 0), avail_count):
                z = avail[j]
                if z.kind not in kinds:
                    continue
                if not (t - zw <= z.confirm_index <= t - 1):
                    continue
                if statuses.get(j, "untested") == "broken":
                    continue
                if z.level_low is None:
                    continue
                mid = (z.level_low + (z.level_high or z.level_low)) / 2.0
                d = mid - c_prev.close
                if prefix.endswith("supply") and d <= 0:
                    continue
                if prefix.endswith("demand") and d >= 0:
                    continue
                ad = abs(d)
                if best is None or ad < best[0]:
                    best = (ad, j, z, mid)
            if best is None:
                f[prefix] = None
                return
            _, j, z, mid = best
            f[prefix] = {
                "kind": z.kind, "level_low": z.level_low,
                "level_high": z.level_high, "mid": round(mid, 8),
                "dist_atr": round((mid - c_prev.close) / rr, 6),
                "status": statuses.get(j, "untested"),
                "age": t - 1 - z.confirm_index,
            }

        _nearest("poi.nearest_supply", _SUPPLY_KINDS)
        _nearest("poi.nearest_demand", _DEMAND_KINDS)
        for kind in ("demand_zone", "supply_zone", "order_block_bullish",
                     "order_block_bearish", "fvg_bullish", "fvg_bearish"):
            f[f"poi.{kind}_active"] = sum(
                1 for j in range(max(win_lo, 0), avail_count)
                if avail[j].kind == kind
                and t - zw <= avail[j].confirm_index <= t - 1
                and statuses.get(j, "untested") != "broken")

        # patterns -----------------------------------------------------------
        pw = self.p["pattern_window"]
        recent_pats = self._recent(self.patterns, self._pat_conf, t, pw)
        lp = recent_pats[-1] if recent_pats else (
            self._last(self.patterns, self._pat_conf, t))
        f["pattern.last_kind"] = lp.kind if lp else None
        f["pattern.last_side"] = lp.side if lp else None
        f["pattern.last_age"] = (t - 1 - lp.confirm_index) if lp else None
        f["pattern.bull_count_5"] = sum(1 for d in recent_pats if d.side == "bullish")
        f["pattern.bear_count_5"] = sum(1 for d in recent_pats if d.side == "bearish")

        # momentum -----------------------------------------------------------
        ind = self.indicators[t - 1] if t - 1 < len(self.indicators) else {}
        f["mom.rsi"] = ind.get("rsi")
        f["mom.roc"] = ind.get("roc")
        f["mom.stoch_k"] = ind.get("stoch_k")
        f["mom.velocity"] = ind.get("velocity")
        dv = self._last(self.divergences, self._conf["divergences"], t)
        f["mom.div_side"] = dv.side if dv else None
        f["mom.div_age"] = (t - 1 - dv.confirm_index) if dv else None

        # liquidity / AMD -----------------------------------------------------
        rg = self._last(self.ranges, self._conf["ranges"], t)
        if rg is not None and rg.level_low is not None and rg.level_high is not None:
            f["liq.in_range"] = bool(rg.level_low <= c_prev.close <= rg.level_high)
            f["liq.range_low"] = rg.level_low
            f["liq.range_high"] = rg.level_high
        else:
            f["liq.in_range"] = None
            f["liq.range_low"] = None
            f["liq.range_high"] = None
        sw = self._last(self.sweeps, self._conf["sweeps"], t)
        f["liq.last_sweep"] = sw.kind if sw else None
        f["liq.sweep_age"] = (t - 1 - sw.confirm_index) if sw else None
        am = self._last(self.amd, self._conf["amd"], t)
        f["liq.last_amd"] = am.kind if am else None
        f["liq.amd_age"] = (t - 1 - am.confirm_index) if am else None

        # ex-ante session features for the TARGET candle ----------------------
        sess = sessions.session_at(self.candles[t].ts)
        f["session.target_session"] = sess["session"]
        f["session.target_kill_zone"] = sess["kill_zone"]

        # orderflow -----------------------------------------------------------
        f["flow.last_delta"] = (self.volume_delta[t - 1]
                                if t - 1 < len(self.volume_delta) else None)
        ab = self._last(self._absorption, [d.confirm_index for d in self._absorption], t)
        ef = self._last(self._effort, [d.confirm_index for d in self._effort], t)
        ac = self._last(self.activity, self._conf["activity"], t)
        f["flow.absorption_age"] = (t - 1 - ab.confirm_index) if ab else None
        f["flow.effort_age"] = (t - 1 - ef.confirm_index) if ef else None
        f["flow.activity_spike_age"] = (t - 1 - ac.confirm_index) if ac else None

        # news (optional, ex-ante) ---------------------------------------------
        if events:
            m = minutes_to_event(self.candles[t].ts, events, impact="high")
            f["news.minutes_to_high_impact"] = m[0] if m else None
            f["news.provided"] = True
        else:
            f["news.minutes_to_high_impact"] = None
            f["news.provided"] = False
        return f


def snapshot_at(candles: Sequence[Candle], t: int,
                events: Sequence[NewsEvent] | None = None,
                params: dict | None = None) -> dict:
    """Reference path: context over candles[:t+1] (target candle included
    ONLY for its ts — its OHLC is never read by the snapshot)."""
    if not (1 <= t < len(candles)):
        raise IndexError(f"t must be in [1, {len(candles) - 1}]")
    ctx = FeatureContext(list(candles)[: t + 1], params=params)
    return ctx.snapshot(t, events=events)


def build_feature_matrix(candles: Sequence[Candle],
                         events: Sequence[NewsEvent] | None = None,
                         params: dict | None = None,
                         start: int | None = None,
                         end: int | None = None) -> list[dict]:
    """Rows {"t", "ts", "features"} for t in [start or 1, end or len-1].

    Incremental fast path: the zone tracker folds ONE candle per row.
    Equivalence with ``snapshot(t)`` is pinned by the L5 battery.
    """
    ctx = FeatureContext(candles, params=params)
    lo = max(1, start or 1)
    hi = min(len(ctx.candles) - 1, end if end is not None else len(ctx.candles) - 1)
    rows: list[dict] = []
    tracker = _ZoneTracker()
    ptr = 0            # zones added to tracker (confirm <= t-2)
    avail_hi = 0       # zones available (confirm <= t-1)
    nz = len(ctx.zones)
    for t in range(lo, hi + 1):
        while ptr < nz and ctx.zones[ptr].confirm_index <= t - 2:
            tracker.add(ptr, ctx.zones[ptr])
            ptr += 1
        tracker.advance(ctx.candles[t - 1])
        while avail_hi < nz and ctx.zones[avail_hi].confirm_index <= t - 1:
            avail_hi += 1
        win_lo = bisect_right(ctx._zone_conf, t - 1 - ctx.p["zone_window"])
        statuses = tracker.snapshot_statuses(win_lo, avail_hi)
        avail = ctx.zones[:avail_hi]
        feat = ctx.snapshot(t, events=events,
                            _zone_state=(statuses, avail, avail_hi))
        rows.append({"t": t, "ts": ctx.candles[t].ts, "features": feat})
    return rows


__all__ = ["FeatureContext", "snapshot_at", "build_feature_matrix",
           "DEFAULT_PARAMS"]
