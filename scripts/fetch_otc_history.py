#!/usr/bin/env python3
"""scripts/fetch_otc_history.py — REAL OTC historical data fetcher.

PIPELINE STEP 1→5 of the user's 18-step order: the walk-forward backtest
(PART 18) is only as honest as the data behind it, so this script pulls
DEEP 1-minute history for the user's OTC pairs straight from the SAME
Quotex platform the live feed uses (PART 1 data-source rule: never
rebuild broker-specific OTC candles from BTC/forex feeds).

AUTH: QX_TOKEN env var (the user's session SSID) — the exact same
MANUAL-TOKEN-MODE path feed.py uses. The token is never printed (only
…last4).

STORAGE: SQLite at data/otc_history.db (gitignored *.db):

    CREATE TABLE candles(asset TEXT, time INT, open REAL, high REAL,
                         low REAL, close REAL, PRIMARY KEY(asset, time))

INSERT OR REPLACE → re-runs are idempotent (resume safe).

USAGE (from repo root):
    QX_TOKEN=... python3 scripts/fetch_otc_history.py \
        --days 10 --pairs "EURUSD_otc,NZDUSD_otc" [--period 60]

Be gentle with the platform: one connect per run, sequential pairs,
built-in throttle between batches (pyquotex does its own 0.1s/batch).
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_PAIRS = [
    "BRLUSD_otc", "NZDUSD_otc", "USDBDT_otc", "USDCOP_otc", "USDIDR_otc",
    "USDINR_otc", "USDMXN_otc", "USDPKR_otc", "USDZAR_otc", "USDDZD_otc",
    "USDPHP_otc",
]

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "otc_history.db")

UA = os.environ.get("QX_UA", "").strip() or (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) "
    "Gecko/20100101 Firefox/119.0")


def init_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS candles(
        asset TEXT NOT NULL, time INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        PRIMARY KEY(asset, time))""")
    conn.commit()
    return conn


def coverage(conn, asset, period=60):
    """(n, gaps, min_t, max_t) — gap = adjacent minutes missing."""
    rows = conn.execute(
        "SELECT time FROM candles WHERE asset=? ORDER BY time",
        (asset,)).fetchall()
    if not rows:
        return 0, 0, None, None
    ts = [r[0] for r in rows]
    gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a != period)
    return len(ts), gaps, ts[0], ts[-1]


def save_candles(conn, asset, candles):
    rows = [(asset, int(c["time"]), float(c["open"]), float(c["high"]),
             float(c["low"]), float(c["close"]))
            for c in candles if c.get("time")]
    conn.executemany(
        "INSERT OR REPLACE INTO candles(asset,time,open,high,low,close) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10,
                    help="history depth per pair (days)")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS),
                    help="comma-separated asset codes")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--sleep-between", type=float, default=2.0,
                    help="pause between pairs (platform kindness)")
    args = ap.parse_args()

    token = os.environ.get("QX_TOKEN", "").strip()
    if not token:
        print("QX_TOKEN env var required (session SSID token)")
        return 2

    # ── connect EXACTLY like feed.py (MANUAL-TOKEN-MODE) ─────────────────
    from pyquotex.stable_api import Quotex
    from pyquotex.network.login import Login
    from pyquotex.types import ReconnectPolicy

    host = os.environ.get("QX_HOST", "market-qx.trade")
    Login.base_url = host
    Login.https_base_url = f"https://{host}"
    client = Quotex(
        email="", password="", host=host, lang="en",
        root_path="/tmp/plybit_fetch",
        reconnect_policy=ReconnectPolicy(
            enabled=False, max_attempts=0, base_delay=2.0,
            max_delay=30.0, stale_timeout=45.0))
    client.set_session(user_agent=UA, ssid=token)
    print(f"[fetch] connecting with token=…{token[-4:]} host={host}")
    try:
        ok, reason = await client.connect()
    except Exception as exc:
        print(f"[fetch] connect raised: {type(exc).__name__}: {exc}")
        ok, reason = False, str(exc)
    print(f"[fetch] connect -> ok={ok} reason={reason}")
    if not ok:
        return 2

    import asyncio
    conn = init_db(args.db)
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    window_secs = args.days * 86400
    report = {}
    try:
        for k, asset in enumerate(pairs):
            n0, g0, t0, t1 = coverage(conn, asset, args.period)
            if k:
                await asyncio.sleep(args.sleep_between)
            print(f"[fetch] {asset}: existing={n0} rows "
                  f"(gaps={g0}) — requesting {args.days}d "
                  f"@{args.period}s …")
            try:
                raw = await asyncio.wait_for(
                    client.get_historical_candles(
                        asset,
                        amount_of_seconds=window_secs,
                        period=args.period,
                        max_workers=1,
                    ),
                    timeout=420.0,
                )
            except asyncio.TimeoutError:
                print(f"[fetch] {asset}: TIMEOUT after 420s — skipped")
                report[asset] = {"status": "timeout"}
                continue
            except Exception as exc:
                print(f"[fetch] {asset}: ERROR {type(exc).__name__}: {exc}")
                report[asset] = {"status": "error", "error": str(exc)}
                continue

            norm = []
            if isinstance(raw, dict):
                for key in ("candles", "data", "history"):
                    if key in raw:
                        raw = raw[key]
                        break
            for c in raw or []:
                try:
                    if not all(kk in c for kk in ("open", "high", "low", "close")):
                        continue
                    norm.append({"time": int(c.get("time", c.get("from", 0))),
                                 "open": float(c["open"]),
                                 "high": float(c["high"]),
                                 "low": float(c["low"]),
                                 "close": float(c["close"])})
                except Exception:
                    continue
            saved = save_candles(conn, asset, norm) if norm else 0
            n, g, mn, mx = coverage(conn, asset, args.period)
            report[asset] = {
                "status": "ok", "fetched": len(norm), "upserted": saved,
                "total": n, "gaps": g,
                "from": mn, "to": mx,
                "days_covered": round((mx - mn) / 86400.0, 2) if mn else 0.0,
            }
            print(f"[fetch] {asset}: +{saved} rows → total={n} "
                  f"gaps={g} span={report[asset]['days_covered']}d")
    finally:
        conn.close()
        try:
            await client.close()
        except Exception:
            pass

    print("\n══ FETCH SUMMARY (সংক্ষিপ্ত ফলাফল) ══")
    for a, r in report.items():
        if r.get("status") == "ok":
            print(f"  {a:14s} total={r['total']:6d} gaps={r['gaps']:3d} "
                  f"span={r['days_covered']:5.2f}d")
        else:
            print(f"  {a:14s} {r.get('status')} {r.get('error', '')}")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
