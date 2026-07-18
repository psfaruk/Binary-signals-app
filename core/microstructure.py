"""
core/microstructure.py — Tick-level microstructure analysis.

Single source of truth for the rich ~20-key microstructure dict computed
from a tick list. Previously duplicated as:
  - analyze_eoc._build_micro   (rich version, used by sim_feed)
  - feed._analyze_microstructure (simpler version, used by real feed)

Now consolidated. Both feeds import from here so they produce identical
microstructure data, eliminating drift between real and simulated feeds.

Returns a dict with ~20 keys including:
  buy_pct, sell_pct, pressure, is_fight, crosses,
  hold_price, hold_visits, phases, reaction, net,
  tick_count, last_react, tick_speed, momentum_shift,
  last_velocity, streaks, v_shape,
  td_buy_pct, td_sell_pct, td_diverge, vap_migration,
  live_wick, orderflow

Returns ``None`` when ``len(ticks) < 10``.
"""


def build_micro(ticks, open_price):
    """Build rich microstructure from a tick list.

    Args:
        ticks: iterable of float tick prices for the current candle
        open_price: the candle's open price

    Returns:
        dict with ~20 microstructure keys, or None if len(ticks) < 10.
    """
    ticks = list(ticks)
    if len(ticks) < 10:
        return None
    op = open_price
    hi = max(ticks)
    lo = min(ticks)
    cur = ticks[-1]
    rng = hi - lo
    n = len(ticks)

    FIGHT_CROSSES = 4

    # ── 1. Tick-weighted buyer/seller pressure ─────────────────────────────
    raw_buy_vol = 0.0
    raw_sell_vol = 0.0
    up_count = 0
    dn_count = 0
    for i in range(1, n):
        delta = ticks[i] - ticks[i - 1]
        if delta > 0:
            raw_buy_vol += delta
            up_count += 1
        elif delta < 0:
            raw_sell_vol += abs(delta)
            dn_count += 1
    total_vol = raw_buy_vol + raw_sell_vol
    buy_pct = round(raw_buy_vol / total_vol * 100) if total_vol > 0 else 50
    sell_pct = 100 - buy_pct
    count_buy_pct = (round(up_count / (up_count + dn_count) * 100)
                     if (up_count + dn_count) > 0 else 50)
    vol_count_diverge = abs(buy_pct - count_buy_pct) > 20

    if buy_pct >= 62:
        pressure = "BUYER"
    elif sell_pct >= 62:
        pressure = "SELLER"
    else:
        pressure = "FIGHT"

    # ── 1b. TIME-DECAY WEIGHTED pressure ──────────────────────────────────
    td_buy_vol = 0.0
    td_sell_vol = 0.0
    for i in range(1, n):
        delta = ticks[i] - ticks[i - 1]
        w = 1.0 + (i - 1) / max(n - 2, 1) * 4.0
        if delta > 0:
            td_buy_vol += delta * w
        elif delta < 0:
            td_sell_vol += abs(delta) * w
    td_total = td_buy_vol + td_sell_vol
    td_buy_pct = round(td_buy_vol / td_total * 100) if td_total > 0 else 50
    td_sell_pct = 100 - td_buy_pct
    td_diverge = abs(td_buy_pct - buy_pct) >= 20

    # ── 2. Fight zone: midpoint crossings ─────────────────────────────────
    mid = (hi + lo) / 2
    crosses = sum(1 for i in range(1, n)
                  if (ticks[i - 1] < mid) != (ticks[i] < mid))
    is_fight = crosses >= FIGHT_CROSSES

    # ── 3. Volume profile: where did price spend the most time? ───────────
    hold_price = None
    hold_visits = 0
    hold_pct_of_total = 0.0
    if rng > 0:
        bin_size = rng / 10
        bins = {}
        for t in ticks:
            b = min(9, int((t - lo) / bin_size))
            bins[b] = bins.get(b, 0) + 1
        top_bin = max(bins, key=bins.get)
        hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
        hold_visits = bins[top_bin]
        hold_pct_of_total = bins.get(top_bin, 0) / n * 100
    else:
        hold_price = round(cur, 6)
        hold_visits = n
        hold_pct_of_total = 100

    # ── 3b. VAP MIGRATION ─────────────────────────────────────────────────
    vap_migration = None
    if rng > 0 and n >= 10:
        half = n // 2
        bin_size = rng / 10
        bins_first, bins_second = {}, {}
        for t in ticks[:half]:
            b = min(9, int((t - lo) / bin_size))
            bins_first[b] = bins_first.get(b, 0) + 1
        for t in ticks[half:]:
            b = min(9, int((t - lo) / bin_size))
            bins_second[b] = bins_second.get(b, 0) + 1
        if bins_first and bins_second:
            top1 = max(bins_first, key=bins_first.get)
            top2 = max(bins_second, key=bins_second.get)
            hold1 = lo + top1 * bin_size + bin_size / 2
            hold2 = lo + top2 * bin_size + bin_size / 2
            migrate_amt = hold2 - hold1
            migrate_pct = migrate_amt / rng if rng > 0 else 0
            if migrate_pct > 0.25:
                vap_migration = {"dir": "UP", "pct": round(migrate_pct, 3),
                                 "amt": round(migrate_amt, 6)}
            elif migrate_pct < -0.25:
                vap_migration = {"dir": "DOWN", "pct": round(migrate_pct, 3),
                                 "amt": round(migrate_amt, 6)}
            else:
                vap_migration = {"dir": "FLAT", "pct": round(migrate_pct, 3),
                                 "amt": round(migrate_amt, 6)}

    # ── 3c. LIVE WICK FORMATION ───────────────────────────────────────────
    live_wick = None
    if rng > 0:
        live_body = abs(cur - op)
        live_upper_wick = hi - max(op, cur)
        live_lower_wick = min(op, cur) - lo
        uw_ratio = live_upper_wick / rng
        lw_ratio = live_lower_wick / rng
        body_ratio = live_body / rng
        last_dir = "FLAT"
        if n >= 3:
            tail = ticks[-3:]
            if tail[-1] > tail[0]:
                last_dir = "UP"
            elif tail[-1] < tail[0]:
                last_dir = "DOWN"
        if lw_ratio > 0.35 and body_ratio < 0.30 and last_dir == "UP":
            live_wick = {"type": "BULL_REJECT", "lw_ratio": round(lw_ratio, 3),
                         "uw_ratio": round(uw_ratio, 3),
                         "body_ratio": round(body_ratio, 3)}
        elif uw_ratio > 0.35 and body_ratio < 0.30 and last_dir == "DOWN":
            live_wick = {"type": "BEAR_REJECT", "lw_ratio": round(lw_ratio, 3),
                         "uw_ratio": round(uw_ratio, 3),
                         "body_ratio": round(body_ratio, 3)}

    # ── 4. Phase momentum (early / mid / late thirds) ─────────────────────
    t3 = max(n // 3, 1)
    early = ticks[t3] - ticks[0]
    mid_m = ticks[2 * t3] - ticks[t3]
    late = ticks[-1] - ticks[2 * t3]

    def _dir(v):
        return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

    phases = [_dir(early), _dir(mid_m), _dir(late)]

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

    # ── 7. TICK SPEED: acceleration / deceleration ───────────────────────
    tick_speed = None
    if n >= 20:
        half = n // 2
        first_half_signed = ticks[half] - ticks[0]
        second_half_signed = ticks[-1] - ticks[half]
        first_half_range = abs(first_half_signed)
        second_half_range = abs(second_half_signed)
        spd_first = first_half_range / half if half > 0 else 0
        spd_second = second_half_range / (n - half) if (n - half) > 0 else 0
        avg_speed = (first_half_range + second_half_range) / n
        if avg_speed > 0:
            accel_ratio = spd_second / spd_first if spd_first > 0 else 1.0
        else:
            accel_ratio = 1.0
        direction_reversed = (first_half_signed * second_half_signed) < 0
        tick_speed = {
            "first": round(spd_first, 8),
            "second": round(spd_second, 8),
            "accel": round(accel_ratio, 3),
            "avg": round(avg_speed, 8),
            "first_dir": "UP" if first_half_signed > 0 else "DOWN" if first_half_signed < 0 else "FLAT",
            "second_dir": "UP" if second_half_signed > 0 else "DOWN" if second_half_signed < 0 else "FLAT",
            "reversed": direction_reversed,
        }

    # ── 8. MOMENTUM SHIFT: direction change in last third ─────────────────
    momentum_shift = None
    if n >= 20:
        t2_3 = 2 * n // 3
        early_dir = "UP" if ticks[t2_3] > ticks[0] else ("DOWN" if ticks[t2_3] < ticks[0] else "FLAT")
        late_dir = "UP" if ticks[-1] > ticks[t2_3] else ("DOWN" if ticks[-1] < ticks[t2_3] else "FLAT")
        if early_dir != "FLAT" and late_dir != "FLAT" and early_dir != late_dir:
            momentum_shift = "BULL_SHIFT" if late_dir == "UP" else "BEAR_SHIFT"

    # ── 9. LAST-N TICK VELOCITY ───────────────────────────────────────────
    last_velocity = None
    if n >= 6:
        last5 = ticks[-1] - ticks[-5] if n >= 5 else ticks[-1] - ticks[0]
        last10 = ticks[-1] - ticks[-10] if n >= 10 else ticks[-1] - ticks[0]
        last20 = ticks[-1] - ticks[-20] if n >= 20 else ticks[-1] - ticks[0]
        spd5 = last5 / min(5, n)
        spd10 = last10 / min(10, n)
        spd20 = last20 / min(20, n)
        if abs(spd10) > 0:
            accel_ratio = spd5 / spd10
        else:
            accel_ratio = 1.0
        last_velocity = {
            "last5_move": round(last5, 6),
            "last10_move": round(last10, 6),
            "last20_move": round(last20, 6),
            "spd5": round(spd5, 8),
            "spd10": round(spd10, 8),
            "spd20": round(spd20, 8),
            "accel": round(accel_ratio, 3),
            "dir5": "UP" if last5 > 0 else ("DOWN" if last5 < 0 else "FLAT"),
            "dir10": "UP" if last10 > 0 else ("DOWN" if last10 < 0 else "FLAT"),
        }

    # ── 10. CONSECUTIVE TICK STREAKS ──────────────────────────────────────
    streaks = []
    if n >= 4:
        cur_dir, cur_len = 0, 0
        for i in range(1, n):
            d = 1 if ticks[i] > ticks[i - 1] else (-1 if ticks[i] < ticks[i - 1] else 0)
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
        streaks = streaks[-4:] if len(streaks) > 4 else streaks

    # Detect V-shape: last 2 streaks opposite directions, both >=3
    v_shape = None
    if len(streaks) >= 2:
        last_d, last_l = streaks[-1]
        prev_d, prev_l = streaks[-2]
        if last_d != prev_d and last_d != 0 and prev_d != 0:
            if last_l >= 3 and prev_l >= 3:
                v_shape = "V_TOP" if prev_d > 0 else "V_BOTTOM"

    # ── 11. ORDER-FLOW IMBALANCE ──────────────────────────────────────────
    orderflow = None
    if n >= 12:
        deltas = []
        for i in range(1, n):
            d = ticks[i] - ticks[i - 1]
            if d != 0:
                deltas.append(d)
        if len(deltas) >= 8:
            abs_deltas = [abs(d) for d in deltas]
            abs_deltas_sorted = sorted(abs_deltas)
            n_deltas = len(abs_deltas_sorted)
            mid_d = n_deltas // 2
            if n_deltas % 2 == 0:
                median_size = (abs_deltas_sorted[mid_d - 1] + abs_deltas_sorted[mid_d]) / 2
            else:
                median_size = abs_deltas_sorted[mid_d]
            mean_size = sum(abs_deltas) / len(abs_deltas)
            big_threshold = max(median_size * 2.0, mean_size * 1.5)
            big_up = big_dn = ret_up = ret_dn = 0
            big_up_vol = big_dn_vol = 0.0
            for d in deltas:
                a = abs(d)
                if a >= big_threshold and big_threshold > 0:
                    if d > 0:
                        big_up += 1
                        big_up_vol += d
                    else:
                        big_dn += 1
                        big_dn_vol += a
                elif a <= median_size:
                    if d > 0:
                        ret_up += 1
                    else:
                        ret_dn += 1
            big_dir = "UP" if big_up > big_dn else ("DOWN" if big_dn > big_up else "FLAT")
            ret_dir = "UP" if ret_up > ret_dn else ("DOWN" if ret_dn > ret_up else "FLAT")
            imbalance = 0
            if big_dir != "FLAT" and ret_dir != "FLAT" and big_dir != ret_dir:
                imbalance = 1
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
                "imbalance": imbalance,
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
        "tick_count": n,
        "last_react": last_react,
        "tick_speed": tick_speed,
        "momentum_shift": momentum_shift,
        "vol_count_diverge": vol_count_diverge,
        "last_velocity": last_velocity,
        "streaks": streaks,
        "v_shape": v_shape,
        "td_buy_pct": td_buy_pct,
        "td_sell_pct": td_sell_pct,
        "td_diverge": td_diverge,
        "vap_migration": vap_migration,
        "live_wick": live_wick,
        "orderflow": orderflow,
    }


# Backward-compat alias for existing code that imports `_build_micro`.
_build_micro = build_micro
