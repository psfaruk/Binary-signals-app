"""
End-of-Candle analysis engine  —  REWRITE (2026-07-10)

Multiple theories vote CALL/PUT. The blend produces the final signal.

KEY CHANGES vs previous version:
  1. TICK-WEIGHTED buyer/seller pressure (bigger moves = more weight)
  2. TICK-SPEED / acceleration analysis (momentum building or dying?)
  3. VOLUME PROFILE (where did price spend the most time?)
  4. MOMENTUM SHIFT detection (late direction change in running candle)
  5. CON theory now checks exhaustion (RSI extreme + candle size decay)
  6. Tighter dead band (|net|<1 instead of <2, confidence<40 instead of <45)
  7. REV theory checks if wick is at a KEY LEVEL (swing high/low)
  8. TRAP theory uses tick-weighted analysis too
  9. MICRO_BUILD theory — new, pure microstructure vote from closed candle ticks

Theories:
  CON   - Continuation (trend following + exhaustion check)
  REV   - Reversal (wick rejection + key-level context)
  RUN   - Running-candle microstructure (tick-weighted pressure + reaction + speed)
  TRAP  - Trap (fake move then reverse, tick-weighted)
  GAP   - Gap fill/rejection
  LAST  - Last-portion exhaustion/recovery (tick-weighted)
  RNG   - Round-number proximity bias
  MST   - Market-state classification
  MICRO - Closed-candle internal microstructure (NEW)
  SHIFT - Momentum shift detection (NEW)
  MEAN  - Mean-reversion detector (OTC-tuned)
  VELOCITY - Last 5/10 tick velocity + V-shape (LIVE)
  LIVE_WICK - Real-time wick rejection forming (LIVE)
  ORDERFLOW - Big-money vs retail tick-size disagreement (LIVE)
  MOMENTUM  - Multi-candle body-size growth/shrink patterns
  CONTINUITY - Cross-candle tick continuity
  HISTORY   - Previous 3 candles' microstructure
  FVG   - Fair Value Gap (gap-fill fade)             [LIQUIDITY, 2026-07-10]
  OB    - Order Block (last opposite candle as S/R)  [LIQUIDITY, 2026-07-10]
  SWEEP - Liquidity sweep / stop hunt (wick breaks swing, close reverses)
                                                        [LIQUIDITY, 2026-07-10]
  STRUCT - BOS / CHOCH structure break               [LIQUIDITY, 2026-07-10]
"""
import math
from collections import Counter


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _round_level(price):
    """Classify how close a price is to a 'round' psychological level."""
    if price <= 0:
        return None, 0, "NONE"
    decimals = max(0, -int(math.floor(math.log10(abs(price)))) + 2)
    big = round(price, max(0, decimals - 3))
    mid = round(price, max(0, decimals - 2))
    d_big = abs(price - big) if big else float("inf")
    d_mid = abs(price - mid) if mid else float("inf")
    if d_big < d_mid and d_big / price < 0.0003:
        return big, d_big, "BIG"
    if d_mid / price < 0.0003:
        return mid, d_mid, "MID"
    return None, 0, "NONE"


def _key_levels(candles):
    """Extract recent swing highs/lows as key levels."""
    if len(candles) < 5:
        return []
    levels = []
    for i in range(2, len(candles) - 2):
        c = candles[i]
        if (c["high"] >= candles[i-1]["high"] and c["high"] >= candles[i-2]["high"] and
            c["high"] >= candles[i+1]["high"] and c["high"] >= candles[i+2]["high"]):
            levels.append({"type": "swing_high", "price": c["high"], "idx": i})
        if (c["low"] <= candles[i-1]["low"] and c["low"] <= candles[i-2]["low"] and
            c["low"] <= candles[i+1]["low"] and c["low"] <= candles[i+2]["low"]):
            levels.append({"type": "swing_low", "price": c["low"], "idx": i})
    return levels[-10:]


def _parse_votes(reasons):
    """Parse reason strings like 'CON:+3 CALL' into (code, direction, magnitude)."""
    votes = []
    for r in reasons:
        if ":" not in str(r):
            continue
        code_part, rest = str(r).split(":", 1)
        code = code_part.strip()
        # Extract the numeric part (first token before the direction word)
        tokens = rest.split()
        if not tokens:
            continue
        first = tokens[0].lstrip("-+")
        mag = 1
        try:
            mag = abs(float(first))
        except ValueError:
            pass
        if "CALL" in rest.upper():
            votes.append((code, 1, mag))
        elif "PUT" in rest.upper():
            votes.append((code, -1, mag))
    return votes


def _atr(candles, n=20):
    if not candles:
        return 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    return sum(c["high"] - c["low"] for c in recent) / len(recent) or 0.0001


def _ema(prices, period):
    if not prices:
        return 0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(d if d > 0 else 0)
        losses.append(-d if d < 0 else 0)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def _classify_regime(candles):
    """Classify market regime using EMA crossover + RSI + ADX-like trending."""
    if len(candles) < 10:
        return {"trend": "SIDEWAYS", "zone": "UNKNOWN"}
    closes = [c["close"] for c in candles[-30:]]  # More data points
    ema9  = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi_val = _rsi(closes)

    # EMA separation as a percentage of price
    sep = abs(ema9 - ema21) / ema21 if ema21 > 0 else 0
    # Strong separation = strong trend
    strong_trend = sep > 0.0003  # ~3 pips for EUR/USD

    if ema9 > ema21 and rsi_val > 48:
        trend = "UPTREND" if strong_trend or rsi_val > 55 else "SIDEWAYS"
    elif ema9 < ema21 and rsi_val < 52:
        trend = "DOWNTREND" if strong_trend or rsi_val < 45 else "SIDEWAYS"
    else:
        trend = "SIDEWAYS"

    # Zone: where price sits relative to recent range
    recent = candles[-10:]
    hi = max(c["high"] for c in recent)
    lo = min(c["low"] for c in recent)
    rng = hi - lo
    if rng == 0:
        zone = "MID"
    else:
        pos = (closes[-1] - lo) / rng
        if pos > 0.80:
            zone = "HIGH"
        elif pos < 0.20:
            zone = "LOW"
        else:
            zone = "MID"

    return {"trend": trend, "zone": zone, "ema9": round(ema9, 6),
            "ema21": round(ema21, 6), "rsi": round(rsi_val, 1)}


# ═══════════════════════════════════════════════════════════════════════════════
#  TICK-WEIGHTED MICROSTRUCTURE BUILDER
#  This is the CORE improvement — every tick's SIZE matters, not just direction.
# ═══════════════════════════════════════════════════════════════════════════════

def _build_micro(ticks, open_price):
    """
    Build rich microstructure from a tick list.
    Used by BOTH the closed-candle MICRO theory AND the running-ticks RUN theory.
    """
    ticks = list(ticks)
    if len(ticks) < 10:
        return None
    op  = open_price
    hi  = max(ticks)
    lo  = min(ticks)
    cur = ticks[-1]
    rng = hi - lo
    n   = len(ticks)

    # ── 1. Tick-weighted buyer/seller pressure ─────────────────────────────
    # Instead of just counting up/down ticks, weight by the SIZE of each move.
    # A 5-pip up-tick matters more than a 0.1-pip up-tick.
    raw_buy_vol = 0.0
    raw_sell_vol = 0.0
    up_count  = 0
    dn_count  = 0
    for i in range(1, n):
        delta = ticks[i] - ticks[i-1]
        if delta > 0:
            raw_buy_vol += delta
            up_count += 1
        elif delta < 0:
            raw_sell_vol += abs(delta)
            dn_count += 1
    total_vol = raw_buy_vol + raw_sell_vol
    buy_pct  = round(raw_buy_vol / total_vol * 100) if total_vol > 0 else 50
    sell_pct = 100 - buy_pct
    # Also keep simple count-based for cross-reference
    count_buy_pct = round(up_count / (up_count + dn_count) * 100) if (up_count + dn_count) > 0 else 50

    # Divergence: if volume-weighted and count-weighted DISAGREE, that's a signal
    vol_count_diverge = abs(buy_pct - count_buy_pct) > 20

    if buy_pct >= 62:
        pressure = "BUYER"
    elif sell_pct >= 62:
        pressure = "SELLER"
    else:
        pressure = "FIGHT"

    # ── 1b. TIME-DECAY WEIGHTED pressure (Priority 2 fix, 2026-07-10) ───────
    # Recent ticks carry more weight than older ticks. In a 60s candle, the
    # last 15s of price action is far more predictive of the next candle's
    # direction than the first 15s. Apply a linear ramp: first tick gets
    # weight 1.0, last tick gets weight 5.0 — so late pressure dominates.
    td_buy_vol  = 0.0
    td_sell_vol = 0.0
    for i in range(1, n):
        delta = ticks[i] - ticks[i-1]
        # Linear time-decay weight: 1.0 at i=1, 5.0 at i=n-1
        w = 1.0 + (i - 1) / max(n - 2, 1) * 4.0
        if delta > 0:
            td_buy_vol  += delta * w
        elif delta < 0:
            td_sell_vol += abs(delta) * w
    td_total = td_buy_vol + td_sell_vol
    td_buy_pct = round(td_buy_vol / td_total * 100) if td_total > 0 else 50
    td_sell_pct = 100 - td_buy_pct
    # Time-decay divergence: when recent pressure disagrees with overall pressure,
    # the market is CHANGING direction → high-information signal
    td_diverge = abs(td_buy_pct - buy_pct) >= 20

    # ── 2. Fight zone: midpoint crossings ─────────────────────────────────
    mid     = (hi + lo) / 2
    crosses = sum(1 for i in range(1, n)
                  if (ticks[i-1] < mid) != (ticks[i] < mid))
    is_fight = crosses >= 4

    # ── 3. Volume profile: where did price spend the most time? ───────────
    hold_price = None
    hold_visits = 0
    if rng > 0:
        bin_size = rng / 10  # 10 bins for finer resolution
        bins = {}
        for t in ticks:
            b = int((t - lo) / bin_size)
            bins[b] = bins.get(b, 0) + 1
        top_bin = max(bins, key=bins.get)
        hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
        hold_visits = bins[top_bin]
        # Volume-at-hold vs volume-at-edges
        edge_bins = {0, 9} if len(bins) >= 10 else {0, len(bins)-1}
        hold_pct_of_total = bins.get(top_bin, 0) / n * 100
    else:
        hold_price  = round(cur, 6)
        hold_visits = n
        hold_pct_of_total = 100

    # ── 3b. VAP MIGRATION (Priority 2 fix, 2026-07-10) ────────────────────
    # Volume-At-Price migration: where did price spend time in the FIRST half
    # vs the SECOND half of the candle? If the "hold price" of the second half
    # is HIGHER than the first half, volume profile is migrating up = uptrend
    # building. If lower, downtrend building. This catches trend formation
    # BEFORE the candle closes — far more actionable than post-close analysis.
    vap_migration = None
    if rng > 0 and n >= 10:
        half = n // 2
        bin_size = rng / 10
        bins_first, bins_second = {}, {}
        for i, t in enumerate(ticks):
            b = int((t - lo) / bin_size)
            if i < half:
                bins_first[b] = bins_first.get(b, 0) + 1
            else:
                bins_second[b] = bins_second.get(b, 0) + 1
        if bins_first and bins_second:
            top1 = max(bins_first,  key=bins_first.get)
            top2 = max(bins_second, key=bins_second.get)
            hold1 = lo + top1 * bin_size + bin_size / 2
            hold2 = lo + top2 * bin_size + bin_size / 2
            migrate_amt = hold2 - hold1
            # Normalize to range so threshold is meaningful across pairs
            migrate_pct = migrate_amt / rng if rng > 0 else 0
            if migrate_pct > 0.25:
                vap_migration = {"dir": "UP",   "pct": round(migrate_pct, 3),
                                  "amt": round(migrate_amt, 6)}
            elif migrate_pct < -0.25:
                vap_migration = {"dir": "DOWN", "pct": round(migrate_pct, 3),
                                  "amt": round(migrate_amt, 6)}
            else:
                vap_migration = {"dir": "FLAT", "pct": round(migrate_pct, 3),
                                  "amt": round(migrate_amt, 6)}

    # ── 3c. LIVE WICK FORMATION (Priority 2 fix, 2026-07-10) ──────────────
    # Detect rejection wicks forming in REAL-TIME on the running candle.
    # The REV theory only fires on CLOSED candles — but a live upper wick
    # that is 2x the body AND the price is dropping = sellers rejecting the
    # high RIGHT NOW. Catching this mid-candle gives a head start signal.
    live_wick = None
    if rng > 0:
        live_body = abs(cur - op)
        live_upper_wick = hi - max(op, cur)
        live_lower_wick = min(op, cur) - lo
        # Normalize to range for cross-pair comparison
        uw_ratio = live_upper_wick / rng
        lw_ratio = live_lower_wick / rng
        body_ratio = live_body / rng
        # Determine last-3-tick direction (is price still moving toward wick or away?)
        last_dir = "FLAT"
        if n >= 3:
            tail = ticks[-3:]
            if tail[-1] > tail[0]:
                last_dir = "UP"
            elif tail[-1] < tail[0]:
                last_dir = "DOWN"
        # BULL_REJECT: long lower wick + price now rising (bottom rejection)
        # BEAR_REJECT: long upper wick + price now falling (top rejection)
        if lw_ratio > 0.35 and body_ratio < 0.30 and last_dir == "UP":
            live_wick = {"type": "BULL_REJECT", "lw_ratio": round(lw_ratio, 3),
                         "uw_ratio": round(uw_ratio, 3), "body_ratio": round(body_ratio, 3)}
        elif uw_ratio > 0.35 and body_ratio < 0.30 and last_dir == "DOWN":
            live_wick = {"type": "BEAR_REJECT", "lw_ratio": round(lw_ratio, 3),
                         "uw_ratio": round(uw_ratio, 3), "body_ratio": round(body_ratio, 3)}

    # ── 4. Phase momentum (early / mid / late thirds) ─────────────────────
    t3 = max(n // 3, 1)
    early = ticks[t3] - ticks[0]
    mid_m = ticks[2 * t3] - ticks[t3]
    late  = ticks[-1] - ticks[2 * t3]

    def _dir(v):
        return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

    phases = [_dir(early), _dir(mid_m), _dir(late)]

    # Phase intensity (how strong each phase is relative to total range)
    def _intensity(v):
        return abs(v) / rng if rng > 0 else 0

    phase_intensity = [_intensity(early), _intensity(mid_m), _intensity(late)]

    # ── 5. Reaction (visited extreme then reversed) ───────────────────────
    reaction = None
    if rng > 0:
        from_hi = (hi - cur) / rng
        from_lo = (cur - lo) / rng
        net = cur - op
        late_q = max(n // 4, 2)
        late_move = ticks[-1] - ticks[-late_q]
        if from_hi > 0.45 and late_move <= 0 and net < 0:
            reaction = "SELLER"
        elif from_lo > 0.45 and late_move >= 0 and net > 0:
            reaction = "BUYER"

    # ── 6. Final-tick exhaustion / recovery ───────────────────────────────
    last_react = None
    if n >= 15:
        last_n2 = max(n // 6, 6)
        fin2 = ticks[-last_n2:]
        fi2_up = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i-1])
        fi2_dn = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i-1])
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

    # ── 7. TICK SPEED: acceleration / deceleration ────────────────────────
    # Compare speed (price change per tick) in first half vs second half
    tick_speed = None
    if n >= 20:
        half = n // 2
        first_half_range = abs(ticks[half] - ticks[0])
        second_half_range = abs(ticks[-1] - ticks[half])
        # Speed = range / tick_count in that half
        spd_first  = first_half_range / half if half > 0 else 0
        spd_second = second_half_range / (n - half) if (n - half) > 0 else 0
        avg_speed  = (first_half_range + second_half_range) / n
        if avg_speed > 0:
            accel_ratio = spd_second / spd_first if spd_first > 0 else 1.0
        else:
            accel_ratio = 1.0
        tick_speed = {
            "first": round(spd_first, 8),
            "second": round(spd_second, 8),
            "accel": round(accel_ratio, 3),
            "avg": round(avg_speed, 8),
        }

    # ── 8. MOMENTUM SHIFT: direction change in last third ─────────────────
    momentum_shift = None
    if n >= 20:
        t2_3 = 2 * n // 3
        early_dir = "UP" if ticks[t2_3] > ticks[0] else ("DOWN" if ticks[t2_3] < ticks[0] else "FLAT")
        late_dir  = "UP" if ticks[-1] > ticks[t2_3] else ("DOWN" if ticks[-1] < ticks[t2_3] else "FLAT")
        if early_dir != "FLAT" and late_dir != "FLAT" and early_dir != late_dir:
            # Confirmed momentum shift in last third
            if late_dir == "UP":
                momentum_shift = "BULL_SHIFT"
            else:
                momentum_shift = "BEAR_SHIFT"

    # ── 9. LAST-N TICK VELOCITY (Priority 1 fix, 2026-07-10) ───────────────
    # The last few ticks carry the most information about the NEXT candle's
    # opening direction. Track velocity of last 5 / 10 / 20 ticks separately
    # and the acceleration between them.
    last_velocity = None
    if n >= 6:
        last5  = ticks[-1] - ticks[-5]  if n >= 5 else ticks[-1] - ticks[0]
        last10 = ticks[-1] - ticks[-10] if n >= 10 else ticks[-1] - ticks[0]
        last20 = ticks[-1] - ticks[-20] if n >= 20 else ticks[-1] - ticks[0]
        # Speed = signed move / tick count (positive = up, negative = down)
        spd5  = last5  / min(5,  n)
        spd10 = last10 / min(10, n)
        spd20 = last20 / min(20, n)
        # Acceleration: is the last-5 speed GREATER (in magnitude) than last-10?
        # If yes and same direction → accelerating (momentum building)
        # If yes and opposite direction → reversal spike
        if abs(spd10) > 0:
            accel_ratio = spd5 / spd10
        else:
            accel_ratio = 1.0
        last_velocity = {
            "last5_move":  round(last5,  6),
            "last10_move": round(last10, 6),
            "last20_move": round(last20, 6),
            "spd5":  round(spd5,  8),
            "spd10": round(spd10, 8),
            "spd20": round(spd20, 8),
            "accel": round(accel_ratio, 3),  # >1 = accelerating, <1 = decelerating
            "dir5":  "UP" if last5  > 0 else ("DOWN" if last5  < 0 else "FLAT"),
            "dir10": "UP" if last10 > 0 else ("DOWN" if last10 < 0 else "FLAT"),
        }

    # ── 10. CONSECUTIVE TICK STREAKS (Priority 1 fix, 2026-07-10) ──────────
    # Run-length encode tick directions to detect V-shape / inverted-V
    # reversals that simple up/down counts miss.
    #   Example: 5-up then 5-down = V-shape top → bearish reversal signal
    #   Example: 5-down then 5-up = V-shape bottom → bullish reversal signal
    streaks = []
    if n >= 4:
        cur_dir, cur_len = 0, 0
        for i in range(1, n):
            d = 1 if ticks[i] > ticks[i-1] else (-1 if ticks[i] < ticks[i-1] else 0)
            if d == 0:
                continue
            if d == cur_dir:
                cur_len += 1
            else:
                if cur_len >= 2:
                    streaks.append((cur_dir, cur_len))
                cur_dir, cur_len = d, 1
        if cur_len >= 2:
            streaks.append((cur_dir, cur_len))
        # Keep only the last 4 streaks for analysis
        streaks = streaks[-4:] if len(streaks) > 4 else streaks

    # Detect V-shape pattern: last 2 streaks opposite directions, both >=3
    v_shape = None
    if len(streaks) >= 2:
        last_d, last_l = streaks[-1]
        prev_d, prev_l = streaks[-2]
        if last_d != prev_d and last_d != 0 and prev_d != 0:
            if last_l >= 3 and prev_l >= 3:
                # V-shape: prev was UP→last DOWN = top reversal (bearish)
                # V-shape: prev was DOWN→last UP = bottom reversal (bullish)
                v_shape = "V_TOP"    if prev_d > 0 else "V_BOTTOM"

    # ── 10b. ORDER-FLOW IMBALANCE (Priority 3, 2026-07-10) ────────────────
    # Look at the DISTRIBUTION of tick sizes, not just the sum.
    # One big buyer tick + many small seller ticks can sum to the same value
    # as balanced flow — but the meaning is opposite. Big ticks = institutional
    # /profit-taking moves; small ticks = retail noise.
    #
    # Classification (anomaly-detection style, robust to clustered tick sizes):
    #   - "Big" tick   = size > max(2 × median, 1.5 × mean)
    #   - "Retail" tick = size <= median
    #   - "Mid" ticks  = between median and big threshold (excluded from vote
    #                    to keep signal clean)
    orderflow = None
    if n >= 12:
        # Compute all tick deltas (signed)
        deltas = []
        for i in range(1, n):
            d = ticks[i] - ticks[i-1]
            if d != 0:
                deltas.append(d)
        if len(deltas) >= 8:
            abs_deltas = [abs(d) for d in deltas]
            abs_deltas_sorted = sorted(abs_deltas)
            median_size = abs_deltas_sorted[len(abs_deltas_sorted) // 2]
            mean_size = sum(abs_deltas) / len(abs_deltas)
            # Big threshold = 2× median (or 1.5× mean, whichever is larger)
            # This is robust: in a normal market, <20% of ticks exceed this.
            big_threshold = max(median_size * 2.0, mean_size * 1.5)
            # Classify ticks
            big_up = big_dn = ret_up = ret_dn = 0
            big_up_vol = big_dn_vol = 0.0
            for d in deltas:
                a = abs(d)
                if a >= big_threshold and big_threshold > 0:
                    if d > 0:
                        big_up += 1; big_up_vol += d
                    else:
                        big_dn += 1; big_dn_vol += a
                elif a <= median_size:
                    if d > 0:
                        ret_up += 1
                    else:
                        ret_dn += 1
            # Determine dominant direction for big and retail
            big_dir = "UP" if big_up > big_dn else ("DOWN" if big_dn > big_up else "FLAT")
            ret_dir = "UP" if ret_up > ret_dn else ("DOWN" if ret_dn > ret_up else "FLAT")
            # Imbalance score: how much do big and retail disagree?
            imbalance = 0
            if big_dir != "FLAT" and ret_dir != "FLAT" and big_dir != ret_dir:
                # Big money going one way, retail going the other —
                # big money usually wins. This is a strong signal.
                imbalance = 1
            # Big-tick volume ratio (who's throwing weight around?)
            big_total_vol = big_up_vol + big_dn_vol
            big_buy_pct = (round(big_up_vol / big_total_vol * 100)
                           if big_total_vol > 0 else 50)
            orderflow = {
                "median_size": round(median_size, 7),
                "mean_size": round(mean_size, 7),
                "big_threshold": round(big_threshold, 7),
                "big_up": big_up, "big_dn": big_dn,
                "ret_up": ret_up, "ret_dn": ret_dn,
                "big_dir": big_dir,
                "ret_dir": ret_dir,
                "imbalance": imbalance,  # 1 = disagreement, 0 = agreement
                "big_buy_pct": big_buy_pct,
                "big_total_vol": round(big_total_vol, 6),
            }

    return {
        "buy_pct": buy_pct, "sell_pct": sell_pct,
        "count_buy_pct": count_buy_pct,
        "pressure": pressure, "is_fight": is_fight, "crosses": crosses,
        "hold_price": hold_price, "hold_visits": hold_visits,
        "hold_pct_of_total": hold_pct_of_total,
        "phases": phases, "phase_intensity": phase_intensity,
        "reaction": reaction, "net": round(cur - op, 6),
        "tick_count": n, "last_react": last_react,
        "tick_speed": tick_speed,
        "momentum_shift": momentum_shift,
        "vol_count_diverge": vol_count_diverge,
        # Priority 1 additions (2026-07-10)
        "last_velocity": last_velocity,
        "streaks": streaks,
        "v_shape": v_shape,
        # Priority 2 additions (2026-07-10)
        "td_buy_pct": td_buy_pct,
        "td_sell_pct": td_sell_pct,
        "td_diverge": td_diverge,
        "vap_migration": vap_migration,
        "live_wick": live_wick,
        # Priority 3 additions (2026-07-10)
        "orderflow": orderflow,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  THEORIES
# ═══════════════════════════════════════════════════════════════════════════════

def _theory_con(candles, muted):
    """CON - Continuation: follow the trend, but CHECK for exhaustion.

    OTC mean-reversion note (2026-07-10 review):
      Continuation theories systematically underperform in 60s OTC markets
      because broker price generators are mean-reverting. 3-candle streaks
      signal exhaustion, not continuation. To compensate:
        - 3-candle streak vote reduced from +2 to +1 (still fires, but
          is easily out-voted by REV/MEAN/SWEEP when they fire)
        - EMA-trend vote ONLY fires when RSI is in the neutral 30-70
          band (no vote in overbought/oversold — those zones belong
          to MEAN/REV)
        - New: 4+ same-direction candles fire a REVERSAL vote (exhaustion)
          instead of a continuation vote. This was previously the
          domain of MEAN, but CON needs to stop voting continuation
          when the streak is clearly extended.
    """
    if "CON" in muted:
        return None
    if len(candles) < 5:
        return None
    regime = _classify_regime(candles)
    score = 0
    reasons = []

    closes = [c["close"] for c in candles]
    # Last 3 candles same direction?
    dirs = []
    for c in candles[-3:]:
        if c["close"] > c["open"]:
            dirs.append(1)
        elif c["close"] < c["open"]:
            dirs.append(-1)
        else:
            dirs.append(0)

    # Count consecutive same-direction candles ending at the last candle.
    # Looks at most last 8 candles (enough to detect 4+ exhaustion without
    # counting ancient history).
    consec = 0
    if candles:
        last_dir = 1 if candles[-1]["close"] > candles[-1]["open"] else (
                  -1 if candles[-1]["close"] < candles[-1]["open"] else 0)
        if last_dir != 0:
            for c in reversed(candles[-8:]):
                c_dir = (1 if c["close"] > c["open"] else
                        -1 if c["close"] < c["open"] else 0)
                if c_dir == last_dir:
                    consec += 1
                else:
                    break

    if all(d >= 0 for d in dirs) and sum(dirs) >= 2:
        # Check for exhaustion: are candles getting SMALLER?
        sizes = [c["high"] - c["low"] for c in candles[-3:]]
        if sizes[0] > sizes[1] > sizes[2] and sizes[2] < sizes[0] * 0.5:
            # Shrinking bullish candles = momentum dying → DON'T follow
            score -= 2
            reasons.append("CON:-2 PUT bull-shrinking-exhaust")
        elif consec >= 4:
            # 4+ consecutive bull candles in mean-reverting OTC = exhaustion
            score -= 1
            reasons.append(f"CON:-1 PUT {consec}-bull-exhaustion")
        else:
            # Reduced from +2 to +1 — continuation theories were over-weighted
            # vs reversal theories in mean-reverting OTC markets.
            score += 1
            reasons.append("CON:+1 CALL 3-bull-continue")
    elif all(d <= 0 for d in dirs) and sum(dirs) <= -2:
        sizes = [c["high"] - c["low"] for c in candles[-3:]]
        if sizes[0] > sizes[1] > sizes[2] and sizes[2] < sizes[0] * 0.5:
            score += 2
            reasons.append("CON:+2 CALL bear-shrinking-exhaust")
        elif consec >= 4:
            score += 1
            reasons.append(f"CON:+1 CALL {consec}-bear-exhaustion")
        else:
            score -= 1
            reasons.append("CON:-1 PUT 3-bear-continue")

    # EMA trend alignment — but ONLY when RSI is in neutral zone
    # (overbought/oversold zones belong to MEAN/REV)
    rsi_val = regime.get("rsi", 50)
    if regime["trend"] == "UPTREND" and 30 <= rsi_val <= 70:
        score += 1
        reasons.append(f"CON:+1 CALL ema-bullish rsi={rsi_val:.0f}")
    elif regime["trend"] == "DOWNTREND" and 30 <= rsi_val <= 70:
        score -= 1
        reasons.append(f"CON:-1 PUT ema-bearish rsi={rsi_val:.0f}")
    # No EMA vote when RSI is in extreme zones — MEAN theory handles those

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_rev(candles, muted):
    """REV - Reversal: wick rejection at extremes OR at key levels."""
    if "REV" in muted:
        return None
    if len(candles) < 3:
        return None
    last = candles[-1]
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    atr = _atr(candles)
    if body < atr * 0.1:
        return None  # Doji — no clear rejection

    score = 0
    reasons = []

    # Get key levels for context
    levels = _key_levels(candles)

    # Strong lower wick = bullish rejection
    # Strengthened from +3/+4 to +4/+5 — reversal theories are the edge in
    # mean-reverting OTC markets and were being out-voted by CON/MICRO/RUN.
    if lower_wick > body * 1.5 and lower_wick > atr * 0.2:
        boost = 4
        # Extra weight if the low is near a swing low (double bottom pattern)
        for lv in levels:
            if lv["type"] == "swing_low" and abs(last["low"] - lv["price"]) < atr * 0.3:
                boost = 5  # Stronger signal at key level
                reasons.append(f"REV:bonus+1 at-swing-low={lv['price']:.5f}")
                break
        score += boost
        reasons.append(f"REV:+{boost} CALL lower-wick={lower_wick:.6f}")

    # Strong upper wick = bearish rejection
    if upper_wick > body * 1.5 and upper_wick > atr * 0.2:
        boost = 4
        for lv in levels:
            if lv["type"] == "swing_high" and abs(last["high"] - lv["price"]) < atr * 0.3:
                boost = 5
                reasons.append(f"REV:bonus+1 at-swing-high={lv['price']:.5f}")
                break
        score -= boost
        reasons.append(f"REV:-{boost} PUT upper-wick={upper_wick:.6f}")

    # Also check: is the candle at a RANGE extreme? (HIGH/LOW zone)
    if len(candles) >= 10:
        recent = candles[-10:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        if rng > 0:
            pos = (last["close"] - lo) / rng
            # At range low with bullish close = bounce
            if pos < 0.15 and last["close"] > last["open"]:
                score += 1
                reasons.append("REV:+1 CALL range-low-bounce")
            # At range high with bearish close = rejection
            elif pos > 0.85 and last["close"] < last["open"]:
                score -= 1
                reasons.append("REV:-1 PUT range-high-reject")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_run(candles, ticks, micro, muted):
    """RUN - Running candle microstructure (uses TICK-WEIGHTED analysis)."""
    if "RUN" in muted:
        return None
    if not micro or not ticks:
        return None

    score = 0
    reasons = []
    buy_pct    = micro.get("buy_pct", 50)
    pressure   = micro.get("pressure")
    reaction   = micro.get("reaction")
    phases     = micro.get("phases", [])
    last_react = micro.get("last_react")
    is_fight   = micro.get("is_fight", False)
    tick_speed = micro.get("tick_speed")
    mom_shift  = micro.get("momentum_shift")
    vol_div    = micro.get("vol_count_diverge", False)

    # ── Tick-weighted buyer/seller pressure ────────────────────────────────
    if buy_pct >= 70:
        score += 3
        reasons.append(f"RUN:+3 CALL buyer-pressure={buy_pct}%")
    elif buy_pct <= 30:
        score -= 3
        reasons.append(f"RUN:-3 PUT seller-pressure={100-buy_pct}%")
    elif buy_pct >= 60:
        score += 2  # Increased from +1 — tick-weighted is more reliable
        reasons.append(f"RUN:+2 CALL buyer-pressure={buy_pct}%")
    elif buy_pct <= 40:
        score -= 2
        reasons.append(f"RUN:-2 PUT seller-pressure={100-buy_pct}%")

    # ── Volume/count divergence: if they disagree, reduce confidence ───────
    if vol_div:
        score = int(score * 0.5)
        reasons.append("RUN:diverge vol-vs-count *0.5")

    # ── Reaction (visited extreme then reversed) ───────────────────────────
    if reaction == "BUYER":
        score += 2
        reasons.append("RUN:+2 CALL buyer-rejection-from-low")
    elif reaction == "SELLER":
        score -= 2
        reasons.append("RUN:-2 PUT seller-rejection-from-high")

    # ── Phase momentum consistency ────────────────────────────────────────
    if len(phases) == 3:
        intensity = micro.get("phase_intensity", [0, 0, 0])
        if phases == ["UP", "UP", "UP"]:
            # Stronger if LATE phase is also intense (not just direction)
            bonus = 3 if intensity[2] > 0.3 else 2
            score += bonus
            reasons.append(f"RUN:+{bonus} CALL all-phases-up late_i={intensity[2]:.2f}")
        elif phases == ["DOWN", "DOWN", "DOWN"]:
            bonus = 3 if intensity[2] > 0.3 else 2
            score -= bonus
            reasons.append(f"RUN:-{bonus} PUT all-phases-down late_i={intensity[2]:.2f}")
        # Late reversal: first two same, last different
        elif phases[0] == phases[1] and phases[2] != phases[0]:
            if phases[2] == "UP":
                score += 1
                reasons.append("RUN:+1 CALL late-phase-up")
            elif phases[2] == "DOWN":
                score -= 1
                reasons.append("RUN:-1 PUT late-phase-down")

    # ── Tick speed / acceleration ─────────────────────────────────────────
    if tick_speed:
        accel = tick_speed["accel"]
        if accel > 1.5:
            # Accelerating in the current direction = momentum building
            net = micro.get("net", 0)
            if net > 0:
                score += 1
                reasons.append(f"RUN:+1 CALL accel={accel:.1f}")
            elif net < 0:
                score -= 1
                reasons.append(f"RUN:-1 PUT accel={accel:.1f}")
        elif accel < 0.5:
            # Decelerating = momentum dying
            net = micro.get("net", 0)
            if net > 0:
                score -= 1
                reasons.append(f"RUN:-1 PUT decel={accel:.1f}")
            elif net < 0:
                score += 1
                reasons.append(f"RUN:+1 CALL decel={accel:.1f}")

    # ── Momentum shift (late direction change) ────────────────────────────
    if mom_shift:
        if mom_shift == "BULL_SHIFT":
            score += 2
            reasons.append("RUN:+2 CALL momentum-shift-bull")
        elif mom_shift == "BEAR_SHIFT":
            score -= 2
            reasons.append("RUN:-2 PUT momentum-shift-bear")

    # ── Exhaustion/recovery from last portion ─────────────────────────────
    if last_react == "EXHAUST":
        net = micro.get("net", 0)
        if net > 0:
            score -= 2
            reasons.append("RUN:-2 PUT bull-exhaust-end")
        elif net < 0:
            score += 2
            reasons.append("RUN:+2 CALL bear-exhaust-end")
    elif last_react == "RECOVERY":
        net = micro.get("net", 0)
        if net > 0:
            score += 1
            reasons.append("RUN:+1 CALL bull-recovery-end")
        elif net < 0:
            score -= 1
            reasons.append("RUN:-1 PUT bear-recovery-end")

    # ── Priority 1 (2026-07-10): last-N tick velocity ─────────────────────
    # Light integration — the heavy lifting is done by the dedicated
    # VELOCITY theory. Here we add a small velocity-aligned vote when
    # last-5 is strongly in one direction AND last-10 confirms.
    last_vel = micro.get("last_velocity")
    if last_vel:
        dir5  = last_vel.get("dir5")
        dir10 = last_vel.get("dir10")
        accel = last_vel.get("accel", 1.0)
        if dir5 == dir10 and dir5 != "FLAT" and accel > 1.5:
            # Strong agreement → small continuation bonus
            if dir5 == "UP":
                score += 1
                reasons.append(f"RUN:+1 CALL last5-vel-up accel={accel:.1f}")
            else:
                score -= 1
                reasons.append(f"RUN:-1 PUT last5-vel-down accel={accel:.1f}")

    # ── Priority 2 (2026-07-10): time-decay weighted pressure ────────────
    # If recent ticks (time-decayed) show DIFFERENT pressure than the
    # overall candle, the market is shifting. Trust the time-decayed value
    # more — it reflects the most recent sentiment.
    td_buy_pct = micro.get("td_buy_pct", 50)
    td_diverge = micro.get("td_diverge", False)
    if td_diverge:
        # Pressure changed mid-candle — vote with the RECENT direction
        if td_buy_pct >= 60:
            score += 2
            reasons.append(
                f"RUN:+2 CALL td-pressure-shift td_buy={td_buy_pct}%")
        elif td_buy_pct <= 40:
            score -= 2
            reasons.append(
                f"RUN:-2 PUT td-pressure-shift td_sell={100-td_buy_pct}%")

    # ── Priority 2 (2026-07-10): VAP migration ───────────────────────────
    # Volume-At-Price migration shows where the "hold price" is moving
    # between first and second half of the candle. Strong migration = trend
    # building; next candle likely continues in migration direction.
    vap = micro.get("vap_migration")
    if vap and vap.get("dir") != "FLAT":
        migrate_pct = abs(vap.get("pct", 0))
        if migrate_pct > 0.40:
            # Strong migration — high conviction trend signal
            if vap["dir"] == "UP":
                score += 2
                reasons.append(
                    f"RUN:+2 CALL vap-migrate-up pct={migrate_pct:.2f}")
            else:
                score -= 2
                reasons.append(
                    f"RUN:-2 PUT vap-migrate-down pct={migrate_pct:.2f}")
        elif migrate_pct > 0.25:
            # Mild migration — small vote
            if vap["dir"] == "UP":
                score += 1
                reasons.append(
                    f"RUN:+1 CALL vap-migrate-up pct={migrate_pct:.2f}")
            else:
                score -= 1
                reasons.append(
                    f"RUN:-1 PUT vap-migrate-down pct={migrate_pct:.2f}")

    # ── Fight zone = uncertainty ──────────────────────────────────────────
    if is_fight:
        score = int(score * 0.3)

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_trap(candles, ticks, muted):
    """TRAP - Trap: big move in one direction then reversal within candle."""
    if "TRAP" in muted:
        return None
    if not ticks or len(ticks) < 15:
        return None
    op  = candles[-1]["open"] if candles else ticks[0]
    hi  = max(ticks)
    lo  = min(ticks)
    cur = ticks[-1]
    rng = hi - lo
    if rng == 0:
        return None

    from_hi = (hi - cur) / rng
    from_lo = (cur - lo) / rng
    net = cur - op

    score = 0
    reasons = []

    # Tick-weighted: check if the VOLUME of the reversal is significant
    n = len(ticks)
    mid_tick = n // 2

    # Find the extreme tick index
    hi_idx = ticks.index(hi)
    lo_idx = ticks.index(lo)

    if from_hi > 0.60 and net < 0:
        # Bull trap: went up high, came back down
        # Stronger if the reversal happened LATE (more ticks selling)
        sell_ticks_after_hi = sum(1 for i in range(hi_idx, n) if i > 0 and ticks[i] < ticks[i-1])
        buy_ticks_to_hi = sum(1 for i in range(1, hi_idx+1) if ticks[i] > ticks[i-1])
        if sell_ticks_after_hi > buy_ticks_to_hi * 0.6:
            score -= 5  # Strong conviction — boosted from -4 to -5
            reasons.append(f"TRAP:-5 PUT bull-trap from-hi={from_hi:.0%} heavy-reversal")
        else:
            score -= 4  # boosted from -3 to -4
            reasons.append(f"TRAP:-4 PUT bull-trap from-hi={from_hi:.0%}")
    elif from_lo > 0.60 and net > 0:
        buy_ticks_after_lo = sum(1 for i in range(lo_idx, n) if i > 0 and ticks[i] > ticks[i-1])
        sell_ticks_to_lo = sum(1 for i in range(1, lo_idx+1) if ticks[i] < ticks[i-1])
        if buy_ticks_after_lo > sell_ticks_to_lo * 0.6:
            score += 5
            reasons.append(f"TRAP:+5 CALL bear-trap from-lo={from_lo:.0%} heavy-reversal")
        else:
            score += 4
            reasons.append(f"TRAP:+4 CALL bear-trap from-lo={from_lo:.0%}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_gap(candles, muted):
    """GAP - Gap between candles (OTC-optimized fade logic, 2026-07-10).

    Web research finding: in OTC markets (no real catalyst), common gaps fill
    ~90% of the time. So the dominant edge is FADE THE GAP, not continue it.

    Previous logic rewarded continuation (gap-up-continue → CALL). That is the
    OPPOSITE of what works in OTC. This rewrite flips it:

      Prev green + Gap UP   → PUT  (fade: gap up after bull = exhaustion)
      Prev green + Gap DOWN → CALL (fill: gap down after bull = pullback buy)
      Prev red   + Gap UP   → PUT  (fill: gap up after bear = dead-cat bounce)
      Prev red   + Gap DOWN → CALL (fade: gap down after bear = exhaustion)

    Score scales with gap size relative to ATR:
      - Small gap (<0.1 ATR): ±1 (weak, mostly noise)
      - Medium gap (0.1-0.3 ATR): ±2 (standard fade)
      - Large gap (>0.3 ATR): ±3 (high-conviction fade)

    Gap-type bonus (uses the classification computed in _save_micro, but
    re-derives it here since theories don't get micro_snap):
      - FILLED: gap was already filled by this candle → strong confirmation +1
      - REJECTED: wick tested gap zone and rejected → +1
      - PURE: gap unvisited → no bonus (uncertain)
      - FLIP: gap up but closed down (or vice versa) → +1 (reversal confirmed)
    """
    if "GAP" in muted:
        return None
    if len(candles) < 2:
        return None
    prev = candles[-2]
    last = candles[-1]
    gap = last["open"] - prev["close"]
    if prev["close"] == 0:
        return None
    gap_pct = gap / prev["close"]

    # Threshold raised from 0.00005 to 0.0002 (0.02%) — old threshold fired
    # on nearly every candle (noise). 0.02% filters out sub-pip jitter.
    if abs(gap_pct) < 0.0002:
        return None

    # Gap size relative to ATR (for conviction scaling)
    atr = _atr(candles)
    gap_size_ratio = abs(gap) / atr if atr > 0 else 0
    if gap_size_ratio > 0.3:
        size_score = 3   # large gap → high-conviction fade
    elif gap_size_ratio > 0.1:
        size_score = 2   # medium gap → standard fade
    else:
        size_score = 1   # small gap → weak signal

    # Previous candle direction (bull = green, bear = red)
    prev_bull = prev["close"] >= prev["open"]
    gap_up = gap > 0

    # OTC fade logic matrix
    score = 0
    reasons = []
    if prev_bull and gap_up:
        # Green → Gap Up → fade (PUT)
        # Rationale: OTC common gaps fill ~90%. Up-gap after bullish close =
        # exhaustion/profit-taking → price returns down.
        score = -size_score
        reasons.append(f"GAP:-{size_score} PUT green-then-gapup-fade "
                       f"gap={gap_pct:.5f} size={gap_size_ratio:.2f}xATR")
    elif prev_bull and not gap_up:
        # Green → Gap Down → fill (CALL)
        # Rationale: gap down after bullish close = pullback; price returns
        # up to fill the gap → CALL.
        score = size_score
        reasons.append(f"GAP:+{size_score} CALL green-then-gapdown-fill "
                       f"gap={gap_pct:.5f} size={gap_size_ratio:.2f}xATR")
    elif not prev_bull and gap_up:
        # Red → Gap Up → fill (PUT)
        # Rationale: gap up after bearish close = dead-cat bounce; price
        # returns down to fill → PUT.
        score = -size_score
        reasons.append(f"GAP:-{size_score} PUT red-then-gapup-fill "
                       f"gap={gap_pct:.5f} size={gap_size_ratio:.2f}xATR")
    else:
        # Red → Gap Down → fade (CALL)
        # Rationale: gap down after bearish close = exhaustion; price bounces
        # up to fade the gap → CALL.
        score = size_score
        reasons.append(f"GAP:+{score} CALL red-then-gapdown-fade "
                       f"gap={gap_pct:.5f} size={gap_size_ratio:.2f}xATR")

    # ── Gap-type bonus (re-derive classification inline) ───────────────────
    # This mirrors the FILLED/REJECTED/PURE/FLIP logic from feed.py's
    # _save_micro, so the theory can use it without needing micro_snap passed
    # in (theories only get candles + ticks).
    is_bull_c = last["close"] >= last["open"]
    pc = prev["close"]
    w_fill = ((gap_up and last["low"] <= pc) or
              (not gap_up and last["high"] >= pc))
    b_fill = ((gap_up and last["close"] <= pc) or
              (not gap_up and last["close"] >= pc))
    if b_fill:
        # Gap fully filled by close → strong confirmation of fade direction
        score = score + (1 if score > 0 else -1)
        reasons.append("GAP:bonus±1 FILLED (close returned to prev close)")
    elif w_fill:
        # Wick tested gap zone — was it rejected?
        if gap_up == is_bull_c:
            # Rejected: wick went into gap but close rejected → bonus
            score = score + (1 if score > 0 else -1)
            reasons.append("GAP:bonus±1 REJECTED (wick tested + rejected)")
    elif gap_up != is_bull_c:
        # FLIP: gap direction opposite to close direction → reversal confirmed
        score = score + (1 if score > 0 else -1)
        reasons.append("GAP:bonus±1 FLIP (gap dir != close dir)")
    # PURE gap (unvisited) → no bonus, direction still uncertain

    return ("CALL" if score > 0 else "PUT", score, reasons)


def _theory_last(candles, ticks, muted):
    """LAST - Last-portion exhaustion/recovery (tick-weighted)."""
    if "LAST" in muted:
        return None
    if not ticks or len(ticks) < 10:
        return None

    n = len(ticks)
    last_n = max(n // 6, 3)
    fin = ticks[-last_n:]
    fi_up = sum(1 for i in range(1, len(fin)) if fin[i] > fin[i-1])
    fi_dn = sum(1 for i in range(1, len(fin)) if fin[i] < fin[i-1])
    fi_tot = fi_up + fi_dn
    if fi_tot < 2:
        return None

    op  = ticks[0]
    cur = ticks[-1]
    net = cur - op
    fbp = fi_up / fi_tot

    score = 0
    reasons = []

    if net > 0:  # Candle is bullish
        if fbp <= 0.25:
            score -= 4  # boosted from -3 to -4 — exhaustion reversal is high-conviction
            reasons.append("LAST:-4 PUT bull-exhaustion-final")
        elif fbp >= 0.85 and fi_tot >= 4:
            score -= 3  # boosted from -2 to -3
            reasons.append("LAST:-3 PUT overextended-bull")
    elif net < 0:  # Candle is bearish
        if fbp >= 0.75:
            score += 4
            reasons.append("LAST:+4 CALL bear-exhaustion-final")
        elif fbp <= 0.15 and fi_tot >= 4:
            score += 3
            reasons.append("LAST:+3 CALL overextended-bear")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_rng(candles, muted):
    """RNG - Round-number proximity bias."""
    if "RNG" in muted:
        return None
    if not candles:
        return None
    last = candles[-1]
    for price in [last["close"], last["high"], last["low"]]:
        lvl, _, strength = _round_level(price)
        if strength == "BIG":
            if last["close"] > last["open"] and price == last["high"]:
                return "PUT", -2, [f"RNG:-2 PUT rejected-at-big-round {lvl}"]
            elif last["close"] < last["open"] and price == last["low"]:
                return "CALL", 2, [f"RNG:+2 CALL rejected-at-big-round {lvl}"]
    return None


def _theory_mst(candles, muted):
    """MST - Market state: classify and apply directional bias."""
    if "MST" in muted:
        return None
    if len(candles) < 10:
        return None

    regime = _classify_regime(candles)
    score = 0
    reasons = []
    state = "NONE"
    bias = None

    recent_atr = _atr(candles[-5:], 5)
    older_atr = _atr(candles[-20:-5], 5) if len(candles) >= 20 else recent_atr
    vol_ratio = recent_atr / older_atr if older_atr > 0 else 1

    if regime["trend"] == "SIDEWAYS" and vol_ratio < 0.7:
        state = "QUIET"
        score = 0
    elif regime["trend"] != "SIDEWAYS" and vol_ratio > 1.3:
        state = "VOLATILE"
        if regime["trend"] == "UPTREND":
            score += 1
            bias = "CALL"
        else:
            score -= 1
            bias = "PUT"
        reasons.append(f"MST:{'+' if score > 0 else ''}{score} {bias} volatile-trend")
    elif regime["trend"] != "SIDEWAYS":
        state = "TRENDING"
        if regime["trend"] == "UPTREND":
            score += 1
            bias = "CALL"
        else:
            score -= 1
            bias = "PUT"
        reasons.append(f"MST:{'+' if score > 0 else ''}{score} {bias} steady-trend")

    result = {"state": state, "bias": bias}
    if score == 0:
        return None, result
    return ("CALL" if score > 0 else "PUT", score, reasons), result


def _theory_micro(candles, ticks, muted):
    """
    MICRO - NEW: Closed candle's internal microstructure vote.

    This is THE MOST IMPORTANT theory for the user's use case:
    it looks at the CLOSED candle's tick-level behavior to predict
    the NEXT candle's direction.

    OTC mean-reversion fix (2026-07-10 review):
      Previous logic: "buyers dominated AND closed up → next continues up"
      This is the SAME logic as CON theory and was systematically wrong
      in mean-reverting OTC markets. Strong close in one direction
      signals exhaustion, not continuation.

      New logic:
        - Strong buyer pressure + bullish close in OTC = exhaustion → PUT
        - Strong buyer pressure + bearish close = failed move → PUT (was PUT before)
        - Strong seller pressure + bearish close in OTC = exhaustion → CALL
        - Strong seller pressure + bullish close = failed move → CALL
        - Phase patterns: late reversal phases still vote momentum shift
        - Reaction and momentum_shift are unchanged (those catch genuine
          microstructure shifts, not just pressure direction)
    """
    if "MICRO" in muted:
        return None
    if not ticks or len(ticks) < 15:
        return None

    micro = _build_micro(ticks, candles[-1]["open"] if candles else ticks[0])
    if not micro:
        return None

    score = 0
    reasons = []

    buy_pct  = micro.get("buy_pct", 50)
    net      = micro.get("net", 0)
    reaction = micro.get("reaction")
    phases   = micro.get("phases", [])
    mom_shift = micro.get("momentum_shift")
    pressure  = micro.get("pressure")
    is_fight  = micro.get("is_fight", False)
    atr = _atr(candles)

    # ── Core: Pressure + candle direction → OTC mean-reversion vote ──────
    # Strong buyer pressure + bullish close = exhaustion → expect PUT next
    # Strong buyer pressure + bearish close = failed move → expect PUT next
    # Strong seller pressure + bearish close = exhaustion → expect CALL next
    # Strong seller pressure + bullish close = failed move → expect CALL next
    # (i.e. STRONG pressure in either direction = mean-reversion signal)
    body = (candles[-1]["close"] - candles[-1]["open"]) if candles else 0
    body_ratio = abs(body) / atr if atr > 0 else 0

    if buy_pct >= 65:
        # Strong buyer pressure → expect mean reversion down
        if body_ratio > 1.0:
            # Big bullish body confirms exhaustion
            score -= 3
            reasons.append(
                f"MICRO:-3 PUT strong-buyers-mean-revert bp={buy_pct}% "
                f"body={body_ratio:.2f}xATR")
        else:
            score -= 2
            reasons.append(
                f"MICRO:-2 PUT strong-buyers bp={buy_pct}% net={net:.5f}")
    elif buy_pct <= 35:
        # Strong seller pressure → expect mean reversion up
        if body_ratio > 1.0:
            score += 3
            reasons.append(
                f"MICRO:+3 CALL strong-sellers-mean-revert sp={100-buy_pct}% "
                f"body={body_ratio:.2f}xATR")
        else:
            score += 2
            reasons.append(
                f"MICRO:+2 CALL strong-sellers sp={100-buy_pct}% net={net:.5f}")

    # ── Reaction at close ─────────────────────────────────────────────────
    # This catches genuine reversal patterns (wick rejection at end) —
    # vote WITH the reaction direction (it's a reversal signal)
    if reaction == "BUYER":
        score += 2
        reasons.append("MICRO:+2 CALL closed-with-buyer-rejection")
    elif reaction == "SELLER":
        score -= 2
        reasons.append("MICRO:-2 PUT closed-with-seller-rejection")

    # ── Phase pattern at close ────────────────────────────────────────────
    # Late reversal phases = momentum shift signals (unchanged)
    if len(phases) == 3:
        if phases == ["DOWN", "DOWN", "UP"]:
            score += 2
            reasons.append("MICRO:+2 CALL phases=DOWN,DOWN,UP")
        elif phases == ["UP", "UP", "DOWN"]:
            score -= 2
            reasons.append("MICRO:-2 PUT phases=UP,UP,DOWN")
        elif phases == ["UP", "DOWN", "UP"]:
            score += 1
            reasons.append("MICRO:+1 CALL V-recovery")
        elif phases == ["DOWN", "UP", "DOWN"]:
            score -= 1
            reasons.append("MICRO:-1 PUT inverted-V")

    # ── Momentum shift from ticks ────────────────────────────────────────
    # Late direction change is a genuine microstructure signal (unchanged)
    if mom_shift == "BULL_SHIFT":
        score += 2
        reasons.append("MICRO:+2 CALL late-momentum-shift-bull")
    elif mom_shift == "BEAR_SHIFT":
        score -= 2
        reasons.append("MICRO:-2 PUT late-momentum-shift-bear")

    # ── Fight zone dampening ─────────────────────────────────────────────
    if is_fight:
        score = int(score * 0.4)

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_continuity(candles, ticks, muted):
    """CONTINUITY - NEW (2026-07-10): Cross-candle tick continuity analysis.

    Analyzes the CONTINUITY between one candle's closing ticks and the next
    candle's opening ticks. This catches momentum carry-over and gap
    confirmation that single-candle theories miss:

      1. STRONG CONTINUATION: last candle's last 5 ticks + current candle's
         first 5 ticks ALL same direction → strong momentum carry → continue
      2. TICK REVERSAL: last candle closed bullish but its last 5 ticks were
         bearish (or vice versa) → internal reversal → next candle likely
         follows the late ticks, not the body direction
      3. OPENING CONFIRMATION: current candle's first few ticks confirm or
         reject the gap direction (works with the signal delay feature)

    Uses `ticks` which on the closed-candle path = the just-closed candle's
    ticks. On the live re-eval path = running candle's ticks. The closed
    candle's ticks are the PRIMARY input here.
    """
    if "CONTINUITY" in muted:
        return None
    if not ticks or len(ticks) < 10:
        return None
    if len(candles) < 2:
        return None

    score = 0
    reasons = []

    n = len(ticks)
    last = candles[-1]
    prev = candles[-2]

    # Last candle's body direction
    body_dir = 1 if last["close"] > last["open"] else (-1 if last["close"] < last["open"] else 0)

    # ── 1. LAST CANDLE'S LATE TICKS vs BODY DIRECTION ─────────────────────
    # If the candle is bullish but its last 5 ticks are bearish → internal
    # reversal → next candle likely bearish
    last5_move = ticks[-1] - ticks[-5] if n >= 5 else ticks[-1] - ticks[0]
    last5_dir = 1 if last5_move > 0 else (-1 if last5_move < 0 else 0)

    if body_dir != 0 and last5_dir != 0 and body_dir != last5_dir:
        # Late ticks OPPOSITE to body → internal reversal
        if last5_dir > 0:
            score += 2
            reasons.append(f"CONTINUITY:+2 CALL late-tick-reversal-up "
                           f"(bull-body but last5={last5_move:.6f})")
        else:
            score -= 2
            reasons.append(f"CONTINUITY:-2 PUT late-tick-reversal-down "
                           f"(bear-body but last5={last5_move:.6f})")

    # ── 2. STRONG CONTINUATION (late ticks match body direction) ──────────
    elif body_dir != 0 and last5_dir != 0 and body_dir == last5_dir:
        # Late ticks CONFIRM body direction → strong momentum carry
        # Check magnitude: if last-5 move is significant (>0.3 ATR)
        atr = _atr(candles)
        if atr > 0:
            last5_strength = abs(last5_move) / atr
            if last5_strength > 0.3:
                if last5_dir > 0:
                    score += 2
                    reasons.append(f"CONTINUITY:+2 CALL strong-carry-up "
                                   f"last5={last5_strength:.2f}xATR")
                else:
                    score -= 2
                    reasons.append(f"CONTINUITY:-2 PUT strong-carry-down "
                                   f"last5={last5_strength:.2f}xATR")
            elif last5_strength > 0.15:
                # Moderate carry
                if last5_dir > 0:
                    score += 1
                    reasons.append(f"CONTINUITY:+1 CALL moderate-carry-up "
                                   f"last5={last5_strength:.2f}xATR")
                else:
                    score -= 1
                    reasons.append(f"CONTINUITY:-1 PUT moderate-carry-down "
                                   f"last5={last5_strength:.2f}xATR")

    # ── 3. GAP CONFIRMATION via opening ticks ─────────────────────────────
    # If there's a gap between prev.close and last.open, check if the last
    # candle's ticks confirm or reject that gap
    gap = last["open"] - prev["close"]
    if prev["close"] > 0:
        gap_pct = gap / prev["close"]
        if abs(gap_pct) > 0.0002:  # 0.02% threshold (matches GAP theory)
            # Check if last candle's overall tick direction confirms or
            # rejects the gap
            overall_move = ticks[-1] - ticks[0]
            overall_dir = 1 if overall_move > 0 else (-1 if overall_move < 0 else 0)
            gap_dir = 1 if gap > 0 else -1

            if overall_dir != 0 and gap_dir != 0:
                if overall_dir == gap_dir:
                    # Ticks confirm gap direction → gap likely continues
                    # (rare in OTC, but when it happens it's strong)
                    if gap_dir > 0:
                        score += 1
                        reasons.append("CONTINUITY:+1 CALL gap-confirmed-by-ticks")
                    else:
                        score -= 1
                        reasons.append("CONTINUITY:-1 PUT gap-confirmed-by-ticks")
                else:
                    # Ticks REJECT gap direction → gap fading (common in OTC)
                    # This aligns with GAP theory's fade logic
                    if gap_dir > 0:
                        # Gap up but ticks moved down → fade confirmed
                        score -= 1
                        reasons.append("CONTINUITY:-1 PUT gap-up-rejected-by-ticks")
                    else:
                        # Gap down but ticks moved up → fade confirmed
                        score += 1
                        reasons.append("CONTINUITY:+1 CALL gap-down-rejected-by-ticks")

    # ── 4. TICK SPEED CONSISTENCY (late vs early) ─────────────────────────
    # If the candle's late ticks are FASTER than early ticks → momentum
    # building into the close → next candle likely continues
    if n >= 20:
        half = n // 2
        early_speed = abs(ticks[half] - ticks[0]) / half if half > 0 else 0
        late_speed = abs(ticks[-1] - ticks[half]) / (n - half) if (n - half) > 0 else 0
        if early_speed > 0:
            speed_ratio = late_speed / early_speed
            if speed_ratio > 2.0:
                # Late ticks 2x faster than early → strong momentum build
                if last5_dir > 0:
                    score += 1
                    reasons.append(f"CONTINUITY:+1 CALL late-speed-surge "
                                   f"ratio={speed_ratio:.1f}")
                elif last5_dir < 0:
                    score -= 1
                    reasons.append(f"CONTINUITY:-1 PUT late-speed-surge "
                                   f"ratio={speed_ratio:.1f}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_momentum(candles, muted):
    """MOMENTUM - NEW (2026-07-10): Multi-candle body-size momentum analysis.

    Compares the BODY SIZES of the last 5 closed candles to detect momentum
    building or fading — a pattern that single-candle theories miss:

      1. GROWING BODIES (momentum building): each candle's body larger than
         the previous → trend accelerating → continuation signal
      2. SHRINKING BODIES (momentum fading): each candle's body smaller than
         the previous → trend exhausting → reversal signal
      3. EXPANSION FROM COMPRESSION: 2 small candles followed by 1 large
         candle → breakout direction likely continues
      4. BODY SIZE DIVERGENCE: price making higher highs but body sizes
         shrinking → classic divergence → reversal warning

    Score scales with how clear the pattern is (3-candle > 2-candle).
    """
    if "MOMENTUM" in muted:
        return None
    if len(candles) < 5:
        return None

    score = 0
    reasons = []

    # Compute body sizes (signed) for last 5 candles
    last5 = candles[-5:]
    bodies = []
    for c in last5:
        body = c["close"] - c["open"]
        bodies.append(body)
    abs_bodies = [abs(b) for b in bodies]
    directions = [1 if b > 0 else (-1 if b < 0 else 0) for b in bodies]

    atr = _atr(candles)
    if atr <= 0:
        return None

    # Normalize body sizes to ATR for cross-pair comparison
    norm_bodies = [ab / atr for ab in abs_bodies]

    # ── 1. GROWING BODIES (momentum building) ─────────────────────────────
    # Check if last 3 bodies are growing: |b3| > |b2| > |b1|
    if len(abs_bodies) >= 3:
        b1, b2, b3 = abs_bodies[-3], abs_bodies[-2], abs_bodies[-1]
        if b3 > b2 > b1 and b3 > b1 * 1.5:
            # Growing bodies → momentum building
            # Direction: follow the last candle's direction
            if directions[-1] > 0:
                score += 3
                reasons.append(f"MOMENTUM:+3 CALL growing-bodies "
                               f"b1={norm_bodies[-3]:.2f}->b3={norm_bodies[-1]:.2f}xATR")
            elif directions[-1] < 0:
                score -= 3
                reasons.append(f"MOMENTUM:-3 PUT growing-bodies "
                               f"b1={norm_bodies[-3]:.2f}->b3={norm_bodies[-1]:.2f}xATR")

        # ── 2. SHRINKING BODIES (momentum fading → reversal) ──────────────
        elif b1 > b2 > b3 and b1 > b3 * 2:
            # Shrinking bodies → exhaustion → reversal
            if directions[-1] > 0:
                score -= 2
                reasons.append(f"MOMENTUM:-2 PUT shrinking-bull-bodies "
                               f"b1={norm_bodies[-3]:.2f}->b3={norm_bodies[-1]:.2f}xATR")
            elif directions[-1] < 0:
                score += 2
                reasons.append(f"MOMENTUM:+2 CALL shrinking-bear-bodies "
                               f"b1={norm_bodies[-3]:.2f}->b3={norm_bodies[-1]:.2f}xATR")

    # ── 3. EXPANSION FROM COMPRESSION (breakout) ──────────────────────────
    # 2 small candles followed by 1 large candle
    if len(abs_bodies) >= 3:
        small_avg = (abs_bodies[-3] + abs_bodies[-2]) / 2
        large = abs_bodies[-1]
        if small_avg > 0 and large > small_avg * 2.5:
            # Breakout: large candle after 2 small → continuation
            if directions[-1] > 0:
                score += 2
                reasons.append(f"MOMENTUM:+2 CALL breakout-from-compression "
                               f"small={norm_bodies[-3]:.2f}->large={norm_bodies[-1]:.2f}xATR")
            elif directions[-1] < 0:
                score -= 2
                reasons.append(f"MOMENTUM:-2 PUT breakout-from-compression "
                               f"small={norm_bodies[-3]:.2f}->large={norm_bodies[-1]:.2f}xATR")

    # ── 4. BODY SIZE DIVERGENCE ───────────────────────────────────────────
    # Price making higher highs but bodies shrinking (or vice versa)
    if len(candles) >= 4:
        last4 = candles[-4:]
        highs = [c["high"] for c in last4]
        lows = [c["low"] for c in last4]
        last4_bodies = abs_bodies[-4:]

        # Bullish divergence: highs rising but bodies shrinking
        if highs[-1] > highs[-2] > highs[-3] and last4_bodies[-1] < last4_bodies[-2] < last4_bodies[-3]:
            score -= 2
            reasons.append("MOMENTUM:-2 PUT bull-divergence (higher-highs, smaller-bodies)")
        # Bearish divergence: lows falling but bodies shrinking
        elif lows[-1] < lows[-2] < lows[-3] and last4_bodies[-1] < last4_bodies[-2] < last4_bodies[-3]:
            score += 2
            reasons.append("MOMENTUM:+2 CALL bear-divergence (lower-lows, smaller-bodies)")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_history(candles, ticks, micro_history, muted):
    """HISTORY - NEW (2026-07-10): Previous candles' microstructure analysis.

    Looks at the LAST 3 closed candles' microstructure (from DB via
    micro_history) to detect multi-candle patterns that single-candle
    theories miss:

      1. CONSECUTIVE PRESSURE: 3 candles in a row with same buyer/seller
         pressure → strong continuation bias (but check exhaustion below)
      2. PRESSURE SHIFT: last candle's pressure flipped vs previous 2 →
         momentum shift signal
      3. REACTION STREAK: last 2-3 candles all showed same reaction
         (BUYER/SELLER) → key level rejection building
      4. GAP CHAIN: multiple consecutive candles with same gap_type
         (FILLED/REJECTED) → persistent fade/fill pattern

    Uses micro_history which is fetched from candle_micro table by
    feed.py/sim_feed.py before calling analyze_eoc.
    """
    if "HISTORY" in muted:
        return None
    if not micro_history or len(micro_history) < 2:
        return None

    score = 0
    reasons = []

    # Get last 3 micro history entries (most recent last)
    hist = micro_history[-3:] if len(micro_history) >= 3 else micro_history

    # ── 1. CONSECUTIVE PRESSURE ───────────────────────────────────────────
    pressures = [h.get("pressure") for h in hist if h.get("pressure")]
    if len(pressures) >= 3:
        if all(p == "BUYER" for p in pressures):
            # 3 consecutive buyer pressure candles
            # Check if last candle's buy_pct is DECREASING (exhaustion)
            buy_pcts = [h.get("buy_pct", 50) for h in hist]
            if len(buy_pcts) >= 3 and buy_pcts[-1] < buy_pcts[-2] < buy_pcts[-3]:
                # Pressure decreasing → exhaustion → reversal
                score -= 2
                reasons.append(f"HISTORY:-2 PUT buyer-pressure-fading "
                               f"{buy_pcts[-3]}->{buy_pcts[-2]}->{buy_pcts[-1]}")
            else:
                # Strong sustained buyer pressure → continuation
                score += 2
                reasons.append(f"HISTORY:+2 CALL 3x-buyer-pressure "
                               f"bp={buy_pcts[-1]}%")
        elif all(p == "SELLER" for p in pressures):
            sell_pcts = [h.get("sell_pct", 50) for h in hist]
            if len(sell_pcts) >= 3 and sell_pcts[-1] < sell_pcts[-2] < sell_pcts[-3]:
                score += 2
                reasons.append(f"HISTORY:+2 CALL seller-pressure-fading "
                               f"{sell_pcts[-3]}->{sell_pcts[-2]}->{sell_pcts[-1]}")
            else:
                score -= 2
                reasons.append(f"HISTORY:-2 PUT 3x-seller-pressure "
                               f"sp={sell_pcts[-1]}%")

    # ── 2. PRESSURE SHIFT (last candle flipped vs previous) ───────────────
    elif len(pressures) >= 2:
        prev_pressures = pressures[:-1]
        last_pressure = pressures[-1]
        if last_pressure and last_pressure != "FIGHT":
            prev_dominant = max(set(prev_pressures), key=prev_pressures.count)
            if prev_dominant and prev_dominant != "FIGHT" and prev_dominant != last_pressure:
                # Pressure flipped → momentum shift
                if last_pressure == "BUYER":
                    score += 2
                    reasons.append(f"HISTORY:+2 CALL pressure-shift "
                                   f"{prev_dominant}->{last_pressure}")
                elif last_pressure == "SELLER":
                    score -= 2
                    reasons.append(f"HISTORY:-2 PUT pressure-shift "
                                   f"{prev_dominant}->{last_pressure}")

    # ── 3. REACTION STREAK ────────────────────────────────────────────────
    reactions = [h.get("reaction") for h in hist if h.get("reaction")]
    if len(reactions) >= 2:
        last2 = reactions[-2:]
        if all(r == "BUYER" for r in last2):
            # 2 consecutive buyer reactions → strong support building → CALL
            score += 2
            reasons.append("HISTORY:+2 CALL 2x-buyer-reaction-streak")
        elif all(r == "SELLER" for r in last2):
            score -= 2
            reasons.append("HISTORY:-2 PUT 2x-seller-reaction-streak")

    # ── 4. GAP CHAIN ──────────────────────────────────────────────────────
    gap_types = [h.get("gap_type") for h in hist if h.get("gap_type") and h.get("gap_type") != "NONE"]
    if len(gap_types) >= 2:
        last2_gaps = gap_types[-2:]
        if all(g == "FILLED" for g in last2_gaps):
            # 2 consecutive filled gaps → strong mean-reversion market →
            # next gap likely fills too. But this is a market-state signal,
            # not directional, so small score toward fade bias based on
            # last candle direction.
            last_close = candles[-1]["close"] if candles else 0
            last_open = candles[-1]["open"] if candles else 0
            if last_close > last_open:
                # Last candle bullish + filled gaps → next likely bearish (fade)
                score -= 1
                reasons.append("HISTORY:-1 PUT 2x-filled-gap bearish-fade-bias")
            elif last_close < last_open:
                score += 1
                reasons.append("HISTORY:+1 CALL 2x-filled-gap bullish-fade-bias")
        elif all(g == "REJECTED" for g in last2_gaps):
            # 2 consecutive rejected gaps → strong rejection at extremes
            last_close = candles[-1]["close"] if candles else 0
            last_open = candles[-1]["open"] if candles else 0
            if last_close > last_open:
                score += 1
                reasons.append("HISTORY:+1 CALL 2x-rejected-gap bull-confirm")
            elif last_close < last_open:
                score -= 1
                reasons.append("HISTORY:-1 PUT 2x-rejected-gap bear-confirm")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_orderflow(candles, ticks, micro, muted):
    """
    ORDERFLOW - NEW (2026-07-10, Priority 3): Big-money vs retail detector.

    Looks at the DISTRIBUTION of tick sizes within the running candle:
      - "Big ticks" (>95th percentile) = institutional / large-order flow
      - "Retail ticks" (<50th percentile) = small noise trades

    Insight: when big money and retail DISAGREE on direction, big money
    usually wins. A single large buyer tick against a flood of tiny seller
    ticks is a strong bullish signal — the next candle likely opens up.

    Decision matrix:
      - imbalance=1 + big_dir=UP   → CALL (+3)  big buyer stepping in
      - imbalance=1 + big_dir=DOWN → PUT (-3)   big seller unloading
      - big_buy_pct >= 75          → CALL (+2)  big money mostly buying
      - big_buy_pct <= 25          → PUT (-2)   big money mostly selling
    """
    if "ORDERFLOW" in muted:
        return None
    if not micro:
        return None

    of = micro.get("orderflow")
    if not of:
        return None

    score = 0
    reasons = []

    big_dir    = of.get("big_dir", "FLAT")
    ret_dir    = of.get("ret_dir", "FLAT")
    imbalance  = of.get("imbalance", 0)
    big_buy_pct = of.get("big_buy_pct", 50)
    big_up      = of.get("big_up", 0)
    big_dn      = of.get("big_dn", 0)
    big_total   = big_up + big_dn

    # ── 1. Big vs retail disagreement (highest conviction) ────────────────
    # Only fire if we have at least 2 big ticks (avoid single-tick flukes)
    if imbalance == 1 and big_total >= 2:
        if big_dir == "UP":
            score += 3
            reasons.append(
                f"ORDERFLOW:+3 CALL big-buyer-vs-retail-seller "
                f"big_up={big_up} ret_dn={of.get('ret_dn', 0)}")
        elif big_dir == "DOWN":
            score -= 3
            reasons.append(
                f"ORDERFLOW:-3 PUT big-seller-vs-retail-buyer "
                f"big_dn={big_dn} ret_up={of.get('ret_up', 0)}")

    # ── 2. Big-tick volume dominance (medium conviction) ──────────────────
    # If big ticks are 75%+ on one side, that's a directional bias even
    # without retail disagreement.
    elif big_total >= 3:
        if big_buy_pct >= 75:
            score += 2
            reasons.append(
                f"ORDERFLOW:+2 CALL big-buy-dominated "
                f"big_buy_pct={big_buy_pct}% n={big_total}")
        elif big_buy_pct <= 25:
            score -= 2
            reasons.append(
                f"ORDERFLOW:-2 PUT big-sell-dominated "
                f"big_buy_pct={big_buy_pct}% n={big_total}")

    # ── 3. Pure big-tick direction (low conviction, needs many ticks) ─────
    elif big_total >= 5:
        if big_dir == "UP" and big_up >= 4:
            score += 1
            reasons.append(
                f"ORDERFLOW:+1 CALL big-tick-bias-up "
                f"big_up={big_up}/{big_total}")
        elif big_dir == "DOWN" and big_dn >= 4:
            score -= 1
            reasons.append(
                f"ORDERFLOW:-1 PUT big-tick-bias-down "
                f"big_dn={big_dn}/{big_total}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_live_wick(candles, ticks, micro, muted):
    """
    LIVE_WICK - NEW (2026-07-10, Priority 2): Real-time wick rejection detector.

    Unlike REV (which only fires on CLOSED candles), this theory catches
    rejection wicks FORMING on the running candle — often 10-30 seconds
    before the candle closes. This is critical because the next candle's
    direction is heavily influenced by how the current candle rejects at
    extremes.

    Decision matrix:
      - BULL_REJECT (long lower wick + price now rising) → CALL (+3 to +4)
      - BEAR_REJECT (long upper wick + price now falling) → PUT (-3 to -4)

    Score boost: if the rejection is happening at a key swing level
    (from candle history), increase the conviction.
    """
    if "LIVE_WICK" in muted:
        return None
    if not micro:
        return None

    live_wick = micro.get("live_wick")
    if not live_wick:
        return None

    wtype = live_wick.get("type")
    lw_ratio = live_wick.get("lw_ratio", 0)
    uw_ratio = live_wick.get("uw_ratio", 0)

    score = 0
    reasons = []

    # Base score on wick magnitude (bigger wick = stronger rejection)
    if wtype == "BULL_REJECT":
        # Stronger rejection with bigger lower wick
        boost = 4 if lw_ratio > 0.50 else 3
        score += boost
        reasons.append(
            f"LIVE_WICK:+{boost} CALL bull-reject lw={lw_ratio:.2f}")
    elif wtype == "BEAR_REJECT":
        boost = 4 if uw_ratio > 0.50 else 3
        score -= boost
        reasons.append(
            f"LIVE_WICK:-{boost} PUT bear-reject uw={uw_ratio:.2f}")

    # Bonus: if a key swing level is nearby (within 0.3 ATR), this is a
    # higher-conviction reversal signal — the wick is rejecting at a known
    # support/resistance level.
    if candles and score != 0:
        levels = _key_levels(candles)
        atr = _atr(candles)
        last_price = ticks[-1] if ticks else (candles[-1]["close"] if candles else 0)
        for lv in levels:
            if abs(last_price - lv["price"]) < atr * 0.3:
                if wtype == "BULL_REJECT" and lv["type"] == "swing_low":
                    score += 1
                    reasons.append(
                        f"LIVE_WICK:+1 at-swing-low={lv['price']:.5f}")
                    break
                elif wtype == "BEAR_REJECT" and lv["type"] == "swing_high":
                    score -= 1
                    reasons.append(
                        f"LIVE_WICK:-1 at-swing-high={lv['price']:.5f}")
                    break

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_velocity(candles, ticks, micro, muted):
    """
    VELOCITY - NEW (2026-07-10, Priority 1): Late-candle tick-velocity signal.

    Uses the running candle's LAST-5 / LAST-10 tick velocity and consecutive
    tick streaks to detect end-of-candle momentum that predicts the NEXT
    candle's direction. This is a HIGH-CONVICTION theory because:

      1. Last-5 ticks carry the most recent market sentiment
      2. V-shape patterns (5-up→5-down or vice versa) signal rejection
      3. Acceleration (last-5 faster than last-10) = momentum building
      4. This fires on running_ticks during LIVE re-eval, NOT just at EOC

    Decision matrix:
      - V_TOP pattern (5-up → 5-down) → strong PUT (reversal at top)
      - V_BOTTOM pattern (5-down → 5-up) → strong CALL (reversal at bottom)
      - last-5 + last-10 same direction + accelerating → continuation
      - last-5 opposite to last-10 + accelerating → reversal spike
    """
    if "VELOCITY" in muted:
        return None
    # Note: ticks may be empty in LIVE re-eval mode (closed candle has no
    # ticks; running_ticks data lives in `micro`). Only require micro.
    if not micro:
        return None

    last_vel = micro.get("last_velocity")
    v_shape  = micro.get("v_shape")
    streaks  = micro.get("streaks", [])
    if not last_vel and not v_shape:
        return None

    score = 0
    reasons = []

    # ── 1. V-shape reversal (highest conviction) ───────────────────────────
    if v_shape == "V_TOP":
        # 5-up → 5-down = top rejection → next candle PUT
        score -= 4
        reasons.append("VELOCITY:-4 PUT V-top-reversal (5up→5down)")
    elif v_shape == "V_BOTTOM":
        score += 4
        reasons.append("VELOCITY:+4 CALL V-bottom-reversal (5down→5up)")

    if last_vel:
        dir5  = last_vel.get("dir5")
        dir10 = last_vel.get("dir10")
        accel = last_vel.get("accel", 1.0)
        spd5  = abs(last_vel.get("spd5", 0))
        spd10 = abs(last_vel.get("spd10", 0))

        # ── 2. Last-5 strong + same dir as last-10 = continuation ─────────
        if dir5 == dir10 and dir5 != "FLAT":
            if accel > 1.3 and spd5 > 0:
                # Accelerating in same direction → strong continuation
                boost = 3 if accel > 1.8 else 2
                if dir5 == "UP":
                    score += boost
                    reasons.append(
                        f"VELOCITY:+{boost} CALL accel-up "
                        f"spd5={spd5:.7f} accel={accel:.1f}")
                else:
                    score -= boost
                    reasons.append(
                        f"VELOCITY:-{boost} PUT accel-down "
                        f"spd5={spd5:.7f} accel={accel:.1f}")

        # ── 3. Last-5 opposite to last-10 = reversal spike ────────────────
        elif dir5 != dir10 and dir5 != "FLAT" and dir10 != "FLAT":
            # Spike against the longer trend → next candle follows the spike
            if accel > 1.2:  # spike is accelerating (not just noise)
                if dir5 == "UP":
                    score += 3
                    reasons.append(
                        f"VELOCITY:+3 CALL spike-reversal-up "
                        f"dir10={dir10} accel={accel:.1f}")
                else:
                    score -= 3
                    reasons.append(
                        f"VELOCITY:-3 PUT spike-reversal-down "
                        f"dir10={dir10} accel={accel:.1f}")

        # ── 4. Deceleration = exhaustion ──────────────────────────────────
        elif dir5 == dir10 and dir5 != "FLAT" and accel < 0.4:
            # Same direction but losing speed → exhaustion → reversal likely
            if dir5 == "UP":
                score -= 2
                reasons.append(
                    f"VELOCITY:-2 PUT bull-exhaust-decel accel={accel:.1f}")
            else:
                score += 2
                reasons.append(
                    f"VELOCITY:+2 CALL bear-exhaust-decel accel={accel:.1f}")

    # ── 5. Long single-direction streak (>=6) = exhaustion candidate ──────
    if streaks:
        last_d, last_l = streaks[-1]
        if last_l >= 6:
            # 6+ ticks in one direction = overextended → reversal likely
            if last_d > 0:
                score -= 1
                reasons.append(f"VELOCITY:-1 PUT long-bull-streak={last_l}")
            elif last_d < 0:
                score += 1
                reasons.append(f"VELOCITY:+1 CALL long-bear-streak={last_l}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_mean(candles, ticks, muted):
    """
    MEAN - NEW (2026-07-10): Mean-reversion detector.

    In mean-reverting OTC markets (Quotex 60s) the most reliable signal is
    REVERSAL after an overextended move. This theory looks at:
      1. Closed candle's body size vs ATR (oversized = exhaustion likely)
      2. RSI extreme at close (overbought/oversold)
      3. Long wick on the OPPOSITE side of the body (failed continuation)
      4. Consecutive same-direction candles beyond normal (3+)

    Whenever these conditions fire, vote for REVERSAL of the closed candle's
    direction. This is the symmetric opposite of CON and the dominant edge
    in the market this app was wrong in.
    """
    if "MEAN" in muted:
        return None
    if len(candles) < 5:
        return None

    last = candles[-1]
    body = last["close"] - last["open"]
    abs_body = abs(body)
    atr = _atr(candles)
    if atr <= 0 or abs_body < atr * 0.05:
        return None  # too small to mean anything

    score = 0
    reasons = []

    # ── 1. Oversized body = exhaustion candidate ───────────────────────────
    body_ratio = abs_body / atr  # 1.0 = body fills ATR
    if body_ratio > 1.4:
        # Big body in either direction → expect reversal next candle
        if body > 0:
            score -= 3
            reasons.append(f"MEAN:-3 PUT big-bull-body={body_ratio:.2f}xATR")
        else:
            score += 3
            reasons.append(f"MEAN:+3 CALL big-bear-body={body_ratio:.2f}xATR")

    # ── 2. RSI extreme ─────────────────────────────────────────────────────
    closes = [c["close"] for c in candles]
    rsi = _rsi(closes)
    if rsi >= 75:
        score -= 2
        reasons.append(f"MEAN:-2 PUT rsi-overbought={rsi:.0f}")
    elif rsi <= 25:
        score += 2
        reasons.append(f"MEAN:+2 CALL rsi-oversold={rsi:.0f}")

    # ── 3. Failed-continuation wick (long wick opposite to close direction) ─
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    if body > 0 and upper_wick > abs_body * 1.2:
        # Bullish body but big upper wick = buyers failed → reversal
        score -= 2
        reasons.append(f"MEAN:-2 PUT failed-bull-continuation wick={upper_wick:.6f}")
    elif body < 0 and lower_wick > abs_body * 1.2:
        score += 2
        reasons.append(f"MEAN:+2 CALL failed-bear-continuation wick={lower_wick:.6f}")

    # ── 4. 4+ same-direction candles = trend exhaustion ────────────────────
    if len(candles) >= 4:
        dirs = []
        for c in candles[-4:]:
            if c["close"] > c["open"]:
                dirs.append(1)
            elif c["close"] < c["open"]:
                dirs.append(-1)
            else:
                dirs.append(0)
        if all(d > 0 for d in dirs):
            score -= 2
            reasons.append("MEAN:-2 PUT 4-bull-exhaustion")
        elif all(d < 0 for d in dirs):
            score += 2
            reasons.append("MEAN:+2 CALL 4-bear-exhaustion")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_shift(candles, ticks, muted):
    """
    SHIFT - NEW: Multi-candle momentum shift detection.

    Looks at the last 5 candles' tick-weighted pressure to detect if
    the overall momentum is shifting direction. This catches trend
    changes that single-candle analysis misses.
    """
    if "SHIFT" in muted:
        return None
    if len(candles) < 5:
        return None

    # We can't access past ticks, so use candle body analysis instead
    # Look at: body sizes shrinking + direction changes
    last5 = candles[-5:]
    bodies = []
    for c in last5:
        body = c["close"] - c["open"]
        bodies.append(body)

    score = 0
    reasons = []

    # Count consecutive same-direction candles
    dirs = [1 if b > 0 else (-1 if b < 0 else 0) for b in bodies]

    # Check for 3+ same direction then opposite
    if len(dirs) >= 4:
        if all(d >= 0 for d in dirs[:3]) and dirs[3] < 0 and dirs[4] < 0:
            # 3 bullish then 2 bearish → shift to PUT
            # But check body sizes: are the bearish bodies growing?
            bear_sizes = [abs(b) for b in bodies[3:]]
            bull_sizes = [abs(b) for b in bodies[:3]]
            avg_bear = sum(bear_sizes) / len(bear_sizes)
            avg_bull = sum(bull_sizes) / len(bull_sizes)
            if avg_bear > avg_bull * 0.5:
                score -= 3
                reasons.append("SHIFT:-3 PUT 3bull->2bear shift")
        elif all(d <= 0 for d in dirs[:3]) and dirs[3] > 0 and dirs[4] > 0:
            bear_sizes = [abs(b) for b in bodies[:3]]
            bull_sizes = [abs(b) for b in bodies[3:]]
            avg_bear = sum(bear_sizes) / len(bear_sizes)
            avg_bull = sum(bull_sizes) / len(bull_sizes)
            if avg_bull > avg_bear * 0.5:
                score += 3
                reasons.append("SHIFT:+3 CALL 3bear->2bull shift")

    # Also check: shrinking bodies in current direction = exhaustion
    if len(bodies) >= 3:
        recent_bodies = [abs(b) for b in bodies[-3:]]
        if recent_bodies[0] > recent_bodies[1] > recent_bodies[2]:
            if bodies[-1] > 0:
                # Shrinking bullish → reversal to PUT
                score -= 2
                reasons.append("SHIFT:-2 PUT shrinking-bull-bodies")
            elif bodies[-1] < 0:
                score += 2
                reasons.append("SHIFT:+2 CALL shrinking-bear-bodies")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


# ═══════════════════════════════════════════════════════════════════════════════
#  LIQUIDITY THEORIES (2026-07-10) — SMC concepts adapted for OTC
#  Since OTC has no real volume, these use price structure only:
#    FVG       — Fair Value Gap (3-candle imbalance, gap-fill fade)
#    OB        — Order Block (last opposite candle before strong move)
#    SWEEP     — Liquidity sweep / stop hunt (wick breaks swing, close reverses)
#    STRUCT    — BOS (continuation) / CHOCH (reversal) structure break
# ═══════════════════════════════════════════════════════════════════════════════

def _find_fvgs(candles, lookback=20):
    """
    Detect unfilled Fair Value Gaps in the last `lookback` candles.

    Bullish FVG: candles[i-1].high < candles[i+1].low  (gap up)
        → expect price to come DOWN to fill it (fade from above)
    Bearish FVG: candles[i-1].low > candles[i+1].high  (gap down)
        → expect price to come UP to fill it (fade from below)

    An FVG is "filled" when a later candle's range covers the entire gap.
    Returns list of {"type": "bull"|"bear", "gap_low": f, "gap_high": f, "idx": int}
    for UNFILLED gaps only.
    """
    if len(candles) < 3:
        return []
    fvgs = []
    start = max(1, len(candles) - lookback)
    for i in range(start, len(candles) - 1):
        prev_high = candles[i - 1]["high"]
        prev_low  = candles[i - 1]["low"]
        next_low  = candles[i + 1]["low"]
        next_high = candles[i + 1]["high"]

        # Bullish FVG (gap up between prev_high and next_low)
        if next_low > prev_high:
            gap_low, gap_high = prev_high, next_low
            filled = False
            for j in range(i + 2, len(candles)):
                if candles[j]["low"] <= gap_low:
                    filled = True
                    break
            if not filled:
                fvgs.append({"type": "bull", "gap_low": gap_low,
                             "gap_high": gap_high, "idx": i})

        # Bearish FVG (gap down between prev_low and next_high)
        elif next_high < prev_low:
            gap_low, gap_high = next_high, prev_low
            filled = False
            for j in range(i + 2, len(candles)):
                if candles[j]["high"] >= gap_high:
                    filled = True
                    break
            if not filled:
                fvgs.append({"type": "bear", "gap_low": gap_low,
                             "gap_high": gap_high, "idx": i})
    return fvgs


def _theory_fvg(candles, muted):
    """
    FVG — Fair Value Gap theory.

    SMC doctrine: price tends to fill unfilled FVGs. In mean-reverting OTC
    markets this is especially reliable because the broker's price generator
    almost always reverts to fill algorithmic gaps.

    Vote logic:
      - Just-formed FVG (within last 3 candles) AND price far from gap
        → continuation toward gap (price seeks the gap): ±2
      - Recent FVG (within last 8 candles) AND price now INSIDE or AT gap
        → fade (gap about to be filled): ±3
    """
    if "FVG" in muted:
        return None
    if len(candles) < 4:
        return None

    fvgs = _find_fvgs(candles, lookback=20)
    if not fvgs:
        return None

    last = candles[-1]
    close = last["close"]
    atr = _atr(candles)
    if atr <= 0:
        return None

    score = 0
    reasons = []

    for fvg in fvgs:
        gap_mid = (fvg["gap_low"] + fvg["gap_high"]) / 2
        age = len(candles) - 1 - fvg["idx"]  # candles since FVG formed
        gap_size = fvg["gap_high"] - fvg["gap_low"]
        gap_ratio = gap_size / atr

        # Too small a gap = noise, skip
        if gap_ratio < 0.15:
            continue

        # Case 1: price is currently INSIDE the gap → fill in progress
        if fvg["gap_low"] <= close <= fvg["gap_high"]:
            # Bullish FVG filled from above → expect continuation DOWN through gap
            if fvg["type"] == "bull":
                score -= 3
                reasons.append(
                    f"FVG:-3 PUT filling-bull-gap age={age} "
                    f"size={gap_ratio:.2f}xATR")
            else:
                score += 3
                reasons.append(
                    f"FVG:+3 CALL filling-bear-gap age={age} "
                    f"size={gap_ratio:.2f}xATR")
            continue

        # Case 2: price approaching gap from outside (within 0.5 ATR)
        if fvg["type"] == "bull" and close > fvg["gap_high"]:
            dist = close - fvg["gap_high"]
            if dist < atr * 0.5:
                # Price above bullish gap → expect drop to fill
                score -= 2
                reasons.append(
                    f"FVG:-2 PUT approaching-bull-gap dist={dist/atr:.2f}xATR "
                    f"age={age}")
        elif fvg["type"] == "bear" and close < fvg["gap_low"]:
            dist = fvg["gap_low"] - close
            if dist < atr * 0.5:
                # Price below bearish gap → expect rise to fill
                score += 2
                reasons.append(
                    f"FVG:+2 CALL approaching-bear-gap dist={dist/atr:.2f}xATR "
                    f"age={age}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _find_order_blocks(candles, lookback=15):
    """
    Find Order Blocks: the last opposite-color candle before a strong
    directional displacement move.

    Bullish OB: last BEARISH candle before a strong BULLISH move (>=1.2x ATR body)
    Bearish OB: last BULLISH candle before a strong BEARISH move

    Returns list of {"type": "bull"|"bear", "ob_low": f, "ob_high": f,
                     "ob_body": f, "idx": int, "displacement": f}
    """
    if len(candles) < 4:
        return []
    atr = _atr(candles)
    if atr <= 0:
        return []

    obs = []
    start = max(2, len(candles) - lookback)
    for i in range(start, len(candles)):
        c = candles[i]
        body = c["close"] - c["open"]
        abs_body = abs(body)
        # Need a strong displacement move (>=1.2x ATR body)
        if abs_body < atr * 1.2:
            continue

        # Find the most recent opposite-color candle before this one
        for j in range(i - 1, max(0, i - 4) - 1, -1):
            prev = candles[j]
            prev_body = prev["close"] - prev["open"]
            if body > 0 and prev_body < 0:  # Bullish move, bearish prev = Bullish OB
                obs.append({
                    "type": "bull",
                    "ob_low": prev["low"],
                    "ob_high": prev["high"],
                    "ob_body": prev_body,
                    "idx": j,
                    "displacement": abs_body,
                })
                break
            if body < 0 and prev_body > 0:  # Bearish move, bullish prev = Bearish OB
                obs.append({
                    "type": "bear",
                    "ob_low": prev["low"],
                    "ob_high": prev["high"],
                    "ob_body": prev_body,
                    "idx": j,
                    "displacement": abs_body,
                })
                break
    return obs[-5:]  # keep last 5


def _theory_ob(candles, muted):
    """
    OB — Order Block theory.

    An OB acts as support/resistance when price revisits it. In OTC the
    broker's mean-reverting generator is especially prone to bouncing off
    recent structural zones, so this is a high-quality edge.

    Vote logic:
      - Price currently INSIDE a recent bullish OB zone → CALL (bounce up)
      - Price currently INSIDE a recent bearish OB zone → PUT (bounce down)
      - Strength scales with OB recency (age < 3 → ±3, age < 8 → ±2, else ±1)
    """
    if "OB" in muted:
        return None
    if len(candles) < 5:
        return None

    obs = _find_order_blocks(candles, lookback=15)
    if not obs:
        return None

    last = candles[-1]
    close = last["close"]
    atr = _atr(candles)
    if atr <= 0:
        return None

    score = 0
    reasons = []

    for ob in obs:
        age = len(candles) - 1 - ob["idx"]
        if age > 12:
            continue  # too stale

        # Recency-based base score
        if age <= 3:
            base = 3
        elif age <= 8:
            base = 2
        else:
            base = 1

        # Displacement strength bonus (stronger OB = more reliable)
        disp_ratio = ob["displacement"] / atr
        if disp_ratio > 1.8:
            base += 1

        # Price currently inside OB zone?
        if ob["ob_low"] <= close <= ob["ob_high"]:
            if ob["type"] == "bull":
                score += base
                reasons.append(
                    f"OB:+{base} CALL inside-bull-OB age={age} "
                    f"disp={disp_ratio:.2f}xATR")
            else:
                score -= base
                reasons.append(
                    f"OB:-{base} PUT inside-bear-OB age={age} "
                    f"disp={disp_ratio:.2f}xATR")

        # Price approaching OB from outside (within 0.3 ATR)?
        elif ob["type"] == "bull" and close > ob["ob_high"]:
            dist = close - ob["ob_high"]
            if dist < atr * 0.3:
                score += base - 1  # slightly weaker when not yet inside
                if base - 1 > 0:
                    reasons.append(
                        f"OB:+{base-1} CALL approaching-bull-OB "
                        f"dist={dist/atr:.2f}xATR age={age}")
        elif ob["type"] == "bear" and close < ob["ob_low"]:
            dist = ob["ob_low"] - close
            if dist < atr * 0.3:
                score -= base - 1
                if base - 1 > 0:
                    reasons.append(
                        f"OB:-{base-1} PUT approaching-bear-OB "
                        f"dist={dist/atr:.2f}xATR age={age}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_sweep(candles, muted):
    """
    SWEEP — Liquidity sweep / stop-hunt detector.

    Classic institutional pattern:
      Bullish sweep (PUT → CALL reversal):
        - Candle's LOW breaks below a recent swing low (stop hunt)
        - But candle CLOSES back above the swept swing low
        → Stops were grabbed, reversal up likely
      Bearish sweep (CALL → PUT reversal):
        - Candle's HIGH breaks above a recent swing high
        - But candle CLOSES back below the swept swing high
        → Reversal down likely

    This is one of the strongest SMC reversal signals. In OTC the broker
    algorithm intentionally creates these sweeps against round-number /
    swing clusters. Heavy weight when wick-to-body ratio is extreme.
    """
    if "SWEEP" in muted:
        return None
    if len(candles) < 6:
        return None

    levels = _key_levels(candles[:-1])  # exclude last candle from level calc
    if not levels:
        return None

    last = candles[-1]
    last_low = last["low"]
    last_high = last["high"]
    last_close = last["close"]
    last_open = last["open"]
    body = last_close - last_open
    abs_body = abs(body)
    atr = _atr(candles)
    if atr <= 0 or abs_body < atr * 0.05:
        return None

    score = 0
    reasons = []

    for lv in levels:
        age = len(candles) - 1 - lv["idx"]
        if age > 10:
            continue  # stale level

        if lv["type"] == "swing_low":
            # Bullish sweep: low pierces below swing_low, close back above
            if (last_low < lv["price"]
                    and last_close > lv["price"]
                    and last_low < lv["price"] - atr * 0.05):  # meaningful pierce
                # Sweep magnitude = how far below the level we went
                pierce = lv["price"] - last_low
                pierce_ratio = pierce / atr
                # Wick-to-body ratio: long lower wick + small body = strong sweep
                lower_wick = min(last_open, last_close) - last_low
                wick_ratio = lower_wick / abs_body if abs_body > 0 else 0

                base = 3
                if pierce_ratio > 0.3:
                    base += 1  # deep sweep
                if wick_ratio > 2.0:
                    base += 1  # very long wick (strong rejection)

                score += base
                reasons.append(
                    f"SWEEP:+{base} CALL bull-sweep swing-low={lv['price']:.5f} "
                    f"pierce={pierce_ratio:.2f}xATR wick={wick_ratio:.2f}x "
                    f"age={age}")

        elif lv["type"] == "swing_high":
            # Bearish sweep: high pierces above swing_high, close back below
            if (last_high > lv["price"]
                    and last_close < lv["price"]
                    and last_high > lv["price"] + atr * 0.05):
                pierce = last_high - lv["price"]
                pierce_ratio = pierce / atr
                upper_wick = last_high - max(last_open, last_close)
                wick_ratio = upper_wick / abs_body if abs_body > 0 else 0

                base = 3
                if pierce_ratio > 0.3:
                    base += 1
                if wick_ratio > 2.0:
                    base += 1

                score -= base
                reasons.append(
                    f"SWEEP:-{base} PUT bear-sweep swing-high={lv['price']:.5f} "
                    f"pierce={pierce_ratio:.2f}xATR wick={wick_ratio:.2f}x "
                    f"age={age}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


def _theory_structure(candles, muted):
    """
    STRUCT — Break of Structure (BOS) / Change of Character (CHOCH).

    Identifies the prevailing structure using the last 2 swing highs and
    last 2 swing lows, then checks if the just-closed candle broke it.

      BOS (continuation):
        - In uptrend (HH + HL): close above last swing high → CALL continuation
        - In downtrend (LH + LL): close below last swing low → PUT continuation
        Score: ±2

      CHOCH (reversal):
        - In uptrend: close below last swing low → PUT (first sign of reversal)
        - In downtrend: close above last swing high → CALL
        Score: ±3 to ±4 (depending on body strength)

    In OTC mean-reverting markets, CHOCH is the higher-quality signal
    because trends rarely persist — the first counter-trend break usually
    DOES mark the reversal. BOS gets lower weight because trend
    continuation is the minority case in 60s OTC.
    """
    if "STRUCT" in muted:
        return None
    if len(candles) < 8:
        return None

    levels = _key_levels(candles[:-1])  # exclude last candle
    swing_highs = [lv for lv in levels if lv["type"] == "swing_high"][-2:]
    swing_lows  = [lv for lv in levels if lv["type"] == "swing_low"][-2:]
    # Need at least 2 of each to determine structure (HH/HL or LH/LL)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    last = candles[-1]
    close = last["close"]
    body = last["close"] - last["open"]
    abs_body = abs(body)
    atr = _atr(candles)
    if atr <= 0:
        return None

    # Determine structure: compare last two swing highs and last two swing lows
    sh1, sh2 = swing_highs[-2], swing_highs[-1]  # older, newer
    sl1, sl2 = swing_lows[-2],  swing_lows[-1]

    making_higher_highs = sh2["price"] > sh1["price"]
    making_higher_lows  = sl2["price"] > sl1["price"]
    making_lower_highs  = sh2["price"] < sh1["price"]
    making_lower_lows   = sl2["price"] < sl1["price"]

    last_sh = sh2["price"]
    last_sl = sl2["price"]

    score = 0
    reasons = []

    # ── BOS (continuation) ────────────────────────────────────────────────
    if making_higher_highs and making_higher_lows and close > last_sh:
        # Uptrend continuation: close broke above last swing high
        score += 2
        body_ratio = abs_body / atr
        if body_ratio > 1.2:
            score += 1
            reasons.append(
                f"STRUCT:+3 CALL bull-BOS close={close:.5f}>HH={last_sh:.5f} "
                f"body={body_ratio:.2f}xATR")
        else:
            reasons.append(
                f"STRUCT:+2 CALL bull-BOS close={close:.5f}>HH={last_sh:.5f}")

    elif making_lower_highs and making_lower_lows and close < last_sl:
        # Downtrend continuation: close broke below last swing low
        score -= 2
        body_ratio = abs_body / atr
        if body_ratio > 1.2:
            score -= 1
            reasons.append(
                f"STRUCT:-3 PUT bear-BOS close={close:.5f}<LL={last_sl:.5f} "
                f"body={body_ratio:.2f}xATR")
        else:
            reasons.append(
                f"STRUCT:-2 PUT bear-BOS close={close:.5f}<LL={last_sl:.5f}")

    # ── CHOCH (reversal) ──────────────────────────────────────────────────
    elif making_higher_highs and making_higher_lows and close < last_sl:
        # Uptrend broken from below → bearish CHOCH
        score -= 3
        body_ratio = abs_body / atr
        if body_ratio > 1.4:
            score -= 1  # strong body confirms the reversal
            reasons.append(
                f"STRUCT:-4 PUT bear-CHOCH uptrend-broken close={close:.5f}<"
                f"HL={last_sl:.5f} body={body_ratio:.2f}xATR")
        else:
            reasons.append(
                f"STRUCT:-3 PUT bear-CHOCH uptrend-broken close={close:.5f}<"
                f"HL={last_sl:.5f}")

    elif making_lower_highs and making_lower_lows and close > last_sh:
        # Downtrend broken from above → bullish CHOCH
        score += 3
        body_ratio = abs_body / atr
        if body_ratio > 1.4:
            score += 1
            reasons.append(
                f"STRUCT:+4 CALL bull-CHOCH downtrend-broken close={close:.5f}>"
                f"LH={last_sh:.5f} body={body_ratio:.2f}xATR")
        else:
            reasons.append(
                f"STRUCT:+3 CALL bull-CHOCH downtrend-broken close={close:.5f}>"
                f"LH={last_sh:.5f}")

    if score == 0:
        return None
    return "CALL" if score > 0 else "PUT", score, reasons


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_eoc(candles, ticks, micro_history=None, period=60,
                muted=None, asset="", running_ticks=None,
                recent_accuracy=None, recent_n=0, currently_flipped=False):
    """
    Main entry point: run all theories and blend into a signal.

    Returns dict with: signal, score, confidence, strength, agree, reasons,
                        regime, market_state, theories_detail

    ADAPTIVE INVERSION (2026-07-10 fix for inverted predictions):
      recent_accuracy  -> float in [0,1] = correct / total of last N graded
                          predictions for THIS asset/period (from db).
                          If None, no adaptive logic is applied.
      recent_n         -> how many recent predictions the accuracy was
                          computed over (used to gate the flip — needs ≥8
      currently_flipped -> whether the PREVIOUS candle's signal was already
                          adaptive-flipped. Adds hysteresis: entering the
                          flip needs accuracy < 40%, but once flipped it
                          takes accuracy climbing back > 55% to revert —
                          without this a flip that starts working pushes
                          accuracy just over 40% and immediately un-flips
                          itself, oscillating candle to candle.
                          samples before flipping, otherwise too noisy).

    If recent_accuracy < 0.40 AND recent_n >= 8  =>  flip CALL<->PUT at the
    end.  This is the safety net against systematic model bias (e.g. when
    the market is strongly mean-reverting and continuation theories vote
    the wrong way on every candle).  The flip is reflected in `signal`,
    `score`, `reasons`, and a `_flipped: True` flag is set on the result.
    """
    if muted is None:
        muted = {}

    if len(candles) < 5:
        return {"signal": "NEUTRAL", "score": 0, "confidence": 0,
                "strength": "WEAK", "agree": 0, "total": 0,
                "reasons": ["INSUFFICIENT_DATA"], "regime": {},
                "market_state": {}}

    regime = _classify_regime(candles)
    all_reasons = []
    call_score = 0
    put_score = 0
    agree = 0
    total_fired = 0
    theories_detail = []

    # ── Build micro from closed-candle ticks (the PRIMARY micro input) ────
    # This is the microstructure of the JUST-CLOSED candle — the core signal.
    closed_micro = None
    if ticks and len(ticks) >= 10:
        closed_micro = _build_micro(ticks, candles[-1]["open"])

    # ── Build micro from running_ticks if provided (for LIVE re-eval) ─────
    running_micro = None
    if running_ticks and len(running_ticks) >= 10:
        op = candles[-1]["close"] if candles else running_ticks[0]
        running_micro = _build_micro(running_ticks, op)

    # ── Run all theories ──────────────────────────────────────────────────
    # Use closed_micro for theories that analyze the closed candle
    # Use running_micro for the RUN theory when doing live re-eval
    run_micro = running_micro if running_micro else closed_micro

    theories = [
        ("CON",        lambda: _theory_con(candles, muted)),
        ("REV",        lambda: _theory_rev(candles, muted)),
        ("RUN",        lambda: _theory_run(candles, ticks, run_micro, muted)),
        ("TRAP",       lambda: _theory_trap(candles, ticks, muted)),
        ("GAP",        lambda: _theory_gap(candles, muted)),
        ("LAST",       lambda: _theory_last(candles, ticks, muted)),
        ("RNG",        lambda: _theory_rng(candles, muted)),
        ("MICRO",      lambda: _theory_micro(candles, ticks, muted)),
        ("MEAN",       lambda: _theory_mean(candles, ticks, muted)),
        ("SHIFT",      lambda: _theory_shift(candles, ticks, muted)),
        ("VELOCITY",   lambda: _theory_velocity(candles, ticks, run_micro, muted)),
        ("LIVE_WICK",  lambda: _theory_live_wick(candles, ticks, run_micro, muted)),
        ("ORDERFLOW",  lambda: _theory_orderflow(candles, ticks, run_micro, muted)),
        # Multi-candle theories (2026-07-10)
        ("MOMENTUM",   lambda: _theory_momentum(candles, muted)),
        ("CONTINUITY", lambda: _theory_continuity(candles, ticks, muted)),
        ("HISTORY",    lambda: _theory_history(candles, ticks, micro_history, muted)),
        # Liquidity / SMC theories (2026-07-10) — price-structure only
        # since OTC has no real volume. See ANALYSIS_running_candle.md.
        ("FVG",        lambda: _theory_fvg(candles, muted)),
        ("OB",         lambda: _theory_ob(candles, muted)),
        ("SWEEP",      lambda: _theory_sweep(candles, muted)),
        ("STRUCT",     lambda: _theory_structure(candles, muted)),
    ]

    # MST is special — returns (result, market_state)
    mst_result = None
    market_state = {}
    if "MST" not in muted:
        try:
            mst = _theory_mst(candles, muted)
            if mst:
                if isinstance(mst, tuple) and len(mst) == 2:
                    mst_result, market_state = mst
                if mst_result:
                    total_fired += 1
                    sig, sc, rs = mst_result
                    all_reasons.extend(rs)
                    if sig == "CALL":
                        call_score += abs(sc)
                    else:
                        put_score += abs(sc)
                    theories_detail.append({"code": "MST", "vote": sig, "score": sc})
        except Exception as e:
            print(f"[analyze] theory MST error: {e}")

    for code, fn in theories:
        try:
            result = fn()
            if result is None:
                continue
            sig, sc, rs = result
            total_fired += 1
            all_reasons.extend(rs)
            if sig == "CALL":
                call_score += abs(sc)
            else:
                put_score += abs(sc)
            theories_detail.append({"code": code, "vote": sig, "score": sc})
        except Exception as e:
            print(f"[analyze] theory {code} error: {e}")

    # ── Blend ─────────────────────────────────────────────────────────────
    net = call_score - put_score
    total = call_score + put_score

    if total == 0 or total_fired == 0:
        return {"signal": "NEUTRAL", "score": 0, "confidence": 0,
                "strength": "WEAK", "agree": 0, "total": 0,
                "reasons": all_reasons or ["NO_THEORY_FIRED"],
                "regime": regime, "market_state": market_state,
                "theories_detail": theories_detail, "_flipped": False}

    agree = max(call_score, put_score)
    confidence = round(agree / total * 100)
    majority = "CALL" if net > 0 else "PUT"

    # Strength thresholds — TIGHTENED
    if confidence >= 65 and abs(net) >= 5:
        strength = "STRONG"
    elif confidence >= 52:
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    # Dead band: TIGHTENED from |net|<2/confidence<45 to |net|<1/confidence<40
    # This lets more signals through while still killing noise
    if abs(net) < 1 or confidence < 40:
        return {"signal": "NEUTRAL", "score": net, "confidence": confidence,
                "strength": "WEAK", "agree": agree, "total": total_fired,
                "reasons": all_reasons, "regime": regime,
                "market_state": market_state,
                "theories_detail": theories_detail, "_flipped": False}

    # ── ADAPTIVE INVERSION (2026-07-10 fix for inverted predictions) ──────
    # If recent_accuracy < 0.40 over a sufficient sample, the model is
    # systematically wrong — flip CALL<->PUT. This is the safety net that
    # turns a 100%-inverted model into a 60%+ correct model.
    # Hysteresis (2026-07-10): entering the flip uses the 40% floor; once
    # flipped, only a recovery above 55% reverts it. Otherwise a flip that
    # starts working nudges accuracy just past 40% and un-flips itself right
    # back into the bad state on the very next candle.
    #
    # TARGETED INVERSION (2026-07-10 review):
    #   Previously flipped ALL theories at once. Problem: if some theories
    #   (e.g. REV, MEAN) were already correct, flipping them made them wrong.
    #   New logic: only flip the theories that have a CONTINUATION bias
    #   (CON, MST, RUN, MICRO, GAP, MOMENTUM, CONTINUITY). Reversal theories
    #   (REV, TRAP, LAST, MEAN, FVG, OB, SWEEP, STRUCT, VELOCITY, LIVE_WICK)
    #   are left alone — they were right.
    flip_threshold = 0.55 if currently_flipped else 0.40
    flipped = False
    if (recent_accuracy is not None
            and recent_n >= 8
            and recent_accuracy < flip_threshold):
        # Theories that vote CONTINUATION by default — these get inverted
        CONTINUATION_BIASED = {
            "CON", "MST", "RUN", "MICRO", "GAP", "MOMENTUM", "CONTINUITY",
            "HISTORY",  # HISTORY inherits whatever direction it saw = continuation
            "SHIFT",    # SHIFT also inherits direction = continuation
        }
        # Invert the per-theory votes (only continuation-biased ones)
        for td in theories_detail:
            if td["code"] in CONTINUATION_BIASED:
                td["vote"] = "PUT" if td["vote"] == "CALL" else "CALL"
                td["score"] = -td["score"]
        # Recompute the blend from the (partially) inverted theories_detail
        call_score = sum(abs(td["score"]) for td in theories_detail
                         if td["vote"] == "CALL")
        put_score = sum(abs(td["score"]) for td in theories_detail
                        if td["vote"] == "PUT")
        net = call_score - put_score
        total = call_score + put_score
        if total > 0:
            agree = max(call_score, put_score)
            confidence = round(agree / total * 100)
            majority = "CALL" if net > 0 else "PUT"
            # Recompute strength
            if confidence >= 65 and abs(net) >= 5:
                strength = "STRONG"
            elif confidence >= 52:
                strength = "MEDIUM"
            else:
                strength = "WEAK"
        flipped = True
        all_reasons.append(
            f"INVERT:+1 {majority} targeted-flip-continuation-theories "
            f"(recent_acc={recent_accuracy:.0%} over n={recent_n})")

    return {
        "signal": majority,
        "score": net,
        "confidence": confidence,
        "strength": strength,
        "agree": agree,
        "total": total_fired,
        "reasons": all_reasons,
        "regime": regime,
        "market_state": market_state,
        "theories_detail": theories_detail,
        "_flipped": flipped,
    }


# Keep the old function name for compatibility with feed.py's import
def _build_micro_from_ticks(ticks, open_price):
    """Compatibility wrapper — delegates to _build_micro."""
    return _build_micro(ticks, open_price)