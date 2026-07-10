"""
Simulated Quotex Feed — replaces pyquotex with realistic tick generation.
Same message protocol as feed.py: snapshot, tick, eoc.
When pyquotex is available, the real feed.py takes over.
"""
import asyncio
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field

import db as _db
from analyze_eoc import analyze_eoc, _round_level, _key_levels, _parse_votes, _atr

PAYOUT_FLOOR = int(os.environ.get("QX_PAYOUT_FLOOR", "81"))
ENABLE_LIVE_THEORY = os.environ.get("ENABLE_LIVE_THEORY", "1") == "1"
ENABLE_STRENGTH_GATE = os.environ.get("ENABLE_STRENGTH_GATE", "1") == "1"
# Signal delay (2026-07-10): withhold prediction for N seconds after candle
# open so opening ticks can confirm gap direction. Mirrors feed.py.
SIGNAL_DELAY_SEC = float(os.environ.get("SIGNAL_DELAY_SEC", "3.0"))
ZONE_LOSS_GUARD = 3

_SIM_PAIRS = [
    {"asset": "EURUSD_otc", "display": "EUR/USD", "status": "otc", "payout": 87, "locked": False},
    {"asset": "GBPUSD_otc", "display": "GBP/USD", "status": "otc", "payout": 85, "locked": False},
    {"asset": "USDJPY_otc", "display": "USD/JPY", "status": "otc", "payout": 83, "locked": False},
    {"asset": "AUDUSD_otc", "display": "AUD/USD", "status": "otc", "payout": 82, "locked": False},
    {"asset": "EURGBP_otc", "display": "EUR/GBP", "status": "otc", "payout": 84, "locked": False},
    {"asset": "GBPJPY_otc", "display": "GBP/JPY", "status": "otc", "payout": 86, "locked": False},
    {"asset": "EURJPY_otc", "display": "EUR/JPY", "status": "otc", "payout": 82, "locked": False},
    {"asset": "NZDUSD_otc", "display": "NZD/USD", "status": "otc", "payout": 80, "locked": True},
    {"asset": "USDCAD_otc", "display": "USD/CAD", "status": "otc", "payout": 83, "locked": False},
    {"asset": "EURCHF_otc", "display": "EUR/CHF", "status": "otc", "payout": 81, "locked": False},
]

# Base prices for simulation
_BASE_PRICES = {
    "EURUSD_otc": 1.08450, "GBPUSD_otc": 1.27150, "USDJPY_otc": 161.250,
    "AUDUSD_otc": 0.67350, "EURGBP_otc": 0.85280, "GBPJPY_otc": 204.850,
    "EURJPY_otc": 174.850, "NZDUSD_otc": 0.61050, "USDCAD_otc": 1.36450,
    "EURCHF_otc": 0.94280,
}

_PIP = {
    "EURUSD_otc": 0.0001, "GBPUSD_otc": 0.0001, "USDJPY_otc": 0.01,
    "AUDUSD_otc": 0.0001, "EURGBP_otc": 0.0001, "GBPJPY_otc": 0.01,
    "EURJPY_otc": 0.01, "NZDUSD_otc": 0.0001, "USDCAD_otc": 0.0001,
    "EURCHF_otc": 0.0001,
}


@dataclass
class _AssetStream:
    asset: str
    period: int
    candles: list = field(default_factory=list)
    ticks: deque = field(default_factory=lambda: deque(maxlen=500))
    candle_open_time: int = 0
    candle_open_price: float = 0.0
    candle_open_is_real: bool = False
    last_tick_ts: float = 0.0
    last_real_tick_wall: float = 0.0
    prediction: dict | None = None
    zone_streak: dict = field(default_factory=lambda: {"regime": None, "zone": None, "losses": 0})
    payout: int | None = None
    sub_started: bool = False
    task: object = None
    always_on: bool = False
    interested_cids: set = field(default_factory=set)
    idle_since: float | None = None
    created_at: float = field(default_factory=time.time)
    base_candles: list = field(default_factory=list)
    base_ticks: list = field(default_factory=list)
    _live_reeval_ticks: int = 0
    # Last-10s optimization (2026-07-10)
    cached_accuracy: tuple = field(default_factory=lambda: (None, 0))
    cached_accuracy_at: float = 0.0
    inverted: bool = False   # adaptive-inversion hysteresis, mirrors feed.py
    live_signal_history: list = field(default_factory=list)
    # Signal delay (2026-07-10)
    signal_delay_until: float = 0.0
    # Sim-specific
    _sim_price: float = 0.0
    _sim_momentum: float = 0.0
    _sim_volatility: float = 0.0


class QuotexFeed:
    def __init__(self):
        self._connected = False
        self._broadcast = None
        self._streams: dict[tuple[str, int], _AssetStream] = {}
        self._max_streams = 45
        self._pairs_list = list(_SIM_PAIRS)
        self._muted_theories: dict[str, str] = {}
        self._last_perf_refresh = 0.0
        self._last_db_cleanup = 0.0
        self._last_pairs_refresh = 0.0

    def available_pairs(self) -> dict:
        return {"pairs": self._pairs_list, "payout_floor": PAYOUT_FLOOR}

    def snapshot(self, asset: str, period: int) -> dict | None:
        stream = self._streams.get((asset, period))
        if not stream or not stream.candles:
            return None
        return {
            "type": "snapshot", "asset": stream.asset, "period": stream.period,
            "candles": stream.candles[-300:], "prediction": stream.prediction,
        }

    async def ensure_stream(self, asset: str, period: int, cid: str | None = None) -> dict:
        key = (asset, period)
        stream = self._streams.get(key)
        if stream is not None:
            if cid:
                stream.interested_cids.add(cid)
                for k, s in self._streams.items():
                    if k != key:
                        s.interested_cids.discard(cid)
            stream.idle_since = None
            # Signal delay (2026-07-10): a joiner inside the opening-tick
            # confirmation window gets prediction=None (PENDING), same gate
            # a live viewer would see — mirrors feed.py.
            gated_prediction = stream.prediction
            if (stream.signal_delay_until > 0
                    and time.time() < stream.signal_delay_until):
                gated_prediction = None
            return {"type": "snapshot", "ok": True, "status": "streaming",
                    "asset": asset, "period": period,
                    "candles": stream.candles[-300:], "prediction": gated_prediction}

        pair = next((p for p in self._pairs_list if p["asset"] == asset), None)
        if pair and pair.get("locked"):
            return {"ok": False, "status": "locked", "payout": pair.get("payout"),
                    "reason": f"Needs {PAYOUT_FLOOR}%"}

        stream = _AssetStream(asset=asset, period=period)
        if cid:
            stream.interested_cids.add(cid)
        self._streams[key] = stream
        stream.task = asyncio.create_task(self._run_stream(stream))
        return {"ok": True, "status": "starting"}

    async def drop_interest(self, cid: str) -> None:
        for s in self._streams.values():
            s.interested_cids.discard(cid)

    def stream_status(self) -> dict:
        now = time.time()
        return {
            "active": [{"asset": s.asset, "period": s.period,
                        "viewers": len(s.interested_cids),
                        "age_sec": round(now - s.created_at)}
                       for s in self._streams.values()],
            "count": len(self._streams), "max": self._max_streams,
            "cooldown_until": None, "cooldown_reason": None,
        }

    async def shutdown(self) -> None:
        for s in list(self._streams.values()):
            if s.task:
                s.task.cancel()

    # ── Simulated history generation ────────────────────────────────────────

    def _gen_history(self, asset: str, n: int = 120) -> list[dict]:
        """Generate realistic 1m candle history."""
        base = _BASE_PRICES.get(asset, 1.0)
        pip = _PIP.get(asset, 0.0001)
        now = int(time.time())
        period = 60
        start = now - n * period
        candles = []
        price = base
        # Slight trend bias
        trend = random.uniform(-0.3, 0.3) * pip

        for i in range(n):
            t = start + i * period
            volatility = pip * random.uniform(1.5, 6.0)
            # Random walk with mean reversion
            change = random.gauss(trend, volatility)
            # Occasional bigger moves
            if random.random() < 0.08:
                change *= random.uniform(2, 4)
            o = price
            # Intra-candle path
            hi = max(o, o + change) + abs(random.gauss(0, volatility * 0.3))
            lo = min(o, o + change) - abs(random.gauss(0, volatility * 0.3))
            c = o + change
            # Mean reversion toward base
            if abs(c - base) > pip * 20:
                change -= (c - base) * 0.05
                c = o + change
            c = round(c, 5)
            o = round(o, 5)
            hi = round(hi, 5)
            lo = round(lo, 5)
            candles.append({"time": t, "open": o, "high": hi, "low": lo, "close": c})
            price = c
            # Slowly drift trend
            trend += random.gauss(0, pip * 0.1)
            trend = max(-pip * 0.5, min(pip * 0.5, trend))

        return candles

    def _gen_tick(self, stream: _AssetStream) -> float:
        """Generate next realistic tick price."""
        pip = _PIP.get(stream.asset, 0.0001)
        price = stream._sim_price

        # Momentum: tends to continue in same direction
        stream._sim_momentum *= 0.97  # decay
        stream._sim_momentum += random.gauss(0, pip * 0.15)

        # Volatility clustering
        stream._sim_volatility = stream._sim_volatility * 0.95 + abs(random.gauss(0, pip * 0.3)) * 0.05
        vol = max(pip * 0.2, stream._sim_volatility)

        # Mean reversion
        base = _BASE_PRICES.get(stream.asset, 1.0)
        reversion = (base - price) * 0.002

        change = stream._sim_momentum + random.gauss(0, vol) + reversion

        # Occasional spikes
        if random.random() < 0.005:
            change += random.gauss(0, pip * 3)

        new_price = price + change
        new_price = round(new_price, 5)
        stream._sim_price = new_price
        return new_price

    # ── Microstructure analysis (same as real feed.py) ─────────────────────

    def _analyze_microstructure(self, ticks, open_price):
        ticks = list(ticks)
        if len(ticks) < 10:
            return None
        op = open_price
        hi = max(ticks)
        lo = min(ticks)
        cur = ticks[-1]
        rng = hi - lo

        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        moves = up_t + dn_t
        buy_pct = round(up_t / moves * 100) if moves else 50
        sell_pct = 100 - buy_pct

        pressure = "BUYER" if buy_pct >= 62 else ("SELLER" if sell_pct >= 62 else "FIGHT")

        mid = (hi + lo) / 2
        crosses = sum(1 for i in range(1, len(ticks))
                      if (ticks[i - 1] < mid) != (ticks[i] < mid))
        is_fight = crosses >= 4

        hold_price = None
        hold_visits = 0
        if rng > 0:
            bin_size = rng / 8
            bins = {}
            for t in ticks:
                b = int((t - lo) / bin_size)
                bins[b] = bins.get(b, 0) + 1
            top_bin = max(bins, key=bins.get)
            hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
            hold_visits = bins[top_bin]
        else:
            hold_price = round(cur, 6)
            hold_visits = len(ticks)

        n = len(ticks)
        t3 = max(n // 3, 1)
        early = ticks[t3] - ticks[0]
        mid_m = ticks[2 * t3] - ticks[t3]
        late = ticks[-1] - ticks[2 * t3]

        def _dir(v):
            return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

        phases = [_dir(early), _dir(mid_m), _dir(late)]

        reaction = None
        if rng > 0:
            from_hi = (hi - cur) / rng
            from_lo = (cur - lo) / rng
            net = cur - op
            late_q = max(n // 4, 2)
            late_move = ticks[-1] - ticks[-late_q]
            if from_hi > 0.50 and late_move <= 0 and net < 0:
                reaction = "SELLER"
            elif from_lo > 0.50 and late_move >= 0 and net > 0:
                reaction = "BUYER"

        last_react = None
        if n >= 15:
            last_n2 = max(n // 6, 6)
            fin2 = ticks[-last_n2:]
            fi2_up = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i - 1])
            fi2_dn = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i - 1])
            fi2_tot = fi2_up + fi2_dn
            if fi2_tot >= 3:
                fbp2 = fi2_up / fi2_tot
                net_run = cur - op
                if net_run > 0:
                    if fbp2 <= 0.30 or (fi2_tot >= 5 and fbp2 >= 0.90):
                        last_react = "EXHAUST"
                    elif 0.55 <= fbp2 <= 0.85 and fi2_dn >= 2:
                        last_react = "RECOVERY"
                elif net_run < 0:
                    if fbp2 >= 0.70 or (fi2_tot >= 5 and fbp2 <= 0.10):
                        last_react = "EXHAUST"
                    elif 0.15 <= fbp2 <= 0.45 and fi2_up >= 2:
                        last_react = "RECOVERY"

        cur_lvl, _, cur_str = _round_level(cur)
        hi_lvl, _, hi_str = _round_level(hi)
        lo_lvl, _, lo_str = _round_level(lo)
        round_info = {
            "near_level": cur_lvl,
            "near_strength": cur_str,
            "hi_level": hi_lvl if hi_str in ("BIG", "MID") else None,
            "hi_strength": hi_str if hi_str in ("BIG", "MID") else None,
            "lo_level": lo_lvl if lo_str in ("BIG", "MID") else None,
            "lo_strength": lo_str if lo_str in ("BIG", "MID") else None,
        }

        return {
            "buy_pct": buy_pct, "sell_pct": sell_pct, "pressure": pressure,
            "is_fight": is_fight, "crosses": crosses,
            "hold_price": hold_price, "hold_visits": hold_visits,
            "phases": phases, "reaction": reaction,
            "net": round(cur - op, 6), "tick_count": n,
            "last_react": last_react, "round": round_info,
        }

    # ── EOC analysis (same logic as feed.py) ──────────────────────────────

    def _analyze_core(self, asset, period, candles, ticks, running_ticks=None,
                      stream=None):
        if len(candles) < 5:
            return None, []
        micro_hist = _db.get_micro_history(asset, period, n=5, before_ctime=candles[-1]["time"])
        # Use cached accuracy if stream provided (avoids DB query on every
        # LIVE re-eval). Fall back to DB query for background trackers.
        if stream is not None:
            acc, n_acc = stream.cached_accuracy
        else:
            try:
                acc, n_acc = _db.recent_accuracy(asset, period, n=20)
            except Exception:
                acc, n_acc = None, 0
        result = analyze_eoc(candles, ticks, micro_history=micro_hist, period=period,
                             muted=self._muted_theories, asset=asset,
                             running_ticks=running_ticks if ENABLE_LIVE_THEORY else None,
                             recent_accuracy=acc, recent_n=n_acc,
                             currently_flipped=stream.inverted if stream is not None else False)
        return result, micro_hist

    async def _run_eoc(self, stream, actual_open=None):
        closed = stream.candles
        base_ticks = list(stream.ticks)
        # Refresh per-candle accuracy cache ONCE here (at candle open).
        # asyncio.to_thread: sqlite3 I/O would otherwise block the shared
        # event loop for every concurrent stream (2026-07-10).
        try:
            stream.cached_accuracy = await asyncio.to_thread(
                _db.recent_accuracy, stream.asset, stream.period, n=20)
            stream.cached_accuracy_at = time.time()
        except Exception:
            stream.cached_accuracy = (None, 0)
            stream.cached_accuracy_at = time.time()
        # Reset flip-suppression tracker for new candle.
        stream.live_signal_history = []
        result, micro_hist = self._analyze_core(stream.asset, stream.period,
                                                  closed, base_ticks, stream=stream)
        if result is None:
            return None
        stream.inverted = result.get("_flipped", False)
        stream.base_candles = closed
        stream.base_ticks = base_ticks
        stream._live_reeval_ticks = 0

        _reg = result.get("regime") or {}
        _key = (_reg.get("trend"), _reg.get("zone"))
        if (result["signal"] != "NEUTRAL"
                and _key == (stream.zone_streak["regime"], stream.zone_streak["zone"])
                and stream.zone_streak["losses"] >= ZONE_LOSS_GUARD):
            result["strength"] = "WEAK"
            result.setdefault("reasons", []).append(
                f"CHOP GUARD: {_key[0]}/{_key[1]} wrong "
                f"{stream.zone_streak['losses']}x running -> WEAK")

        if result["signal"] == "NEUTRAL":
            return {**result, "candle": None, "payout": stream.payout}

        # Prediction candle
        last = closed[-1]
        op = actual_open if actual_open is not None else last["close"]
        atr = _atr(closed[-20:]) if len(closed) >= 20 else (last["high"] - last["low"]) or 0.0001
        t = last["time"] + stream.period
        body = atr * 0.45
        wick = atr * 0.25
        tail = atr * 0.15
        if result["signal"] == "CALL":
            candle = {"time": t, "open": op, "high": round(op + body + wick, 6),
                      "low": round(op - tail, 6), "close": round(op + body, 6)}
        else:
            candle = {"time": t, "open": op, "high": round(op + tail, 6),
                      "low": round(op - body - wick, 6), "close": round(op - body, 6)}
        return {**result, "candle": candle, "payout": stream.payout}

    def _accuracy(self, just_closed, pred):
        if not pred or pred["signal"] not in ("CALL", "PUT"):
            return None
        if just_closed["close"] == just_closed["open"]:
            return "draw"
        actual_up = just_closed["close"] > just_closed["open"]
        pred_up = pred["signal"] == "CALL"
        return "correct" if actual_up == pred_up else "wrong"

    def _grade_and_log(self, asset, period, closed, prediction, micro_snap, candles):
        accuracy = self._accuracy(closed, prediction)
        if not prediction:
            return accuracy
        try:
            import json as _json
            reasons = prediction.get("reasons", [])
            is_draw = closed["close"] == closed["open"]
            actual_up = closed["close"] > closed["open"]
            _net = {}
            for code, vdir, mag in _parse_votes(reasons):
                _net[code] = _net.get(code, 0) + vdir * mag
            votes = []
            fired, right, wrong = set(), set(), set()
            for code, net in _net.items():
                fired.add(code)
                if net == 0:
                    continue
                voted_up = net > 0
                if is_draw:
                    outcome = "draw"
                else:
                    outcome = "right" if voted_up == actual_up else "wrong"
                    (right if outcome == "right" else wrong).add(code)
                votes.append((code, "CALL" if voted_up else "PUT", abs(net), outcome))
            if not accuracy:
                _db.log_theory_votes(asset, period, closed["time"], votes)
                return accuracy
            _reg = (prediction.get("regime") or {})
            regime, zone = _reg.get("trend"), _reg.get("zone")
            sig = prediction["signal"]
            tags = []
            if is_draw:
                tags.append("DRAW")
            move = closed["close"] - closed["open"]
            pm = f"{sig} s={prediction['score']:+d} {prediction.get('strength')} agree={prediction.get('agree')}"
            if fired:
                _db.log_signal(asset, period, closed["time"], sig, prediction["score"],
                               prediction["confidence"], ",".join(sorted(fired)),
                               "UP" if actual_up else ("FLAT" if is_draw else "DOWN"),
                               accuracy, strength=prediction.get("strength"),
                               agree=prediction.get("agree"),
                               right_codes=",".join(sorted(right)),
                               wrong_codes=",".join(sorted(wrong)),
                               reasons=_json.dumps(reasons),
                               a_open=closed["open"], a_close=closed["close"],
                               regime=regime, zone=zone,
                               tags=",".join(tags), postmortem=pm, votes=votes)
        except Exception as _e:
            print(f"[db] log_signal error: {_e}")
        return accuracy

    def _save_micro(self, asset, period, closed, micro_snap, candles, ticks):
        try:
            import json as _tick_json
            _gap_pct = 0.0
            _gap_type = "NONE"
            if len(candles) >= 2:
                _pc = candles[-2]["close"]
                if _pc > 0:
                    _raw_gap = closed["open"] - _pc
                    _gp = _raw_gap / _pc
                    if abs(_gp) >= 0.0001:
                        _gap_pct = _gp
                        _gap_type = "SIM"
            micro_snap["gap_pct"] = _gap_pct
            micro_snap["gap_type"] = _gap_type
            micro_snap["key_levels"] = _key_levels(candles)
            _tl = list(ticks)
            if len(_tl) > 240:
                _st = len(_tl) / 240
                _tl = [_tl[int(i * _st)] for i in range(240)]
            micro_snap["ticks_json"] = _tick_json.dumps([round(x, 6) for x in _tl])
            _db.save(asset, period, closed, micro_snap)
        except Exception as _me:
            print(f"[db] micro save error: {_me}")

    def _running_confirmation(self, stream):
        if not stream.prediction or len(stream.ticks) < 5:
            return None
        pred = stream.prediction.get("signal")
        if pred == "NEUTRAL":
            return None
        ticks = list(stream.ticks)
        open_p = stream.candle_open_price
        net = ticks[-1] - open_p
        mid = len(ticks) // 2
        first_half = ticks[mid] - ticks[0]
        second_half = ticks[-1] - ticks[mid]
        if first_half > 0 and second_half > 0:
            running_dir = "UP"
        elif first_half < 0 and second_half < 0:
            running_dir = "DOWN"
        else:
            running_dir = "UP" if net >= 0 else "DOWN"
        if (pred == "CALL" and running_dir == "UP") or (pred == "PUT" and running_dir == "DOWN"):
            return "CONFIRMING"
        return "OPPOSING"

    def _apply_strength_gate(self, stream, prediction):
        if not prediction or prediction.get("signal") not in ("CALL", "PUT"):
            return prediction
        conf = self._running_confirmation(stream)
        if conf is None or len(stream.ticks) < 10:
            return prediction
        current = prediction.get("strength", "WEAK")
        new_strength = current
        if current == "WEAK" and conf == "CONFIRMING":
            new_strength = "MEDIUM"
        elif current == "MEDIUM" and conf == "OPPOSING":
            new_strength = "WEAK"
        elif current == "STRONG" and conf == "OPPOSING":
            new_strength = "MEDIUM"
        else:
            return prediction
        new_pred = dict(prediction)
        new_pred["strength"] = new_strength
        return new_pred

    def _running_candle(self, stream):
        op = stream.candle_open_price
        ticks = list(stream.ticks)
        if not ticks:
            return {"time": stream.candle_open_time, "open": op, "high": op, "low": op, "close": op}
        return {"time": stream.candle_open_time, "open": op,
                "high": max(ticks), "low": min(ticks), "close": ticks[-1]}

    async def _close_running_and_start_new(self, stream, new_open_time, first_tick, open_is_real=True):
        if new_open_time <= stream.candle_open_time:
            return None
        closed = self._running_candle(stream)
        if stream.candles and stream.candles[-1]["time"] == closed["time"]:
            stream.candles[-1] = closed
        elif not stream.candles or stream.candles[-1]["time"] < closed["time"]:
            stream.candles.append(closed)
        if len(stream.candles) > 500:
            stream.candles = stream.candles[-400:]
        _micro_snap = self._analyze_microstructure(stream.ticks, stream.candle_open_price) if len(stream.ticks) >= 10 else None
        accuracy = self._grade_and_log(stream.asset, stream.period, closed, stream.prediction, _micro_snap, stream.candles)
        if accuracy in ("correct", "wrong"):
            _reg = (stream.prediction or {}).get("regime") or {}
            _key = (_reg.get("trend"), _reg.get("zone"))
            if _key == (stream.zone_streak["regime"], stream.zone_streak["zone"]):
                stream.zone_streak["losses"] = stream.zone_streak["losses"] + 1 if accuracy == "wrong" else 0
            else:
                stream.zone_streak = {"regime": _key[0], "zone": _key[1], "losses": 1 if accuracy == "wrong" else 0}
        stream.prediction = await self._run_eoc(stream, actual_open=first_tick)
        # Signal delay (2026-07-10): withhold prediction broadcast for
        # SIGNAL_DELAY_SEC seconds after the new candle opens.
        stream.signal_delay_until = time.time() + SIGNAL_DELAY_SEC
        if _micro_snap:
            self._save_micro(stream.asset, stream.period, closed, _micro_snap, stream.candles, list(stream.ticks))
        stream.candle_open_time = new_open_time
        stream.candle_open_price = first_tick
        stream.candle_open_is_real = open_is_real
        stream.ticks.clear()
        stream.ticks.append(first_tick)
        return accuracy

    # ── Simulated stream lifecycle ─────────────────────────────────────────

    def _floor_to_period(self, ts, period):
        return (int(ts) // period) * period

    async def _run_stream(self, stream):
        key = (stream.asset, stream.period)
        try:
            # Generate history
            history = self._gen_history(stream.asset, 120)
            stream.candles = history
            last = history[-1]
            stream._sim_price = last["close"]
            stream._sim_momentum = 0.0
            pip = _PIP.get(stream.asset, 0.0001)
            stream._sim_volatility = pip * 1.0
            stream.candle_open_time = last["time"] + stream.period
            stream.candle_open_price = last["close"]
            stream.ticks.clear()
            stream.ticks.append(last["close"])
            stream.candle_open_is_real = False
            pair = next((p for p in self._pairs_list if p["asset"] == stream.asset), None)
            stream.payout = pair["payout"] if pair else None
            stream.prediction = await self._run_eoc(stream, actual_open=last["close"])
            # Initial subscription joins mid-candle — no signal delay.
            stream.signal_delay_until = 0.0
            await self._broadcast({
                "type": "snapshot", "asset": stream.asset, "period": stream.period,
                "candles": history, "prediction": stream.prediction,
            })
            print(f"[sim] started {stream.asset} with {len(history)} candles, price={stream._sim_price}")

            # Stream loop
            while True:
                now = time.time()
                current_period = self._floor_to_period(now, stream.period)

                # Check candle boundary
                if stream.candle_open_time > 0 and current_period != stream.candle_open_time:
                    last_px = list(stream.ticks)[-1] if stream.ticks else stream._sim_price
                    accuracy = await self._close_running_and_start_new(stream, current_period, last_px)
                    running = self._running_candle(stream)
                    all_c = stream.candles + [running]
                    # Signal delay (2026-07-10): withhold prediction at EOC;
                    # it will be delivered via the tick loop once the delay
                    # gate passes. Candle data + accuracy still flow.
                    await self._broadcast({
                        "type": "eoc", "asset": stream.asset, "period": stream.period,
                        "candles": all_c[-300:], "prediction": None,
                        "accuracy": accuracy,
                    })

                # Generate tick
                tick_price = self._gen_tick(stream)
                stream.ticks.append(tick_price)
                stream.last_real_tick_wall = time.time()

                # Build running candle + micro
                running = self._running_candle(stream)
                if not stream.candles or stream.candles[-1]["time"] < running["time"]:
                    stream.candles.append(running)

                # Micro analysis
                micro = self._analyze_microstructure(stream.ticks, stream.candle_open_price)

                # LIVE theory re-eval
                # Priority 1 (2026-07-10): ADAPTIVE refresh rate.
                #   - Last 5s: every 2 ticks (Priority 3 critical zone)
                #   - Last 10s: every 3 ticks
                #   - Last 30s: every 10 ticks
                #   - Earlier: every 30 ticks (cheap baseline)
                # Priority 3 (2026-07-10): volatility speedup — if last 3
                # ticks moved >0.5 ATR, cut interval in half.
                pred_changed = False
                if ENABLE_LIVE_THEORY and stream.base_candles and len(stream.ticks) >= 15:
                    if stream.candle_open_time > 0:
                        time_to_close = (stream.candle_open_time + stream.period) - time.time()
                        if time_to_close < 5:
                            reeval_interval = 2   # Priority 3: critical zone
                        elif time_to_close < 10:
                            reeval_interval = 3
                        elif time_to_close < 30:
                            reeval_interval = 10
                        else:
                            reeval_interval = 30
                    else:
                        reeval_interval = 30

                    # Priority 3: volatility speedup
                    if len(stream.ticks) >= 4 and reeval_interval > 2:
                        try:
                            recent = list(stream.ticks)[-4:]
                            recent_range = max(recent) - min(recent)
                            _atr_val = (_atr(stream.candles[-20:])
                                        if len(stream.candles) >= 20
                                        else 0.0001)
                            if _atr_val > 0 and recent_range > _atr_val * 0.5:
                                reeval_interval = max(2, reeval_interval // 2)
                        except Exception:
                            pass

                    if len(stream.ticks) - stream._live_reeval_ticks >= reeval_interval:
                        try:
                            fresh, _ = self._analyze_core(stream.asset, stream.period,
                                                           stream.base_candles, stream.base_ticks,
                                                           running_ticks=list(stream.ticks),
                                                           stream=stream)
                            stream._live_reeval_ticks = len(stream.ticks)
                            if fresh and fresh.get("signal") in ("CALL", "PUT"):
                                # Flip suppression (2026-07-10)
                                sig = fresh["signal"]
                                stream.live_signal_history.append(
                                    (len(stream.ticks), sig, time.time()))
                                cutoff = time.time() - 10
                                stream.live_signal_history = [
                                    h for h in stream.live_signal_history
                                    if h[2] >= cutoff]
                                dirs = [h[1] for h in stream.live_signal_history]
                                flips = 0
                                for i in range(1, len(dirs)):
                                    if dirs[i] != dirs[i-1]:
                                        flips += 1
                                if flips >= 3 and len(dirs) >= 4:
                                    fresh = dict(fresh)
                                    fresh["signal"] = "NEUTRAL"
                                    fresh["strength"] = "WEAK"
                                    fresh.setdefault("reasons", []).append(
                                        f"FLIP_SUPPRESS: {flips} flips in last 10s → NEUTRAL")
                                    stream.prediction = fresh
                                    pred_changed = True
                                    # NOTE: do NOT `continue` — fall through
                                    # to broadcast so client sees NEUTRAL.
                                elif flips >= 2 and len(dirs) >= 3:
                                    fresh = dict(fresh)
                                    if fresh.get("strength") != "WEAK":
                                        fresh["strength"] = "WEAK"
                                        fresh.setdefault("reasons", []).append(
                                            f"FLIP_DEMOTE: {flips} flips in last 10s → WEAK")
                                    stream.prediction = {**(stream.prediction or {}), **fresh}
                                    pred_changed = True
                                else:
                                    stream.prediction = {**(stream.prediction or {}), **fresh}
                                    pred_changed = True
                        except Exception as exc:
                            import traceback as tb
                            print(f"[sim] LIVE re-eval error: {exc}")
                            tb.print_exc()

                # Strength gate
                if ENABLE_STRENGTH_GATE and stream.prediction and stream.prediction.get("signal") != "NEUTRAL":
                    gated = self._apply_strength_gate(stream, stream.prediction)
                    if gated is not stream.prediction:
                        stream.prediction = gated
                        pred_changed = True

                # Broadcast tick
                msg = {
                    "type": "tick", "asset": stream.asset, "period": stream.period,
                    "candle": running, "running_conf": self._running_confirmation(stream),
                    "micro": micro,
                }
                # ── Signal delay gate (2026-07-10) ──────────────────────────
                # While the opening-tick confirmation window is still active,
                # do NOT broadcast the prediction. Candle data, micro, and
                # running_conf still flow. Once the gate passes, the FIRST
                # eligible tick broadcasts the prediction.
                now_ts = time.time()
                if stream.signal_delay_until > 0 and now_ts < stream.signal_delay_until:
                    # Still in delay window — withhold prediction
                    pass
                else:
                    if stream.signal_delay_until > 0:
                        # Gate just opened — clear it and force broadcast
                        stream.signal_delay_until = 0.0
                        pred_changed = True
                    if pred_changed:
                        msg["prediction"] = stream.prediction
                await self._broadcast(msg)

                # Tick interval: ~100ms (simulating real feed speed)
                await asyncio.sleep(random.uniform(0.08, 0.15))

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            import traceback as tb
            print(f"[sim] stream {key} error: {exc}")
            tb.print_exc()
        finally:
            self._streams.pop(key, None)
            print(f"[sim] stream {key} stopped")

    # ── Manager loop ───────────────────────────────────────────────────────

    async def run(self, broadcast):
        self._broadcast = broadcast
        _db.init()
        _db.cleanup()
        self._connected = True
        print("[sim] connected (simulation mode)")
        await self._broadcast({"type": "pairs", "pairs": self._pairs_list, "payout_floor": PAYOUT_FLOOR})

        while True:
            try:
                # Refresh theory mutes
                if time.time() - self._last_perf_refresh > 300:
                    self._last_perf_refresh = time.time()
                # Sweep idle
                now = time.time()
                for key, s in list(self._streams.items()):
                    if s.always_on:
                        continue
                    if s.interested_cids:
                        s.idle_since = None
                        continue
                    if s.idle_since is None:
                        s.idle_since = now
                    elif now - s.idle_since > 300:
                        if s.task:
                            s.task.cancel()
                        self._streams.pop(key, None)
            except Exception as exc:
                print(f"[sim] manager error: {exc}")
            await asyncio.sleep(5)